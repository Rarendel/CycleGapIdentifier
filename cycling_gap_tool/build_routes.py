"""
build_routes.py
Standalone Phase 3 script: build recommended cycling routes for finalised gaps.

Usage:
  python build_routes.py --gaps ./output/waterloo_region_gaps_finalised.geojson
                         --region "Waterloo Region"
                         --output ./output_routes

The gaps GeoJSON is the file exported from the review map (active gaps only).
The road network is re-fetched from OSM using the same bbox as the gap endpoints.

Outputs (written to --output directory):
  {region}_routes.geojson      — proposed route alignments
  {region}_routes_summary.csv  — one row per gap with route metrics
  {region}_routes_map.html     — interactive map of proposed routes
"""

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.osm_fetcher import fetch_road_network, extract_nodes_and_ways
from core.graph_builder import build_road_graph
from routing.route_builder import build_routes


def main():
    parser = argparse.ArgumentParser(
        description="Build recommended cycling routes for finalised gaps"
    )
    parser.add_argument(
        "--gaps", required=True,
        help="Path to finalised gaps GeoJSON (from review map export)"
    )
    parser.add_argument(
        "--region", default="Region",
        help="Region display name"
    )
    parser.add_argument(
        "--output", default="./output_routes",
        help="Output directory for route files"
    )
    parser.add_argument(
        "--strategy", choices=["lts", "shortest", "compare"], default="lts",
        help="Routing strategy: 'lts' (comfortable low-stress route, default), "
             "'shortest' (most direct physical alignment, LTS scored along it), "
             "or 'compare' (build both and annotate how the low-stress route "
             "differs from the direct one)."
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Enable debug logging"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger(__name__)

    # ── Load gaps GeoJSON ─────────────────────────────────────────────────────
    logger.info(f"Loading gaps from: {args.gaps}")
    if not os.path.exists(args.gaps):
        logger.error(f"Gaps file not found: {args.gaps}")
        sys.exit(1)

    with open(args.gaps) as f:
        gaps_geojson = json.load(f)

    gap_features = [
        f for f in gaps_geojson.get("features", [])
        if f.get("geometry", {}).get("type") == "Point"
    ]
    logger.info(f"Loaded {len(gap_features)} gap points")

    if not gap_features:
        logger.error("No Point features found in gaps GeoJSON. "
                     "Make sure to export from the review map (not the analysis map).")
        sys.exit(1)

    # ── Derive bounding box from gap coordinates ──────────────────────────────
    lats = [f["geometry"]["coordinates"][1] for f in gap_features]
    lons = [f["geometry"]["coordinates"][0] for f in gap_features]

    # Also include end coordinates from properties
    for f in gap_features:
        p = f["properties"]
        if p.get("end_lat"):
            lats.append(p["end_lat"])
        if p.get("end_lon"):
            lons.append(p["end_lon"])

    # Add 10% padding to bbox
    lat_pad = (max(lats) - min(lats)) * 0.1 + 0.01
    lon_pad = (max(lons) - min(lons)) * 0.1 + 0.01
    bbox = (
        min(lats) - lat_pad,
        min(lons) - lon_pad,
        max(lats) + lat_pad,
        max(lons) + lon_pad,
    )
    logger.info(f"Derived bbox: {tuple(round(x, 4) for x in bbox)}")

    # ── Fetch road network ────────────────────────────────────────────────────
    logger.info("Fetching road network from OSM (this may take 30-60 seconds)...")
    road_data = fetch_road_network(bbox)
    _, road_ways = extract_nodes_and_ways(road_data)
    road_graph = build_road_graph(road_ways)
    logger.info(f"Road graph: {road_graph.number_of_nodes()} nodes, "
                f"{road_graph.number_of_edges()} edges")

    if road_graph.number_of_edges() == 0:
        logger.error("Empty road graph — check OSM connectivity for this area.")
        sys.exit(1)

    # ── Build routes ──────────────────────────────────────────────────────────
    logger.info(f"Building routes (strategy={args.strategy})...")
    routes = build_routes(gaps_geojson, road_graph, args.output, args.region,
                          strategy=args.strategy)

    # ── Summary ───────────────────────────────────────────────────────────────
    found    = sum(1 for r in routes if r.found)
    fallback = sum(1 for r in routes if r.fallback)
    quality  = {"excellent": 0, "good": 0, "fair": 0, "poor": 0, "not_found": 0}
    for r in routes:
        quality[r.route_quality] = quality.get(r.route_quality, 0) + 1
    total_cost = sum(r.estimated_cost for r in routes if r.found)
    total_len  = sum(r.total_length_m for r in routes if r.found)

    region_slug = args.region.lower().replace(" ", "_")
    print("\n" + "=" * 60)
    print(f"  ROUTE BUILDING COMPLETE — {args.region}")
    print("=" * 60)
    print(f"  Gaps processed:           {len(routes)}")
    print(f"  Routes found:             {found}")
    print(f"  Fallback routes:          {fallback} (no low-stress path)")
    print(f"  Route quality:")
    print(f"    Excellent (LTS 1-2):    {quality['excellent']}")
    print(f"    Good:                   {quality['good']}")
    print(f"    Fair (some LTS 3):      {quality['fair']}")
    print(f"    Poor/Not found:         {quality['poor'] + quality['not_found']}")
    print(f"  Total route length:       {total_len/1000:.1f} km")
    print(f"  Estimated total cost:     ${total_cost:,.0f} CAD")
    print(f"\n  Outputs:")
    print(f"    Routes GeoJSON: {args.output}/{region_slug}_routes.geojson")
    print(f"    Summary CSV:    {args.output}/{region_slug}_routes_summary.csv")
    print(f"    Routes map:     {args.output}/{region_slug}_routes_map.html")
    print("=" * 60)


if __name__ == "__main__":
    main()
