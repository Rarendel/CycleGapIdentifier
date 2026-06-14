"""
spines.py
Identifies cycling network spines — high-quality, well-connected corridors that
act as the backbone of the cycling network. Gap priority is boosted for gaps that
improve access to these spines.

Two detection modes:

1. AUTO-DETECT (default)
   Uses graph centrality analysis to find the most connected, highest-quality
   segments of the existing cycling network. Clusters results into named corridors
   using nearby road name lookup.

2. USER-DEFINED (via --spines argument)
   A list of road names provided at runtime. Overrides auto-detection.

   Future: ATMP/TMP document parser will extract spine corridors directly from
   the plan document (e.g. Appendix B/C high-priority corridors in Moving Forward).

Waterloo Region known spines (from Moving Forward TMP, 2019):
  Iron Horse Trail, Spurline Trail, King Street (separated lanes),
  Erb Street, University Avenue, Columbia Street, Ottawa Street MUT,
  Grand River Trail, Homer Watson Boulevard, Franklin Boulevard
"""

import math
import networkx as nx
import logging

logger = logging.getLogger(__name__)

SPINE_FACILITY_TYPES = {"protected_track", "shared_path"}
MIN_SPINE_EDGE_M = 200
TOP_SPINE_NODES = 50

# Known regional spine names for Waterloo Region — from Moving Forward TMP 2019
WATERLOO_REGION_KNOWN_SPINES = [
    "Iron Horse Trail", "Spurline Trail", "King Street", "Erb Street",
    "University Avenue", "Columbia Street", "Ottawa Street",
    "Homer Watson Boulevard", "Franklin Boulevard", "Grand River Trail",
    "Laurel Trail", "Sportsworld Drive", "Northfield Drive", "Fischer-Hallman Road",
]


def detect_spines(cycling_graph, road_graph, user_defined: list = None) -> list:
    """
    Detect or load cycling network spines. Returns list of Spine dicts.
    """
    if user_defined:
        logger.info(f"Using {len(user_defined)} user-defined spines")
        return _build_user_spines(user_defined, cycling_graph, road_graph)

    spines = _auto_detect_spines(cycling_graph, road_graph)
    if not spines:
        logger.warning("Auto-detection found no spines — falling back to component analysis")
        spines = _component_spines(cycling_graph)

    logger.info(f"Detected {len(spines)} cycling spines")
    return spines


def score_spine_proximity(gap, spines: list) -> float:
    """
    Score how much a gap improves access to cycling spines (0-1).
    Incorporated into connectivity criterion, not a separate score.
    """
    if not spines or not gap.start_coord or not gap.end_coord:
        return 0.5

    mid_lat = (gap.start_coord[0] + gap.end_coord[0]) / 2
    mid_lon = (gap.start_coord[1] + gap.end_coord[1]) / 2

    min_dist = min(
        _haversine_m(mid_lon, mid_lat, s["centroid"][1], s["centroid"][0])
        for s in spines
    )

    score = max(0.1, 1.0 - (min_dist / 2000.0))
    if gap.start_facility in SPINE_FACILITY_TYPES or gap.end_facility in SPINE_FACILITY_TYPES:
        score = min(1.0, score * 1.25)
    return score


def _auto_detect_spines(cycling_graph, road_graph) -> list:
    spine_edges = [
        (u, v) for u, v, d in cycling_graph.edges(data=True)
        if d.get("facility_type") in SPINE_FACILITY_TYPES
        and d.get("length_m", 0) >= MIN_SPINE_EDGE_M
    ]
    if not spine_edges:
        return []

    spine_subgraph = cycling_graph.edge_subgraph(spine_edges).copy()
    if spine_subgraph.number_of_nodes() < 4:
        return []

    try:
        centrality = nx.betweenness_centrality(spine_subgraph, weight="length_m", normalized=True)
    except Exception as e:
        logger.warning(f"Centrality analysis failed: {e}")
        return []

    top_nodes = sorted(centrality, key=centrality.get, reverse=True)[:TOP_SPINE_NODES]
    return _cluster_spine_nodes(cycling_graph, road_graph, top_nodes, spine_subgraph)


def _cluster_spine_nodes(cycling_graph, road_graph, top_nodes, spine_subgraph) -> list:
    top_set = set(top_nodes)
    top_subgraph = spine_subgraph.subgraph(top_set).copy()
    components = list(nx.connected_components(top_subgraph))
    spines = []

    for i, component in enumerate(components):
        if len(component) < 2:
            continue
        lats = [cycling_graph.nodes[n]["lat"] for n in component if "lat" in cycling_graph.nodes[n]]
        lons = [cycling_graph.nodes[n]["lon"] for n in component if "lon" in cycling_graph.nodes[n]]
        if not lats:
            continue

        centroid = (sum(lats)/len(lats), sum(lons)/len(lons))
        total_length = sum(
            cycling_graph[u][v].get("length_m", 0)
            for u, v in top_subgraph.edges()
            if u in component and v in component
        )
        name = _lookup_spine_name(road_graph, centroid) or f"Spine Corridor {i+1}"
        facilities = [
            cycling_graph[u][v].get("facility_type")
            for u, v in cycling_graph.edges(component)
            if cycling_graph[u][v].get("facility_type") in SPINE_FACILITY_TYPES
        ]
        facility = max(set(facilities), key=facilities.count) if facilities else "protected_track"
        spines.append({
            "name": name, "nodes": component, "centroid": centroid,
            "source": "auto", "total_length_m": total_length, "facility_type": facility,
        })
    return spines


def _lookup_spine_name(road_graph, centroid: tuple, radius_m: float = 100.0) -> str:
    best_name, best_dist = "", float("inf")
    for u, v, data in road_graph.edges(data=True):
        name = data.get("name", "")
        if not name or "lat" not in road_graph.nodes[u]:
            continue
        mid_lat = (road_graph.nodes[u]["lat"] + road_graph.nodes[v]["lat"]) / 2
        mid_lon = (road_graph.nodes[u]["lon"] + road_graph.nodes[v]["lon"]) / 2
        dist = _haversine_m(centroid[1], centroid[0], mid_lon, mid_lat)
        if dist < best_dist and dist <= radius_m:
            best_dist = dist
            best_name = name
    for known in WATERLOO_REGION_KNOWN_SPINES:
        if known.lower() in best_name.lower():
            return known
    return best_name


def _component_spines(cycling_graph) -> list:
    components = sorted(nx.connected_components(cycling_graph), key=len, reverse=True)
    spines = []
    for i, component in enumerate(components[:5]):
        lats = [cycling_graph.nodes[n]["lat"] for n in component if "lat" in cycling_graph.nodes[n]]
        lons = [cycling_graph.nodes[n]["lon"] for n in component if "lon" in cycling_graph.nodes[n]]
        if not lats:
            continue
        spines.append({
            "name": f"Network Component {i+1}", "nodes": component,
            "centroid": (sum(lats)/len(lats), sum(lons)/len(lons)),
            "source": "auto", "total_length_m": 0, "facility_type": "unknown",
        })
    return spines


def _build_user_spines(names: list, cycling_graph, road_graph) -> list:
    spines = []
    for name in names:
        matching_nodes = {
            node for u, v, data in road_graph.edges(data=True)
            if name.lower() in data.get("name", "").lower()
            for node in (u, v)
        }
        if not matching_nodes:
            logger.warning(f"No match for user spine '{name}'")
            continue
        lats = [road_graph.nodes[n]["lat"] for n in matching_nodes if "lat" in road_graph.nodes[n]]
        lons = [road_graph.nodes[n]["lon"] for n in matching_nodes if "lon" in road_graph.nodes[n]]
        if lats:
            spines.append({
                "name": name, "nodes": matching_nodes,
                "centroid": (sum(lats)/len(lats), sum(lons)/len(lons)),
                "source": "user", "total_length_m": 0, "facility_type": "protected_track",
            })
    return spines


def _haversine_m(lon1, lat1, lon2, lat2) -> float:
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
