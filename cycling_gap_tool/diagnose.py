"""
diagnose.py  —  Gap Analysis Diagnostic Tool
=============================================
Runs the full graph-build + gap-find pipeline for a small bounding box
and logs every decision: what nodes exist, what facility types they have,
what components they form, why each gap is or is not created.

Usage:
    python diagnose.py --bbox "49.845,49.875,-97.175,-97.135"
    python diagnose.py --region "Winnipeg" --bbox "49.845,49.875,-97.175,-97.135"
    python diagnose.py --region "Winnipeg" --bbox "49.845,49.875,-97.175,-97.135" --out diag.txt

Bbox format: min_lat,max_lat,min_lon,max_lon

To find the bbox for a problem area:
  1. Open the output HTML map in a browser
  2. Hover over the gap dot — note the approximate lat/lon from the URL or popup
  3. Add ~0.01 degrees in each direction as padding
  4. Run this script with that bbox

Output: a plain-text report written to stdout AND --out file (if given).
The report covers:
  - All cycling_ways fetched (facility type, length, start/end coords)
  - All nodes in cycling_graph within the bbox (facility, degree, component)
  - What components exist (size, total length, qualifying status)
  - What dangling nodes were considered and why each gap was/wasn't created
  - What near-miss candidates were evaluated
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import networkx as nx

from core.osm_fetcher import (
    fetch_cycling_network,
    fetch_road_network,
    extract_nodes_and_ways,
    get_bbox_for_region,
)
from core.infra_loader import load_infra_file, bbox_from_ways
from core.graph_builder import (
    build_cycling_graph,
    build_cycling_graph_from_road_attributes,
    build_road_graph,
    merge_cycling_graphs,
    get_connected_components,
    _classify_cycling_facility,
    _way_length_m,
)
from core.gap_finder import (
    _endpoint_facility,
    _haversine_m,
    _component_total_length,
    _nearest_cross_component_pair,
    NON_QUALIFYING_FACILITIES,
    MIN_COMPONENT_LENGTH_M,
    DANGLING_SEARCH_RADIUS_M,
    MAX_GAP_STRAIGHT_LINE_M,
)

# ── helpers ───────────────────────────────────────────────────────────────────

def _in_bbox(lat, lon, bbox):
    min_lat, max_lat, min_lon, max_lon = bbox
    return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon


def _parse_bbox(s):
    parts = [float(x.strip()) for x in s.split(",")]
    if len(parts) != 4:
        raise ValueError("bbox must be min_lat,max_lat,min_lon,max_lon")
    return tuple(parts)


# ── main diagnostic ───────────────────────────────────────────────────────────

def run_diagnostic(bbox, region, out_path,
                   infra_file=None, infra_field_map=None):
    lines = []

    def log(msg=""):
        lines.append(msg)
        print(msg)

    min_lat, max_lat, min_lon, max_lon = bbox
    log("=" * 72)
    log(f"CYCLING GAP DIAGNOSTIC — {region}")
    log(f"Bbox: lat [{min_lat}, {max_lat}]  lon [{min_lon}, {max_lon}]")
    if infra_file:
        log(f"Infrastructure source: {infra_file}")
    else:
        log(f"Infrastructure source: OpenStreetMap (Overpass API)")
    log("=" * 72)

    # ── 1. Fetch / load data ──────────────────────────────────────────────────
    if infra_file:
        log("\n[1] LOADING INFRASTRUCTURE FROM FILE")
        cycling_ways, source_info = load_infra_file(
            filepath=infra_file,
            field_map_path=infra_field_map,
            region_name=region,
        )
        log(f"  Source:   {source_info.source_name}")
        log(f"  Loaded:   {source_info.loaded_count} way segments "
            f"({source_info.feature_count} raw features)")
        log(f"  Skipped (status):   {source_info.skipped_status}")
        log(f"  Skipped (geometry): {source_info.skipped_geometry}")

        full_bbox = bbox_from_ways(cycling_ways)
        log(f"\n  Fetching OSM road network for region bbox {full_bbox}...")
        road_data = fetch_road_network(full_bbox)
        _, road_ways = extract_nodes_and_ways(road_data)
        log(f"  Road ways fetched: {len(road_ways)}")

        # Section [2] — shapefile ways in bbox
        log("\n[2] INFRASTRUCTURE FILE WAYS INSIDE BBOX")
        log(f"  {'ID':<18} {'facility':<20} {'name':<28} {'len_m':>7}  coords")
        log(f"  {'-'*18} {'-'*20} {'-'*28} {'-'*7}  {'-'*6}")
        local_cycling_ways = []
        for w in cycling_ways:
            coords = w.get("coords", [])
            in_box = any(_in_bbox(lat, lon, bbox) for lon, lat in coords)
            if not in_box:
                continue
            fac = _classify_cycling_facility(w.get("tags", {}))
            length = w.get("_length_m", 0)
            name = w.get("tags", {}).get("name", "")
            src_fac = w.get("tags", {}).get("_source_facility", "")
            local_cycling_ways.append(w)
            log(f"  {str(w['id']):<18} {fac:<20} {name:<28} {length:>7.0f}m  "
                f"{len(coords)} pts  [raw: {src_fac}]")
        if not local_cycling_ways:
            log("  ** NO INFRASTRUCTURE FILE WAYS FOUND IN THIS BBOX")

        # Placeholder for section [3] — road ways (same as OSM path below)
        log("\n[3] ROAD WAYS WITH CYCLEWAY TAGS INSIDE BBOX (from OSM road fetch)")

    else:
        log("\n[1] FETCHING OSM DATA")
        full_bbox = get_bbox_for_region(region)
        log(f"  Region bbox: {full_bbox}")
        cycling_data = fetch_cycling_network(full_bbox)
        road_data = fetch_road_network(full_bbox)
        _, cycling_ways = extract_nodes_and_ways(cycling_data)
        _, road_ways = extract_nodes_and_ways(road_data)
        log(f"  Total cycling ways fetched: {len(cycling_ways)}")
        log(f"  Total road ways fetched:    {len(road_ways)}")

        # ── 2. Show raw cycling ways inside bbox ──────────────────────────────
        log("\n[2] CYCLING WAYS INSIDE BBOX (raw, before graph build)")
        log(f"  {'OSM ID':<12} {'facility':<20} {'len_m':>7}  start_coord → end_coord")
        log(f"  {'-'*12} {'-'*20} {'-'*7}  {'-'*40}")
        local_cycling_ways = []
        for w in cycling_ways:
            coords = w.get("coords", [])
            if not coords:
                continue
            in_box = any(_in_bbox(lat, lon, bbox) for lon, lat in coords)
            if not in_box:
                continue
            fac = _classify_cycling_facility(w.get("tags", {}))
            length = _way_length_m(coords)
            sc = coords[0]
            ec = coords[-1]
            local_cycling_ways.append(w)
            log(f"  {w['id']:<12} {fac:<20} {length:>7.0f}m  "
                f"({sc[1]:.5f},{sc[0]:.5f}) → ({ec[1]:.5f},{ec[0]:.5f})")
            tags = w.get("tags", {})
            relevant = {k: v for k, v in tags.items()
                        if k in ("highway","bicycle","foot","cycleway","surface","name")}
            log(f"             tags: {relevant}")

        if not local_cycling_ways:
            log("  ** NO CYCLING WAYS FOUND IN THIS BBOX — widen the bbox")

        log("\n[3] ROAD WAYS WITH CYCLEWAY TAGS INSIDE BBOX (raw)")

    # ── [3] Road ways with cycleway tags ─────────────────────────────────────
    if not infra_file:
        log(f"  {'OSM ID':<12} {'highway':<14} {'cycleway':<18} {'facility':<20} {'len_m':>7}")
        log(f"  {'-'*12} {'-'*14} {'-'*18} {'-'*20} {'-'*7}")
    for w in road_ways:
        coords = w.get("coords", [])
        if not coords:
            continue
        in_box = any(_in_bbox(lat, lon, bbox) for lon, lat in coords)
        if not in_box:
            continue
        tags = w.get("tags", {})
        cy = tags.get("cycleway", "") or tags.get("cycleway:left", "") or tags.get("cycleway:right", "")
        if not cy:
            continue
        fac = _classify_cycling_facility(tags)
        length = _way_length_m(coords)
        hw = tags.get("highway", "")
        log(f"  {w['id']:<12} {hw:<14} {cy:<18} {fac:<20} {length:>7.0f}m")

    # ── [4] Build graphs ──────────────────────────────────────────────────────
    log("\n[4] BUILDING CYCLING GRAPH")
    cycling_graph_primary = build_cycling_graph(cycling_ways)

    if infra_file:
        import networkx as _nx
        cycling_graph = merge_cycling_graphs(cycling_graph_primary, _nx.Graph())
        log(f"  Shapefile graph (after snap): "
            f"{cycling_graph.number_of_nodes()} nodes, "
            f"{cycling_graph.number_of_edges()} edges")
    else:
        cycling_graph_roads = build_cycling_graph_from_road_attributes(road_ways)
        cycling_graph = merge_cycling_graphs(cycling_graph_primary, cycling_graph_roads)
        log(f"  Primary graph:     {cycling_graph_primary.number_of_nodes()} nodes, "
            f"{cycling_graph_primary.number_of_edges()} edges")
        log(f"  Road-attr graph:   {cycling_graph_roads.number_of_nodes()} nodes, "
            f"{cycling_graph_roads.number_of_edges()} edges")
        log(f"  Merged+snapped:    {cycling_graph.number_of_nodes()} nodes, "
            f"{cycling_graph.number_of_edges()} edges")

    # Apply the same topology cleaning the main pipeline uses, so the diagnostic
    # reflects the graph gaps are actually found on. Reported explicitly so a
    # maintainer can see how much fragmentation the clean resolved in this bbox.
    import networkx as _nx2
    from core.network_clean import clean_topology
    comps_before = _nx2.number_connected_components(cycling_graph)
    cycling_graph = clean_topology(cycling_graph)
    comps_after = _nx2.number_connected_components(cycling_graph)
    log(f"  Topology cleaned:  {cycling_graph.number_of_nodes()} nodes, "
        f"{cycling_graph.number_of_edges()} edges "
        f"(components {comps_before}→{comps_after})")

    road_graph = build_road_graph(road_ways)

    # ── 5. Nodes inside bbox ──────────────────────────────────────────────────
    log("\n[5] CYCLING GRAPH NODES INSIDE BBOX")
    components = get_connected_components(cycling_graph)
    node_to_comp = {}
    for i, comp in enumerate(components):
        for n in comp:
            node_to_comp[n] = i

    local_nodes = [
        n for n in cycling_graph.nodes()
        if "lat" in cycling_graph.nodes[n]
        and _in_bbox(cycling_graph.nodes[n]["lat"], cycling_graph.nodes[n]["lon"], bbox)
    ]

    log(f"  Found {len(local_nodes)} nodes in bbox  ({cycling_graph.number_of_nodes()} total in graph)")
    log(f"  {'node_key':<26} {'lat':>9} {'lon':>10} {'deg':>4} {'comp':>5} {'facility':<20} {'edges'}")
    log(f"  {'-'*26} {'-'*9} {'-'*10} {'-'*4} {'-'*5} {'-'*20} {'-'*30}")

    for n in sorted(local_nodes):
        nd = cycling_graph.nodes[n]
        deg = cycling_graph.degree(n)
        comp = node_to_comp.get(n, -1)
        fac = _endpoint_facility(cycling_graph, n)
        edge_facs = [d.get("facility_type","?") for _, _, d in cycling_graph.edges(n, data=True)]
        log(f"  {n:<26} {nd['lat']:>9.5f} {nd['lon']:>10.5f} {deg:>4} {comp:>5} {fac:<20} {edge_facs}")

    if not local_nodes:
        log("  ** NO CYCLING GRAPH NODES IN THIS BBOX")
        log("  Possible causes:")
        log("  - All ways in this area have facility_type in NON_QUALIFYING_FACILITIES")
        log("  - Ways exist but their START/END nodes are outside this bbox")
        log("  - Snapping moved nodes outside bbox boundaries")
        # Show nearest nodes
        log("\n  Nearest nodes outside bbox:")
        def dist_to_bbox_centre(n):
            lat = cycling_graph.nodes[n].get("lat", 0)
            lon = cycling_graph.nodes[n].get("lon", 0)
            clat = (min_lat + max_lat) / 2
            clon = (min_lon + max_lon) / 2
            return _haversine_m(clon, clat, lon, lat)
        all_nodes = [n for n in cycling_graph.nodes() if "lat" in cycling_graph.nodes[n]]
        nearest = sorted(all_nodes, key=dist_to_bbox_centre)[:10]
        for n in nearest:
            nd = cycling_graph.nodes[n]
            fac = _endpoint_facility(cycling_graph, n)
            d = dist_to_bbox_centre(n)
            comp = node_to_comp.get(n, -1)
            log(f"    {n:<26} lat={nd['lat']:.5f} lon={nd['lon']:.5f} "
                f"fac={fac} comp={comp} dist_to_centre={d:.0f}m")

    # ── 6. Components that touch the bbox ─────────────────────────────────────
    log("\n[6] COMPONENTS THAT HAVE AT LEAST ONE NODE IN BBOX")
    local_comps = set(node_to_comp[n] for n in local_nodes)

    for ci in sorted(local_comps):
        comp = components[ci]
        total_len = _component_total_length(cycling_graph, comp)
        in_bbox_count = sum(
            1 for n in comp
            if "lat" in cycling_graph.nodes[n]
            and _in_bbox(cycling_graph.nodes[n]["lat"], cycling_graph.nodes[n]["lon"], bbox)
        )
        is_main = ci == 0
        qualifies = total_len >= MIN_COMPONENT_LENGTH_M
        log(f"\n  Component {ci}{'  [MAIN]' if is_main else ''}:")
        log(f"    nodes={len(comp)}  total_length={total_len:.0f}m  "
            f"nodes_in_bbox={in_bbox_count}")
        log(f"    qualifies for island_gap (>={MIN_COMPONENT_LENGTH_M}m): {qualifies}")
        if not qualifies:
            log(f"    ** FILTERED OUT by MIN_COMPONENT_LENGTH_M — "
                f"increase to catch this component")

        # Show all nodes of this component that are in or near bbox
        padding = 0.02  # ~2km padding to show surrounding context
        padded_bbox = (min_lat - padding, max_lat + padding,
                       min_lon - padding, max_lon + padding)
        comp_nodes_near = [
            n for n in comp
            if "lat" in cycling_graph.nodes[n]
            and _in_bbox(cycling_graph.nodes[n]["lat"], cycling_graph.nodes[n]["lon"], padded_bbox)
        ]
        for n in comp_nodes_near[:20]:  # cap at 20 per component
            nd = cycling_graph.nodes[n]
            fac = _endpoint_facility(cycling_graph, n)
            deg = cycling_graph.degree(n)
            in_b = "IN-BBOX" if _in_bbox(nd["lat"], nd["lon"], bbox) else "nearby"
            log(f"    [{in_b}] {n:<26} lat={nd['lat']:.5f} lon={nd['lon']:.5f} "
                f"deg={deg} fac={fac}")

        if not is_main and qualifies:
            # Show what gap would be created to main component
            if components[0]:
                island_node, main_node, dist = _nearest_cross_component_pair(
                    cycling_graph, set(comp), components[0]
                )
                if island_node and main_node:
                    idata = cycling_graph.nodes[island_node]
                    mdata = cycling_graph.nodes[main_node]
                    ifac = _endpoint_facility(cycling_graph, island_node)
                    mfac = _endpoint_facility(cycling_graph, main_node)
                    size = len(comp)
                    if size >= 10:
                        threshold = MAX_GAP_STRAIGHT_LINE_M * 5
                    elif size >= 5:
                        threshold = MAX_GAP_STRAIGHT_LINE_M * 2.5
                    else:
                        threshold = MAX_GAP_STRAIGHT_LINE_M * 1.5
                    log(f"    → nearest pair to main component: dist={dist:.0f}m "
                        f"(threshold={threshold:.0f}m)")
                    log(f"       island_node: {island_node} fac={ifac}")
                    log(f"       main_node:   {main_node} fac={mfac}")
                    if dist > threshold:
                        log(f"    ** GAP SUPPRESSED: dist {dist:.0f}m > threshold {threshold:.0f}m")
                    elif ifac in NON_QUALIFYING_FACILITIES:
                        log(f"    ** GAP SUPPRESSED: island_node facility '{ifac}' is non-qualifying")
                    elif mfac in NON_QUALIFYING_FACILITIES:
                        log(f"    ** GAP SUPPRESSED: main_node facility '{mfac}' is non-qualifying")
                    else:
                        log(f"    ** GAP WOULD BE CREATED: island→main, {dist:.0f}m")

    # ── 7. Dangling nodes in/near bbox ────────────────────────────────────────
    log("\n[7] DANGLING NODES (degree=1) IN/NEAR BBOX")
    padding = 0.005
    padded = (min_lat - padding, max_lat + padding,
              min_lon - padding, max_lon + padding)

    dangling = [
        n for n in cycling_graph.nodes()
        if cycling_graph.degree(n) == 1
        and "lat" in cycling_graph.nodes[n]
        and _in_bbox(cycling_graph.nodes[n]["lat"], cycling_graph.nodes[n]["lon"], padded)
    ]
    log(f"  Found {len(dangling)} dangling nodes in/near bbox")

    for node_a in dangling:
        nd = cycling_graph.nodes[node_a]
        fac_a = _endpoint_facility(cycling_graph, node_a)
        comp_a = node_to_comp.get(node_a, -1)
        in_b = "IN-BBOX" if _in_bbox(nd["lat"], nd["lon"], bbox) else "nearby"
        log(f"\n  [{in_b}] Dangling node: {node_a}")
        log(f"    lat={nd['lat']:.5f} lon={nd['lon']:.5f} comp={comp_a} fac={fac_a}")

        if fac_a in NON_QUALIFYING_FACILITIES:
            log(f"    ** SKIP: own facility '{fac_a}' is non-qualifying")
            continue

        # Find nearest cross-component node within search radius
        best_node = None
        best_dist = float("inf")
        candidates_checked = 0
        candidates_skipped_same_comp = 0
        candidates_skipped_non_qual = 0

        for node_b in cycling_graph.nodes():
            if node_b == node_a:
                continue
            if "lat" not in cycling_graph.nodes[node_b]:
                continue
            comp_b = node_to_comp.get(node_b, -2)
            if comp_b == comp_a:
                candidates_skipped_same_comp += 1
                continue
            lat_b = cycling_graph.nodes[node_b]["lat"]
            lon_b = cycling_graph.nodes[node_b]["lon"]
            d = _haversine_m(nd["lon"], nd["lat"], lon_b, lat_b)
            if d > DANGLING_SEARCH_RADIUS_M:
                continue
            candidates_checked += 1
            fac_b = _endpoint_facility(cycling_graph, node_b)
            if fac_b in NON_QUALIFYING_FACILITIES:
                candidates_skipped_non_qual += 1
                continue
            if d < best_dist:
                best_dist = d
                best_node = node_b

        log(f"    Cross-comp candidates within {DANGLING_SEARCH_RADIUS_M}m: "
            f"{candidates_checked} checked, "
            f"{candidates_skipped_same_comp} same-comp skipped, "
            f"{candidates_skipped_non_qual} non-qualifying skipped")

        if best_node:
            bn = cycling_graph.nodes[best_node]
            bf = _endpoint_facility(cycling_graph, best_node)
            log(f"    → BEST TARGET: {best_node} dist={best_dist:.0f}m fac={bf}")
            log(f"      lat={bn['lat']:.5f} lon={bn['lon']:.5f} "
                f"comp={node_to_comp.get(best_node,-1)}")
            log(f"    ** GAP WOULD BE CREATED: {node_a} → {best_node}, {best_dist:.0f}m")
        else:
            log(f"    ** NO GAP: no qualifying cross-component node within "
                f"{DANGLING_SEARCH_RADIUS_M}m")
            # Show what's closest even if filtered
            best_any = None
            best_any_dist = float("inf")
            for node_b in cycling_graph.nodes():
                if node_b == node_a:
                    continue
                if "lat" not in cycling_graph.nodes[node_b]:
                    continue
                comp_b = node_to_comp.get(node_b, -2)
                if comp_b == comp_a:
                    continue
                lat_b = cycling_graph.nodes[node_b]["lat"]
                lon_b = cycling_graph.nodes[node_b]["lon"]
                d = _haversine_m(nd["lon"], nd["lat"], lon_b, lat_b)
                if d < best_any_dist:
                    best_any_dist = d
                    best_any = node_b
            if best_any:
                bf = _endpoint_facility(cycling_graph, best_any)
                bn = cycling_graph.nodes[best_any]
                log(f"    Closest cross-comp node (any facility, any dist): "
                    f"{best_any} dist={best_any_dist:.0f}m fac={bf} "
                    f"lat={bn['lat']:.5f} lon={bn['lon']:.5f}")

    # ── 8. Near-miss scan inside bbox ─────────────────────────────────────────
    NEAR_MISS_MAX_M = 300
    NEAR_MISS_MIN_M = 20
    log(f"\n[8] NEAR-MISS SCAN INSIDE BBOX (max={NEAR_MISS_MAX_M}m)")

    infra_nodes_local = [
        n for n in cycling_graph.nodes()
        if "lat" in cycling_graph.nodes[n]
        and _in_bbox(cycling_graph.nodes[n]["lat"], cycling_graph.nodes[n]["lon"], bbox)
        and _endpoint_facility(cycling_graph, n) not in NON_QUALIFYING_FACILITIES
    ]
    log(f"  Infrastructure nodes in bbox: {len(infra_nodes_local)}")

    seen = set()
    near_miss_candidates = []
    for node_a in infra_nodes_local:
        la = cycling_graph.nodes[node_a]["lat"]
        loa = cycling_graph.nodes[node_a]["lon"]
        comp_a = node_to_comp.get(node_a, -1)
        for node_b in cycling_graph.nodes():
            if node_b == node_a:
                continue
            comp_b = node_to_comp.get(node_b, -2)
            if comp_b == comp_a:
                continue
            if "lat" not in cycling_graph.nodes[node_b]:
                continue
            pair = tuple(sorted([node_a, node_b]))
            if pair in seen:
                continue
            seen.add(pair)
            lb = cycling_graph.nodes[node_b]["lat"]
            lob = cycling_graph.nodes[node_b]["lon"]
            d = _haversine_m(loa, la, lob, lb)
            if d < NEAR_MISS_MIN_M or d > NEAR_MISS_MAX_M:
                continue
            fb = _endpoint_facility(cycling_graph, node_b)
            near_miss_candidates.append((d, node_a, node_b, fb))

    near_miss_candidates.sort()
    log(f"  Cross-component pairs within {NEAR_MISS_MIN_M}–{NEAR_MISS_MAX_M}m: "
        f"{len(near_miss_candidates)}")
    for d, na, nb, fb in near_miss_candidates[:20]:
        fa = _endpoint_facility(cycling_graph, na)
        ca = node_to_comp.get(na, -1)
        cb = node_to_comp.get(nb, -1)
        qual = fa not in NON_QUALIFYING_FACILITIES and fb not in NON_QUALIFYING_FACILITIES
        status = "WOULD CREATE GAP" if qual else f"FILTERED (fa={fa} fb={fb})"
        log(f"  {d:>6.0f}m  comp{ca}→comp{cb}  fa={fa:<18} fb={fb:<18} → {status}")

    # ── 9. Summary ────────────────────────────────────────────────────────────
    log("\n[9] SUMMARY")
    log(f"  Cycling graph total: {cycling_graph.number_of_nodes()} nodes, "
        f"{cycling_graph.number_of_edges()} edges")
    log(f"  Components: {len(components)} total, "
        f"main has {len(components[0])} nodes")
    filtered_islands = sum(
        1 for c in components[1:]
        if _component_total_length(cycling_graph, c) < MIN_COMPONENT_LENGTH_M
    )
    log(f"  Islands filtered by MIN_COMPONENT_LENGTH_M ({MIN_COMPONENT_LENGTH_M}m): "
        f"{filtered_islands}")
    log(f"  Dangling nodes in/near bbox: {len(dangling)}")
    log(f"  Near-miss candidates in bbox: {len(near_miss_candidates)}")
    log()
    log("  To diagnose a specific gap:")
    log("  1. Note the gap's lat/lon from the review map popup")
    log("  2. Re-run with a tighter bbox centred on that location")
    log("  3. Look for the relevant nodes in sections [5]-[8]")
    log("=" * 72)

    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"\nDiagnostic written to: {out_path}")


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Gap Analysis Diagnostic Tool")
    parser.add_argument(
        "--bbox", required=True,
        help="min_lat,max_lat,min_lon,max_lon  e.g. 49.845,49.875,-97.175,-97.135"
    )
    parser.add_argument(
        "--region", default="Winnipeg",
        help="Region name for OSM fetch (default: Winnipeg)"
    )
    parser.add_argument(
        "--infra-file", default=None, dest="infra_file",
        help="Path to municipal cycling network GeoJSON or Shapefile (optional)"
    )
    parser.add_argument(
        "--infra-field-map", default=None, dest="infra_field_map",
        help="Path to YAML field-map config for --infra-file"
    )
    parser.add_argument(
        "--out", default=None,
        help="Write output to this file as well as stdout"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    bbox = _parse_bbox(args.bbox)
    run_diagnostic(bbox, args.region, args.out,
                   infra_file=args.infra_file,
                   infra_field_map=args.infra_field_map)


if __name__ == "__main__":
    main()
