"""
osm_fetcher.py
Fetches cycling infrastructure and road network data from OpenStreetMap
via the Overpass API. No external GIS libraries required.

Data pulled:
  - Existing cycling infrastructure (cycleways, bike lanes, shared paths)
  - Road network with attributes needed for LTS scoring
    (highway class, speed, lanes, cycleway tags)
  - Key destinations (schools, transit, employment, amenities)
  - Dissemination area centroids for equity scoring (StatsCan via OSM proxy)
"""

import requests
import json
import time
import logging

logger = logging.getLogger(__name__)

OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
OVERPASS_TIMEOUT = 90  # seconds — increased for large region queries


def _query(overpass_ql: str, retries: int = 3) -> dict:
    """
    Execute an Overpass QL query and return parsed JSON.
    Rotates through mirror servers on failure for resilience.
    """
    mirrors = OVERPASS_MIRRORS.copy()
    attempt = 0
    while attempt < retries * len(mirrors):
        mirror = mirrors[attempt % len(mirrors)]
        try:
            logger.debug(f"Querying {mirror} (attempt {attempt + 1})")
            response = requests.post(
                mirror,
                data={"data": overpass_ql},
                headers={
                    "User-Agent": "CyclingGapTool/1.0 (active transportation research)",
                    "Accept": "application/json, */*",
                },
                timeout=OVERPASS_TIMEOUT
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.warning(f"Overpass query attempt {attempt + 1} failed ({mirror}): {e}")
            attempt += 1
            if attempt < retries * len(mirrors):
                time.sleep(min(2 ** (attempt // len(mirrors)), 16))
    raise ConnectionError(
        "Failed to reach any Overpass API mirror after retries. "
        "Check your internet connection or try again later."
    )


def fetch_cycling_network(bbox: tuple) -> dict:
    """
    Fetch all cycling infrastructure within a bounding box.
    bbox: (min_lat, min_lon, max_lat, max_lon)

    OSM tags captured:
      highway=cycleway                  — dedicated off-road path
      cycleway=lane|track|shared_lane   — on-road provision
      highway=path/footway + bicycle=designated
      route=bicycle                     — named cycling routes
    """
    s, w, n, e = bbox
    # Tier 1: Explicitly drawn cycling infrastructure (highest reliability)
    # Tier 2: Designated paths and multi-use trails (high reliability)
    # Deliberately EXCLUDES bicycle=yes on residential/service — too noisy for gap analysis
    # Shared lanes (sharrows) excluded from graph; shown as display layer only
    query = f"""
    [out:json][timeout:{OVERPASS_TIMEOUT}];
    (
      way["highway"="cycleway"]({s},{w},{n},{e});
      way["highway"~"path|footway"]["bicycle"="designated"]({s},{w},{n},{e});
      way["highway"~"path|footway"]["bicycle"="yes"]["foot"!="designated"]({s},{w},{n},{e});
      way["highway"~"primary|secondary|tertiary"]["cycleway"~"track|lane|opposite_lane"]({s},{w},{n},{e});
      way["highway"~"primary|secondary|tertiary"]["cycleway:left"~"track|lane"]({s},{w},{n},{e});
      way["highway"~"primary|secondary|tertiary"]["cycleway:right"~"track|lane"]({s},{w},{n},{e});
      way["highway"~"primary|secondary|tertiary"]["cycleway:both"~"track|lane"]({s},{w},{n},{e});
    );
    out body geom;
    """
    logger.info("Fetching cycling network from OSM...")
    return _query(query)


def fetch_road_network(bbox: tuple) -> dict:
    """
    Fetch road network with LTS-relevant attributes.
    Captures: highway class, maxspeed, lanes, cycleway tags, oneway.
    Road classification used as AADT proxy where traffic counts unavailable.
    """
    s, w, n, e = bbox
    query = f"""
    [out:json][timeout:{OVERPASS_TIMEOUT}];
    (
      way["highway"~"motorway|trunk|primary|secondary|tertiary|
                     unclassified|residential|service|living_street"]
         ({s},{w},{n},{e});
      way["cycleway"~"track|lane|opposite_lane|shared_lane"]({s},{w},{n},{e});
      way["cycleway:left"~"track|lane"]({s},{w},{n},{e});
      way["cycleway:right"~"track|lane"]({s},{w},{n},{e});
      way["cycleway:both"~"track|lane"]({s},{w},{n},{e});
    );
    out body geom;
    """
    logger.info("Fetching road network from OSM...")
    return _query(query)


def fetch_destinations(bbox: tuple) -> dict:
    """
    Fetch key trip generators for destination proximity scoring.
    Categories aligned with active transportation demand research:
      - Education (schools, universities, colleges)
      - Transit (bus stops, train stations, LRT)
      - Employment proxies (commercial, industrial, office)
      - Community (libraries, community centres, parks, healthcare)
    """
    s, w, n, e = bbox
    query = f"""
    [out:json][timeout:{OVERPASS_TIMEOUT}];
    (
      node["amenity"~"school|university|college|library|
                       community_centre|hospital|clinic"]({s},{w},{n},{e});
      node["public_transport"~"station|stop_position"]({s},{w},{n},{e});
      node["railway"~"station|halt|tram_stop"]({s},{w},{n},{e});
      node["shop"="supermarket"]({s},{w},{n},{e});
      way["landuse"~"commercial|industrial|retail|office"]({s},{w},{n},{e});
      way["leisure"="park"]({s},{w},{n},{e});
    );
    out body geom;
    """
    logger.info("Fetching destination data from OSM...")
    return _query(query)


def fetch_admin_boundary(region_name: str) -> dict:
    """
    Fetch the administrative boundary for a named region.
    Returns the boundary polygon for bounding box calculation.
    """
    query = f"""
    [out:json][timeout:30];
    relation["name"="{region_name}"]["boundary"="administrative"];
    out body geom;
    """
    logger.info(f"Fetching boundary for: {region_name}")
    return _query(query)


def get_bbox_for_region(region_name: str) -> tuple:
    """
    Returns (min_lat, min_lon, max_lat, max_lon) for a named region.
    Falls back to hardcoded values for known regions if API lookup fails.
    """
    # Hardcoded fallbacks for known test regions
    known_regions = {
        # Waterloo Region
        "Waterloo Region": (43.37, -80.55, 43.60, -80.20),
        "City of Kitchener": (43.39, -80.53, 43.50, -80.38),
        "City of Waterloo": (43.44, -80.56, 43.55, -80.46),
        "City of Cambridge": (43.34, -80.40, 43.43, -80.25),
        # BC
        "City of Vancouver": (49.195, -123.225, 49.320, -123.020),
        "Vancouver": (49.195, -123.225, 49.320, -123.020),
        "City of Victoria": (48.400, -123.440, 48.490, -123.310),
        # California
        "City of San Jose": (37.250, -122.020, 37.470, -121.830),
        "San Jose": (37.250, -122.020, 37.470, -121.830),
        # Ontario
        "City of Toronto": (43.580, -79.640, 43.860, -79.120),
        "Toronto": (43.580, -79.640, 43.860, -79.120),
        "City of Ottawa": (45.250, -76.000, 45.530, -75.490),
        "Ottawa": (45.250, -76.000, 45.530, -75.490),
        # Alberta
        "City of Calgary": (50.845, -114.270, 51.210, -113.860),
        "Calgary": (50.845, -114.270, 51.210, -113.860),
        "City of Edmonton": (53.390, -113.720, 53.700, -113.270),
    }

    if region_name in known_regions:
        logger.info(f"Using known bbox for {region_name}")
        return known_regions[region_name]

    # Attempt dynamic lookup via Nominatim
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "q": region_name,
                "format": "json",
                "limit": 1,
                "polygon_geojson": 0,
            },
            headers={"User-Agent": "CyclingGapTool/1.0"},
            timeout=10,
        )
        results = resp.json()
        if results:
            bb = results[0]["boundingbox"]
            return (float(bb[0]), float(bb[2]), float(bb[1]), float(bb[3]))
    except Exception as e:
        logger.warning(f"Nominatim lookup failed: {e}")

    raise ValueError(
        f"Could not determine bbox for '{region_name}'. "
        "Add it to known_regions or provide bbox manually."
    )


def load_master_plan(filepath: str) -> list:
    """
    Optional: Load a municipality's AT Master Plan or TMP as GeoJSON.
    Returns a list of planned corridor features for cross-referencing.

    Expected format: GeoJSON FeatureCollection with LineString features.
    Attributes used: 'name', 'status' (planned/existing), 'priority' (if present)

    This layer allows the tool to:
      - Flag gaps that align with planned corridors (boosts priority score)
      - Flag gaps that conflict with planned routes (may already be addressed)
    """
    try:
        with open(filepath, "r") as f:
            data = json.load(f)
        features = data.get("features", [])
        logger.info(f"Loaded {len(features)} features from master plan: {filepath}")
        return features
    except FileNotFoundError:
        logger.info("No master plan file provided — skipping TMP layer.")
        return []
    except json.JSONDecodeError as e:
        logger.warning(f"Could not parse master plan GeoJSON: {e}")
        return []


def extract_nodes_and_ways(osm_data: dict) -> tuple:
    """
    Split raw Overpass response into nodes dict and ways list.
    Nodes are keyed by OSM id for quick coordinate lookup.
    """
    nodes = {}
    ways = []

    for element in osm_data.get("elements", []):
        if element["type"] == "node":
            nodes[element["id"]] = {
                "lat": element["lat"],
                "lon": element["lon"],
                "tags": element.get("tags", {}),
            }
        elif element["type"] == "way":
            # Overpass with geom returns geometry directly on the way
            if "geometry" in element:
                coords = [(pt["lon"], pt["lat"]) for pt in element["geometry"]]
            else:
                coords = []
            ways.append({
                "id": element["id"],
                "nodes": element.get("nodes", []),
                "coords": coords,
                "tags": element.get("tags", {}),
            })

    return nodes, ways
