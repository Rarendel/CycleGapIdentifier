"""
build_routes_shortest.py
Phase 3 (Variant B): shortest-physical-path routing.

DEPRECATED AS A SEPARATE IMPLEMENTATION. The routing logic now lives in one
place — routing/route_builder.build_routes(..., strategy=...). This script is
retained as a thin, backward-compatible wrapper so existing commands keep
working; it simply calls the unified builder with strategy="shortest".

For new work prefer:
    python build_routes.py --gaps ... --strategy shortest
    python build_routes.py --gaps ... --strategy compare   # both, side by side

Shortest-path routing finds the most direct physical alignment between gap
endpoints regardless of Level of Traffic Stress, while still scoring LTS on each
segment. Use it to spot corridors where the direct route is already low-stress
(easy retrofits) versus those where it is high-stress (road diet / cycle track /
new ROW needed). The 'compare' strategy reports that trade-off automatically.

Usage:
  python build_routes_shortest.py
    --gaps ./output/waterloo_region_gaps_finalised.geojson
    --region "Waterloo Region"
    --output ./output_routes_shortest
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
        description="Build shortest-physical-path cycling routes for finalised gaps "
                    "(wrapper around build_routes.py --strategy shortest)"
    )
    parser.add_argument("--gaps", required=True,
                        help="Path to finalised gaps GeoJSON (from review map export)")
    parser.add_argument("--region", default="Region", help="Region display name")
    parser.add_argument("--output", default="./output_routes_shortest",
                        help="Output directory for route files")
    parser.add_argument("--compare", action="store_true",
                        help="Run the 'compare' strategy instead — build both the "
                             "shortest and low-stress routes and annotate the "
                             "trade-off between them.")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger(__name__)

    if not os.path.exists(args.gaps):
        logger.error(f"Gaps file not found: {args.gaps}")
        sys.exit(1)

    with open(args.gaps) as f:
        gaps_geojson = json.load(f)

    gap_features = [
        f for f in gaps_geojson.get("features", [])
        if f.get("geometry", {}).get("type") == "Point"
    ]
    if not gap_features:
        logger.error("No Point features found in gaps GeoJSON. "
                     "Export from the review map (not the analysis map).")
        sys.exit(1)
    logger.info(f"Loaded {len(gap_features)} gap points")

    # Derive bbox from gap endpoints (same logic as build_routes.py).
    lats = [f["geometry"]["coordinates"][1] for f in gap_features]
    lons = [f["geometry"]["coordinates"][0] for f in gap_features]
    for f in gap_features:
        p = f["properties"]
        if p.get("end_lat"):
            lats.append(p["end_lat"])
        if p.get("end_lon"):
            lons.append(p["end_lon"])
    lat_pad = (max(lats) - min(lats)) * 0.1 + 0.01
    lon_pad = (max(lons) - min(lons)) * 0.1 + 0.01
    bbox = (min(lats) - lat_pad, min(lons) - lon_pad,
            max(lats) + lat_pad, max(lons) + lon_pad)
    logger.info(f"Derived bbox: {tuple(round(x, 4) for x in bbox)}")

    logger.info("Fetching road network from OSM (this may take 30-60 seconds)...")
    road_data = fetch_road_network(bbox)
    _, road_ways = extract_nodes_and_ways(road_data)
    road_graph = build_road_graph(road_ways)
    logger.info(f"Road graph: {road_graph.number_of_nodes()} nodes, "
                f"{road_graph.number_of_edges()} edges")
    if road_graph.number_of_edges() == 0:
        logger.error("Empty road graph — check OSM connectivity for this area.")
        sys.exit(1)

    strategy = "compare" if args.compare else "shortest"
    routes = build_routes(gaps_geojson, road_graph, args.output, args.region,
                          strategy=strategy)

    found = sum(1 for r in routes if r.found)
    print("\n" + "=" * 60)
    print(f"  ROUTE BUILDING COMPLETE ({strategy}) - {args.region}")
    print("=" * 60)
    print(f"  Gaps processed: {len(routes)}")
    print(f"  Routes found:   {found}")
    print(f"  Outputs in:     {args.output}")
    print("=" * 60)


if __name__ == "__main__":
    main()
