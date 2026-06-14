"""
core/infra_loader.py
====================
Loads cycling infrastructure from authoritative municipal data sources
(Shapefile, GeoJSON) as an alternative to the OSM Overpass pipeline.

The loader produces the same internal ``cycling_ways`` list consumed by
``graph_builder.build_cycling_graph``, so the rest of the pipeline
(gap finding, scoring, reporting) is completely unchanged.

Supported formats
-----------------
- GeoJSON (.geojson, .json)   — no extra dependencies beyond the stdlib
- Shapefile (.shp)             — requires ``fiona`` and ``pyproj``

Coordinate systems
------------------
All geometries are re-projected to WGS84 (EPSG:4326) lon/lat if a
``source_crs`` is specified in the field map YAML.  GeoJSON exports from
most municipal portals are already in WGS84 and need no reprojection.

Field mapping
-------------
Each municipality stores facility types under different attribute names
and with different value vocabularies.  A YAML sidecar (see
``data/field_maps/winnipeg.yaml`` for a worked example) describes:
  - which attribute holds the facility type
  - how to map source values → internal facility types
  - which status values mean "existing" (vs planned/removed)
  - which attribute holds the feature name

OSM fallback
------------
When no ``--infra-file`` is given, ``main.py`` falls back to the OSM
Overpass pipeline automatically.  This module is never called in that case.

Usage (from main.py)
--------------------
    from core.infra_loader import load_infra_file, InfraSource

    cycling_ways, source_info = load_infra_file(
        filepath   = args.infra_file,
        field_map  = args.infra_field_map,
        region_name = args.region,
    )
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Internal facility type constants (mirrors graph_builder.py) ───────────────
VALID_FACILITY_TYPES = {
    "protected_track",
    "cycle_lane",
    "shared_path",
    "shared_roadway",
    "signed_route",
    "unknown",
}


@dataclass
class InfraSource:
    """Metadata about the data source, included in map output."""
    source_name: str
    filepath: str
    format: str          # "geojson" | "shapefile"
    feature_count: int   # raw features in file
    loaded_count: int    # features that passed filters and loaded successfully
    skipped_status: int  # dropped by status filter
    skipped_facility: int  # facility mapped to excluded type or unknown
    skipped_geometry: int  # null/invalid/too-short geometry
    crs_reprojected: bool
    field_map_path: Optional[str]


# ── Field map loading ─────────────────────────────────────────────────────────

def _load_field_map(field_map_path: Optional[str]) -> dict:
    """
    Load a YAML field-map config.  Returns a dict with defaults for any
    key not present in the file.

    Requires PyYAML (``pip install pyyaml``).  Raises ImportError with a
    helpful message if not installed so the user knows what to add.
    """
    defaults = {
        "source_name": "Municipal Cycling Network",
        "source_crs": None,
        "status_field": None,
        "status_include": [],
        "facility_field": "FACILITY_TYPE",
        "facility_map": {},
        "facility_fallback": "unknown",
        "name_field": "NAME",
        "name_fallback": "Unnamed",
        "min_segment_length_m": 5,
    }

    if field_map_path is None:
        logger.warning(
            "No --infra-field-map provided.  Using bare defaults: "
            "facility_field='FACILITY_TYPE', all features mapped to 'unknown' "
            "(excluded from analysis). Pass a field map YAML to get real results."
        )
        return defaults

    try:
        import yaml  # type: ignore
    except ImportError:
        raise ImportError(
            "PyYAML is required to read field-map configs. "
            "Install it with:  pip install pyyaml"
        )

    with open(field_map_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    return {**defaults, **data}


# ── Coordinate reprojection ───────────────────────────────────────────────────

def _make_transformer(source_crs: Optional[str]):
    """
    Return a callable(lon_or_x, lat_or_y) → (lon_wgs84, lat_wgs84).
    Returns None if no reprojection is needed (source is already WGS84).
    """
    if not source_crs:
        return None

    try:
        from pyproj import Transformer  # type: ignore
    except ImportError:
        raise ImportError(
            "pyproj is required for CRS reprojection. "
            "Install it with:  pip install pyproj"
        )

    transformer = Transformer.from_crs(
        source_crs, "EPSG:4326", always_xy=True
    )
    return transformer.transform


def _reproject_coords(
    coords: List[Tuple[float, float]],
    transform,
) -> List[Tuple[float, float]]:
    """Apply transformer to a list of (x, y) coordinate pairs."""
    if transform is None:
        return coords
    result = []
    for x, y in coords:
        lon, lat = transform(x, y)
        result.append((lon, lat))
    return result


# ── Geometry helpers ──────────────────────────────────────────────────────────

def _haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Haversine distance in metres between two WGS84 points."""
    import math
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _segment_length_m(coords: List[Tuple[float, float]]) -> float:
    total = 0.0
    for i in range(len(coords) - 1):
        total += _haversine_m(coords[i][0], coords[i][1], coords[i+1][0], coords[i+1][1])
    return total


def _extract_linestrings(geometry: dict) -> List[List[Tuple[float, float]]]:
    """
    Flatten a GeoJSON geometry (LineString or MultiLineString) into a list
    of coordinate lists.  Each entry is a list of (lon, lat) tuples.
    """
    gtype = geometry.get("type", "")
    coords = geometry.get("coordinates", [])

    if gtype == "LineString":
        return [[(c[0], c[1]) for c in coords]]

    if gtype == "MultiLineString":
        return [[(c[0], c[1]) for c in part] for part in coords]

    logger.debug(f"Skipping unsupported geometry type: {gtype}")
    return []


# ── Facility type mapping ─────────────────────────────────────────────────────

def _map_facility(raw_value: str, facility_map: dict, fallback: str) -> str:
    """
    Map a raw source value to an internal facility type.
    Matching is case-insensitive and strips extra whitespace.
    """
    if not raw_value:
        return fallback

    cleaned = str(raw_value).strip()

    # Exact match first
    if cleaned in facility_map:
        mapped = facility_map[cleaned]
    else:
        # Case-insensitive fallback
        lower = cleaned.lower()
        mapped = next(
            (v for k, v in facility_map.items() if k.lower() == lower),
            fallback,
        )

    if mapped not in VALID_FACILITY_TYPES:
        logger.warning(
            f"Field map returned invalid facility type '{mapped}' for value "
            f"'{cleaned}' — using fallback '{fallback}'"
        )
        return fallback

    return mapped


def _passes_status_filter(props: dict, cfg: dict) -> bool:
    """Return True if this feature passes the status filter."""
    status_field = cfg.get("status_field")
    status_include = cfg.get("status_include") or []

    if not status_field or not status_include:
        return True  # no filter configured → include everything

    value = props.get(status_field, "")
    if value is None:
        return False

    return str(value).strip().lower() in [s.lower() for s in status_include]


# ── Feature → cycling_way conversion ─────────────────────────────────────────

def _feature_to_ways(
    feature: dict,
    cfg: dict,
    transform,
    feature_index: int,
) -> Tuple[List[dict], dict]:
    """
    Convert one GeoJSON Feature to zero or more cycling_ways dicts.

    Returns (ways_list, counters) where counters has keys:
      skipped_status, skipped_facility, skipped_geometry
    """
    counters = {"skipped_status": 0, "skipped_facility": 0, "skipped_geometry": 0}
    props = feature.get("properties") or {}
    geometry = feature.get("geometry")

    if not geometry:
        counters["skipped_geometry"] += 1
        return [], counters

    # ── Status filter ─────────────────────────────────────────────────────────
    if not _passes_status_filter(props, cfg):
        counters["skipped_status"] += 1
        return [], counters

    # ── Facility type ─────────────────────────────────────────────────────────
    facility_field = cfg.get("facility_field", "FACILITY_TYPE")
    raw_facility = props.get(facility_field, "")
    facility_type = _map_facility(
        raw_facility,
        cfg.get("facility_map", {}),
        cfg.get("facility_fallback", "unknown"),
    )

    # ── Name ──────────────────────────────────────────────────────────────────
    name_field = cfg.get("name_field")
    name = cfg.get("name_fallback", "Unnamed")
    if name_field and props.get(name_field):
        name = str(props[name_field]).strip() or name

    # ── Build OSM-compatible tags dict ────────────────────────────────────────
    # The rest of the pipeline (_classify_cycling_facility, display, scoring)
    # reads 'highway' and 'name' tags.  We set these to values that produce
    # the correct internal facility type without ambiguity.
    tags = _facility_to_osm_tags(facility_type, name, props)
    # Store the source facility string for diagnostics
    tags["_source_facility"] = str(raw_facility)

    # ── Geometry ──────────────────────────────────────────────────────────────
    linestrings = _extract_linestrings(geometry)
    if not linestrings:
        counters["skipped_geometry"] += 1
        return [], counters

    min_len = cfg.get("min_segment_length_m", 5)
    ways = []
    for part_idx, raw_coords in enumerate(linestrings):
        coords = _reproject_coords(raw_coords, transform)
        if len(coords) < 2:
            counters["skipped_geometry"] += 1
            continue
        length = _segment_length_m(coords)
        if length < min_len:
            counters["skipped_geometry"] += 1
            continue

        way_id = f"shp_{feature_index}_{part_idx}"
        ways.append({
            "id": way_id,
            "coords": coords,        # list of (lon, lat) — same as OSM pipeline
            "tags": tags,
            "nodes": [],             # not used downstream, kept for compat
            "_length_m": length,     # diagnostic only
        })

    # Increment skipped_geometry once at the feature level only if NO valid
    # segments were produced.  Individual short/bad segments within a
    # multi-part geometry are already counted above; adding an extra +1 for
    # the feature itself would double-count for single-LineString features.
    if not ways and counters["skipped_geometry"] == 0:
        counters["skipped_geometry"] += 1

    return ways, counters


def _facility_to_osm_tags(facility_type: str, name: str, props: dict) -> dict:
    """
    Produce a minimal OSM-compatible tags dict for a given internal facility type.

    The graph builder and display code both call _classify_cycling_facility(tags)
    to determine how to colour and classify each way.  By setting the right
    combination of 'highway' and 'bicycle'/'cycleway' tags we can deterministically
    produce any facility type without ambiguity.
    """
    base = {"name": name}

    if facility_type == "protected_track":
        # highway=cycleway is the cleanest way to get protected_track
        return {**base, "highway": "cycleway", "bicycle": "designated", "foot": "no"}

    if facility_type == "cycle_lane":
        # A primary road with an explicit cycle lane attribute
        return {**base, "highway": "secondary", "cycleway": "lane"}

    if facility_type == "shared_path":
        # Shared-use path: off-road but not bicycle-exclusive
        return {**base, "highway": "path", "bicycle": "designated", "foot": "designated"}

    if facility_type == "shared_roadway":
        return {**base, "highway": "residential", "bicycle": "yes"}

    if facility_type == "signed_route":
        return {**base, "route": "bicycle"}

    # unknown / fallback
    return {**base, "highway": "path", "bicycle": "yes"}


# ── GeoJSON loader ────────────────────────────────────────────────────────────

def _load_geojson(filepath: str, cfg: dict) -> Tuple[List[dict], InfraSource]:
    """Load a GeoJSON FeatureCollection."""
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    features = data.get("features", [])
    if not features:
        raise ValueError(f"No features found in GeoJSON file: {filepath}")

    transform = _make_transformer(cfg.get("source_crs"))
    reprojected = transform is not None

    ways = []
    total_skipped_status = 0
    total_skipped_facility = 0
    total_skipped_geometry = 0

    for i, feature in enumerate(features):
        fw, c = _feature_to_ways(feature, cfg, transform, i)
        ways.extend(fw)
        total_skipped_status += c["skipped_status"]
        total_skipped_facility += c["skipped_facility"]
        total_skipped_geometry += c["skipped_geometry"]

    source = InfraSource(
        source_name=cfg.get("source_name", "GeoJSON Source"),
        filepath=filepath,
        format="geojson",
        feature_count=len(features),
        loaded_count=len(ways),
        skipped_status=total_skipped_status,
        skipped_facility=total_skipped_facility,
        skipped_geometry=total_skipped_geometry,
        crs_reprojected=reprojected,
        field_map_path=None,
    )
    return ways, source


# ── Shapefile loader ──────────────────────────────────────────────────────────

def _load_shapefile(filepath: str, cfg: dict) -> Tuple[List[dict], InfraSource]:
    """Load a Shapefile using fiona."""
    try:
        import fiona  # type: ignore
    except ImportError:
        raise ImportError(
            "fiona is required to read Shapefiles. "
            "Install it with:  pip install fiona"
        )

    transform = _make_transformer(cfg.get("source_crs"))
    reprojected = transform is not None

    ways = []
    feature_count = 0
    total_skipped_status = 0
    total_skipped_facility = 0
    total_skipped_geometry = 0

    with fiona.open(filepath, "r") as src:
        # If source_crs not in field map, read from file
        if not cfg.get("source_crs") and src.crs:
            crs_str = src.crs.to_epsg()
            if crs_str and crs_str != 4326:
                # Warn rather than fail — user should set source_crs in field map
                logger.warning(
                    f"Shapefile CRS detected as EPSG:{crs_str} but source_crs is "
                    f"not set in field map. If coordinates look wrong, add "
                    f"'source_crs: EPSG:{crs_str}' to your field map YAML."
                )

        for i, record in enumerate(src):
            feature_count += 1
            # Fiona records are mappings — convert to GeoJSON-compatible dict
            geojson_feature = {
                "type": "Feature",
                "geometry": record["geometry"],
                "properties": dict(record["properties"]),
            }
            fw, c = _feature_to_ways(geojson_feature, cfg, transform, i)
            ways.extend(fw)
            total_skipped_status += c["skipped_status"]
            total_skipped_facility += c["skipped_facility"]
            total_skipped_geometry += c["skipped_geometry"]

    source = InfraSource(
        source_name=cfg.get("source_name", "Shapefile Source"),
        filepath=filepath,
        format="shapefile",
        feature_count=feature_count,
        loaded_count=len(ways),
        skipped_status=total_skipped_status,
        skipped_facility=total_skipped_facility,
        skipped_geometry=total_skipped_geometry,
        crs_reprojected=reprojected,
        field_map_path=None,
    )
    return ways, source


# ── Public entry point ────────────────────────────────────────────────────────

def load_infra_file(
    filepath: str,
    field_map_path: Optional[str] = None,
    region_name: str = "",
) -> Tuple[List[dict], InfraSource]:
    """
    Load cycling infrastructure from a GeoJSON or Shapefile.

    Parameters
    ----------
    filepath : str
        Path to the .geojson, .json, or .shp file.
    field_map_path : str, optional
        Path to a YAML field-map config (see data/field_maps/winnipeg.yaml).
        If None, defaults are used and most features will map to 'unknown'.
    region_name : str
        Used only for log messages.

    Returns
    -------
    cycling_ways : list of dict
        Same format as extract_nodes_and_ways() from osm_fetcher.py:
        [{id, coords: [(lon,lat),...], tags: {...}, nodes: []}]
        Ready to pass to build_cycling_graph().

    source : InfraSource
        Metadata about the load operation for logging and report headers.

    Raises
    ------
    FileNotFoundError
        If filepath does not exist.
    ValueError
        If the file format is not recognised or contains no usable features.
    """
    if not os.path.exists(filepath):
        raise FileNotFoundError(
            f"Infrastructure file not found: {filepath}\n"
            f"Check the --infra-file path and try again."
        )

    cfg = _load_field_map(field_map_path)

    ext = os.path.splitext(filepath)[1].lower()
    logger.info(f"Loading infrastructure from {os.path.basename(filepath)} ({ext})")

    if ext in (".geojson", ".json"):
        ways, source = _load_geojson(filepath, cfg)
    elif ext == ".shp":
        ways, source = _load_shapefile(filepath, cfg)
    else:
        raise ValueError(
            f"Unsupported file format: '{ext}'. "
            "Supported formats: .geojson, .json, .shp"
        )

    source.field_map_path = field_map_path

    # ── Summary logging ───────────────────────────────────────────────────────
    excluded_facility = [
        w for w in ways
        if w["tags"].get("highway") in ("residential",) or
           w["tags"].get("route") == "bicycle"
    ]

    logger.info(
        f"Infrastructure load complete ({source.source_name}):\n"
        f"  File:              {filepath}\n"
        f"  Total features:    {source.feature_count}\n"
        f"  Loaded as ways:    {source.loaded_count}\n"
        f"  Skipped (status):  {source.skipped_status}\n"
        f"  Skipped (geometry):{source.skipped_geometry}\n"
        f"  CRS reprojected:   {source.crs_reprojected}"
    )

    if source.loaded_count == 0:
        raise ValueError(
            f"No usable features loaded from {filepath}.\n"
            "Possible causes:\n"
            "  1. status_field / status_include in field map doesn't match your data\n"
            "  2. facility_field name is wrong — check your file's attribute names\n"
            "  3. All features have null or empty geometry\n"
            "Run with --verbose to see per-feature debug messages."
        )

    return ways, source


# ── Bbox extraction from loaded ways ─────────────────────────────────────────

def bbox_from_ways(ways: List[dict]) -> Tuple[float, float, float, float]:
    """
    Derive a bounding box from a list of cycling_ways.
    Returns (min_lat, min_lon, max_lat, max_lon) — same order as get_bbox_for_region().
    """
    lats, lons = [], []
    for w in ways:
        for lon, lat in w.get("coords", []):
            lats.append(lat)
            lons.append(lon)

    if not lats:
        raise ValueError("Cannot compute bbox — no coordinates found in loaded ways.")

    # Add 0.005° (~500m) padding so road-network fetch covers the full area
    PAD = 0.005
    return (min(lats) - PAD, min(lons) - PAD, max(lats) + PAD, max(lons) + PAD)
