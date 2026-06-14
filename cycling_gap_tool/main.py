"""
main.py
Entry point for the Cycling Infrastructure Gap Analysis Tool.

Usage:
  python main.py --region "Waterloo Region" --output ./output
  python main.py --region "City of Kitchener" --master-plan ./data/kitchener_atmp.geojson

  # With authoritative municipal shapefile instead of OSM cycling data:
  python main.py --region "Winnipeg" \\
    --infra-file ./data/winnipeg_at_network.geojson \\
    --infra-field-map ./data/field_maps/winnipeg.yaml

Optional arguments:
  --region            Region name (default: "Waterloo Region")
  --output            Output directory (default: ./output)
  --master-plan       Path to GeoJSON AT Master Plan / TMP file
  --infra-file        Path to municipal cycling network GeoJSON or Shapefile.
                      When provided, replaces OSM cycling data as the primary
                      infrastructure source.  OSM road network is still fetched
                      for routing, LTS scoring, and barrier detection.
  --infra-field-map   Path to YAML field-map config for --infra-file.
                      See data/field_maps/winnipeg.yaml for a worked example.
  --no-osm            Skip all OSM data fetching entirely. Only valid with
                      --infra-file. Gap detection runs purely on the supplied
                      file geometry; LTS, barrier, and destination scoring
                      are replaced with neutral defaults. Useful for a first
                      pass on a new city before OSM data quality is verified,
                      or when working offline.
  --no-equity         Skip equity scoring (use if census data unavailable)
  --verbose           Enable debug logging
"""

import argparse
import logging
import os
import sys

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.osm_fetcher import (
    get_bbox_for_region,
    fetch_cycling_network,
    fetch_road_network,
    fetch_destinations,
    load_master_plan,
    extract_nodes_and_ways,
)
from core.infra_loader import load_infra_file, bbox_from_ways
from core.graph_builder import (
    build_cycling_graph,
    build_road_graph,
    build_cycling_graph_from_road_attributes,
    merge_cycling_graphs,
)
from core.gap_finder import (
    find_island_gaps,
    find_detour_gaps,
    find_dangling_gaps,
    find_corridor_gaps,
    find_near_miss_gaps,
    find_junction_connector_gaps,
    match_master_plan,
    assign_gap_ids,
    annotate_separation,
    deduplicate_all_gaps,
    filter_roundabout_crossings,
)
from scoring.priority import score_all_gaps, compute_network_baseline
from scoring.spines import detect_spines
from output.report import generate_html_map, generate_csv_report, generate_geojson
from output.review_map import generate_review_map


def main():
    parser = argparse.ArgumentParser(
        description="Cycling Infrastructure Gap Analysis Tool"
    )
    parser.add_argument(
        "--region", default="Waterloo Region",
        help="Municipality or region name"
    )
    parser.add_argument(
        "--output", default="./output",
        help="Output directory for reports and maps"
    )
    parser.add_argument(
        "--master-plan", default=None,
        help="Path to GeoJSON AT Master Plan or TMP file (optional)"
    )
    parser.add_argument(
        "--infra-file", default=None,
        dest="infra_file",
        help=(
            "Path to a municipal cycling network GeoJSON or Shapefile. "
            "Replaces OSM cycling data as the primary infrastructure source. "
            "OSM road network is still fetched for routing and scoring."
        )
    )
    parser.add_argument(
        "--infra-field-map", default=None,
        dest="infra_field_map",
        help=(
            "Path to YAML field-map config for --infra-file. "
            "See data/field_maps/winnipeg.yaml for a worked example."
        )
    )
    parser.add_argument(
        "--no-osm", action="store_true", dest="no_osm",
        help=(
            "Skip all OSM data fetching. Requires --infra-file. "
            "Gap detection runs on the supplied file only; LTS, barrier, "
            "and destination scoring use neutral defaults."
        )
    )
    parser.add_argument(
        "--no-equity", action="store_true",
        help="Skip equity scoring (use if census data unavailable)"
    )
    parser.add_argument(
        "--spines", nargs="*", default=None,
        help="Manually specify spine corridor names (e.g. --spines 'Iron Horse Trail' 'King Street'). "
             "Defaults to auto-detection from network."
    )
    parser.add_argument(
        "--dedup-buffer", type=float, default=300.0,
        help="Spatial deduplication buffer in metres (default: 300)"
    )
    parser.add_argument(
        "--no-clean-topology", action="store_true", dest="no_clean_topology",
        help="Disable topology cleaning (degree-2 dissolve + endpoint-to-edge "
             "snap). Cleaning is ON by default and substantially reduces "
             "false-positive same-corridor / micro gaps."
    )
    parser.add_argument(
        "--edge-snap-m", type=float, default=18.0, dest="edge_snap_m",
        help="Endpoint-to-edge (T-junction) snap tolerance in metres "
             "(default: 18). Larger values connect more dead-ends but risk "
             "bridging genuinely separate parallel facilities."
    )
    parser.add_argument(
        "--junction-connectors", action="store_true", dest="junction_connectors",
        help="Enable the opt-in junction-connector detector (Method 6). Finds "
             "connector gaps between facility termini and nearby different "
             "facilities that the dangling/corridor detectors structurally "
             "miss. Off by default."
    )
    parser.add_argument(
        "--connector-max-m", type=float, default=500.0, dest="connector_max_m",
        help="Max connector distance for --junction-connectors (default: 500). "
             "Connectors up to --connector-quick-win-m are tagged 'quick_win'; "
             "longer ones up to this cap are 'network_link'."
    )
    parser.add_argument(
        "--connector-quick-win-m", type=float, default=150.0,
        dest="connector_quick_win_m",
        help="Upper bound (m) for a connector to be classed 'quick_win' "
             "(default: 150)."
    )
    parser.add_argument(
        "--connector-buildability-m", type=float, default=25.0,
        dest="connector_buildability_m",
        help="Max distance (m) the connector line may stray from a road/path "
             "and still be considered buildable (default: 25)."
    )
    parser.add_argument(
        "--dismissed", default=None,
        help="Path to a dismissed.json sidecar from a previous review session"
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Enable debug logging"
    )
    args = parser.parse_args()

    # ── Logging ───────────────────────────────────────────────────────────────
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger(__name__)

    logger.info(f"=== Cycling Gap Analysis Tool ===")
    logger.info(f"Region: {args.region}")
    logger.info(f"Output: {args.output}")

    # ── Validate flag combinations ────────────────────────────────────────────
    if args.no_osm and not args.infra_file:
        parser.error("--no-osm requires --infra-file. "
                     "There would be no infrastructure data to analyse.")

    os.makedirs(args.output, exist_ok=True)

    # ── Step 1: Determine data source and fetch data ──────────────────────────
    infra_source_info = None
    road_ways = []
    destinations = []

    if args.infra_file and args.no_osm:
        # ── Path A: Authoritative file only — no OSM fetching at all ─────────
        logger.info(
            f"Step 1/6: Loading cycling infrastructure from file (OSM disabled): "
            f"{args.infra_file}"
        )
        cycling_ways, infra_source_info = load_infra_file(
            filepath=args.infra_file,
            field_map_path=args.infra_field_map,
            region_name=args.region,
        )
        logger.info(
            f"  Loaded {infra_source_info.loaded_count} way segments "
            f"from {infra_source_info.source_name}"
        )
        logger.info(
            "  --no-osm: skipping road network fetch. LTS, barrier, and "
            "destination scores will use neutral defaults."
        )
        bbox = bbox_from_ways(cycling_ways)
        # No road_ways, no destinations — gap finders handle empty road_graph

    elif args.infra_file:
        # ── Path A: Authoritative municipal shapefile / GeoJSON ───────────────
        logger.info(
            f"Step 1/6: Loading cycling infrastructure from file: {args.infra_file}"
        )
        cycling_ways, infra_source_info = load_infra_file(
            filepath=args.infra_file,
            field_map_path=args.infra_field_map,
            region_name=args.region,
        )
        logger.info(
            f"  Loaded {infra_source_info.loaded_count} way segments "
            f"from {infra_source_info.source_name}"
        )

        # Derive bbox from the loaded geometry so road-network fetch covers
        # the same area without requiring a separate --bbox argument
        bbox = bbox_from_ways(cycling_ways)
        logger.info(
            f"  Bbox derived from infrastructure file: {bbox}"
        )
        logger.info(
            "Step 2/6: Fetching OSM road network (for routing, LTS, barriers)..."
        )
        road_data = fetch_road_network(bbox)
        destination_data = fetch_destinations(bbox)
        _, road_ways = extract_nodes_and_ways(road_data)
        destinations = _parse_destinations(destination_data)
        logger.info(f"  Destinations: {len(destinations)}")

        # road_attribute cycling graph not needed — shapefile is the authority
        cycling_graph_roads = None

    else:
        # ── Path C: OSM Overpass (default, no --infra-file) ───────────────────
        logger.info("Step 1/6: Fetching bounding box...")
        bbox = get_bbox_for_region(args.region)
        logger.info(f"  Bounding box: {bbox}")

        logger.info("Step 2/6: Fetching OSM data (this may take 30-60 seconds)...")
        cycling_data = fetch_cycling_network(bbox)
        road_data = fetch_road_network(bbox)
        destination_data = fetch_destinations(bbox)

        logger.info(
            f"  Cycling ways: "
            f"{sum(1 for e in cycling_data.get('elements', []) if e['type'] == 'way')}"
        )
        logger.info(
            f"  Road ways: "
            f"{sum(1 for e in road_data.get('elements', []) if e['type'] == 'way')}"
        )

        _, cycling_ways = extract_nodes_and_ways(cycling_data)
        _, road_ways = extract_nodes_and_ways(road_data)
        destinations = _parse_destinations(destination_data)
        logger.info(f"  Destinations: {len(destinations)}")
        cycling_graph_roads = None  # built below in Step 2

    # Load optional master plan
    master_plan_features = []
    if args.master_plan:
        master_plan_features = load_master_plan(args.master_plan)

    # ── Step 2: Build graphs ──────────────────────────────────────────────────
    logger.info("Step 3/6: Building network graphs...")
    cycling_graph_primary = build_cycling_graph(cycling_ways)

    if args.infra_file:
        # Shapefile/GeoJSON is the complete cycling authority — no road-attribute
        # supplementary graph needed. Merge step still runs for node snapping.
        import networkx as _nx
        cycling_graph = merge_cycling_graphs(cycling_graph_primary, _nx.Graph())
        logger.info(
            f"  Infrastructure file graph: {cycling_graph_primary.number_of_edges()} edges"
        )
    else:
        # OSM path: supplement with road-attribute cycling infrastructure
        cycling_graph_roads = build_cycling_graph_from_road_attributes(road_ways)
        cycling_graph = merge_cycling_graphs(cycling_graph_primary, cycling_graph_roads)
        logger.info(
            f"  Standalone cycling ways: {cycling_graph_primary.number_of_edges()} edges"
        )
        logger.info(
            f"  Road-attribute cycling infra: {cycling_graph_roads.number_of_edges()} edges"
        )

    # road_graph is used for routing, LTS, and barriers.
    # When --no-osm is set, road_ways is empty so road_graph is an empty graph —
    # gap finders handle this gracefully (no routing paths, no barriers detected,
    # LTS and destination scores default to neutral values).
    road_graph = build_road_graph(road_ways)
    logger.info(
        f"  Merged + snapped cycling graph: {cycling_graph.number_of_nodes()} nodes, "
        f"{cycling_graph.number_of_edges()} edges"
    )
    if args.no_osm:
        logger.info(
            f"  Road graph: empty (--no-osm). Scoring dimensions that require "
            f"road data (LTS, barriers, destinations) will use neutral defaults."
        )
    else:
        logger.info(f"  Road graph: {road_graph.number_of_nodes()} nodes")

    if cycling_graph.number_of_edges() == 0:
        logger.error(
            "No cycling infrastructure found in this region. "
            "Check the region name or expand the bounding box."
        )
        sys.exit(1)

    # ── Step 2b: Clean topology (false-positive control) ──────────────────────
    # Dissolve degree-2 pass-through nodes and snap dead-ends onto nearby edge
    # interiors (T-junction fragmentation). This collapses the raw-OSM
    # fragmentation that otherwise surfaces as same-corridor / micro-gaps before
    # the gap finders ever run. Toggle with --no-clean-topology.
    if not args.no_clean_topology:
        from core.network_clean import clean_topology
        logger.info("Cleaning network topology (degree-2 dissolve + endpoint snap)...")
        cycling_graph = clean_topology(
            cycling_graph,
            edge_snap_m=args.edge_snap_m,
        )
        logger.info(
            f"  Cleaned cycling graph: {cycling_graph.number_of_nodes()} nodes, "
            f"{cycling_graph.number_of_edges()} edges"
        )
    else:
        logger.info("Topology cleaning disabled (--no-clean-topology).")

    # ── Step 3b: Detect spines ───────────────────────────────────────────────────
    logger.info("Detecting cycling network spines...")
    spines = detect_spines(cycling_graph, road_graph, user_defined=args.spines)
    for spine in spines[:5]:
        logger.info(f"  Spine: {spine['name']} ({spine['source']}, {spine['total_length_m']:.0f}m)")

    # ── Step 3: Find gaps ─────────────────────────────────────────────────────
    logger.info("Step 4/6: Identifying gaps...")
    island_gaps   = find_island_gaps(cycling_graph, road_graph)
    detour_gaps   = find_detour_gaps(cycling_graph, road_graph)
    dangling_gaps = find_dangling_gaps(cycling_graph, road_graph)
    corridor_gaps = find_corridor_gaps(cycling_graph, road_graph)
    # Near-miss scan runs last so it can deduplicate against all previous results
    existing = island_gaps + detour_gaps + dangling_gaps + corridor_gaps
    near_miss_gaps = find_near_miss_gaps(cycling_graph, road_graph, existing_gaps=existing)
    all_gaps = existing + near_miss_gaps

    # Method 6 (opt-in): junction connectors a planner would draw by eye.
    if args.junction_connectors:
        connector_gaps = find_junction_connector_gaps(
            cycling_graph, road_graph,
            max_distance_m=args.connector_max_m,
            quick_win_max_m=args.connector_quick_win_m,
            buildability_tol_m=args.connector_buildability_m,
        )
        all_gaps = all_gaps + connector_gaps

    if not all_gaps:
        logger.warning("No gaps found. The cycling network may be well-connected, "
                       "or the region may have limited OSM data.")
        sys.exit(0)

    # Cross-method dedup: collapse the same physical break reported by more than
    # one detection method (e.g. island + dangling at the same endpoints).
    all_gaps = deduplicate_all_gaps(all_gaps, min_separation_m=args.dedup_buffer)

    # Roundabout suppressor: drop short gaps whose midpoint sits on a roundabout
    # (cyclist can continue around the loop; OSM just doesn't model the
    # cycling continuation through the junction).
    all_gaps = filter_roundabout_crossings(all_gaps, road_graph)

    # ── Step 3c: Holistic separation analysis ─────────────────────────────────
    # Compute each gap's separation ratio (current on-network path ÷ straight
    # line) and drop pairs that are effectively already connected. This is the
    # holistic "are these two groups genuinely separated?" filter, applied after
    # all per-method detection so it sees the complete candidate set.
    all_gaps = annotate_separation(all_gaps, cycling_graph)

    if not all_gaps:
        logger.warning("All candidate gaps were already-connected fragments. "
                       "Network appears well-connected after topology cleaning.")
        sys.exit(0)

    # Cross-reference with master plan
    all_gaps = match_master_plan(all_gaps, master_plan_features)
    all_gaps = assign_gap_ids(all_gaps)

    logger.info(
        f"  Island: {len(island_gaps)} | Detour: {len(detour_gaps)} | "
        f"Dangling: {len(dangling_gaps)} | Corridor: {len(corridor_gaps)} | "
        f"Near-miss: {len(near_miss_gaps)} | Total: {len(all_gaps)}"
    )

    # ── Step 4: Score gaps ────────────────────────────────────────────────────
    logger.info("Step 5/6: Scoring gaps...")

    # Equity data: placeholder — in v2 load StatsCan DA income quintiles
    equity_data = {} if args.no_equity else _load_equity_placeholder()

    network_baseline = compute_network_baseline(cycling_graph, all_gaps)
    scored_gaps = score_all_gaps(
        all_gaps, road_graph, destinations, equity_data, network_baseline, spines=spines
    )

    logger.info(f"  Top gap: {scored_gaps[0]['gap_id']} — score {scored_gaps[0]['composite_score']}")

    # ── Step 5: Generate outputs ──────────────────────────────────────────────
    logger.info("Step 6/6: Generating outputs...")

    region_slug = args.region.lower().replace(" ", "_")

    map_path = os.path.join(args.output, f"{region_slug}_gap_analysis.html")
    csv_path = os.path.join(args.output, f"{region_slug}_gaps.csv")
    geojson_path = os.path.join(args.output, f"{region_slug}_gaps.geojson")

    # Combine ways for map display
    if args.no_osm:
        # Road ways not fetched — only show the infrastructure file geometry
        display_ways = cycling_ways
        logger.info(
            f"  Data source: {infra_source_info.source_name} "
            f"[{infra_source_info.format}] — OSM road layer disabled"
        )
    elif args.infra_file:
        # Shapefile ways are the display source; also show OSM road-attribute
        # cycle lanes so any lanes tagged on roads appear on the map too.
        display_ways = cycling_ways + [
            w for w in road_ways
            if w.get("tags", {}).get("cycleway") in ("track", "lane")
        ]
        logger.info(
            f"  Data source: {infra_source_info.source_name} "
            f"[{infra_source_info.format}] + OSM roads"
        )
    else:
        display_ways = cycling_ways + [
            w for w in road_ways
            if w.get("tags", {}).get("cycleway") in ("track", "lane", "opposite_lane")
        ]
    generate_html_map(scored_gaps, display_ways, args.region, map_path, destinations=destinations, spines=spines)

    # Phase 2: Interactive review map
    review_path = os.path.join(args.output, f"{region_slug}_review.html")
    generate_review_map(
        scored_gaps, display_ways, args.region, review_path,
        dismissed_path=args.dismissed,
        destinations=destinations,
        spines=spines,
    )
    generate_csv_report(scored_gaps, csv_path)
    generate_geojson(scored_gaps, geojson_path)

    # ── Summary ───────────────────────────────────────────────────────────────
    high = sum(1 for g in scored_gaps if g["composite_score"] >= 66)
    medium = sum(1 for g in scored_gaps if 33 <= g["composite_score"] < 66)
    low = sum(1 for g in scored_gaps if g["composite_score"] < 33)
    barriers = sum(1 for g in scored_gaps if g["crosses_barrier"])
    plan_matches = sum(1 for g in scored_gaps if g["master_plan_match"])

    print("\n" + "=" * 60)
    print(f"  CYCLING GAP ANALYSIS COMPLETE — {args.region}")
    print("=" * 60)
    if infra_source_info:
        print(f"  Infrastructure source: {infra_source_info.source_name}")
        print(f"    File:    {infra_source_info.filepath}")
        print(f"    Loaded:  {infra_source_info.loaded_count} way segments "
              f"({infra_source_info.feature_count} raw features)")
        if args.no_osm:
            print(f"    Mode:    File-only (--no-osm). Road/LTS/destination "
                  f"scoring used neutral defaults.")
    else:
        print(f"  Infrastructure source: OpenStreetMap (Overpass API)")
    print(f"  Total gaps identified:      {len(scored_gaps)}")
    print(f"  Gap types:")
    print(f"    Island:    {sum(1 for g in scored_gaps if g['gap_type']=='island')}")
    print(f"    Detour:    {sum(1 for g in scored_gaps if g['gap_type']=='detour')}")
    print(f"    Dangling:  {sum(1 for g in scored_gaps if g['gap_type']=='dangling')}")
    print(f"    Corridor:  {sum(1 for g in scored_gaps if g['gap_type']=='corridor')}")
    print(f"    High priority  (≥66):     {high}")
    print(f"    Medium priority (33-65):  {medium}")
    print(f"    Low priority   (<33):     {low}")
    print(f"  Barrier crossings flagged:  {barriers}")
    print(f"  Cycling spines detected:    {len(spines)}")
    print(f"  Master plan matches:        {plan_matches}")
    print(f"\n  Top 5 Priority Gaps:")
    for g in scored_gaps[:5]:
        plan = " [TMP MATCH]" if g["master_plan_match"] else ""
        barrier = " [BARRIER]" if g["crosses_barrier"] else ""
        print(
            f"    #{g['rank']} {g['gap_id']} — {g['composite_score']}/100 — "
            f"{g['recommended_facility']} — {g['straight_line_m']:.0f}m{plan}{barrier}"
        )
    print(f"\n  Outputs:")
    print(f"    Map:     {map_path}")
    print(f"    Review:  {review_path}")
    print(f"    CSV:     {csv_path}")
    print(f"    GeoJSON: {geojson_path}")
    print("=" * 60)


def _parse_destinations(destination_data: dict) -> list:
    """Convert Overpass destination response to flat list of dicts."""
    destinations = []
    for element in destination_data.get("elements", []):
        tags = element.get("tags", {})
        dest_type = (
            tags.get("amenity") or
            tags.get("public_transport") or
            tags.get("railway") or
            tags.get("shop") or
            tags.get("landuse") or
            tags.get("leisure") or
            "unknown"
        )
        if element["type"] == "node":
            destinations.append({
                "lat": element["lat"],
                "lon": element["lon"],
                "type": dest_type,
                "name": tags.get("name", ""),
            })
        elif element["type"] == "way" and element.get("geometry"):
            # Use centroid of way
            coords = element["geometry"]
            if coords:
                lat = sum(p["lat"] for p in coords) / len(coords)
                lon = sum(p["lon"] for p in coords) / len(coords)
                destinations.append({
                    "lat": lat, "lon": lon,
                    "type": dest_type,
                    "name": tags.get("name", ""),
                })
    return destinations


def _load_equity_placeholder() -> dict:
    """
    Placeholder for StatsCan DA income quintile data.
    In v2: load from StatsCan open data API or preprocessed CSV.
    Returns empty dict — equity scoring will use neutral default (0.5).

    To add real equity data:
      - Download StatsCan DA boundary + median income data
      - Compute income quintiles across the region
      - Build dict: {(da_centroid_lat, da_centroid_lon): quintile_1_to_5}
    """
    return {}


if __name__ == "__main__":
    main()
