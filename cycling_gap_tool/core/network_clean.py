"""
core/network_clean.py
=====================
Topology cleaning for the cycling graph, applied AFTER build/merge/snap but
BEFORE gap finding.  This is the primary false-positive control mechanism.

Raw OSM geometry fragments a single physical corridor into many short ways and
many degree-2 "pass-through" vertices.  Node-to-node snapping (in
graph_builder._snap_nearby_nodes) fixes coincident endpoints but cannot fix:

  1. Long runs of degree-2 vertices that artificially inflate node/edge counts
     and let gap finders pair two points that are really the same corridor.
  2. A dead-end that stops a few metres from the *middle* of another way
     (a T-junction with no shared node) — node snapping never connects these,
     so they surface later as a false "gap".

Two passes address these:

  consolidate_degree2(G)
      Dissolve every degree-2 node into the edge geometry, keeping only
      intersections (degree >= 3) and true endpoints (degree == 1).  A node is
      preserved where the facility_type changes across it, so genuine facility
      transitions stay visible to the corridor-gap scan.

  snap_endpoints_to_edges(G, tolerance_m)
      Project each degree-1 endpoint onto nearby edges; if it lands within
      tolerance of an edge interior, split that edge and connect.  This is the
      endpoint-to-edge ("T-junction") snap that node snapping misses.

Both are pure-networkx + stdlib — no GIS dependencies — consistent with the
tool's zero-GIS-core philosophy.  An OSMnx-backed equivalent
(simplify_graph + consolidate_intersections) can be substituted by callers who
already have the geospatial stack; see README "Topology cleaning".

Author note: the degree-2 dissolve mirrors the intent of OSMnx's
simplify_graph (Boeing 2025, "Topological Graph Simplification Solutions to the
Street Intersection Miscount Problem", Transactions in GIS 29(3) e70037),
implemented here without the GeoPandas/Shapely dependency.
"""

import logging
import math
import networkx as nx

from core.graph_builder import _haversine_m, _coord_key

logger = logging.getLogger(__name__)

# Endpoint-to-edge snap default.  Slightly looser than the 15 m node snap because
# a dead-end pointing at the side of a through-way is a strong fragmentation
# signal; we still cap it so we don't bridge genuinely separate facilities.
DEFAULT_EDGE_SNAP_M = 18.0

# When dissolving degree-2 chains, do not merge across a facility_type change.
# These are the transitions corridor-gap analysis depends on.
PRESERVE_FACILITY_BOUNDARIES = True


# ─── Pass 1: degree-2 dissolve ────────────────────────────────────────────────

def consolidate_degree2(G: nx.Graph,
                        preserve_facility_boundaries: bool = PRESERVE_FACILITY_BOUNDARIES
                        ) -> nx.Graph:
    """
    Collapse degree-2 pass-through nodes, retaining full edge geometry.

    A continuous trail that OSM split into N ways (and the builder turned into
    N+ two-point edges through a chain of degree-2 vertices) becomes a single
    edge between the two topologically meaningful nodes that bracket it.  The
    concatenated polyline is preserved in the edge `coords` attribute and
    `length_m` is the sum of the dissolved segments, so no geometry or distance
    is lost.

    Nodes are NEVER dissolved if:
      - degree != 2 (intersections and endpoints are always kept), or
      - the two incident edges have different facility_type and
        preserve_facility_boundaries is True (keeps real transitions visible).

    Returns a new graph; the input is not modified.
    """
    H = G.copy()
    initial_nodes = H.number_of_nodes()
    initial_edges = H.number_of_edges()

    # Iterate to a fixed point: dissolving one node can expose its neighbour.
    # Each pass removes at least one node when any are dissolvable, so this
    # terminates in O(number of degree-2 chains) passes.
    changed = True
    guard = 0
    while changed:
        changed = False
        guard += 1
        if guard > initial_nodes + 5:   # safety: cannot exceed node count
            logger.warning("consolidate_degree2: iteration guard tripped")
            break

        for n in list(H.nodes):
            if H.degree(n) != 2:
                continue

            neighbours = list(H.edges(n, data=True))
            # degree 2 but a single parallel/self structure — skip defensively
            if len(neighbours) != 2:
                continue
            (a, _na, da), (b, _nb, db) = (
                (e[0] if e[0] != n else e[1], n, e[2]) for e in neighbours
            )
            # The generator above re-derives the "other" endpoint for each edge.

            if a == b:
                continue                      # would create a self-loop — keep node
            if H.has_edge(a, b):
                # Dissolving would collapse onto an existing a–b edge in a simple
                # Graph, silently dropping one path. Keep the node to preserve
                # both connections (this is a real triangle/parallel case).
                continue

            ft_a = da.get("facility_type")
            ft_b = db.get("facility_type")
            if preserve_facility_boundaries and ft_a != ft_b:
                continue                      # genuine transition — keep as node

            merged_coords = _join_coords(
                _edge_coords(H, a, n, da),
                _edge_coords(H, n, b, db),
            )
            merged_len = da.get("length_m", 0.0) + db.get("length_m", 0.0)

            # Carry osm_id as a flattened list so traceability survives merges.
            merged_osm = _merge_osm_ids(da.get("osm_id"), db.get("osm_id"))

            # facility_type: identical by construction (or boundaries not
            # preserved — then prefer the higher-quality of the two so the
            # dissolved edge is not silently downgraded).
            facility = ft_a if ft_a == ft_b else _better_facility(ft_a, ft_b)

            new_attrs = {
                "length_m": merged_len,
                "facility_type": facility,
                "coords": merged_coords,
                "osm_id": merged_osm,
            }
            # Preserve a representative tags dict if present (for labelling).
            if "tags" in da:
                new_attrs["tags"] = da["tags"]
            elif "tags" in db:
                new_attrs["tags"] = db["tags"]
            if da.get("source") or db.get("source"):
                new_attrs["source"] = da.get("source") or db.get("source")

            H.add_edge(a, b, **new_attrs)
            H.remove_node(n)
            changed = True

    logger.info(
        "Degree-2 dissolve: %d→%d nodes, %d→%d edges",
        initial_nodes, H.number_of_nodes(),
        initial_edges, H.number_of_edges(),
    )
    return H


# ─── Pass 2: endpoint-to-edge ("T-junction") snap ─────────────────────────────

def snap_endpoints_to_edges(G: nx.Graph,
                            tolerance_m: float = DEFAULT_EDGE_SNAP_M) -> nx.Graph:
    """
    Connect degree-1 endpoints that lie within tolerance_m of the *interior* of
    another edge by splitting that edge at the projection point and joining.

    This catches the common fragmentation where a way dead-ends a few metres
    from the side of a through-way with no shared node — invisible to
    node-to-node snapping, and a frequent source of false "gaps".

    Conservative by design:
      - Only degree-1 nodes are projected (true dead-ends, the fragmentation
        signature). A degree>=2 node near another edge is far more likely a real
        parallel facility and is left alone.
      - The candidate edge must not already be incident to the endpoint.
      - The projection must fall on the edge interior (not just near a shared
        node, which the node snapper already handles).

    Returns a new graph; the input is not modified.
    """
    H = G.copy()
    GRID_DEG = tolerance_m / 111000.0
    connected = 0

    # Spatial grid for candidate lookup.  Cell size = tolerance so an endpoint
    # only needs to check its own cell + 8 neighbours.  An edge can be long
    # (especially after the degree-2 dissolve concatenates a whole corridor),
    # so it MUST be indexed into every cell its geometry passes through — not
    # just its endpoint cells — or an endpoint projecting onto the middle of a
    # long edge would never see it as a candidate.
    edge_grid = {}

    def _cells_along(u, v):
        """Yield every grid cell the edge u→v passes through, walking its
        coord polyline (falling back to the straight node-to-node line)."""
        data = H[u][v]
        coords = data.get("coords")
        if not coords or len(coords) < 2:
            coords = [(H.nodes[u]["lon"], H.nodes[u]["lat"]),
                      (H.nodes[v]["lon"], H.nodes[v]["lat"])]
        seen = set()
        for i in range(len(coords) - 1):
            lon1, lat1 = coords[i]
            lon2, lat2 = coords[i + 1]
            # number of samples so consecutive samples are < 1 cell apart
            span = max(abs(lat2 - lat1), abs(lon2 - lon1))
            steps = max(1, int(span / GRID_DEG) + 1)
            for s in range(steps + 1):
                f = s / steps
                la = lat1 + (lat2 - lat1) * f
                lo = lon1 + (lon2 - lon1) * f
                cell = (int(la / GRID_DEG), int(lo / GRID_DEG))
                if cell not in seen:
                    seen.add(cell)
                    yield cell

    def _index_edge(u, v):
        if "lat" not in H.nodes[u] or "lat" not in H.nodes[v]:
            return
        for cell in _cells_along(u, v):
            edge_grid.setdefault(cell, set()).add((u, v))

    for u, v in H.edges():
        _index_edge(u, v)

    degree1 = [n for n in H.nodes if H.degree(n) == 1 and "lat" in H.nodes[n]]

    for n in degree1:
        lat_n = H.nodes[n]["lat"]
        lon_n = H.nodes[n]["lon"]
        cell_r = int(lat_n / GRID_DEG)
        cell_c = int(lon_n / GRID_DEG)

        candidates = set()
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                candidates |= edge_grid.get((cell_r + dr, cell_c + dc), set())

        best = None  # (dist, u, v, proj_lat, proj_lon, t)
        for (u, v) in candidates:
            if not H.has_edge(u, v):
                continue                 # edge was split in a prior iteration
            if n in (u, v):
                continue                 # endpoint's own edge
            if H.has_edge(n, u) or H.has_edge(n, v):
                continue                 # already adjacent — node snap territory

            ulat, ulon = H.nodes[u]["lat"], H.nodes[u]["lon"]
            vlat, vlon = H.nodes[v]["lat"], H.nodes[v]["lon"]
            d, plat, plon, t = _point_to_segment(lat_n, lon_n,
                                                 ulat, ulon, vlat, vlon)
            if d > tolerance_m:
                continue
            # Require an INTERIOR projection. If it projects to (near) an
            # endpoint, the node snapper already covers it; skip to avoid
            # creating a zero-length sliver edge.
            if t < 0.05 or t > 0.95:
                continue
            if best is None or d < best[0]:
                best = (d, u, v, plat, plon, t)

        if best is None:
            continue

        d, u, v, plat, plon, t = best
        _split_edge_and_connect(H, u, v, n, plat, plon)
        connected += 1

        # The old (u,v) edge is gone; its stale grid entry is harmless because
        # the candidate loop guards every edge with H.has_edge(u, v).  Index the
        # new sub-edges and the connector so later endpoints can still snap.
        split_key = _coord_key(plon, plat)
        for nb in (u, v, n):
            if H.has_edge(split_key, nb):
                _index_edge(split_key, nb)

    logger.info("Endpoint-to-edge snap: connected %d dead-ends "
                "(tolerance %.0f m)", connected, tolerance_m)
    return H


# ─── geometry / attribute helpers ─────────────────────────────────────────────

def _point_to_segment(plat, plon, alat, alon, blat, blon):
    """
    Distance (m) from point P to segment A–B, plus the projection point and the
    parametric position t in [0,1] along A→B.  Uses a local equirectangular
    approximation, which is accurate at these short distances.
    """
    # Project lon/lat to local metres around A.
    lat0 = math.radians(alat)
    mx = 111320.0 * math.cos(lat0)        # metres per degree lon
    my = 110540.0                          # metres per degree lat
    ax, ay = 0.0, 0.0
    bx, by = (blon - alon) * mx, (blat - alat) * my
    px, py = (plon - alon) * mx, (plat - alat) * my

    dx, dy = bx - ax, by - ay
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq == 0:
        t = 0.0
    else:
        t = ((px - ax) * dx + (py - ay) * dy) / seg_len_sq
        t = max(0.0, min(1.0, t))
    projx, projy = ax + t * dx, ay + t * dy
    dist = math.hypot(px - projx, py - projy)

    proj_lon = alon + (projx / mx)
    proj_lat = alat + (projy / my)
    return dist, proj_lat, proj_lon, t


def _split_edge_and_connect(H, u, v, n, plat, plon):
    """
    Split edge (u,v) at projection point (plat,plon), creating a split node, and
    connect dead-end n to it.  Edge attributes (facility_type, etc.) are
    inherited by both halves; lengths are recomputed from geometry.
    """
    data = dict(H[u][v])
    facility = data.get("facility_type", "unknown")
    osm_id = data.get("osm_id")
    tags = data.get("tags")

    split_key = _coord_key(plon, plat)
    # If the split point coincides with an existing node, just reuse it.
    if not H.has_node(split_key):
        H.add_node(split_key, lat=plat, lon=plon)

    ulat, ulon = H.nodes[u]["lat"], H.nodes[u]["lon"]
    vlat, vlon = H.nodes[v]["lat"], H.nodes[v]["lon"]
    nlat, nlon = H.nodes[n]["lat"], H.nodes[n]["lon"]

    H.remove_edge(u, v)

    def _add(p, q, p_ll, q_ll):
        plon_, plat_ = p_ll
        qlon_, qlat_ = q_ll
        length = _haversine_m(plon_, plat_, qlon_, qlat_)
        if length <= 0 or p == q:
            return
        attrs = {"length_m": length, "facility_type": facility,
                 "osm_id": osm_id, "coords": [(plon_, plat_), (qlon_, qlat_)]}
        if tags is not None:
            attrs["tags"] = tags
        if not H.has_edge(p, q):
            H.add_edge(p, q, **attrs)

    _add(u, split_key, (ulon, ulat), (plon, plat))
    _add(split_key, v, (plon, plat), (vlon, vlat))
    # Connector from the dead-end to the split point. Inherit the dead-end's
    # own facility type so the new link is not misclassified.
    conn_facility = _incident_facility(H, n) or facility
    conn_len = _haversine_m(nlon, nlat, plon, plat)
    if conn_len > 0 and not H.has_edge(n, split_key):
        H.add_edge(n, split_key, length_m=conn_len,
                   facility_type=conn_facility,
                   osm_id=osm_id, coords=[(nlon, nlat), (plon, plat)],
                   source="endpoint_snap")


def _edge_coords(G, frm, to, data):
    """
    Return the edge's coord polyline oriented frm→to.  Edges store coords as a
    list of (lon,lat); a dissolved edge already holds a multi-point polyline.
    Orientation is decided by which end is closest to `frm`.
    """
    coords = list(data.get("coords") or [])
    if len(coords) < 2:
        # Fall back to straight line from node positions.
        coords = [(G.nodes[frm]["lon"], G.nodes[frm]["lat"]),
                  (G.nodes[to]["lon"], G.nodes[to]["lat"])]
        return coords
    frm_ll = (G.nodes[frm]["lon"], G.nodes[frm]["lat"])
    d_start = _haversine_m(frm_ll[0], frm_ll[1], coords[0][0], coords[0][1])
    d_end = _haversine_m(frm_ll[0], frm_ll[1], coords[-1][0], coords[-1][1])
    if d_end < d_start:
        coords = list(reversed(coords))
    return coords


def _join_coords(a, b):
    """Concatenate two oriented polylines, dropping a duplicated shared vertex."""
    if not a:
        return list(b)
    if not b:
        return list(a)
    if _close(a[-1], b[0]):
        return list(a) + list(b[1:])
    return list(a) + list(b)


def _close(p, q, eps=1e-7):
    return abs(p[0] - q[0]) < eps and abs(p[1] - q[1]) < eps


def _merge_osm_ids(x, y):
    out = []
    for v in (x, y):
        if v is None:
            continue
        if isinstance(v, list):
            out.extend(v)
        else:
            out.append(v)
    # de-dup preserving order
    seen = set()
    res = []
    for v in out:
        if v not in seen:
            seen.add(v)
            res.append(v)
    return res if len(res) != 1 else res[0]


_FACILITY_RANK = {
    "protected_track": 3,
    "shared_path": 3,
    "cycle_lane": 2,
    "shared_roadway": 1,
    "signed_route": 1,
    "unknown": 0,
}


def _better_facility(a, b):
    return a if _FACILITY_RANK.get(a, 0) >= _FACILITY_RANK.get(b, 0) else b


def _incident_facility(G, n):
    """Best qualifying facility_type among edges incident to n, or None."""
    best, best_rank = None, -1
    for _, _, d in G.edges(n, data=True):
        ft = d.get("facility_type", "unknown")
        r = _FACILITY_RANK.get(ft, 0)
        if r > best_rank:
            best, best_rank = ft, r
    return best


# ─── public convenience ───────────────────────────────────────────────────────

def clean_topology(G: nx.Graph,
                   edge_snap_m: float = DEFAULT_EDGE_SNAP_M,
                   preserve_facility_boundaries: bool = PRESERVE_FACILITY_BOUNDARIES
                   ) -> nx.Graph:
    """
    Full topology-cleaning pipeline: endpoint-to-edge snap, then degree-2
    dissolve.  Order matters — snapping first turns some dead-ends into degree-2
    pass-throughs that the dissolve then absorbs, yielding the cleanest result.

    Logs a component-count before/after so the fragmentation reduction is
    visible at INFO level.
    """
    before_components = nx.number_connected_components(G)
    before_nodes = G.number_of_nodes()

    G = snap_endpoints_to_edges(G, tolerance_m=edge_snap_m)
    G = consolidate_degree2(G, preserve_facility_boundaries=preserve_facility_boundaries)

    after_components = nx.number_connected_components(G)
    logger.info(
        "Topology clean complete: nodes %d→%d, components %d→%d "
        "(%d fragments resolved)",
        before_nodes, G.number_of_nodes(),
        before_components, after_components,
        max(0, before_components - after_components),
    )
    return G
