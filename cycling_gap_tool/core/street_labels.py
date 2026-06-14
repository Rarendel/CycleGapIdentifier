"""
street_labels.py
Assigns human-readable "from street / to street" labels to gaps, and
suppresses false-positive gaps caused by parallel infrastructure on the
same road corridor (e.g. northbound and southbound cycle tracks).

From/To labelling:
  For each gap endpoint, find the nearest named road edge in the road graph.
  Falls back to nearest named road even if not at an intersection.
  Output: "King St N" → "University Ave" style labels.

Same-road suppression:
  If both gap endpoints are attributed to the same named road AND the
  perpendicular separation between them is less than SAME_ROAD_LATERAL_M,
  the gap is flagged as a same-road duplicate and excluded from output.
  This handles the common case of bidirectional cycle tracks appearing as
  two separate network components.
"""

import math
import logging

logger = logging.getLogger(__name__)

SAME_ROAD_LATERAL_M = 25.0  # suppress if endpoints are on same road within this width


def _haversine_m(lon1, lat1, lon2, lat2) -> float:
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def find_nearest_road_name(lat: float, lon: float, road_graph) -> str:
    """
    Return the name of the nearest named road edge to a coordinate.
    Falls back to highway classification if no name is tagged.
    Returns empty string if road_graph has no edges.
    """
    best_name = ""
    best_dist = float("inf")

    for u, v, data in road_graph.edges(data=True):
        name = data.get("name", "").strip()
        if not name:
            continue
        if "lat" not in road_graph.nodes[u]:
            continue

        # Use edge midpoint for distance
        mid_lat = (road_graph.nodes[u]["lat"] + road_graph.nodes[v]["lat"]) / 2
        mid_lon = (road_graph.nodes[u]["lon"] + road_graph.nodes[v]["lon"]) / 2
        dist = _haversine_m(lon, lat, mid_lon, mid_lat)

        if dist < best_dist:
            best_dist = dist
            best_name = name

    return best_name


def label_gaps(gaps: list, road_graph) -> list:
    """
    Add from_street and to_street labels to each gap.
    Also flags same-road duplicates for filtering.
    """
    for gap in gaps:
        if not gap.start_coord or not gap.end_coord:
            gap.from_street = ""
            gap.to_street = ""
            gap.is_same_road_duplicate = False
            continue

        start_name = find_nearest_road_name(
            gap.start_coord[0], gap.start_coord[1], road_graph
        )
        end_name = find_nearest_road_name(
            gap.end_coord[0], gap.end_coord[1], road_graph
        )

        gap.from_street = start_name
        gap.to_street = end_name

        # Same-road check: same name + short lateral distance
        if start_name and start_name == end_name:
            lateral = _haversine_m(
                gap.start_coord[1], gap.start_coord[0],
                gap.end_coord[1], gap.end_coord[0]
            )
            gap.is_same_road_duplicate = (lateral < SAME_ROAD_LATERAL_M)
            if gap.is_same_road_duplicate:
                logger.debug(
                    f"Suppressing same-road gap on '{start_name}' "
                    f"({lateral:.0f}m separation)"
                )
        else:
            gap.is_same_road_duplicate = False

    original_count = len(gaps)
    gaps = [g for g in gaps if not getattr(g, "is_same_road_duplicate", False)]
    suppressed = original_count - len(gaps)
    if suppressed:
        logger.info(f"Suppressed {suppressed} same-road false positive gaps")

    return gaps


def format_gap_label(gap) -> str:
    """
    Return a display label like 'King St N → University Ave'
    for use in map popups and CSV.
    """
    from_s = getattr(gap, "from_street", "") or "Unknown"
    to_s = getattr(gap, "to_street", "") or "Unknown"
    if from_s == to_s:
        return from_s
    return f"{from_s} → {to_s}"
