"""
graph_builder.py
Converts OSM way data into a networkx graph for connectivity analysis.

Nodes = intersections / endpoints (identified by lat/lon rounded to ~5m precision)
Edges = way segments between nodes, with LTS and facility type as attributes

The cycling graph and the road graph are built separately:
  - cycling_graph: only infrastructure where cycling is explicitly provided
  - road_graph:    full road network, used to find candidate gap-fill corridors
"""

import math
import networkx as nx
import logging

logger = logging.getLogger(__name__)

# Round coordinates to this many decimal places for node deduplication
# 5 decimal places ≈ 1.1m precision — fine for network analysis
COORD_PRECISION = 5


def _coord_key(lon: float, lat: float) -> str:
    """Create a hashable node key from coordinates."""
    return f"{round(lat, COORD_PRECISION)},{round(lon, COORD_PRECISION)}"


def _haversine_m(lon1, lat1, lon2, lat2) -> float:
    """
    Calculate distance in metres between two lat/lon points.
    Uses haversine formula.
    """
    R = 6371000  # Earth radius in metres
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _way_length_m(coords: list) -> float:
    """Sum of segment lengths for a way defined by (lon, lat) coord list."""
    total = 0.0
    for i in range(len(coords) - 1):
        total += _haversine_m(coords[i][0], coords[i][1], coords[i+1][0], coords[i+1][1])
    return total


def _classify_cycling_facility(tags: dict) -> str:
    """
    Classify OSM tags into a cycling facility type string.
    Used for both edge attributes and gap-end context analysis.

    Returns one of:
      'protected_track'   — physically separated from traffic
      'cycle_lane'        — painted lane on road
      'shared_path'       — multi-use path, no motor traffic
      'shared_roadway'    — no dedicated facility, low stress road
      'signed_route'      — route designation only, no physical infra
      'unknown'
    """
    hw = tags.get("highway", "")
    cy = tags.get("cycleway", "")
    bicycle = tags.get("bicycle", "")
    segregated = tags.get("segregated", "")

    if hw == "cycleway":
        return "protected_track"
    if cy in ("track",):
        return "protected_track"
    if hw in ("path", "footway") and bicycle == "designated":
        if segregated == "yes":
            return "protected_track"
        return "shared_path"
    if cy in ("lane", "opposite_lane"):
        return "cycle_lane"
    if cy in ("shared_lane", "share_busway"):
        return "shared_roadway"
    if bicycle in ("designated", "yes") and hw in ("residential", "living_street", "service"):
        return "shared_roadway"
    if tags.get("route") == "bicycle":
        return "signed_route"

    return "unknown"


def build_cycling_graph(cycling_ways: list) -> nx.Graph:
    """
    Build an undirected graph of the cycling network.
    Each edge carries:
      - length_m: segment length in metres
      - facility_type: classified cycling infrastructure type
      - osm_id: source way ID for traceability
      - tags: full OSM tags dict
      - coords: list of (lon, lat) for the segment
    """
    G = nx.Graph()

    # Tier quality gate — only Tier 1 and Tier 2 enter gap analysis
    # Tier 1: dedicated cycling infrastructure (highway=cycleway, designated paths)
    # Tier 2: cycle tracks and lanes on major roads
    # Excluded: shared_roadway, signed_route (too noisy, generates false dangling nodes)
    EXCLUDED_FACILITY_TYPES = {"shared_roadway", "signed_route", "unknown"}

    for way in cycling_ways:
        coords = way["coords"]
        if len(coords) < 2:
            continue

        tags = way["tags"]
        facility_type = _classify_cycling_facility(tags)

        # Skip low-quality facilities — they inflate dangling node count
        # and generate noise gaps rather than real infrastructure gaps
        if facility_type in EXCLUDED_FACILITY_TYPES:
            continue

        # Add every consecutive coordinate pair as a separate graph edge.
        #
        # CRITICAL FIX: previously this only added coords[0] and coords[-1],
        # treating each OSM way as a single two-node edge. OSM splits continuous
        # trails into many short ways at intersections and attribute-change points.
        # Adjacent ways share a physical node (same OSM node ID → identical
        # coordinates), so they must share a graph node to appear connected.
        # With the start/end-only approach, Winnipeg's cycling network fragmented
        # into 785 separate components despite being mostly physically connected —
        # the Speers Rd MUP alone split into 10 disconnected 2-node components.
        # Using all intermediate coordinates means any two ways sharing a node
        # will produce the same _coord_key at that point and auto-connect.
        for i in range(len(coords) - 1):
            lon_a, lat_a = coords[i]
            lon_b, lat_b = coords[i + 1]
            key_a = _coord_key(lon_a, lat_a)
            key_b = _coord_key(lon_b, lat_b)

            G.add_node(key_a, lat=lat_a, lon=lon_a)
            G.add_node(key_b, lat=lat_b, lon=lon_b)

            if key_a == key_b:
                continue  # zero-length segment (same-point way) — skip

            seg_len = _haversine_m(lon_a, lat_a, lon_b, lat_b)
            if not G.has_edge(key_a, key_b):
                G.add_edge(
                    key_a, key_b,
                    length_m=seg_len,
                    facility_type=facility_type,
                    osm_id=way["id"],
                    tags=tags,
                    coords=[coords[i], coords[i + 1]],
                )

    logger.info(
        f"Cycling graph (pre-snap): {G.number_of_nodes()} nodes, {G.number_of_edges()} edges"
    )
    G = _snap_nearby_nodes(G, snap_distance_m=15.0)
    logger.info(
        f"Cycling graph (post-snap): {G.number_of_nodes()} nodes, {G.number_of_edges()} edges"
    )
    return G


def build_road_graph(road_ways: list) -> nx.Graph:
    """
    Build a graph of the full road network for gap-fill candidate analysis.
    Each edge carries road attributes needed for LTS scoring and OTM Book 18
    facility recommendations.

    Attributes:
      - length_m
      - highway: OSM highway classification
      - maxspeed: posted speed limit (km/h), parsed from OSM tag
      - lanes: number of lanes (int)
      - cycleway: existing cycling provision tag
      - oneway: bool
      - name: road name if tagged
      - aadt_proxy: estimated AADT category from road classification
                    (used where actual counts unavailable — flagged in output)
    """
    G = nx.Graph()

    for way in road_ways:
        coords = way["coords"]
        if len(coords) < 2:
            continue

        tags = way["tags"]
        length = _way_length_m(coords)

        start_key = _coord_key(coords[0][0], coords[0][1])
        end_key = _coord_key(coords[-1][0], coords[-1][1])

        G.add_node(start_key, lat=coords[0][1], lon=coords[0][0])
        G.add_node(end_key, lat=coords[-1][1], lon=coords[-1][0])

        if start_key != end_key:
            G.add_edge(
                start_key,
                end_key,
                length_m=length,
                highway=tags.get("highway", "unclassified"),
                maxspeed=_parse_speed(tags.get("maxspeed", "")),
                lanes=_parse_lanes(tags.get("lanes", "")),
                cycleway=tags.get("cycleway", "none"),
                oneway=tags.get("oneway", "no") == "yes",
                name=tags.get("name", ""),
                junction=tags.get("junction", ""),
                aadt_proxy=_aadt_proxy(tags.get("highway", "")),
                aadt_is_proxy=True,  # flag: actual counts not used
                osm_id=way["id"],
                coords=coords,
            )

    logger.info(
        f"Road graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges"
    )
    return G


def _parse_speed(maxspeed_tag: str) -> int:
    """
    Parse OSM maxspeed tag to integer km/h.
    Handles: '50', '50 km/h', '30 mph', 'CA:urban', blank.
    Returns 0 if unparseable (caller should treat as unknown).
    """
    if not maxspeed_tag:
        return 0
    tag = maxspeed_tag.strip().lower()

    # Named speed zones (Canadian context)
    named = {
        "ca:urban": 50, "ca:rural": 80, "ca:motorway": 100,
        "urban": 50, "rural": 80,
    }
    if tag in named:
        return named[tag]

    # Strip units
    tag = tag.replace("km/h", "").replace("kmh", "").strip()
    if "mph" in tag:
        try:
            return round(float(tag.replace("mph", "").strip()) * 1.60934)
        except ValueError:
            return 0
    try:
        return int(float(tag))
    except ValueError:
        return 0


def _parse_lanes(lanes_tag: str) -> int:
    """Parse OSM lanes tag to integer. Returns 1 if unparseable."""
    try:
        return int(float(lanes_tag.strip()))
    except (ValueError, AttributeError):
        return 1


def _aadt_proxy(highway_class: str) -> str:
    """
    Assign AADT proxy category from OSM highway classification.
    Used where actual traffic counts are unavailable.
    Based on typical Canadian municipal traffic volume ranges.

    NOTE: This is a proxy. Where municipal traffic model outputs are available
    they should override this value. Future version input: AADT shapefile/CSV.
    """
    proxies = {
        "motorway":      ">40000",
        "trunk":         "20000-40000",
        "primary":       "10000-25000",
        "secondary":     "5000-15000",
        "tertiary":      "1000-5000",
        "unclassified":  "500-2000",
        "residential":   "<1000",
        "living_street": "<200",
        "service":       "<500",
    }
    return proxies.get(highway_class, "unknown")


def get_connected_components(G: nx.Graph) -> list:
    """
    Return list of connected components, sorted by size descending.
    The largest component is typically the main cycling network.
    Smaller components are isolated segments — potential gap endpoints.
    """
    components = sorted(
        nx.connected_components(G), key=len, reverse=True
    )
    logger.info(
        f"Found {len(components)} connected components. "
        f"Largest has {len(components[0])} nodes."
    )
    return components


def get_component_bounds(G: nx.Graph, component: set) -> tuple:
    """Return (min_lat, min_lon, max_lat, max_lon) for a component's nodes."""
    lats = [G.nodes[n]["lat"] for n in component if "lat" in G.nodes[n]]
    lons = [G.nodes[n]["lon"] for n in component if "lon" in G.nodes[n]]
    if not lats:
        return None
    return (min(lats), min(lons), max(lats), max(lons))


def component_centroid(G: nx.Graph, component: set) -> tuple:
    """Return (lat, lon) centroid of a component."""
    lats = [G.nodes[n]["lat"] for n in component if "lat" in G.nodes[n]]
    lons = [G.nodes[n]["lon"] for n in component if "lon" in G.nodes[n]]
    if not lats:
        return (0, 0)
    return (sum(lats) / len(lats), sum(lons) / len(lons))

def _snap_nearby_nodes(G: nx.Graph, snap_distance_m: float = 15.0) -> nx.Graph:
    """
    Merge nodes that are within snap_distance_m of each other into a single node.
    
    This fixes OSM topology gaps where two ways cross or nearly touch but don't
    share a node ID — extremely common at intersections and trail connections.
    Without snapping, visually connected infrastructure appears disconnected in 
    the graph, producing false-positive gaps like the Albert/Phillip case.
    
    Algorithm:
      1. Build a grid index of all nodes
      2. For each node, find all other nodes within snap_distance_m
      3. Merge close node pairs using union-find, redirecting all edges
    """
    if G.number_of_nodes() == 0:
        return G

    nodes = list(G.nodes(data=True))
    GRID_DEG = snap_distance_m / 111000.0

    # Build grid index
    grid = {}
    for node_key, attrs in nodes:
        if "lat" not in attrs:
            continue
        cell = (int(attrs["lat"] / GRID_DEG), int(attrs["lon"] / GRID_DEG))
        grid.setdefault(cell, []).append(node_key)

    # Union-Find for merging
    parent = {n: n for n, _ in nodes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        parent[find(x)] = find(y)

    # Find pairs to merge
    merged = 0
    for node_key, attrs in nodes:
        if "lat" not in attrs:
            continue
        lat, lon = attrs["lat"], attrs["lon"]
        cell_r = int(lat / GRID_DEG)
        cell_c = int(lon / GRID_DEG)
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                for other_key in grid.get((cell_r + dr, cell_c + dc), []):
                    if other_key == node_key:
                        continue
                    other_attrs = G.nodes[other_key]
                    if "lat" not in other_attrs:
                        continue
                    dist = _haversine_m(lon, lat, other_attrs["lon"], other_attrs["lat"])
                    if dist <= snap_distance_m and find(node_key) != find(other_key):
                        union(node_key, other_key)
                        merged += 1

    if merged == 0:
        return G

    # Build new graph with merged nodes
    H = nx.Graph()

    # Add representative node for each component
    for node_key, attrs in nodes:
        rep = find(node_key)
        if not H.has_node(rep):
            H.add_node(rep, **attrs)

    # Re-add edges using representative nodes
    for u, v, data in G.edges(data=True):
        rep_u = find(u)
        rep_v = find(v)
        if rep_u != rep_v:
            H.add_edge(rep_u, rep_v, **data)

    logger.debug(f"Node snapping: merged {merged} near-duplicate nodes, "
                 f"{G.number_of_nodes()} → {H.number_of_nodes()} nodes")
    return H


def build_cycling_graph_from_road_attributes(road_ways: list) -> nx.Graph:
    """
    Extract cycling infrastructure that exists as road attributes (cycleway=track/lane)
    rather than as standalone cycling ways.

    Many cycle tracks in OSM are tagged on the parent road way rather than drawn
    as separate geometries. These are invisible to a query for highway=cycleway
    but represent real, rideable infrastructure. This function extracts them and
    returns a supplementary graph that can be merged with the main cycling graph.

    Only Tier 1/2 facilities enter the graph — the same exclusion set used by
    build_cycling_graph.  shared_lane (sharrows) and opposite_lane are excluded
    because they represent paint markings, not physical infrastructure.  Allowing
    them in creates spurious graph connections: a sharrow node snapped to a real
    cycling node absorbs isolated infrastructure into the main component, masking
    genuine gaps, and also produces false-positive long-distance gap arrows whose
    endpoints sit on roads with no visible cycling provision.
    """
    # Mirror the exclusion set from build_cycling_graph — must stay in sync.
    EXCLUDED_FACILITY_TYPES = {"shared_roadway", "signed_route", "unknown"}

    # Only pass cycleway values that represent real, rideable infrastructure.
    # shared_lane = sharrows (paint only), opposite_lane = contraflow advisory only.
    QUALIFYING_CYCLEWAY_VALUES = {"track", "lane"}

    G = nx.Graph()

    for way in road_ways:
        tags = way.get("tags", {})
        coords = way.get("coords", [])

        if len(coords) < 2:
            continue

        # Check for qualifying cycling facility attributes on road ways
        cycleway = tags.get("cycleway", "")
        cycleway_left = tags.get("cycleway:left", tags.get("cycleway:both", ""))
        cycleway_right = tags.get("cycleway:right", tags.get("cycleway:both", ""))

        has_facility = cycleway in QUALIFYING_CYCLEWAY_VALUES or \
                       cycleway_left in QUALIFYING_CYCLEWAY_VALUES or \
                       cycleway_right in QUALIFYING_CYCLEWAY_VALUES

        if not has_facility:
            continue

        facility_type = _classify_cycling_facility(tags)

        # Belt-and-suspenders: apply the same tier gate as build_cycling_graph.
        if facility_type in EXCLUDED_FACILITY_TYPES:
            continue

        length = _way_length_m(coords)  # kept for logging only

        # Same intermediate-node fix as build_cycling_graph — see comment there.
        for i in range(len(coords) - 1):
            lon_a, lat_a = coords[i]
            lon_b, lat_b = coords[i + 1]
            key_a = _coord_key(lon_a, lat_a)
            key_b = _coord_key(lon_b, lat_b)

            G.add_node(key_a, lat=lat_a, lon=lon_a)
            G.add_node(key_b, lat=lat_b, lon=lon_b)

            if key_a == key_b:
                continue

            seg_len = _haversine_m(lon_a, lat_a, lon_b, lat_b)
            if not G.has_edge(key_a, key_b):
                G.add_edge(
                    key_a, key_b,
                    length_m=seg_len,
                    facility_type=facility_type,
                    osm_id=way["id"],
                    tags=tags,
                    coords=[coords[i], coords[i + 1]],
                    source="road_attribute",
                )

    logger.info(
        f"Road-attribute cycling graph: {G.number_of_nodes()} nodes, "
        f"{G.number_of_edges()} edges"
    )
    return G


def merge_cycling_graphs(primary: nx.Graph, secondary: nx.Graph,
                          snap_distance_m: float = 15.0) -> nx.Graph:
    """
    Merge two cycling graphs (standalone ways + road-attribute ways) into one,
    then snap nearby nodes to connect them at intersections.
    """
    merged = nx.Graph()

    for node, attrs in primary.nodes(data=True):
        merged.add_node(node, **attrs)
    for u, v, data in primary.edges(data=True):
        merged.add_edge(u, v, **data)

    for node, attrs in secondary.nodes(data=True):
        if not merged.has_node(node):
            merged.add_node(node, **attrs)
    for u, v, data in secondary.edges(data=True):
        if not merged.has_edge(u, v):
            merged.add_edge(u, v, **data)

    # Snap to connect the two graphs at shared locations
    merged = _snap_nearby_nodes(merged, snap_distance_m=snap_distance_m)

    logger.info(
        f"Merged cycling graph: {merged.number_of_nodes()} nodes, "
        f"{merged.number_of_edges()} edges"
    )
    return merged
