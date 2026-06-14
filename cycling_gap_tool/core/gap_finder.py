"""
gap_finder.py
Identifies gaps in the cycling network using two complementary methods:

Method 1 — Disconnected Components
  Isolated segments or clusters not connected to the main network.
  These are "island" gaps — they exist but aren't reachable.

Method 2 — High-Detour Corridors
  Pairs of main-network nodes that are geographically close but require
  a large detour on the cycling network. The straight-line gap between them
  is a candidate for a new connection.

For each gap, the finder also identifies candidate road segments from the
road network that could physically fill the gap, and captures the facility
types at each gap endpoint for OTM Book 18 recommendation.
"""

import math
import networkx as nx
import logging
from core.graph_builder import (
    _haversine_m, _coord_key, component_centroid, get_connected_components
)

logger = logging.getLogger(__name__)

# Thresholds (tunable)
MIN_ISLAND_LENGTH_M = 50          # Ignore tiny isolated segments (likely data errors)
MAX_GAP_STRAIGHT_LINE_M = 600     # Maximum straight-line distance to consider a gap
MIN_DETOUR_FACTOR = 2.0           # Network distance / straight-line distance threshold
MAX_DETOUR_SAMPLE_NODES = 80      # Cap sampling for performance on large graphs
                                   # 80 nodes = ~3160 pairs max, manageable on most hardware
CANDIDATE_ROAD_BUFFER_M = 100     # Max distance from gap endpoints to candidate roads
SAME_ROAD_LATERAL_M = 30          # Suppress micro-gaps smaller than twice the snap
                                   # tolerance (15m × 2 = 30m).  Gaps under 30m are
                                   # indistinguishable from OSM coordinate-precision
                                   # noise and would be resolved by the snapper if
                                   # the data were perfectly clean.

# Facility types that do NOT qualify as real cycling infrastructure endpoints.
# Matches the EXCLUDED_FACILITY_TYPES in graph_builder.build_cycling_graph.
# Used throughout gap_finder to guard against nodes whose edges are sharrows,
# signed routes, or unclassified ways that slipped through the graph builder.
NON_QUALIFYING_FACILITIES = {"shared_roadway", "signed_route", "unknown"}
MAX_GAP_DISPLAY_M = 600           # Hard cap on gap line length shown in map
MIN_COMPONENT_LENGTH_M = 50       # Ignore isolated components shorter than this (noise filter)
                                   # Previously 150m — lowered now that the intermediate-node
                                   # graph fix means micro-components are genuine orphan stubs,
                                   # not fragmented sections of a real trail.
MIN_CORRIDOR_GAP_M = 20           # Minimum gap length to flag in corridor scan
MAX_CORRIDOR_GAP_M = 800          # Maximum gap length to flag in corridor scan
DANGLING_SEARCH_RADIUS_M = 400    # Max distance to search for dangling endpoint connections
                                   # distance — catches opposite-side-of-road false positives
DEFAULT_DEDUP_BUFFER_M = 300      # Default spatial deduplication buffer (tuneable)


class Gap:
    """Represents a single identified gap in the cycling network."""

    def __init__(self, gap_type: str):
        self.gap_type = gap_type          # 'island' or 'detour'
        self.gap_id = None                # assigned during scoring
        self.start_coord = None           # (lat, lon) of gap start
        self.end_coord = None             # (lat, lon) of gap end
        self.straight_line_m = 0.0        # crow-flies distance
        self.detour_factor = None         # None for island gaps
        self.start_facility = None        # facility type at start endpoint
        self.end_facility = None          # facility type at end endpoint
        self.candidate_roads = []         # road edges that could fill the gap
        self.crosses_barrier = False      # flag: rail, highway, water crossing
        self.master_plan_match = None     # name of matching planned corridor if any
        self.component_size = 0           # for islands: number of nodes in component
        self.from_street = None           # nearest named road at start endpoint
        self.to_street = None             # nearest named road at end endpoint
        # ── Holistic-network fields (separation analysis) ─────────────────────
        # separation_ratio = current best cycling-only path between the two
        #   endpoints (via road graph as proxy) ÷ straight-line distance.
        #   High ratio (or inf) = the two groups are close as the crow flies but
        #   far apart on the existing network — the formal version of
        #   "visually obvious that they don't connect". 1.0 ≈ already adjacent.
        self.separation_ratio = None
        self.current_network_m = None     # current best on-network path length (m)
        self.already_connected = False    # short low-stress path already exists
        # For junction-connector gaps only: 'quick_win' (short) or
        # 'network_link' (longer, substantial connection). None for other types.
        self.connector_class = None

    def to_dict(self) -> dict:
        return {
            "gap_id": self.gap_id,
            "gap_type": self.gap_type,
            "start_lat": self.start_coord[0] if self.start_coord else None,
            "start_lon": self.start_coord[1] if self.start_coord else None,
            "end_lat": self.end_coord[0] if self.end_coord else None,
            "end_lon": self.end_coord[1] if self.end_coord else None,
            "straight_line_m": round(self.straight_line_m, 1),
            "detour_factor": round(self.detour_factor, 2) if self.detour_factor else None,
            "start_facility": self.start_facility,
            "end_facility": self.end_facility,
            "crosses_barrier": self.crosses_barrier,
            "master_plan_match": self.master_plan_match,
            "component_size": self.component_size,
            "from_street": self.from_street,
            "to_street": self.to_street,
            "connector_class": self.connector_class,
            "separation_ratio": (round(self.separation_ratio, 2)
                                 if self.separation_ratio is not None else None),
            "current_network_m": (round(self.current_network_m, 0)
                                  if self.current_network_m is not None else None),
        }


def find_island_gaps(
    cycling_graph: nx.Graph,
    road_graph: nx.Graph,
) -> list:
    """
    Find isolated cycling infrastructure components (islands).
    These are connected sub-networks not linked to the main network.

    Grouping by main-network anchor:
    Multiple separate suburban trail clusters often share the same nearest
    main-network entry point (e.g., six South Winnipeg trail islands all
    connected to the same node on Abinojii Mikanah).  Reporting one gap per
    island produces a cluster of arrows all pointing at the same dot, which
    is confusing and inflates the gap count.  Instead, islands are grouped by
    their nearest main-network node; only the shortest gap per anchor is kept
    as the primary gap, and the others are merged into a 'component_count'
    attribute so the report can say "3 isolated clusters connect here".
    """
    components = get_connected_components(cycling_graph)
    if len(components) < 2:
        logger.info("No island gaps found — network is fully connected.")
        return []

    main_component = components[0]
    islands = components[1:]

    # First pass: compute the best gap for every qualifying island
    # anchor_best: main_node_key → (dist, gap_object)
    anchor_best: dict = {}
    # anchor_all: main_node_key → list of (dist, gap_object) for all islands
    anchor_all: dict = {}

    for island in islands:
        island_length = _component_total_length(cycling_graph, island)
        # Post-consolidation, genuine "islands" are sizeable disconnected
        # sub-networks. A short fragment is almost always a stub/artifact rather
        # than a network worth a dedicated connection, so require meaningful
        # extent (MIN_ISLAND_EXTENT_M) rather than the looser noise floor.
        if island_length < MIN_ISLAND_EXTENT_M:
            continue

        island_node, nearest_main_node, dist = _nearest_cross_component_pair(
            cycling_graph, island, main_component
        )

        if island_node is None or nearest_main_node is None:
            continue

        size = len(island)
        if size >= 10:
            island_threshold = MAX_GAP_STRAIGHT_LINE_M * 5
        elif size >= 5:
            island_threshold = MAX_GAP_STRAIGHT_LINE_M * 2.5
        else:
            island_threshold = MAX_GAP_STRAIGHT_LINE_M * 1.5

        if dist > island_threshold:
            logger.debug(
                f"Island ({size} nodes), nearest node {island_node}, is {dist:.0f}m from main "
                f"network (threshold {island_threshold:.0f}m) — skipping"
            )
            continue

        gap = Gap("island")
        gap.start_coord = (
            cycling_graph.nodes[island_node]["lat"],
            cycling_graph.nodes[island_node]["lon"],
        )
        gap.end_coord = (
            cycling_graph.nodes[nearest_main_node]["lat"],
            cycling_graph.nodes[nearest_main_node]["lon"],
        )
        gap.straight_line_m = dist
        gap.component_size = size
        gap.start_facility = _endpoint_facility(cycling_graph, island_node)
        gap.end_facility = _endpoint_facility(cycling_graph, nearest_main_node)

        if gap.start_facility == "unknown" or gap.end_facility == "unknown":
            continue

        gap.candidate_roads = _find_candidate_roads(
            road_graph, gap.start_coord, gap.end_coord
        )
        gap.crosses_barrier = _check_barrier_crossing(
            road_graph, gap.start_coord, gap.end_coord
        )
        gap.from_street, gap.to_street = _nearest_street_labels(
            road_graph, gap.start_coord, gap.end_coord
        )

        anchor_all.setdefault(nearest_main_node, []).append((dist, gap))

    # Second pass: for each anchor keep only the shortest gap, annotate with
    # the total count of islands sharing that anchor so the report is informative.
    gaps = []
    for anchor_node, candidates in anchor_all.items():
        candidates.sort(key=lambda x: x[0])          # shortest first
        _, best_gap = candidates[0]
        if len(candidates) > 1:
            # Store how many isolated clusters connect at this anchor so the
            # report template can render "X isolated clusters share this entry point"
            best_gap.component_size = sum(g.component_size for _, g in candidates)
            logger.debug(
                f"Anchor {anchor_node}: {len(candidates)} islands merged → "
                f"keeping shortest ({candidates[0][0]:.0f}m), "
                f"suppressing {len(candidates)-1} duplicates"
            )
        gaps.append(best_gap)

    logger.info(f"Found {len(gaps)} island gaps ({len(anchor_all)} unique anchor nodes)")
    return gaps


def find_detour_gaps(
    cycling_graph: nx.Graph,
    road_graph: nx.Graph,
) -> list:
    """
    Find high-detour gaps: places where two points on the cycling network
    are geographically close but require a large network detour to connect.

    Uses a grid-based spatial index to avoid O(n²) distance checks.
    Only node pairs within MAX_GAP_STRAIGHT_LINE_M are evaluated for
    network distance, which is the expensive operation.

    Performance target: < 2 minutes for Waterloo Region on a modern laptop.
    """
    components = get_connected_components(cycling_graph)
    main_component = components[0]
    main_nodes = list(main_component)

    # Sample nodes if network is large
    import random
    random.seed(42)
    if len(main_nodes) > MAX_DETOUR_SAMPLE_NODES:
        sample = random.sample(main_nodes, MAX_DETOUR_SAMPLE_NODES)
    else:
        sample = main_nodes

    # Pre-build coordinate lookup for ALL main component nodes (not just sample)
    all_coords = {
        n: (cycling_graph.nodes[n]["lat"], cycling_graph.nodes[n]["lon"])
        for n in main_nodes if "lat" in cycling_graph.nodes[n]
    }

    # ── Stratified spatial sampling ───────────────────────────────────────────
    # Divide the bounding box into a grid and sample evenly from each cell.
    # This ensures geographic spread across the whole region rather than
    # clustering in the densest existing infrastructure areas.
    GRID_DEG = MAX_GAP_STRAIGHT_LINE_M / 111000.0
    strat_grid = {}
    for n, (lat, lon) in all_coords.items():
        cell = (int(lat / GRID_DEG), int(lon / GRID_DEG))
        strat_grid.setdefault(cell, []).append(n)

    occupied_cells = list(strat_grid.keys())
    nodes_per_cell = max(1, MAX_DETOUR_SAMPLE_NODES // max(1, len(occupied_cells)))
    sample = []
    for cell_nodes in strat_grid.values():
        random.shuffle(cell_nodes)
        sample.extend(cell_nodes[:nodes_per_cell])

    # If we have leftover budget, top up with random picks from undersampled cells
    if len(sample) < MAX_DETOUR_SAMPLE_NODES:
        remaining = [n for n in main_nodes if n in all_coords and n not in set(sample)]
        random.shuffle(remaining)
        sample.extend(remaining[:MAX_DETOUR_SAMPLE_NODES - len(sample)])

    sample = [n for n in sample if n in all_coords]
    node_coords = {n: all_coords[n] for n in sample}

    logger.info(
        f"Detour analysis: {len(sample)} nodes sampled across "
        f"{len(occupied_cells)} spatial cells (stratified)"
    )

    # Build proximity index: bucket sampled nodes into grid cells for fast pair lookup
    grid = {}
    for n in sample:
        lat, lon = node_coords[n]
        cell = (int(lat / GRID_DEG), int(lon / GRID_DEG))
        grid.setdefault(cell, []).append(n)

    gaps = []
    seen_pairs = set()

    for node_a in sample:
        coord_a = node_coords[node_a]
        lat_a, lon_a = coord_a
        cell_r = int(lat_a / GRID_DEG)
        cell_c = int(lon_a / GRID_DEG)

        # Only check nodes in same cell and 8 neighbours
        candidates = []
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                candidates.extend(grid.get((cell_r + dr, cell_c + dc), []))

        for node_b in candidates:
            if node_b == node_a:
                continue
            pair_key = tuple(sorted([node_a, node_b]))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            coord_b = node_coords[node_b]
            straight = _haversine_m(coord_a[1], coord_a[0], coord_b[1], coord_b[0])
            if straight > MAX_GAP_STRAIGHT_LINE_M or straight < 50:
                continue

            # Network distance with length cutoff to avoid full graph traversal
            cutoff = straight * 6  # stop early if path is clearly not high-detour
            try:
                net_dist = nx.shortest_path_length(
                    cycling_graph, node_a, node_b, weight="length_m"
                )
            except nx.NetworkXNoPath:
                net_dist = straight * 10

            detour_factor = net_dist / straight if straight > 0 else 1.0

            if detour_factor >= MIN_DETOUR_FACTOR:
                if _is_same_road_gap(road_graph, coord_a, coord_b):
                    continue
                gap = Gap("detour")
                gap.start_coord = coord_a
                gap.end_coord = coord_b
                gap.straight_line_m = straight
                gap.detour_factor = detour_factor
                gap.start_facility = _endpoint_facility(cycling_graph, node_a)
                gap.end_facility = _endpoint_facility(cycling_graph, node_b)

                # Skip if EITHER endpoint has no known infrastructure.
                # A detour gap is only meaningful when both ends connect to real
                # cycling infrastructure — otherwise the "gap" is just a node
                # that was snapped in from the road graph with no actual facility.
                # (Island gaps are exempt from this stricter rule because the island
                # itself is known infrastructure by definition.)
                if gap.start_facility == "unknown" or gap.end_facility == "unknown":
                    continue

                gap.candidate_roads = _find_candidate_roads(
                    road_graph, coord_a, coord_b
                )
                gap.crosses_barrier = _check_barrier_crossing(
                    road_graph, coord_a, coord_b
                )
                gap.from_street, gap.to_street = _nearest_street_labels(
                    road_graph, coord_a, coord_b
                )
                gaps.append(gap)

    # Deduplicate spatially overlapping gaps (default 300m buffer, tuneable)
    gaps = _deduplicate_gaps(gaps)
    logger.info(f"Found {len(gaps)} detour gaps")
    return gaps


# ─── Holistic network analysis: separation ratio + already-connected ─────────

# A real, "visually obvious" gap has a high separation ratio: the two groups of
# links are close in straight-line terms but far apart (or unreachable) on the
# existing cycling network. These thresholds control suppression of the
# opposite case — endpoints that are effectively already connected.
ALREADY_CONNECTED_RATIO = 1.4    # network path <= 1.4x straight line ⇒ basically
                                  # already connected; not a real network gap
ALREADY_CONNECTED_ABS_M = 60.0   # ...but only suppress if the straight line is
                                  # also short; long gaps with low ratio are
                                  # still worth showing
MIN_ISLAND_EXTENT_M = 250.0      # an isolated component must have at least this
                                  # much real infrastructure before a connection
                                  # to it is worth flagging (post-consolidation,
                                  # smaller components are almost always stubs)
ENDPOINT_TO_CYCLING_MAX_M = 50.0  # a gap endpoint must lie within this distance
                                  # of an actual cycling node, or the gap is
                                  # suppressed as a wrong-endpoint artifact
                                  # (drifted across a river, around a roundabout,
                                  # onto a same-named-but-disconnected segment)


def annotate_separation(gaps: list, cycling_graph: nx.Graph,
                        node_index: dict = None) -> list:
    """
    Compute a separation ratio for every gap and suppress two classes of
    false positive surfaced by the inf-ratio diagnostic:

      A. ALREADY-CONNECTED — endpoints share a short low-multiple cycling
         path; the gap is a fragmentation artifact.
      B. WRONG-ENDPOINT — at least one endpoint is far (> ENDPOINT_TO_CYCLING_MAX_M)
         from any actual cycling node. Real on-network gaps have cycling within a
         few tens of metres at both ends. Endpoints far from cycling are almost
         always an artifact of a detector reaching across a river / roundabout /
         disconnected same-named road. Examples from Kitchener:
            • GAP-036 Bridge St E — endpoint lands across the Grand River with
              no cycling on the far bank.
            • GAP-148 Ottawa St S → Alpine Rd — endpoint lands on the asphalt
              of a roundabout, dozens of metres from the bike track that loops
              around it.

    The on-network path is measured on the CYCLING graph. If the two endpoints
    snap to different cycling components the ratio is +inf (a real separated-
    network signal); if either endpoint snaps to NO cycling node within the
    proximity gate, the gap is dropped as wrong-endpoint instead of being given
    a misleading inf.

    Uses a 50 m-cell spatial grid so nearest-node lookup is O(1) per gap rather
    than O(|V|), which matters on full-city graphs.
    """
    if not gaps:
        return gaps

    # Spatial grid: O(1) nearest-cycling-node lookup per gap.
    grid = _build_cycling_node_index(cycling_graph, cell_m=50.0)
    deg = grid.get("_cell_deg")

    def _nearest_cyc_node(coord):
        """Return (node_key, dist_m) within ENDPOINT_TO_CYCLING_MAX_M, or (None, inf)."""
        if deg is None:
            return None, float("inf")
        lat, lon = coord
        cr = int(lat / deg); cc = int(lon / deg)
        # Expand search ring until we cover ENDPOINT_TO_CYCLING_MAX_M.
        max_span = max(1, int(ENDPOINT_TO_CYCLING_MAX_M / (deg * 111000.0)) + 1)
        best_nk, best_d = None, float("inf")
        for span in range(1, max_span + 1):
            for dr in range(-span, span + 1):
                for dc in range(-span, span + 1):
                    # only the boundary ring at this span (cells already covered
                    # by inner spans are skipped)
                    if span > 1 and abs(dr) != span and abs(dc) != span:
                        continue
                    for entry in grid.get((cr + dr, cc + dc), []):
                        if not isinstance(entry, tuple):
                            continue
                        nk, nlat, nlon = entry
                        d = _haversine_m(lon, lat, nlon, nlat)
                        if d < best_d:
                            best_d, best_nk = d, nk
            # Early exit: once we've found a node and expanded one more ring,
            # we have the true nearest within this span.
            if best_nk is not None and span >= 2:
                break
        if best_nk is None or best_d > ENDPOINT_TO_CYCLING_MAX_M:
            return None, float("inf")
        return best_nk, best_d

    suppressed_already = 0
    suppressed_wrong_endpoint = 0
    kept = []
    for gap in gaps:
        if not gap.start_coord or not gap.end_coord:
            kept.append(gap)
            continue
        straight = gap.straight_line_m or _haversine_m(
            gap.start_coord[1], gap.start_coord[0],
            gap.end_coord[1], gap.end_coord[0]
        )
        if straight <= 0:
            kept.append(gap)
            continue

        na, da = _nearest_cyc_node(gap.start_coord)
        nb, db = _nearest_cyc_node(gap.end_coord)

        # B. Wrong-endpoint suppression — either end is far from any cycling.
        if na is None or nb is None:
            gap.already_connected = False
            gap.separation_ratio = None
            gap.current_network_m = None
            gap._suppressed_reason = "wrong_endpoint"
            suppressed_wrong_endpoint += 1
            continue

        if na == nb:
            gap.separation_ratio = float("inf")
            gap.current_network_m = None
            kept.append(gap)
            continue

        try:
            net = nx.shortest_path_length(cycling_graph, na, nb, weight="length_m")
            gap.current_network_m = net
            gap.separation_ratio = net / straight if straight > 0 else float("inf")
        except nx.NetworkXNoPath:
            gap.current_network_m = None
            gap.separation_ratio = float("inf")

        # A. Already-connected — short straight line AND low network multiple.
        if (gap.separation_ratio is not None
                and gap.separation_ratio != float("inf")
                and gap.separation_ratio <= ALREADY_CONNECTED_RATIO
                and straight <= ALREADY_CONNECTED_ABS_M):
            gap.already_connected = True
            suppressed_already += 1
            continue

        kept.append(gap)

    total_suppressed = suppressed_already + suppressed_wrong_endpoint
    if total_suppressed:
        logger.info(
            "Separation analysis: suppressed %d already-connected + %d "
            "wrong-endpoint = %d total. %d gaps remain.",
            suppressed_already, suppressed_wrong_endpoint,
            total_suppressed, len(kept),
        )
    else:
        logger.info("Separation analysis: nothing to suppress.")
    return kept


def match_master_plan(gaps: list, master_plan_features: list) -> list:
    """
    Cross-reference gaps against planned corridors in the AT Master Plan / TMP.
    Gaps that align with planned corridors are flagged — this boosts their
    priority score and adds a note that the municipality has already identified
    this need.

    Matching is spatial: gap centroid within 200m of a planned corridor.
    """
    if not master_plan_features:
        return gaps

    for gap in gaps:
        if not gap.start_coord or not gap.end_coord:
            continue
        gap_centroid = (
            (gap.start_coord[0] + gap.end_coord[0]) / 2,
            (gap.start_coord[1] + gap.end_coord[1]) / 2,
        )
        for feature in master_plan_features:
            if feature.get("geometry", {}).get("type") not in ("LineString", "MultiLineString"):
                continue
            coords = feature["geometry"]["coordinates"]
            if feature["geometry"]["type"] == "MultiLineString":
                coords = [pt for line in coords for pt in line]
            for lon, lat in coords:
                dist = _haversine_m(gap_centroid[1], gap_centroid[0], lon, lat)
                if dist < 200:
                    name = feature.get("properties", {}).get("name", "Unnamed corridor")
                    gap.master_plan_match = name
                    break
            if gap.master_plan_match:
                break

    matched = sum(1 for g in gaps if g.master_plan_match)
    logger.info(f"{matched} gaps match planned master plan corridors")
    return gaps


def assign_gap_ids(gaps: list) -> list:
    """Assign sequential IDs to gaps for reporting."""
    for i, gap in enumerate(gaps):
        gap.gap_id = f"GAP-{i+1:03d}"
    return gaps


# ─── Internal helpers ────────────────────────────────────────────────────────

def _component_total_length(G: nx.Graph, component: set) -> float:
    total = 0.0
    for u, v, data in G.edges(data=True):
        if u in component and v in component:
            total += data.get("length_m", 0)
    return total


def _nearest_cross_component_pair(
    G: nx.Graph, set_a: set, set_b: set
) -> tuple:
    """
    Find the closest pair of nodes where one node is in set_a and the other in set_b.

    Uses a grid spatial index so this runs in near-linear time rather than O(n*m).
    Returns (node_from_a, node_from_b, dist_m).

    This replaces the old centroid-proxy approach used in find_island_gaps, which
    pointed gap arrows at the main-network node nearest to the island's *centroid*
    rather than the true closest connection point. For elongated islands, or islands
    whose centroid sits far from their nearest endpoint, the centroid approach
    consistently picked the wrong main-network node and drew a long gap line to a
    distant point.
    """
    GRID_DEG = MAX_GAP_STRAIGHT_LINE_M / 111000.0

    # Build a spatial grid for set_b (the main component — typically larger)
    grid_b = {}
    for node in set_b:
        if "lat" not in G.nodes[node]:
            continue
        lat = G.nodes[node]["lat"]
        lon = G.nodes[node]["lon"]
        cell = (int(lat / GRID_DEG), int(lon / GRID_DEG))
        grid_b.setdefault(cell, []).append(node)

    best_a, best_b, best_dist = None, None, float("inf")

    for node_a in set_a:
        if "lat" not in G.nodes[node_a]:
            continue
        lat_a = G.nodes[node_a]["lat"]
        lon_a = G.nodes[node_a]["lon"]
        cell_r = int(lat_a / GRID_DEG)
        cell_c = int(lon_a / GRID_DEG)

        # Check own cell + 8 neighbours — enough given cell size = MAX_GAP_STRAIGHT_LINE_M
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                for node_b in grid_b.get((cell_r + dr, cell_c + dc), []):
                    lat_b = G.nodes[node_b]["lat"]
                    lon_b = G.nodes[node_b]["lon"]
                    d = _haversine_m(lon_a, lat_a, lon_b, lat_b)
                    if d < best_dist:
                        best_dist = d
                        best_a = node_a
                        best_b = node_b

    # Fallback: if nothing found within grid cells, do a full brute-force search
    # (handles tiny / edge-case islands with very few nodes)
    if best_a is None:
        for node_a in set_a:
            if "lat" not in G.nodes[node_a]:
                continue
            coord_a = (G.nodes[node_a]["lat"], G.nodes[node_a]["lon"])
            nb, d = _nearest_node(G, set_b, coord_a)
            if nb is not None and d < best_dist:
                best_dist = d
                best_a = node_a
                best_b = nb

    return best_a, best_b, best_dist


def _nearest_node(G: nx.Graph, node_set: set, coord: tuple) -> tuple:
    """Find nearest node in node_set to (lat, lon) coord. Returns (node_key, dist_m)."""
    best_node, best_dist = None, float("inf")
    for node in node_set:
        if "lat" not in G.nodes[node]:
            continue
        d = _haversine_m(
            coord[1], coord[0],
            G.nodes[node]["lon"], G.nodes[node]["lat"]
        )
        if d < best_dist:
            best_dist = d
            best_node = node
    return best_node, best_dist


def _nearest_node_in_set(G: nx.Graph, node_set: set, coord: tuple) -> tuple:
    return _nearest_node(G, node_set, coord)


def _endpoint_facility(G: nx.Graph, node: str) -> str:
    """
    Get the best qualifying facility type of edges connected to a node.

    Returns 'unknown' if the node has no edges, or if every edge carries only
    a non-qualifying facility (shared_roadway, signed_route, unknown).  This
    mirrors the EXCLUDED_FACILITY_TYPES gate in graph_builder and means callers
    can test `== "unknown"` / `in NON_QUALIFYING_FACILITIES` uniformly.
    """
    facilities = []
    for _, _, data in G.edges(node, data=True):
        ft = data.get("facility_type", "unknown")
        if ft not in NON_QUALIFYING_FACILITIES:
            facilities.append(ft)
    if not facilities:
        return "unknown"
    # Return the highest-quality qualifying facility type present
    priority = ["protected_track", "shared_path", "cycle_lane"]
    for p in priority:
        if p in facilities:
            return p
    return facilities[0]


def _find_candidate_roads(road_graph: nx.Graph, start: tuple, end: tuple) -> list:
    """
    Find road edges within buffer of the gap corridor that could host new cycling infra.
    Returns list of dicts with road attributes.
    """
    candidates = []
    # Midpoint of gap
    mid_lat = (start[0] + end[0]) / 2
    mid_lon = (start[1] + end[1]) / 2

    gap_length = _haversine_m(start[1], start[0], end[1], end[0])
    search_radius = max(CANDIDATE_ROAD_BUFFER_M, gap_length * 0.3)

    for u, v, data in road_graph.edges(data=True):
        if "lat" not in road_graph.nodes[u]:
            continue
        edge_mid_lat = (road_graph.nodes[u]["lat"] + road_graph.nodes[v]["lat"]) / 2
        edge_mid_lon = (road_graph.nodes[u]["lon"] + road_graph.nodes[v]["lon"]) / 2
        dist = _haversine_m(mid_lon, mid_lat, edge_mid_lon, edge_mid_lat)
        if dist <= search_radius:
            candidates.append({
                "osm_id": data.get("osm_id"),
                "name": data.get("name", ""),
                "highway": data.get("highway", ""),
                "maxspeed": data.get("maxspeed", 0),
                "lanes": data.get("lanes", 1),
                "aadt_proxy": data.get("aadt_proxy", "unknown"),
                "aadt_is_proxy": data.get("aadt_is_proxy", True),
                "distance_from_gap_m": round(dist, 1),
            })

    # Sort by proximity
    candidates.sort(key=lambda x: x["distance_from_gap_m"])
    return candidates[:5]  # Return top 5 candidates


def _check_barrier_crossing(road_graph: nx.Graph, start: tuple, end: tuple) -> bool:
    """
    Check if the gap corridor likely crosses a major barrier:
    motorway, trunk road, railway, or waterway.
    Uses road graph to detect high-classification roads in the corridor.
    """
    mid_lat = (start[0] + end[0]) / 2
    mid_lon = (start[1] + end[1]) / 2
    gap_length = _haversine_m(start[1], start[0], end[1], end[0])
    search_radius = gap_length * 0.5

    barrier_classes = {"motorway", "trunk", "motorway_link", "trunk_link"}

    for u, v, data in road_graph.edges(data=True):
        if data.get("highway") not in barrier_classes:
            continue
        if "lat" not in road_graph.nodes[u]:
            continue
        edge_mid_lat = (road_graph.nodes[u]["lat"] + road_graph.nodes[v]["lat"]) / 2
        edge_mid_lon = (road_graph.nodes[u]["lon"] + road_graph.nodes[v]["lon"]) / 2
        dist = _haversine_m(mid_lon, mid_lat, edge_mid_lon, edge_mid_lat)
        if dist <= search_radius:
            return True
    return False


def _point_to_segment_m(plat, plon, alat, alon, blat, blon) -> float:
    """
    Shortest distance in metres from point P to the line segment A–B, using a
    local equirectangular projection (accurate at the short distances involved
    in roundabout/gap geometry).
    """
    lat0 = math.radians(alat)
    mx = 111320.0 * math.cos(lat0)   # metres per degree longitude at this lat
    my = 110540.0                     # metres per degree latitude
    ax, ay = 0.0, 0.0
    bx, by = (blon - alon) * mx, (blat - alat) * my
    px, py = (plon - alon) * mx, (plat - alat) * my
    dx, dy = bx - ax, by - ay
    seg_sq = dx * dx + dy * dy
    if seg_sq == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / seg_sq
    t = max(0.0, min(1.0, t))
    projx, projy = ax + t * dx, ay + t * dy
    return math.hypot(px - projx, py - projy)


def filter_roundabout_crossings(gaps: list, road_graph: nx.Graph,
                                margin_m: float = 25.0) -> list:
    """
    Suppress gaps whose straight line passes through (or terminates at) a
    roundabout.

    OSM commonly tags the cycling continuation around a roundabout as part of
    the roundabout carriageway itself (or omits it), so the cycling graph shows
    two separated dead-ends on opposite legs even though a rider can clearly
    continue around the loop. These present as short dangling gaps with infinite
    separation ratio — see GAP-148 (Ottawa St S → Alpine Rd) in Kitchener.

    Robust geometry: rather than testing a handful of discrete points against
    roundabout edge-midpoints (which leaves blind spots when neither the gap
    midpoint nor its endpoints happen to align with an edge midpoint), each
    roundabout is reduced to a CENTROID and an effective RADIUS from its node
    coordinates. A gap is suppressed when its straight-line segment passes
    within (radius + margin_m) of any roundabout centroid.

    Conservative on length: only gaps up to 300 m are candidates, so a long
    legitimate corridor that merely happens to pass near a roundabout is kept.
    """
    if not gaps or road_graph.number_of_edges() == 0:
        return gaps

    # Build connected clusters of roundabout nodes → one centroid+radius each.
    rsub = nx.Graph()
    for u, v, data in road_graph.edges(data=True):
        if data.get("junction", "") not in ("roundabout", "circular"):
            continue
        if "lat" not in road_graph.nodes[u] or "lat" not in road_graph.nodes[v]:
            continue
        rsub.add_edge(u, v)
        for n in (u, v):
            rsub.nodes[n]["lat"] = road_graph.nodes[n]["lat"]
            rsub.nodes[n]["lon"] = road_graph.nodes[n]["lon"]

    if rsub.number_of_nodes() == 0:
        return gaps

    roundabouts = []  # list of (clat, clon, radius_m)
    for comp in nx.connected_components(rsub):
        lats = [rsub.nodes[n]["lat"] for n in comp]
        lons = [rsub.nodes[n]["lon"] for n in comp]
        clat = sum(lats) / len(lats)
        clon = sum(lons) / len(lons)
        # Effective radius = max node distance from centroid (with a small floor
        # so a degenerate two-node "roundabout" still has a sensible footprint).
        radius = max(
            (_haversine_m(clon, clat, lo, la) for la, lo in zip(lats, lons)),
            default=0.0,
        )
        roundabouts.append((clat, clon, max(radius, 12.0)))

    kept = []
    suppressed = 0
    for gap in gaps:
        if not gap.start_coord or not gap.end_coord:
            kept.append(gap); continue
        if gap.straight_line_m and gap.straight_line_m > 300:
            kept.append(gap); continue

        hit = False
        for clat, clon, radius in roundabouts:
            d = _point_to_segment_m(
                clat, clon,
                gap.start_coord[0], gap.start_coord[1],
                gap.end_coord[0], gap.end_coord[1],
            )
            if d <= radius + margin_m:
                hit = True
                break
        if hit:
            suppressed += 1
            continue
        kept.append(gap)

    if suppressed:
        logger.info("Roundabout filter: suppressed %d gap(s) crossing a roundabout",
                    suppressed)
    return kept


def deduplicate_all_gaps(gaps: list, min_separation_m: float = DEFAULT_DEDUP_BUFFER_M) -> list:
    """
    Cross-method deduplication over the COMBINED gap set.

    Each find_* method deduplicates only within its own results, so the same
    physical break can still be reported twice — e.g. once as an 'island' gap
    and once as a 'dangling' gap with near-identical endpoints. Running the
    shared spatial dedup over the union collapses these, keeping the most
    informative gap per cluster (see _deduplicate_gaps sort order).
    """
    before = len(gaps)
    kept = _deduplicate_gaps(gaps, min_separation_m=min_separation_m)
    if before != len(kept):
        logger.info("Cross-method dedup: %d → %d gaps", before, len(kept))
    return kept


def _deduplicate_gaps(gaps: list, min_separation_m: float = DEFAULT_DEDUP_BUFFER_M) -> list:
    """
    Remove spatially redundant gaps using three checks:

    1. Midpoint proximity — gaps whose midpoints are within min_separation_m
       of an already-kept gap are dropped (original behaviour).

    2. Shared endpoint deduplication — if multiple gaps share one endpoint
       (within ENDPOINT_DEDUP_M) and converge on different nearby points on
       the same corridor, only the highest-scoring gap is kept. This eliminates
       the "fan" pattern where one node pairs with many close nodes on a
       parallel facility.

    3. Parallel corridor deduplication — gaps that run roughly parallel and
       overlap spatially are collapsed to the highest-priority one.
    """
    ENDPOINT_DEDUP_M = 150  # two endpoints within this distance = same logical endpoint
                            # increased from 80m to better catch same-corridor fans

    if not gaps:
        return gaps

    # Keep the most informative gap in each spatial cluster. detour_factor only
    # exists for detour gaps, so sorting on it alone discarded island/corridor
    # gaps arbitrarily when they coincided with a dangling gap. Rank by gap-type
    # priority first (island carries component context; corridor carries
    # same-street continuity; both are more useful to a planner than a bare
    # dangling pair), then by detour factor, then by straight-line length.
    _TYPE_PRIORITY = {"island": 4, "corridor": 3, "detour": 2, "dangling": 1, "connector": 1}
    gaps_sorted = sorted(
        gaps,
        key=lambda g: (
            _TYPE_PRIORITY.get(g.gap_type, 0),
            g.detour_factor or 0,
            g.straight_line_m or 0,
        ),
        reverse=True,
    )
    kept = []

    for gap in gaps_sorted:
        if not gap.start_coord or not gap.end_coord:
            continue

        gap_mid = (
            (gap.start_coord[0] + gap.end_coord[0]) / 2,
            (gap.start_coord[1] + gap.end_coord[1]) / 2,
        )

        duplicate = False
        for existing in kept:
            if not existing.start_coord:
                continue

            ex_mid = (
                (existing.start_coord[0] + existing.end_coord[0]) / 2,
                (existing.start_coord[1] + existing.end_coord[1]) / 2,
            )

            # Check 1: midpoint proximity
            if _haversine_m(gap_mid[1], gap_mid[0], ex_mid[1], ex_mid[0]) < min_separation_m:
                duplicate = True
                break

            # Check 2: shared endpoint — any endpoint of this gap is within
            # ENDPOINT_DEDUP_M of any endpoint of an existing kept gap.
            # Catches the "fan" pattern.
            gap_endpoints = [gap.start_coord, gap.end_coord]
            existing_endpoints = [existing.start_coord, existing.end_coord]
            shared_count = 0
            for ge in gap_endpoints:
                for ee in existing_endpoints:
                    if _haversine_m(ge[1], ge[0], ee[1], ee[0]) < ENDPOINT_DEDUP_M:
                        shared_count += 1
            # If both gaps share one endpoint cluster, they're describing the same gap
            if shared_count >= 1:
                # Additional check: are the non-shared endpoints also close?
                # If so, these are truly the same gap from different node pairs
                for ge in gap_endpoints:
                    for ee in existing_endpoints:
                        if (_haversine_m(ge[1], ge[0], ee[1], ee[0]) < ENDPOINT_DEDUP_M * 2):
                            duplicate = True
                            break
                    if duplicate:
                        break

            if duplicate:
                break

        if not duplicate:
            kept.append(gap)

    return kept


def _is_same_road_gap(road_graph, coord_a: tuple, coord_b: tuple,
                       cycling_graph=None, node_a: str = None, node_b: str = None) -> bool:
    """
    Detect false-positive gaps between opposite sides of the same road.
    Returns True ONLY if the gap is very short (< SAME_ROAD_LATERAL_M).

    We intentionally do NOT suppress gaps based on shared road name alone —
    a wide arterial with cycle tracks on both sides (e.g. University Ave)
    is a real gap even though both endpoints share the same road name.
    The connectivity is what matters: if the nodes are in different components
    they represent a real gap regardless of proximity.
    """
    straight = _haversine_m(coord_a[1], coord_a[0], coord_b[1], coord_b[0])
    # Only suppress truly trivial micro-gaps (data noise, < 20m)
    if straight <= 20:
        return True
    return False


def _nearest_road_name(road_graph, coord: tuple, max_dist_m: float = 50.0) -> str:
    """Return the name of the nearest named road edge to a coordinate."""
    best_name = ""
    best_dist = float("inf")
    for u, v, data in road_graph.edges(data=True):
        name = data.get("name", "")
        if not name:
            continue
        if "lat" not in road_graph.nodes[u]:
            continue
        mid_lat = (road_graph.nodes[u]["lat"] + road_graph.nodes[v]["lat"]) / 2
        mid_lon = (road_graph.nodes[u]["lon"] + road_graph.nodes[v]["lon"]) / 2
        dist = _haversine_m(coord[1], coord[0], mid_lon, mid_lat)
        if dist < best_dist and dist <= max_dist_m:
            best_dist = dist
            best_name = name
    return best_name


def _nearest_street_labels(road_graph, start: tuple, end: tuple) -> tuple:
    """
    Return (from_street, to_street) labels for a gap by finding the nearest
    named road to each endpoint. Falls back to nearest road regardless of name.
    Used for human-readable gap labelling in CSV and map outputs.
    """
    from_name = _nearest_road_name(road_graph, start, max_dist_m=150.0)
    to_name = _nearest_road_name(road_graph, end, max_dist_m=150.0)

    # Fallback: use nearest road even without a name tag
    if not from_name:
        from_name = _nearest_road_class_label(road_graph, start)
    if not to_name:
        to_name = _nearest_road_class_label(road_graph, end)

    return from_name or "Unknown", to_name or "Unknown"


def _nearest_road_class_label(road_graph, coord: tuple, max_dist_m: float = 200.0) -> str:
    """Fallback label using highway class when road name is absent."""
    best_label = ""
    best_dist = float("inf")
    for u, v, data in road_graph.edges(data=True):
        if "lat" not in road_graph.nodes[u]:
            continue
        mid_lat = (road_graph.nodes[u]["lat"] + road_graph.nodes[v]["lat"]) / 2
        mid_lon = (road_graph.nodes[u]["lon"] + road_graph.nodes[v]["lon"]) / 2
        dist = _haversine_m(coord[1], coord[0], mid_lon, mid_lat)
        if dist < best_dist and dist <= max_dist_m:
            best_dist = dist
            hw = data.get("highway", "road")
            best_label = f"Unnamed {hw}"
    return best_label


# ─── Method 3: Dangling endpoint search ──────────────────────────────────────

def find_dangling_gaps(
    cycling_graph: nx.Graph,
    road_graph: nx.Graph,
) -> list:
    """
    Find gaps at dangling endpoints — degree-1 nodes where a cycling corridor
    abruptly ends with no onward connection in the cycling graph.

    Uses a spatial grid index (same approach as find_detour_gaps) to avoid
    O(n²) distance calculations. With ~3000 dangling nodes the naive approach
    takes 18+ minutes; the grid index reduces this to seconds by only
    comparing nodes within the same grid cell neighbourhood.

    Performance target: < 30 seconds for Waterloo Region.
    """
    components = get_connected_components(cycling_graph)

    # Build lookup: node → component index
    node_to_comp = {}
    for i, comp in enumerate(components):
        for node in comp:
            node_to_comp[node] = i

    # Pre-compute each component's total infrastructure length ONCE.
    #
    # PERFORMANCE FIX: the previous implementation called
    # _component_total_length() (a full edge scan) inside the dangling-node
    # list comprehension AND re-derived component membership with
    # `[c for c in components if n in c][0]` per node.  On a city network that
    # is O(n_nodes × n_components × n_edges) — minutes of needless work that
    # reintroduced the very O(n²) behaviour the grid index was added to remove.
    # We now make a single pass over the edges to accumulate per-component
    # length, then look each node's component up in O(1).
    comp_length = [0.0] * len(components)
    for u, v, data in cycling_graph.edges(data=True):
        ci = node_to_comp.get(u)
        if ci is not None and ci == node_to_comp.get(v):
            comp_length[ci] += data.get("length_m", 0.0)

    # Find all degree-1 nodes (dangling endpoints) — only from components with
    # meaningful infrastructure (noise filter).  Also cap the total to bound
    # runtime; a very high count signals OSM noise rather than real gaps.
    MAX_DANGLING = 500  # cap — more than this indicates OSM noise, not real gaps

    dangling = [
        n for n in cycling_graph.nodes()
        if cycling_graph.degree(n) == 1
        and "lat" in cycling_graph.nodes[n]
        and n in node_to_comp
        and comp_length[node_to_comp[n]] >= MIN_COMPONENT_LENGTH_M
    ]

    if len(dangling) > MAX_DANGLING:
        logger.warning(
            f"Found {len(dangling)} dangling nodes — capping at {MAX_DANGLING}. "
            f"High count suggests OSM noise; consider reviewing cycling data quality."
        )
        # Prioritise nodes from larger components (more meaningful infrastructure).
        # Uses the precomputed component sizes — no per-node component search.
        comp_size = [len(c) for c in components]
        dangling = sorted(
            dangling,
            key=lambda n: comp_size[node_to_comp[n]],
            reverse=True,
        )[:MAX_DANGLING]

    logger.info(f"Analysing {len(dangling)} dangling endpoint nodes")

    # ── Build spatial grid index over ALL cycling nodes ───────────────────────
    # Cell size = DANGLING_SEARCH_RADIUS_M so each dangling node checks only
    # its cell + 8 neighbours rather than all nodes
    GRID_DEG = DANGLING_SEARCH_RADIUS_M / 111000.0

    all_cyc_nodes = [
        n for n in cycling_graph.nodes()
        if "lat" in cycling_graph.nodes[n]
    ]

    grid = {}
    for n in all_cyc_nodes:
        lat = cycling_graph.nodes[n]["lat"]
        lon = cycling_graph.nodes[n]["lon"]
        cell = (int(lat / GRID_DEG), int(lon / GRID_DEG))
        grid.setdefault(cell, []).append(n)

    # ── Search for nearest cross-component node for each dangling endpoint ────
    gaps = []
    seen_pairs = set()

    for node_a in dangling:
        coord_a = (cycling_graph.nodes[node_a]["lat"], cycling_graph.nodes[node_a]["lon"])
        comp_a = node_to_comp.get(node_a, -1)

        lat_a, lon_a = coord_a
        cell_r = int(lat_a / GRID_DEG)
        cell_c = int(lon_a / GRID_DEG)

        # Collect candidates from neighbouring grid cells only
        candidates = []
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                candidates.extend(grid.get((cell_r + dr, cell_c + dc), []))

        best_node = None
        best_dist = float("inf")

        for node_b in candidates:
            if node_b == node_a:
                continue
            if node_to_comp.get(node_b, -2) == comp_a:
                continue  # same component — not a gap

            # Suppress if both nodes share a road graph edge (same OSM way/link)
            if road_graph.has_edge(node_a, node_b):
                continue

            # Require node_b to have at least one qualifying cycling infrastructure
            # edge.  NON_QUALIFYING_FACILITIES covers unknown, shared_roadway, and
            # signed_route — all of which represent non-physical or advisory-only
            # provision that should never anchor a gap recommendation.
            if _endpoint_facility(cycling_graph, node_b) in NON_QUALIFYING_FACILITIES:
                continue

            coord_b = (cycling_graph.nodes[node_b]["lat"], cycling_graph.nodes[node_b]["lon"])
            dist = _haversine_m(coord_a[1], coord_a[0], coord_b[1], coord_b[0])

            if SAME_ROAD_LATERAL_M < dist < best_dist and dist <= DANGLING_SEARCH_RADIUS_M:
                best_dist = dist
                best_node = node_b

        if best_node is None:
            continue

        pair_key = tuple(sorted([node_a, best_node]))
        if pair_key in seen_pairs or (node_a,) in seen_pairs:
            continue
        seen_pairs.add(pair_key)
        seen_pairs.add((node_a,))  # max 1 gap per dangling endpoint

        coord_b = (cycling_graph.nodes[best_node]["lat"], cycling_graph.nodes[best_node]["lon"])

        gap = Gap("dangling")
        gap.start_coord = coord_a
        gap.end_coord = coord_b
        gap.straight_line_m = best_dist
        gap.detour_factor = None
        gap.start_facility = _endpoint_facility(cycling_graph, node_a)
        gap.end_facility = _endpoint_facility(cycling_graph, best_node)
        gap.candidate_roads = _find_candidate_roads(road_graph, coord_a, coord_b)
        gap.crosses_barrier = _check_barrier_crossing(road_graph, coord_a, coord_b)
        gap.from_street, gap.to_street = _nearest_street_labels(
            road_graph, coord_a, coord_b
        )
        gaps.append(gap)

    gaps = _deduplicate_gaps(gaps)
    logger.info(f"Found {len(gaps)} dangling endpoint gaps")
    return gaps


# ─── Method 6: Junction-connector search (opt-in) ────────────────────────────

# Defaults — all overridable from main.py via CLI.
JUNCTION_QUICK_WIN_MAX_M = 150.0      # ≤ this ⇒ classed 'quick_win'
JUNCTION_DEFAULT_MAX_M = 500.0        # overall search cap (network_link upper bound)
JUNCTION_BUILDABILITY_TOL_M = 25.0    # connector line must stay within this of a road
JUNCTION_BUILDABILITY_SAMPLES = 6     # points sampled along the connector line


def find_junction_connector_gaps(
    cycling_graph: nx.Graph,
    road_graph: nx.Graph,
    max_distance_m: float = JUNCTION_DEFAULT_MAX_M,
    quick_win_max_m: float = JUNCTION_QUICK_WIN_MAX_M,
    buildability_tol_m: float = JUNCTION_BUILDABILITY_TOL_M,
) -> list:
    """
    Method 6 (opt-in) — connector gaps a planner would draw by eye that the
    other detectors structurally cannot see.

    The dangling finder requires BOTH ends to be degree-1 dead-ends; the
    corridor finder requires the SAME street name. This leaves a blind spot:
    a facility *terminus* sitting near a *different* facility on a *different*
    street, where the obvious short (or moderate) connecting link is never
    proposed — e.g. the south end of the Franklin St cycle lane and the
    east–west lane just south of it.

    Anchors here are broader than dangling endpoints. A node qualifies as a
    facility terminus if it is:
      • degree-1 (a true dead-end), OR
      • a node where the qualifying facility TYPE changes across it (e.g. a
        cycle lane meeting a protected track), OR
      • a node where a cycling edge meets only non-qualifying edges onward
        (the facility effectively ends even though the road continues).

    Each anchor is paired with the nearest point of a DIFFERENT cycling
    component within max_distance_m. To keep recall high without flooding the
    list with implausible links, every candidate must pass a BUILDABILITY gate:
    the straight connector line must stay within buildability_tol_m of an actual
    road/path for its whole length (sampled), and must not cross a major barrier
    (motorway/trunk/rail/water — reused from _check_barrier_crossing). This is
    the constraint a planner applies implicitly: there has to be somewhere to
    build it.

    Candidates are tagged connector_class = 'quick_win' (≤ quick_win_max_m) or
    'network_link' (longer), so the prioritised output can separate small wins
    from substantial network projects.

    Emits into the shared gap list, so it inherits cross-method dedup, the
    roundabout suppressor, the wrong-endpoint suppressor, and scoring. It cannot
    reintroduce previously-fixed false positives because those filters run
    downstream of it.
    """
    components = get_connected_components(cycling_graph)
    node_to_comp = {}
    for i, comp in enumerate(components):
        for node in comp:
            node_to_comp[node] = i

    comp_length = [0.0] * len(components)
    for u, v, data in cycling_graph.edges(data=True):
        ci = node_to_comp.get(u)
        if ci is not None and ci == node_to_comp.get(v):
            comp_length[ci] += data.get("length_m", 0.0)

    # ── Identify facility-terminus anchors ────────────────────────────────────
    anchors = []
    for n in cycling_graph.nodes():
        if "lat" not in cycling_graph.nodes[n]:
            continue
        ci = node_to_comp.get(n)
        if ci is None or comp_length[ci] < MIN_COMPONENT_LENGTH_M:
            continue
        if _endpoint_facility(cycling_graph, n) in NON_QUALIFYING_FACILITIES:
            continue
        if _is_facility_terminus(cycling_graph, n):
            anchors.append(n)

    if not anchors:
        logger.info("Found 0 junction-connector gaps (no facility termini)")
        return []

    logger.info(f"Analysing {len(anchors)} facility-terminus anchors "
                f"(junction-connector, max {max_distance_m:.0f} m)")

    # Spatial grid over all cycling nodes (cell = search distance).
    GRID_DEG = max_distance_m / 111000.0
    grid = {}
    for n in cycling_graph.nodes():
        if "lat" not in cycling_graph.nodes[n]:
            continue
        lat = cycling_graph.nodes[n]["lat"]; lon = cycling_graph.nodes[n]["lon"]
        grid.setdefault((int(lat / GRID_DEG), int(lon / GRID_DEG)), []).append(n)

    # Road-edge grid for the buildability gate.
    road_grid, road_grid_deg = _build_road_edge_grid(road_graph,
                                                     cell_m=max(buildability_tol_m * 4, 80.0))

    gaps = []
    seen_pairs = set()

    for a in anchors:
        coord_a = (cycling_graph.nodes[a]["lat"], cycling_graph.nodes[a]["lon"])
        comp_a = node_to_comp.get(a, -1)
        lat_a, lon_a = coord_a
        cr = int(lat_a / GRID_DEG); cc = int(lon_a / GRID_DEG)

        candidates = []
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                candidates.extend(grid.get((cr + dr, cc + dc), []))

        best_node, best_dist = None, float("inf")
        for b in candidates:
            if b == a:
                continue
            if node_to_comp.get(b, -2) == comp_a:
                continue  # same component — already connected
            if road_graph.has_edge(a, b):
                continue
            if _endpoint_facility(cycling_graph, b) in NON_QUALIFYING_FACILITIES:
                continue
            coord_b = (cycling_graph.nodes[b]["lat"], cycling_graph.nodes[b]["lon"])
            dist = _haversine_m(coord_a[1], coord_a[0], coord_b[1], coord_b[0])
            if SAME_ROAD_LATERAL_M < dist < best_dist and dist <= max_distance_m:
                best_dist = dist
                best_node = b

        if best_node is None:
            continue

        pair_key = tuple(sorted([a, best_node]))
        if pair_key in seen_pairs or (a,) in seen_pairs:
            continue

        coord_b = (cycling_graph.nodes[best_node]["lat"], cycling_graph.nodes[best_node]["lon"])

        # Buildability gate — line must follow a road and not cross a barrier.
        if _check_barrier_crossing(road_graph, coord_a, coord_b):
            continue
        if not _connector_follows_road(coord_a, coord_b, road_grid, road_grid_deg,
                                       buildability_tol_m):
            continue

        seen_pairs.add(pair_key)
        seen_pairs.add((a,))

        gap = Gap("connector")
        gap.start_coord = coord_a
        gap.end_coord = coord_b
        gap.straight_line_m = best_dist
        gap.detour_factor = None
        gap.start_facility = _endpoint_facility(cycling_graph, a)
        gap.end_facility = _endpoint_facility(cycling_graph, best_node)
        gap.candidate_roads = _find_candidate_roads(road_graph, coord_a, coord_b)
        gap.crosses_barrier = False
        gap.from_street, gap.to_street = _nearest_street_labels(road_graph, coord_a, coord_b)
        gap.connector_class = "quick_win" if best_dist <= quick_win_max_m else "network_link"
        gaps.append(gap)

    gaps = _deduplicate_gaps(gaps)
    n_quick = sum(1 for g in gaps if g.connector_class == "quick_win")
    n_link = len(gaps) - n_quick
    logger.info(f"Found {len(gaps)} junction-connector gaps "
                f"({n_quick} quick-win, {n_link} network-link)")
    return gaps


def _is_facility_terminus(G: nx.Graph, node: str) -> bool:
    """
    True if `node` is a genuine end of a cycling facility:
      • degree 1 (dead-end), or
      • the qualifying facility type changes across it (a junction between two
        different facility types — e.g. cycle_lane ↔ protected_track), or
      • it has exactly one qualifying cycling edge and the rest are
        non-qualifying (the facility ends though the road continues).
    Degree-2 pass-throughs with a single consistent facility are NOT termini
    (those were already dissolved by topology cleaning anyway).
    """
    deg = G.degree(node)
    if deg == 1:
        return True

    qualifying_types = []
    qualifying_count = 0
    for _, _, data in G.edges(node, data=True):
        ft = data.get("facility_type", "unknown")
        if ft in NON_QUALIFYING_FACILITIES:
            continue
        qualifying_count += 1
        qualifying_types.append(ft)

    if qualifying_count == 0:
        return False
    if qualifying_count == 1 and deg >= 2:
        # one qualifying edge among otherwise non-qualifying/road edges → the
        # cycling facility terminates here
        return True
    if len(set(qualifying_types)) >= 2:
        # facility type changes across this node
        return True
    return False


def _build_road_edge_grid(road_graph: nx.Graph, cell_m: float = 100.0):
    """Spatial grid of road edges keyed by every cell their span touches.
    Returns (grid, cell_deg). Each cell holds (u, v) edge tuples."""
    deg = cell_m / 111000.0
    grid = {}
    for u, v in road_graph.edges():
        if "lat" not in road_graph.nodes[u] or "lat" not in road_graph.nodes[v]:
            continue
        ulat, ulon = road_graph.nodes[u]["lat"], road_graph.nodes[u]["lon"]
        vlat, vlon = road_graph.nodes[v]["lat"], road_graph.nodes[v]["lon"]
        # sample cells along the edge so long edges are indexed everywhere
        span = max(abs(ulat - vlat), abs(ulon - vlon))
        steps = max(1, int(span / deg) + 1)
        seen = set()
        entry = (ulat, ulon, vlat, vlon)
        for s in range(steps + 1):
            f = s / steps
            la = ulat + (vlat - ulat) * f
            lo = ulon + (vlon - ulon) * f
            cell = (int(la / deg), int(lo / deg))
            if cell not in seen:
                seen.add(cell)
                grid.setdefault(cell, []).append(entry)
    return grid, deg


def _connector_follows_road(coord_a, coord_b, road_grid, grid_deg, tol_m,
                            n_samples: int = JUNCTION_BUILDABILITY_SAMPLES,
                            min_fraction: float = 0.5) -> bool:
    """
    True if at least `min_fraction` of sampled points along the A–B line lie
    within tol_m of some road edge. This is the buildability gate: it confirms
    there is a street/path network in the vicinity to host the connector, and
    rejects links that cut across large road-free voids (water, rail yards,
    parkland, industrial blocks).

    Note this is deliberately a *proxy*, not a routing check. A real connector
    is often L-shaped (down one street, along another), so its straight line
    only partially follows any single road — hence min_fraction defaults to 0.5
    rather than requiring the whole line to track one road. The barrier check
    (_check_barrier_crossing) handles hard obstacles separately. Tightening
    tol_m or min_fraction trades recall for precision; both are exposed for
    tuning.
    """
    # Need the road graph node coords; the grid only stores edge tuples, so we
    # carry a reference to the graph via closure-free lookup using the grid.
    # We approximate each road edge by its endpoints' segment.
    hits = 0
    for i in range(n_samples + 1):
        f = i / n_samples
        plat = coord_a[0] + (coord_b[0] - coord_a[0]) * f
        plon = coord_a[1] + (coord_b[1] - coord_a[1]) * f
        cell = (int(plat / grid_deg), int(plon / grid_deg))
        near = False
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                for (ulat, ulon, vlat, vlon) in road_grid.get((cell[0] + dr, cell[1] + dc), []):
                    if _point_to_segment_m(plat, plon, ulat, ulon, vlat, vlon) <= tol_m:
                        near = True
                        break
                if near:
                    break
            if near:
                break
        if near:
            hits += 1
    return (hits / (n_samples + 1)) >= min_fraction


def find_near_miss_gaps(
    cycling_graph: nx.Graph,
    road_graph: nx.Graph,
    existing_gaps: list = None,
) -> list:
    """
    Method 5 — Near-miss endpoint scan.

    Finds gaps between cycling nodes in *different* components that are
    geographically close, regardless of whether they are degree-1 dangling
    nodes.  This catches the common case where a cycle lane ends at a
    T-junction (degree > 1, so invisible to find_dangling_gaps) but is
    clearly disconnected from a nearby facility across the street.

    The scan is deliberately conservative:
      - Both endpoints must have known cycling infrastructure (facility != unknown).
      - Straight-line distance must be within NEAR_MISS_MAX_M.
      - Gaps already captured by other methods (within DEDUP_M of an existing
        gap midpoint) are suppressed — this function only adds coverage, it
        does not replace the other methods.
      - Results are deduplicated against each other and against existing_gaps.

    Performance: grid-indexed, O(n) in practice.  For a city-scale network
    (~5000 nodes) this runs in well under a second.
    """
    NEAR_MISS_MAX_M   = 300    # maximum straight-line distance to flag
    NEAR_MISS_MIN_M   = 30     # minimum — must exceed snap tolerance (15m) × 2
                               # previously 20m, raised to 30m to suppress sub-snap
                               # artefacts where two OSM nodes at the same physical
                               # point differ by 16–24m due to coordinate precision
    EXISTING_DEDUP_M  = 200    # suppress if already covered by another method

    components = get_connected_components(cycling_graph)
    if len(components) < 2:
        logger.info("Near-miss scan: network is fully connected — no candidates.")
        return []

    # Build node → component index
    node_to_comp = {}
    for i, comp in enumerate(components):
        for node in comp:
            node_to_comp[node] = i

    # Only consider nodes that carry qualifying cycling infrastructure.
    # NON_QUALIFYING_FACILITIES (shared_roadway, signed_route, unknown) are excluded
    # — these nodes should not anchor gap recommendations.
    infra_nodes = [
        n for n in cycling_graph.nodes()
        if "lat" in cycling_graph.nodes[n]
        and _endpoint_facility(cycling_graph, n) not in NON_QUALIFYING_FACILITIES
    ]

    logger.info(
        f"Near-miss scan: {len(infra_nodes)} infrastructure nodes across "
        f"{len(components)} components"
    )

    # Spatial grid index — cell size = NEAR_MISS_MAX_M so each node only
    # checks its own cell + 8 neighbours
    GRID_DEG = NEAR_MISS_MAX_M / 111000.0
    grid = {}
    for n in infra_nodes:
        lat = cycling_graph.nodes[n]["lat"]
        lon = cycling_graph.nodes[n]["lon"]
        cell = (int(lat / GRID_DEG), int(lon / GRID_DEG))
        grid.setdefault(cell, []).append(n)

    # Pre-compute midpoints of existing gaps for deduplication
    existing_mids = []
    for g in (existing_gaps or []):
        if g.start_coord and g.end_coord:
            existing_mids.append((
                (g.start_coord[0] + g.end_coord[0]) / 2,
                (g.start_coord[1] + g.end_coord[1]) / 2,
            ))

    # Per component-pair best: only keep the SHORTEST gap between any two
    # component indices.  Without this, a component with many nodes close to
    # another component generates dozens of near-identical gap candidates that
    # deduplication can't fully collapse (because they spread across >200m).
    # Key: (min(comp_a, comp_b), max(comp_a, comp_b)) → (dist, node_a, node_b)
    best_per_pair: dict = {}

    seen_pairs = set()

    for node_a in infra_nodes:
        lat_a = cycling_graph.nodes[node_a]["lat"]
        lon_a = cycling_graph.nodes[node_a]["lon"]
        comp_a = node_to_comp.get(node_a, -1)

        cell_r = int(lat_a / GRID_DEG)
        cell_c = int(lon_a / GRID_DEG)

        candidates = []
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                candidates.extend(grid.get((cell_r + dr, cell_c + dc), []))

        for node_b in candidates:
            if node_b == node_a:
                continue
            comp_b = node_to_comp.get(node_b, -2)
            if comp_b == comp_a:
                continue  # same component — not a gap

            pair_key = tuple(sorted([node_a, node_b]))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            lat_b = cycling_graph.nodes[node_b]["lat"]
            lon_b = cycling_graph.nodes[node_b]["lon"]
            dist = _haversine_m(lon_a, lat_a, lon_b, lat_b)

            if dist < NEAR_MISS_MIN_M or dist > NEAR_MISS_MAX_M:
                continue

            comp_pair = (min(comp_a, comp_b), max(comp_a, comp_b))
            if comp_pair not in best_per_pair or dist < best_per_pair[comp_pair][0]:
                best_per_pair[comp_pair] = (dist, node_a, node_b)

    gaps = []
    for (dist, node_a, node_b) in best_per_pair.values():
        lat_a = cycling_graph.nodes[node_a]["lat"]
        lon_a = cycling_graph.nodes[node_a]["lon"]
        lat_b = cycling_graph.nodes[node_b]["lat"]
        lon_b = cycling_graph.nodes[node_b]["lon"]
        coord_a = (lat_a, lon_a)
        coord_b = (lat_b, lon_b)
        mid = ((lat_a + lat_b) / 2, (lon_a + lon_b) / 2)

        # Suppress if an existing gap already covers this location
        already_covered = any(
            _haversine_m(mid[1], mid[0], em[1], em[0]) < EXISTING_DEDUP_M
            for em in existing_mids
        )
        if already_covered:
            continue

        gap = Gap("dangling")
        gap.start_coord = coord_a
        gap.end_coord   = coord_b
        gap.straight_line_m  = dist
        gap.detour_factor    = None
        gap.start_facility   = _endpoint_facility(cycling_graph, node_a)
        gap.end_facility     = _endpoint_facility(cycling_graph, node_b)
        gap.candidate_roads  = _find_candidate_roads(road_graph, coord_a, coord_b)
        gap.crosses_barrier  = _check_barrier_crossing(road_graph, coord_a, coord_b)
        gap.from_street, gap.to_street = _nearest_street_labels(
            road_graph, coord_a, coord_b
        )
        gaps.append(gap)

        # Track midpoint so dedup within this pass works correctly
        existing_mids.append(mid)

    gaps = _deduplicate_gaps(gaps, min_separation_m=EXISTING_DEDUP_M)
    logger.info(f"Found {len(gaps)} near-miss gaps")
    return gaps


# ─── Method 4: Corridor gap scan ─────────────────────────────────────────────

def find_corridor_gaps(
    cycling_graph: nx.Graph,
    road_graph: nx.Graph,
) -> list:
    """
    Scan named road corridors for cycling infrastructure gaps — segments where
    a road has cycling facilities on either side of a stretch with none.

    Walks the road graph by actual topological adjacency along same-named edges,
    so a corridor is only ever traversed where the road truly connects through.
    A candidate gap must additionally have cycling infrastructure within
    CORRIDOR_NEAR_CYCLING_M of BOTH endpoints — this is what prevents the
    classic false positives:

      • "Bridge Street East → Bridge Street East" across a river bridge with no
        cycling on either end (one endpoint lands far from any cycling node).
      • A corridor gap across a roundabout where the green track enters and
        exits on different legs (the roundabout center is well away from any
        cycling node).
      • Two unconnected fragments of a same-named arterial split by a freeway
        (no topological adjacency between them on the road graph).

    Replaces the previous lat-only sort, which produced near-random ordering
    on east-west corridors and bridged unrelated same-named segments.
    """
    # Group road edges by street name, but build per-name SUBGRAPHS so we can
    # walk by actual graph adjacency rather than by sorting endpoints.
    corridors = {}
    for u, v, data in road_graph.edges(data=True):
        name = data.get("name", "")
        if not name:
            continue
        corridors.setdefault(name, []).append((u, v, data))

    # Pre-build a spatial index over cycling-graph nodes — used to require that
    # both gap endpoints sit close to actual cycling infrastructure (the gate
    # that suppresses wrong-end / across-the-river / roundabout false positives).
    cycling_node_index = _build_cycling_node_index(cycling_graph)

    QUALIFYING_CYCLEWAY_VALUES = {"track", "lane"}

    def _edge_has_qualifying_cycling(d: dict) -> bool:
        return (
            d.get("cycleway", "") in QUALIFYING_CYCLEWAY_VALUES or
            d.get("cycleway:left", "") in QUALIFYING_CYCLEWAY_VALUES or
            d.get("cycleway:right", "") in QUALIFYING_CYCLEWAY_VALUES or
            d.get("cycleway:both", "") in QUALIFYING_CYCLEWAY_VALUES
        )

    gaps = []

    for street_name, edges in corridors.items():
        if len(edges) < 3:
            continue

        total_length = sum(d.get("length_m", 0) for _, _, d in edges)
        cycled_length = sum(
            d.get("length_m", 0) for _, _, d in edges
            if _edge_has_qualifying_cycling(d)
        )
        if total_length == 0:
            continue
        cycle_fraction = cycled_length / total_length
        if cycle_fraction < 0.30:
            continue

        # Build the same-name subgraph: nodes from edges sharing this name,
        # with each edge carrying its has_cycling flag.
        sub = nx.Graph()
        for u, v, d in edges:
            sub.add_edge(u, v,
                         length_m=d.get("length_m", 0.0),
                         has_cycling=_edge_has_qualifying_cycling(d),
                         data=d)
            for n in (u, v):
                if "lat" in road_graph.nodes[n]:
                    sub.nodes[n]["lat"] = road_graph.nodes[n]["lat"]
                    sub.nodes[n]["lon"] = road_graph.nodes[n]["lon"]

        # Walk each linearly connected sub-corridor (one connected component of
        # the same-name subgraph) and emit gaps along it.
        for comp in nx.connected_components(sub):
            comp_sub = sub.subgraph(comp).copy()
            for gap_record in _walk_corridor_for_gaps(comp_sub, street_name):
                start_coord, end_coord, gap_length, start_facility, end_facility = gap_record

                # Endpoint-to-cycling proximity gate — the single check that
                # eliminates wrong-end / roundabout / across-the-river FPs.
                # Both gap endpoints must lie close to actual cycling infra.
                if not _has_cycling_within(cycling_node_index, start_coord, CORRIDOR_NEAR_CYCLING_M):
                    continue
                if not _has_cycling_within(cycling_node_index, end_coord, CORRIDOR_NEAR_CYCLING_M):
                    continue

                if start_facility in NON_QUALIFYING_FACILITIES or end_facility in NON_QUALIFYING_FACILITIES:
                    continue

                if gap_length < MIN_CORRIDOR_GAP_M or gap_length > MAX_CORRIDOR_GAP_M:
                    continue
                if _haversine_m(start_coord[1], start_coord[0],
                                end_coord[1], end_coord[0]) <= SAME_ROAD_LATERAL_M:
                    continue

                gap = Gap("corridor")
                gap.start_coord = start_coord
                gap.end_coord = end_coord
                gap.straight_line_m = gap_length
                gap.detour_factor = None
                gap.start_facility = start_facility
                gap.end_facility = end_facility
                gap.candidate_roads = _find_candidate_roads(
                    road_graph, start_coord, end_coord
                )
                gap.crosses_barrier = False
                gap.from_street = street_name
                gap.to_street = street_name
                gaps.append(gap)

    gaps = _deduplicate_gaps(gaps)
    logger.info(f"Found {len(gaps)} corridor gaps")
    return gaps


# ── Corridor-walk helpers ─────────────────────────────────────────────────────

# Maximum distance from a corridor gap endpoint to the nearest cycling node for
# the gap to be considered legitimate. A real "this road needs cycle lanes"
# corridor gap will have cycling infra (the existing lanes) very close to both
# of its endpoints; if one end is hundreds of metres from any cycling, the
# walker has drifted off the actual cycling network (across a river, around a
# roundabout, onto a same-named-but-disconnected segment).
CORRIDOR_NEAR_CYCLING_M = 35.0


def _walk_corridor_for_gaps(comp_sub: nx.Graph, street_name: str):
    """
    Emit (start_coord, end_coord, length_m, start_facility, end_facility) for
    each gap found by walking a single same-name connected sub-corridor.

    A corridor here is a graph component of a same-name subgraph — almost always
    a chain (degree-1 endpoints with degree-2 interior), occasionally with a few
    junctions. For each maximal path between high-degree nodes (or chain
    endpoints), we walk it edge-by-edge and emit a gap when we find a stretch
    of has_cycling=False edges sandwiched between has_cycling=True edges.
    """
    # Find chain "paths" through the component. We treat any degree-1 or
    # degree>=3 node as a path boundary; the corridor is then made of paths
    # between consecutive boundaries.
    boundary_nodes = [n for n in comp_sub.nodes
                      if comp_sub.degree(n) == 1 or comp_sub.degree(n) >= 3]
    if not boundary_nodes:
        # Pure cycle (e.g. a roundabout-only sub-corridor) — skip; gap semantics
        # don't apply to a closed loop with no chain ends.
        return

    visited_edges = set()
    for start in boundary_nodes:
        for nbr in comp_sub.neighbors(start):
            edge_key = tuple(sorted((start, nbr)))
            if edge_key in visited_edges:
                continue

            # Walk from start through nbr along the degree-2 chain until we hit
            # another boundary node (or come back to start).
            path = [start, nbr]
            visited_edges.add(edge_key)
            cur = nbr
            prev = start
            while comp_sub.degree(cur) == 2 and cur != start:
                nxt = next(n for n in comp_sub.neighbors(cur) if n != prev)
                ek = tuple(sorted((cur, nxt)))
                if ek in visited_edges:
                    break
                visited_edges.add(ek)
                path.append(nxt)
                prev, cur = cur, nxt

            if len(path) < 3:
                continue
            yield from _scan_path_for_gaps(comp_sub, path)


def _scan_path_for_gaps(sub: nx.Graph, path: list):
    """Emit gap tuples from a single chain path through a same-name corridor."""
    in_gap = False
    gap_start_coord = None
    gap_start_facility = None
    gap_length = 0.0
    prev_was_cycling = False

    for i in range(len(path) - 1):
        u, v = path[i], path[i + 1]
        ed = sub[u][v]
        has_cycling = ed.get("has_cycling", False)
        edge_len = ed.get("length_m", 0.0)
        data = ed.get("data", {})

        if "lat" not in sub.nodes[u] or "lat" not in sub.nodes[v]:
            # Missing coords — reset state, can't position a gap.
            in_gap = False; gap_start_coord = None; gap_length = 0.0
            prev_was_cycling = False
            continue

        if not in_gap:
            if has_cycling:
                prev_was_cycling = True
                continue
            # No-cycling edge: open a gap only if the immediately previous edge
            # along the path had qualifying cycling.
            if not prev_was_cycling:
                continue
            in_gap = True
            gap_start_coord = (sub.nodes[u]["lat"], sub.nodes[u]["lon"])
            gap_length = edge_len
            # Recover the prior edge's facility (we know it was cycling).
            if i > 0:
                pu, pv = path[i - 1], path[i]
                gap_start_facility = _infer_facility_from_tags(sub[pu][pv].get("data", {}))
            else:
                gap_start_facility = "unknown"
        else:
            if not has_cycling:
                gap_length += edge_len
                if gap_length > MAX_CORRIDOR_GAP_M:
                    in_gap = False; gap_start_coord = None; gap_length = 0.0
                    prev_was_cycling = False
                continue
            # has_cycling → gap closes here. Emit.
            gap_end_coord = (sub.nodes[u]["lat"], sub.nodes[u]["lon"])
            end_facility = _infer_facility_from_tags(data)
            yield (gap_start_coord, gap_end_coord, gap_length,
                   gap_start_facility, end_facility)
            in_gap = False
            gap_start_coord = None
            gap_length = 0.0
            prev_was_cycling = True


# ── Cycling-node spatial index (used by the corridor proximity gate) ─────────

def _build_cycling_node_index(cycling_graph: nx.Graph, cell_m: float = 50.0):
    """
    Spatial grid of cycling-graph nodes, keyed by lat/lon cells of about
    `cell_m` metres. Each cell holds (node_key, lat, lon) triples so callers
    can both compute distance AND look the node up without scanning the
    whole node set. Used by _has_cycling_within (boolean radius test) and by
    annotate_separation's nearest-node lookup.
    """
    grid = {}
    deg = cell_m / 111000.0
    for n, attrs in cycling_graph.nodes(data=True):
        if "lat" not in attrs:
            continue
        cell = (int(attrs["lat"] / deg), int(attrs["lon"] / deg))
        grid.setdefault(cell, []).append((n, attrs["lat"], attrs["lon"]))
    grid["_cell_deg"] = deg
    return grid


def _has_cycling_within(grid: dict, coord: tuple, radius_m: float) -> bool:
    """True if any cycling-graph node lies within radius_m of (lat, lon)."""
    if not grid:
        return False
    deg = grid.get("_cell_deg")
    if deg is None:
        return False
    lat, lon = coord
    cr = int(lat / deg); cc = int(lon / deg)
    span = max(1, int(radius_m / (deg * 111000.0)) + 1)
    for dr in range(-span, span + 1):
        for dc in range(-span, span + 1):
            for entry in grid.get((cr + dr, cc + dc), []):
                # Skip the _cell_deg sentinel key (it's not a list)
                if not isinstance(entry, tuple):
                    continue
                _, nlat, nlon = entry
                if _haversine_m(lon, lat, nlon, nlat) <= radius_m:
                    return True
    return False




def _infer_facility_from_tags(tags: dict) -> str:
    """Infer facility type from road tags — used in corridor gap scan."""
    cy = tags.get("cycleway", tags.get("cycleway:left", tags.get("cycleway:right",
         tags.get("cycleway:both", ""))))
    if cy in ("track",):
        return "protected_track"
    if cy in ("lane", "opposite_lane"):
        return "cycle_lane"
    if cy in ("shared_lane", "share_busway"):
        return "shared_roadway"
    return "unknown"
