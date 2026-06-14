"""
route_builder.py
Phase 3: Build recommended cycling routes for each finalised gap.

Takes the GeoJSON output from the review map (active gaps only) and finds
the optimal cycling-compatible alignment along the road network using
LTS-weighted shortest path. Lower LTS = preferred route.

For each gap:
  1. Find the road network path between endpoints minimising LTS × distance
  2. Classify each segment with OTM Book 18 facility recommendation
  3. Aggregate route quality metrics (worst LTS, total length, cost estimate)
  4. Flag segments requiring special treatment (barriers, wide roads, etc.)

Output:
  - routes.geojson  — one MultiLineString per gap with per-segment properties
  - routes_summary.csv — one row per gap with route metrics
  - routes_map.html — interactive map showing proposed alignments

References:
  - LTS framework: Mekuria, Furth & Nixon (2012)
  - Facility selection: OTM Book 18 (MTO)
"""

import json
import math
import csv
import os
import logging
import networkx as nx
from scoring.lts import score_segment_lts
from scoring.otm18 import recommend_facility, FACILITY_DESCRIPTIONS

logger = logging.getLogger(__name__)

# LTS cost multipliers — lower LTS = lower cost = preferred
LTS_COST_MULTIPLIERS = {
    1: 1.0,   # LTS 1 — no penalty, ideal
    2: 1.5,   # LTS 2 — slight penalty
    3: 3.0,   # LTS 3 — significant penalty, avoid where possible
    4: 8.0,   # LTS 4 — strong penalty, use only if no alternative
}

# Maximum acceptable detour ratio vs crow-flies distance
MAX_ROUTE_DETOUR = 2.5

# Cost estimate multipliers ($/m) by facility type — rough order-of-magnitude
# for early feasibility screening. Not for detailed cost estimation.
COST_PER_METRE = {
    "grade_separated_path":  8000,   # very high — structures, ROW
    "protected_cycle_track": 2500,   # medium-high — civil works
    "painted_cycle_lane":     300,   # low — markings and signage only
    "shared_roadway":          80,   # minimal — signage only
    "barrier_crossing":      15000,  # very high — site-specific
}


class Route:
    """Represents a recommended cycling route for a single gap."""

    def __init__(self, gap_id: str, gap_type: str, from_street: str, to_street: str):
        self.gap_id = gap_id
        self.gap_type = gap_type
        self.from_street = from_street
        self.to_street = to_street
        self.strategy = "lts"       # 'lts' or 'shortest' — which weight chose this path
        self.segments = []          # list of RouteSegment
        self.found = False          # whether a route was found
        self.fallback = False       # True if route uses LTS 3/4 (no low-stress option)
        self.total_length_m = 0.0
        self.straight_line_m = 0.0
        self.detour_ratio = 1.0
        self.worst_lts = 1
        self.dominant_facility = ""
        self.estimated_cost = 0.0
        self.route_quality = ""     # 'excellent' / 'good' / 'fair' / 'poor'
        self.notes = []
        self.start_coord = None     # (lat, lon)
        self.end_coord = None
        # LTS-problem analysis (most informative on the shortest-path strategy,
        # but computed for both so the comparison is symmetric).
        self.lts_problem_length_m = 0.0   # length of LTS 3/4 segments
        self.lts_problem_pct = 0.0        # % of route length at LTS 3/4
        self.upgrade_complexity = ""      # 'straightforward' / 'moderate' / 'complex'
        # Comparison fields — populated only in 'compare' mode on the primary
        # (LTS) route, summarising how it relates to the shortest-path route.
        self.compare = None               # dict or None

    def to_geojson_feature(self) -> dict:
        """Convert route to a GeoJSON Feature with MultiLineString geometry."""
        coords = []
        for seg in self.segments:
            if seg.coords:
                coords.append([[c[0], c[1]] for c in seg.coords])  # [lon, lat]

        return {
            "type": "Feature",
            "geometry": {
                "type": "MultiLineString" if len(coords) > 1 else "LineString",
                "coordinates": coords[0] if len(coords) == 1 else coords,
            },
            "properties": {
                "gap_id": self.gap_id,
                "gap_type": self.gap_type,
                "strategy": self.strategy,
                "from_street": self.from_street,
                "to_street": self.to_street,
                "found": self.found,
                "fallback": self.fallback,
                "total_length_m": round(self.total_length_m, 1),
                "straight_line_m": round(self.straight_line_m, 1),
                "detour_ratio": round(self.detour_ratio, 2),
                "worst_lts": self.worst_lts,
                "lts_problem_length_m": round(self.lts_problem_length_m, 1),
                "lts_problem_pct": self.lts_problem_pct,
                "upgrade_complexity": self.upgrade_complexity,
                "dominant_facility": self.dominant_facility,
                "estimated_cost": round(self.estimated_cost),
                "route_quality": self.route_quality,
                "compare": self.compare,
                "notes": self.notes,
                "segments": [s.to_dict() for s in self.segments],
            }
        }

    def to_summary_row(self) -> dict:
        row = {
            "gap_id": self.gap_id,
            "gap_type": self.gap_type,
            "strategy": self.strategy,
            "from_street": self.from_street,
            "to_street": self.to_street,
            "route_found": self.found,
            "fallback_route": self.fallback,
            "total_length_m": round(self.total_length_m, 1),
            "straight_line_m": round(self.straight_line_m, 1),
            "detour_ratio": round(self.detour_ratio, 2),
            "worst_lts": self.worst_lts,
            "lts_problem_length_m": round(self.lts_problem_length_m, 1),
            "lts_problem_pct": self.lts_problem_pct,
            "upgrade_complexity": self.upgrade_complexity,
            "dominant_facility": self.dominant_facility,
            "estimated_cost_cad": round(self.estimated_cost),
            "route_quality": self.route_quality,
            "notes": "; ".join(self.notes),
        }
        # Flatten comparison metrics into the summary row when present.
        if self.compare:
            row["compare_shortest_length_m"] = self.compare.get("shortest_length_m")
            row["compare_length_ratio"] = self.compare.get("length_ratio")
            row["compare_shortest_worst_lts"] = self.compare.get("shortest_worst_lts")
            row["compare_verdict"] = self.compare.get("verdict")
        return row


class RouteSegment:
    """A single road segment within a route."""

    def __init__(self):
        self.road_name = ""
        self.highway_class = ""
        self.length_m = 0.0
        self.lts = 2
        self.lts_label = ""
        self.recommended_facility = ""
        self.recommended_facility_type = ""
        self.cost_tier = ""
        self.otm18_basis = ""
        self.coords = []            # list of (lon, lat)
        self.requires_property = False
        self.notes = []

    def to_dict(self) -> dict:
        return {
            "road_name": self.road_name,
            "highway_class": self.highway_class,
            "length_m": round(self.length_m, 1),
            "lts": self.lts,
            "lts_label": self.lts_label,
            "recommended_facility": self.recommended_facility,
            "recommended_facility_type": self.recommended_facility_type,
            "cost_tier": self.cost_tier,
            "otm18_basis": self.otm18_basis,
        }


def build_routes(
    gaps_geojson: dict,
    road_graph: nx.Graph,
    output_dir: str,
    region_name: str = "Region",
    strategy: str = "lts",
) -> list:
    """
    Main entry point. Build routes for all gaps in the GeoJSON.

    Args:
      gaps_geojson: FeatureCollection from review map export (active gaps only)
      road_graph:   networkx Graph from build_road_graph()
      output_dir:   directory to write outputs
      region_name:  display name for the region
      strategy:     'lts'      — minimise LTS-weighted cost (comfortable route)
                    'shortest' — minimise physical distance, score LTS along it
                    'compare'  — build both and annotate the LTS route with how
                                 it differs from the shortest path

    Returns list of Route objects (the primary strategy's routes; in 'compare'
    mode the primary is the LTS route, each carrying a `.compare` summary).
    """
    if strategy not in ("lts", "shortest", "compare"):
        raise ValueError(f"Unknown strategy {strategy!r}; "
                         "expected 'lts', 'shortest', or 'compare'")

    os.makedirs(output_dir, exist_ok=True)

    # Single weighted-graph build serves every strategy: it carries both the
    # physical length and the LTS cost on each edge, so the shortest-path and
    # LTS-weighted searches run on the same graph without rebuilding.
    weighted_graph = _build_weighted_graph(road_graph)

    gap_features = [
        f for f in gaps_geojson.get("features", [])
        if f.get("geometry", {}).get("type") == "Point"
    ]
    logger.info(f"Building routes for {len(gap_features)} gaps (strategy={strategy})")

    routes = []
    for feature in gap_features:
        if strategy == "shortest":
            route = _route_for_gap(feature, weighted_graph, road_graph, weight="length_m")
        else:
            # 'lts' and 'compare' both use the LTS route as the primary result.
            route = _route_for_gap(feature, weighted_graph, road_graph, weight="lts_cost")
            if strategy == "compare":
                shortest = _route_for_gap(feature, weighted_graph, road_graph,
                                          weight="length_m")
                route.compare = _compare_routes(route, shortest)

        routes.append(route)
        status = "✓" if route.found else "✗"
        quality = route.route_quality if route.found else "not found"
        extra = ""
        if route.compare:
            extra = f", shortest×{route.compare.get('length_ratio', '?')}"
        logger.info(f"  {status} {route.gap_id}: {quality}, "
                    f"{route.total_length_m:.0f}m, LTS {route.worst_lts}{extra}")

    # Strategy-suffixed filenames so 'lts' and 'shortest' outputs don't collide.
    region_slug = region_name.lower().replace(" ", "_")
    suffix = {"lts": "", "shortest": "_shortest", "compare": "_compare"}[strategy]

    geojson_path = os.path.join(output_dir, f"{region_slug}_routes{suffix}.geojson")
    _write_routes_geojson(routes, gaps_geojson, geojson_path)

    csv_path = os.path.join(output_dir, f"{region_slug}_routes{suffix}_summary.csv")
    _write_routes_csv(routes, csv_path)

    map_path = os.path.join(output_dir, f"{region_slug}_routes{suffix}_map.html")
    _write_routes_map(routes, gaps_geojson, region_name, map_path)

    found = sum(1 for r in routes if r.found)
    logger.info(f"Routes built: {found}/{len(routes)} found, "
                f"outputs written to {output_dir}")

    return routes


def _compare_routes(lts_route: "Route", shortest_route: "Route") -> dict:
    """
    Summarise how the low-stress route relates to the shortest physical route.
    Returns a dict attached to the LTS route's `.compare`.

    The verdict captures the planning-relevant trade-off:
      - 'direct_is_comfortable' — shortest path is already LTS ≤2; just retrofit it
      - 'comfort_for_free'      — direct path is high-stress but a comfortable
                                  route exists at nearly the same length (best case)
      - 'detour_buys_comfort'   — a comfortable route exists but needs a real detour
      - 'no_comfortable_route'  — even the LTS route is high-stress (fallback)
    """
    if not lts_route.found or not shortest_route.found:
        return {
            "shortest_found": shortest_route.found,
            "lts_found": lts_route.found,
            "verdict": "incomplete",
        }

    short_len = shortest_route.total_length_m or 0.0
    lts_len = lts_route.total_length_m or 0.0
    length_ratio = round(lts_len / short_len, 2) if short_len > 0 else 1.0

    if shortest_route.worst_lts <= 2:
        # The direct route is already comfortable — just retrofit it.
        verdict = "direct_is_comfortable"
    elif lts_route.fallback or lts_route.worst_lts >= 4:
        # Even the low-stress search couldn't find comfort.
        verdict = "no_comfortable_route"
    elif length_ratio <= 1.1:
        # Direct route is high-stress, but a comfortable route exists at nearly
        # the same length — comfort is essentially free. The best case.
        verdict = "comfort_for_free"
    else:
        # A comfortable route exists but requires a meaningful detour.
        verdict = "detour_buys_comfort"

    return {
        "shortest_length_m": round(short_len, 1),
        "lts_length_m": round(lts_len, 1),
        "length_ratio": length_ratio,
        "shortest_worst_lts": shortest_route.worst_lts,
        "lts_worst_lts": lts_route.worst_lts,
        "shortest_problem_pct": shortest_route.lts_problem_pct,
        "verdict": verdict,
    }


def _build_weighted_graph(road_graph: nx.Graph) -> nx.Graph:
    """
    Create a copy of the road graph with LTS-weighted edge costs.
    Cost = LTS_multiplier × length_m
    This makes the shortest path algorithm naturally prefer low-stress routes.
    """
    G = nx.Graph()

    for node, attrs in road_graph.nodes(data=True):
        G.add_node(node, **attrs)

    for u, v, data in road_graph.edges(data=True):
        lts_result = score_segment_lts(
            highway=data.get("highway", ""),
            maxspeed=data.get("maxspeed", 0),
            lanes=data.get("lanes", 1),
            cycleway=data.get("cycleway", "none"),
        )
        lts = lts_result["lts"]
        length = data.get("length_m", 50)
        multiplier = LTS_COST_MULTIPLIERS.get(lts, 3.0)

        G.add_edge(u, v, **data,
                   lts=lts,
                   lts_label=lts_result["lts_label"],
                   lts_cost=length * multiplier,
                   lts_notes=lts_result["notes"])

    logger.info(f"Weighted graph: {G.number_of_edges()} edges with LTS costs")
    return G


def _route_for_gap(feature: dict, weighted_graph: nx.Graph,
                   road_graph: nx.Graph, weight: str = "lts_cost") -> Route:
    """
    Find a route for a single gap feature.

    weight = 'lts_cost' → comfortable (low-stress) route
    weight = 'length_m' → shortest physical route (LTS still scored per segment)
    """
    p = feature["properties"]
    coords = feature["geometry"]["coordinates"]  # [lon, lat] — gap MIDPOINT

    route = Route(
        gap_id=p.get("gap_id", ""),
        gap_type=p.get("gap_type", ""),
        from_street=p.get("from_street", ""),
        to_street=p.get("to_street", ""),
    )
    route.strategy = "shortest" if weight == "length_m" else "lts"

    # Endpoints: the Point geometry is the gap MIDPOINT, not an endpoint. Use the
    # explicit start_lat/start_lon from properties when present (this was a bug
    # in the LTS variant, which snapped the start to the midpoint and could route
    # from the middle of the gap). Fall back to the midpoint only if absent.
    start_lat = p.get("start_lat")
    start_lon = p.get("start_lon")
    if start_lat is not None and start_lon is not None:
        route.start_coord = (start_lat, start_lon)
    else:
        route.start_coord = (coords[1], coords[0])

    end_lat = p.get("end_lat")
    end_lon = p.get("end_lon")
    if end_lat is None or end_lon is None:
        route.notes.append("No end coordinate — gap may be missing endpoint data")
        return route
    route.end_coord = (end_lat, end_lon)

    route.straight_line_m = _haversine_m(
        route.start_coord[1], route.start_coord[0], end_lon, end_lat
    )

    # Snap endpoints to nearest road nodes (wide fallback if the tight snap fails).
    start_node = _nearest_road_node(weighted_graph, route.start_coord)
    end_node = _nearest_road_node(weighted_graph, route.end_coord)
    if start_node is None:
        start_node = _nearest_road_node_wide(weighted_graph, route.start_coord)
    if end_node is None:
        end_node = _nearest_road_node_wide(weighted_graph, route.end_coord)

    if start_node is None or end_node is None:
        route.notes.append("Could not snap endpoints to road network")
        return route

    if start_node == end_node:
        route.notes.append("Start and end snap to same road node — gap too small to route")
        return route

    # Find path by the requested weight.
    path = _find_path(weighted_graph, start_node, end_node, weight=weight)

    # For the LTS strategy, fall back to the physical shortest path if no
    # weighted path exists, and flag it. (The shortest strategy already uses
    # length_m, so this only applies to lts_cost.)
    if path is None and weight == "lts_cost":
        path = _find_path(weighted_graph, start_node, end_node, weight="length_m")
        if path is not None:
            route.fallback = True
            route.notes.append(
                "No low-stress route found — fallback to shortest physical path. "
                "Review corridor for infrastructure upgrade opportunities."
            )

    if path is None:
        route.notes.append(
            "No road network path found between endpoints. "
            "Check if endpoints are in disconnected road graph areas."
        )
        return route

    # Build segment objects from path
    route.segments = _build_segments(weighted_graph, road_graph, path)
    if not route.segments:
        route.notes.append("Path found but no valid segments extracted")
        return route

    route.found = True
    route.total_length_m = sum(s.length_m for s in route.segments)
    route.detour_ratio = (route.total_length_m / route.straight_line_m
                          if route.straight_line_m > 0 else 1.0)

    # Detour guard — now acts, not just annotates: an excessive detour caps
    # quality and is flagged for a direct-corridor study rather than presented
    # as a clean recommendation.
    excessive_detour = route.detour_ratio > MAX_ROUTE_DETOUR
    if excessive_detour:
        route.notes.append(
            f"Route detour ratio {route.detour_ratio:.1f}x exceeds threshold "
            f"({MAX_ROUTE_DETOUR}x) — direct corridor study recommended."
        )

    # Aggregate metrics
    lts_values = [s.lts for s in route.segments]
    route.worst_lts = max(lts_values)

    # LTS-problem analysis (length and share of route at LTS 3/4)
    problem = [s for s in route.segments if s.lts >= 3]
    route.lts_problem_length_m = sum(s.length_m for s in problem)
    route.lts_problem_pct = round(
        100 * route.lts_problem_length_m / route.total_length_m
        if route.total_length_m > 0 else 0, 1
    )
    route.upgrade_complexity = _classify_upgrade_complexity(route)

    # Length-weighted dominant facility
    facility_lengths = {}
    for seg in route.segments:
        ft = seg.recommended_facility_type
        facility_lengths[ft] = facility_lengths.get(ft, 0) + seg.length_m
    if facility_lengths:
        route.dominant_facility = max(facility_lengths, key=facility_lengths.get)

    # Cost estimate (unchanged for now; cost-model refinement deferred)
    route.estimated_cost = sum(
        COST_PER_METRE.get(s.recommended_facility_type, 500) * s.length_m
        for s in route.segments
    )

    # Route quality classification (detour guard now feeds into it)
    route.route_quality = _classify_route_quality(route, excessive_detour)

    return route


def _classify_upgrade_complexity(route: Route) -> str:
    """
    Classify how hard the route is to bring to a comfortable standard, based on
    the share of its length currently at LTS 3/4. Mirrors the analysis the
    shortest-path variant produced, now available for both strategies.
    """
    if route.worst_lts <= 2:
        return "straightforward"
    if route.lts_problem_pct <= 30:
        return "moderate"
    return "complex"


def _build_segments(weighted_graph: nx.Graph, road_graph: nx.Graph,
                    path: list) -> list:
    """Convert a node path into RouteSegment objects with OTM Book 18 recommendations."""
    segments = []

    for i in range(len(path) - 1):
        u, v = path[i], path[i+1]
        if not weighted_graph.has_edge(u, v):
            continue

        data = weighted_graph[u][v]
        seg = RouteSegment()
        seg.road_name = data.get("name", "Unnamed road")
        seg.highway_class = data.get("highway", "")
        seg.length_m = data.get("length_m", 0)
        seg.lts = data.get("lts", 2)
        seg.lts_label = data.get("lts_label", "")

        # Get OTM Book 18 recommendation for this segment
        # Build a minimal gap-like object for the recommendation function
        from core.gap_finder import Gap
        mock_gap = Gap("route_segment")
        mock_gap.start_coord = (
            weighted_graph.nodes[u].get("lat", 0),
            weighted_graph.nodes[u].get("lon", 0),
        )
        mock_gap.end_coord = (
            weighted_graph.nodes[v].get("lat", 0),
            weighted_graph.nodes[v].get("lon", 0),
        )
        mock_gap.crosses_barrier = False
        mock_gap.straight_line_m = seg.length_m

        candidate = [{
            "highway": data.get("highway", ""),
            "maxspeed": data.get("maxspeed", 0),
            "lanes": data.get("lanes", 1),
            "aadt_proxy": data.get("aadt_proxy", "unknown"),
            "aadt_is_proxy": data.get("aadt_is_proxy", True),
            "name": seg.road_name,
        }]

        from scoring.lts import score_gap_lts_context
        lts_ctx = score_gap_lts_context("unknown", "unknown", candidate)
        rec = recommend_facility(mock_gap, lts_ctx, candidate)

        seg.recommended_facility = rec["facility_short"]
        seg.recommended_facility_type = rec["facility_type"]
        seg.cost_tier = rec["cost_tier"]
        seg.otm18_basis = rec["otm18_basis"]
        seg.notes = data.get("lts_notes", []) + rec.get("notes", [])

        # Get coords for this edge
        seg.coords = _edge_coords(weighted_graph, u, v)

        segments.append(seg)

    return segments


def _classify_route_quality(route: Route, excessive_detour: bool = False) -> str:
    """
    Classify overall route quality based on LTS and detour ratio.

    excellent: LTS 1-2 throughout, minimal detour
    good:      LTS 1-2 mostly, acceptable detour
    fair:      Some LTS 3 segments or high detour
    poor:      LTS 4 segments, very high detour / fallback, or a detour that
               exceeds MAX_ROUTE_DETOUR (the route wanders so far it is not a
               credible single recommendation without a corridor study)
    """
    if not route.found:
        return "not_found"
    if route.fallback or route.worst_lts >= 4 or excessive_detour:
        return "poor"
    if route.worst_lts == 3 or route.detour_ratio > 1.8:
        return "fair"
    if route.worst_lts <= 2 and route.detour_ratio <= 1.4:
        return "excellent"
    return "good"


# ─── Output writers ───────────────────────────────────────────────────────────

def _write_routes_geojson(routes: list, original_gaps: dict, output_path: str):
    """Write routes as GeoJSON, preserving gap point features for reference."""
    features = []

    # Add gap point markers for reference
    for f in original_gaps.get("features", []):
        if f["geometry"]["type"] == "Point":
            features.append(f)

    # Add route LineStrings
    for route in routes:
        if route.found:
            features.append(route.to_geojson_feature())

    fc = {"type": "FeatureCollection", "features": features}
    with open(output_path, "w") as f:
        json.dump(fc, f, indent=2)
    logger.info(f"Routes GeoJSON: {output_path}")


def _write_routes_csv(routes: list, output_path: str):
    if not routes:
        return
    rows = [r.to_summary_row() for r in routes]
    # Fieldnames = union of all row keys, preserving first-seen order, so the
    # extra compare_* columns appear only when 'compare' mode produced them.
    fieldnames = []
    for row in rows:
        for k in row:
            if k not in fieldnames:
                fieldnames.append(k)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    logger.info(f"Routes CSV: {output_path}")


def _write_routes_map(routes: list, gaps_geojson: dict,
                      region_name: str, output_path: str):
    """Generate standalone HTML map showing all proposed routes."""
    from output.report import _gaps_to_geojson

    # Build routes GeoJSON for map
    route_features = [r.to_geojson_feature() for r in routes if r.found]
    routes_json = json.dumps({"type": "FeatureCollection", "features": route_features})
    gaps_json = json.dumps(gaps_geojson)

    # Centre on first gap
    pts = [f for f in gaps_geojson.get("features", [])
           if f["geometry"]["type"] == "Point"]
    centre_lat = pts[0]["geometry"]["coordinates"][1] if pts else 43.48
    centre_lon = pts[0]["geometry"]["coordinates"][0] if pts else -80.52

    found = sum(1 for r in routes if r.found)
    excellent = sum(1 for r in routes if r.route_quality == "excellent")
    good = sum(1 for r in routes if r.route_quality == "good")
    fair = sum(1 for r in routes if r.route_quality == "fair")
    poor = sum(1 for r in routes if r.route_quality in ("poor", "not_found"))
    total_cost = sum(r.estimated_cost for r in routes if r.found)

    QUALITY_COLOURS = {
        "excellent": "#2ecc71",
        "good":      "#3498db",
        "fair":      "#f39c12",
        "poor":      "#e74c3c",
        "not_found": "#666666",
    }
    quality_colours_json = json.dumps(QUALITY_COLOURS)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Proposed Routes — {region_name}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
          background:#0d1117; color:#c9d1d9; height:100vh; overflow:hidden; }}
  #layout {{ display:grid; grid-template-columns:300px 1fr; height:100vh; }}
  #sidebar {{ background:#161b22; border-right:1px solid #21262d;
              display:flex; flex-direction:column; overflow:hidden; }}
  #header {{ padding:14px 16px; border-bottom:1px solid #21262d; }}
  #header h1 {{ font-size:0.85rem; color:#58a6ff; font-weight:700; }}
  #header p {{ font-size:0.68rem; color:#8b949e; margin-top:2px; }}
  #stats {{ display:grid; grid-template-columns:1fr 1fr; gap:8px;
             padding:12px 16px; border-bottom:1px solid #21262d; }}
  .stat {{ background:#0d1117; border-radius:6px; padding:8px 10px;
           border:1px solid #21262d; }}
  .stat-num {{ font-size:1.3rem; font-weight:700; }}
  .stat-label {{ font-size:0.6rem; color:#8b949e; text-transform:uppercase;
                 letter-spacing:0.06em; margin-top:1px; }}
  #route-list {{ flex:1; overflow-y:auto; }}
  .route-item {{ padding:10px 16px; border-bottom:1px solid #21262d;
                 cursor:pointer; transition:background 0.1s; }}
  .route-item:hover {{ background:rgba(88,166,255,0.05); }}
  .route-item.selected {{ background:rgba(88,166,255,0.1);
                          border-left:2px solid #58a6ff; }}
  .ri-id {{ font-size:0.62rem; color:#8b949e; }}
  .ri-quality {{ font-size:0.72rem; font-weight:600; margin-top:2px; }}
  .ri-streets {{ font-size:0.65rem; color:#8b949e; margin-top:2px; white-space:nowrap;
                 overflow:hidden; text-overflow:ellipsis; }}
  .ri-meta {{ font-size:0.62rem; color:#8b949e; margin-top:3px; }}
  #detail {{ padding:16px; border-top:1px solid #21262d; background:#0d1117;
             flex-shrink:0; max-height:280px; overflow-y:auto; }}
  #detail h3 {{ font-size:0.72rem; color:#58a6ff; margin-bottom:8px;
                text-transform:uppercase; letter-spacing:0.07em; }}
  .detail-row {{ display:flex; justify-content:space-between; font-size:0.7rem;
                 padding:3px 0; border-bottom:1px solid #21262d; }}
  .detail-label {{ color:#8b949e; }}
  .detail-val {{ font-weight:500; text-align:right; }}
  .seg-list {{ margin-top:8px; }}
  .seg-item {{ font-size:0.65rem; padding:4px 6px; margin-bottom:3px;
               background:#161b22; border-radius:3px; border:1px solid #21262d; }}
  .seg-road {{ font-weight:600; color:#c9d1d9; }}
  .seg-facility {{ color:#8b949e; }}
  #map {{ width:100%; height:100%; }}
  #map-legend {{ position:absolute; bottom:20px; right:12px; z-index:1000;
                 background:rgba(13,17,23,0.92); padding:10px 12px;
                 border-radius:6px; border:1px solid #21262d; font-size:0.62rem; }}
  .leg-title {{ color:#8b949e; text-transform:uppercase; letter-spacing:0.07em;
                font-size:0.58rem; margin-bottom:5px; }}
  .leg-row {{ display:flex; align-items:center; gap:6px; margin-bottom:3px; color:#c9d1d9; }}
  .leg-line {{ width:20px; height:3px; border-radius:1px; flex-shrink:0; }}
  ::-webkit-scrollbar {{ width:4px; }}
  ::-webkit-scrollbar-thumb {{ background:#21262d; border-radius:2px; }}
</style>
</head>
<body>
<div id="layout">
  <div id="sidebar">
    <div id="header">
      <h1>Proposed Routes — {region_name}</h1>
      <p>{found} routes found · Est. total cost: ${total_cost:,.0f} CAD</p>
    </div>
    <div id="stats">
      <div class="stat">
        <div class="stat-num" style="color:#2ecc71">{excellent}</div>
        <div class="stat-label">Excellent</div>
      </div>
      <div class="stat">
        <div class="stat-num" style="color:#3498db">{good}</div>
        <div class="stat-label">Good</div>
      </div>
      <div class="stat">
        <div class="stat-num" style="color:#f39c12">{fair}</div>
        <div class="stat-label">Fair</div>
      </div>
      <div class="stat">
        <div class="stat-num" style="color:#e74c3c">{poor}</div>
        <div class="stat-label">Poor/Not found</div>
      </div>
    </div>
    <div id="route-list"></div>
    <div id="detail">
      <h3>Route Detail</h3>
      <div id="detail-content" style="color:#8b949e;font-size:0.68rem">
        Click a route to see segment details
      </div>
    </div>
  </div>
  <div style="position:relative">
    <div id="map"></div>
    <div id="map-legend">
      <div class="leg-title">Route Quality</div>
      <div class="leg-row"><div class="leg-line" style="background:#2ecc71"></div>Excellent (LTS 1-2)</div>
      <div class="leg-row"><div class="leg-line" style="background:#3498db"></div>Good (LTS 1-2 mostly)</div>
      <div class="leg-row"><div class="leg-line" style="background:#f39c12"></div>Fair (some LTS 3)</div>
      <div class="leg-row"><div class="leg-line" style="background:#e74c3c"></div>Poor (LTS 4 / fallback)</div>
    </div>
  </div>
</div>

<script>
const ROUTES = {routes_json};
const GAPS   = {gaps_json};
const QCOLS  = {quality_colours_json};

const map = L.map('map').setView([{centre_lat}, {centre_lon}], 13);
L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png',
  {{attribution:'© OpenStreetMap © CARTO',maxZoom:19}}).addTo(map);

// Draw gap point markers
GAPS.features.forEach(function(f) {{
  if (f.geometry.type !== 'Point') return;
  const p = f.properties;
  L.circleMarker([f.geometry.coordinates[1],f.geometry.coordinates[0]],
    {{radius:5,fillColor:'#58a6ff',color:'#fff',weight:1.5,fillOpacity:0.7}})
  .bindTooltip('<b>' + p.gap_id + '</b><br>' + (p.from_street||'') + ' → ' + (p.to_street||''))
  .addTo(map);
}});

// Draw route lines and build sidebar
const routeLayers = {{}};
let selectedId = null;

ROUTES.features.forEach(function(f) {{
  const p = f.properties;
  const col = QCOLS[p.route_quality] || '#666';
  const coords = f.geometry.type === 'MultiLineString'
    ? f.geometry.coordinates.map(line => line.map(c => [c[1],c[0]]))
    : [f.geometry.coordinates.map(c => [c[1],c[0]])];

  const layers = coords.map(function(line) {{
    return L.polyline(line, {{color:col,weight:4,opacity:0.85}})
      .on('click', function() {{ selectRoute(p.gap_id); }});
  }});
  layers.forEach(l => l.addTo(map));
  routeLayers[p.gap_id] = layers;
}});

// Build route list
const list = document.getElementById('route-list');
ROUTES.features.forEach(function(f) {{
  const p = f.properties;
  const col = QCOLS[p.route_quality] || '#666';
  const div = document.createElement('div');
  div.className = 'route-item';
  div.dataset.id = p.gap_id;
  div.innerHTML =
    '<div class="ri-id">' + p.gap_id + ' · ' + p.gap_type.toUpperCase() + '</div>' +
    '<div class="ri-quality" style="color:' + col + '">' + (p.route_quality||'').toUpperCase() +
      ' · LTS ' + p.worst_lts + '</div>' +
    '<div class="ri-streets">📍 ' + (p.from_street||'?') + ' → ' + (p.to_street||'?') + '</div>' +
    '<div class="ri-meta">' + (p.total_length_m||0).toFixed(0) + 'm · ' +
      (p.detour_ratio||1).toFixed(1) + 'x detour · $' +
      (p.estimated_cost||0).toLocaleString() + '</div>';
  div.addEventListener('click', function() {{ selectRoute(p.gap_id); }});
  list.appendChild(div);
}});

function selectRoute(gapId) {{
  selectedId = gapId;

  // Update list highlighting
  document.querySelectorAll('.route-item').forEach(function(el) {{
    el.classList.toggle('selected', el.dataset.id === gapId);
  }});

  // Zoom to route
  const layers = routeLayers[gapId];
  if (layers && layers.length) {{
    const group = L.featureGroup(layers);
    map.fitBounds(group.getBounds().pad(0.2));
  }}

  // Show detail
  const f = ROUTES.features.find(f => f.properties.gap_id === gapId);
  if (!f) return;
  const p = f.properties;
  const segs = (p.segments || []).map(function(s) {{
    return '<div class="seg-item">' +
      '<div class="seg-road">' + (s.road_name||'Unnamed') + ' · ' +
        s.length_m.toFixed(0) + 'm · LTS ' + s.lts + '</div>' +
      '<div class="seg-facility">' + (s.recommended_facility||'—') + '</div>' +
    '</div>';
  }}).join('');

  const notes = (p.notes||[]).map(n => '<li style="font-size:0.63rem;color:#f39c12;padding:1px 0">' + n + '</li>').join('');

  document.getElementById('detail-content').innerHTML =
    '<div class="detail-row"><span class="detail-label">Total length</span>' +
      '<span class="detail-val">' + (p.total_length_m||0).toFixed(0) + 'm</span></div>' +
    '<div class="detail-row"><span class="detail-label">Detour ratio</span>' +
      '<span class="detail-val">' + (p.detour_ratio||1).toFixed(2) + 'x</span></div>' +
    '<div class="detail-row"><span class="detail-label">Worst LTS</span>' +
      '<span class="detail-val">LTS ' + (p.worst_lts||'—') + '</span></div>' +
    '<div class="detail-row"><span class="detail-label">Dominant facility</span>' +
      '<span class="detail-val">' + (p.dominant_facility||'—').replace(/_/g,' ') + '</span></div>' +
    '<div class="detail-row"><span class="detail-label">Est. cost (CAD)</span>' +
      '<span class="detail-val">$' + (p.estimated_cost||0).toLocaleString() + '</span></div>' +
    (notes ? '<ul style="margin-top:6px;padding-left:12px">' + notes + '</ul>' : '') +
    '<div class="seg-list">' + segs + '</div>';

  // Scroll list item into view
  const el = document.querySelector('[data-id="' + gapId + '"]');
  if (el) el.scrollIntoView({{block:'nearest',behavior:'smooth'}});
}}
</script>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    logger.info(f"Routes map: {output_path}")


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _nearest_road_node(G: nx.Graph, coord: tuple, max_dist_m: float = 300.0) -> str:
    """Find nearest node in graph to (lat, lon) coord within max_dist_m."""
    lat, lon = coord
    best_node, best_dist = None, float("inf")
    for node, attrs in G.nodes(data=True):
        if "lat" not in attrs:
            continue
        d = _haversine_m(lon, lat, attrs["lon"], attrs["lat"])
        if d < best_dist:
            best_dist = d
            best_node = node
    return best_node if best_dist < max_dist_m else None


def _nearest_road_node_wide(G: nx.Graph, coord: tuple) -> str:
    """Wider fallback search — up to 1500m. Used when normal snap fails."""
    return _nearest_road_node(G, coord, max_dist_m=1500.0)


def _find_path(G: nx.Graph, source: str, target: str, weight: str):
    """Find shortest path by given weight. Returns node list or None."""
    try:
        return nx.shortest_path(G, source, target, weight=weight)
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return None


def _path_length(G: nx.Graph, path: list) -> float:
    """Total physical length of a node path in metres."""
    total = 0.0
    for i in range(len(path) - 1):
        if G.has_edge(path[i], path[i+1]):
            total += G[path[i]][path[i+1]].get("length_m", 0)
    return total


def _edge_coords(G: nx.Graph, u: str, v: str) -> list:
    """Get coordinate list for an edge as [(lon, lat), ...]."""
    data = G[u][v] if G.has_edge(u, v) else {}
    if "coords" in data and data["coords"]:
        return data["coords"]
    # Fall back to endpoint coordinates
    coords = []
    for node in (u, v):
        if "lat" in G.nodes[node]:
            coords.append([G.nodes[node]["lon"], G.nodes[node]["lat"]])
    return coords


def _haversine_m(lon1, lat1, lon2, lat2) -> float:
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
