# Cycling Infrastructure Gap Analysis Tool

Identifies missing connections in municipal cycling networks and prioritises
them by connectivity, buildability, equity, and level-of-traffic-stress.

---

## Quick start

```bash
pip install -r requirements.txt

# OSM mode (no local data needed)
python main.py --region "Winnipeg" --output ./output

# Shapefile / GeoJSON mode (authoritative municipal data)
python main.py --region "Winnipeg" \
  --infra-file ./data/winnipeg_at_network.geojson \
  --infra-field-map ./data/field_maps/winnipeg.yaml \
  --output ./output
```

Outputs in `./output/`:
- `<region>_gap_analysis.html` — interactive priority map
- `<region>_review.html`       — gap review and dismissal tool
- `<region>_gaps.csv`          — tabular gap data
- `<region>_gaps.geojson`      — GeoJSON for GIS import

---

## Infrastructure data sources

### Option A — OpenStreetMap (default, no files needed)

Fetches cycling infrastructure from the Overpass API. Good for first runs
and regions without open municipal data. OSM quality varies by city.

### Option B — Municipal Shapefile or GeoJSON (recommended)

When a city publishes its Active Transportation network as open data, use
`--infra-file` to replace the OSM cycling layer with the authoritative source.
The OSM road network is still fetched for routing, LTS scoring, and barriers.

**Winnipeg Open Data:**
1. Download from https://data.winnipeg.ca → search "Active Transportation"
2. Export as GeoJSON (WGS84) or Shapefile
3. Run with `--infra-file` and `--infra-field-map ./data/field_maps/winnipeg.yaml`

**Other cities:**
1. Copy `data/field_maps/template.yaml` to `data/field_maps/yourcity.yaml`
2. Open your file in QGIS to identify attribute names and values
3. Fill in `facility_field`, `facility_map`, `status_field`, etc.

#### Internal facility types

| Type | Description | In gap analysis |
|------|-------------|-----------------|
| `protected_track` | Physically separated cycle track or MUP | Yes |
| `cycle_lane` | Painted on-road bike lane | Yes |
| `shared_path` | Off-road path shared with pedestrians | Yes |
| `shared_roadway` | Sharrows or advisory markings | No |
| `signed_route` | Wayfinding signs only | No |
| `unknown` | Unmapped or unclassified | No |

#### Supported file formats

| Format | Extension | Extra dependencies |
|--------|-----------|-------------------|
| GeoJSON | `.geojson`, `.json` | `pyyaml` |
| Shapefile | `.shp` | `pyyaml`, `fiona`, `pyproj` |

Install GIS extras: `pip install -r requirements-gis.txt`

---

## All command-line options

```
python main.py [options]

  Core
  --region TEXT             Municipality name (default: "Waterloo Region")
  --output DIR              Output directory (default: ./output)
  --infra-file PATH         Municipal cycling network GeoJSON or Shapefile
  --infra-field-map PATH    YAML field-map config for --infra-file
  --master-plan PATH        GeoJSON AT Master Plan for cross-referencing
  --spines NAME [...]       Override auto-detected spine corridor names
  --dismissed PATH          dismissed.json from a previous review session
  --no-osm                  Skip all OSM fetching. Requires --infra-file. LTS,
                            barrier, and destination scores use neutral defaults.
  --no-equity               Skip equity scoring
  --verbose                 Enable debug logging

  Topology cleaning (false-positive control; on by default)
  --no-clean-topology       Disable the degree-2 dissolve + endpoint-to-edge
                            snap pass. Cleaning is ON by default and is the main
                            reason a clean run shows far fewer "dangling" gaps.
  --edge-snap-m FLOAT       Endpoint-to-edge (T-junction) snap tolerance in
                            metres (default: 18). Larger values connect more
                            dead-ends but risk bridging separate parallel
                            facilities.

  Deduplication
  --dedup-buffer METRES     Spatial deduplication radius (default: 300)

  Junction connectors (Method 6 — opt-in)
  --junction-connectors     Enable the junction-connector detector. Finds
                            connector gaps between a facility terminus and a
                            nearby different facility that the dangling/corridor
                            detectors structurally miss. Off by default.
  --connector-max-m FLOAT   Max connector distance (default: 500). Connectors
                            up to --connector-quick-win-m are tagged
                            'quick_win'; longer ones up to this cap are
                            'network_link'.
  --connector-quick-win-m FLOAT
                            Upper bound (m) for the 'quick_win' class
                            (default: 150).
  --connector-buildability-m FLOAT
                            Max distance (m) the connector line may stray from a
                            road/path and still be considered buildable
                            (default: 25). Loosen if real L-shaped connections
                            are being rejected; tighten to cut noise.
```

### Junction connectors (catching what a planner would draw)

The five core detectors each have a strict trigger: the dangling finder needs
*both* ends to be degree-1 dead-ends, and the corridor finder needs the *same*
street name. That leaves a blind spot — a facility *terminus* sitting near a
*different* facility on a *different* street, where the obvious connecting link
is never proposed (e.g. the south end of a cycle lane and the cross-street lane
just beyond it).

`--junction-connectors` adds Method 6 to capture these. It uses broader anchors
(degree-1 dead-ends, nodes where the facility *type* changes, and facility
terminations) and pairs each with the nearest *different* component within
`--connector-max-m`. Two classes are produced so the prioritised list separates
small wins from larger projects:

| `connector_class` | Distance | Use |
|-------------------|----------|-----|
| `quick_win`    | ≤ `--connector-quick-win-m` (150 m) | Small, high-certainty connections |
| `network_link` | up to `--connector-max-m` (500 m)   | Substantial network-completing projects |

A **buildability gate** keeps recall high without noise: the connector line must
stay within `--connector-buildability-m` of a road for at least half its length
and must not cross a major barrier (motorway, trunk, rail, water). Connectors
flow through the same dedup, roundabout, wrong-endpoint, and scoring stages as
every other gap, so enabling the flag cannot reintroduce previously-fixed false
positives. The class appears as a `connector_class` column in the CSV and as a
Connector filter/badge in the review map.

```bash
# Enable connectors, widen the search, and loosen buildability for sprawl
python main.py --region "Kitchener" --junction-connectors \
  --connector-max-m 700 --connector-buildability-m 40 --output ./output
```


---

## Diagnostic tool

When gaps appear in unexpected locations:

```bash
# OSM mode
python diagnose.py --region "Winnipeg" \
  --bbox "49.845,49.875,-97.175,-97.135" --out diag.txt

# Shapefile mode
python diagnose.py --region "Winnipeg" \
  --infra-file ./data/winnipeg_at_network.geojson \
  --infra-field-map ./data/field_maps/winnipeg.yaml \
  --bbox "49.845,49.875,-97.175,-97.135" --out diag.txt
```

Bbox format: `min_lat,max_lat,min_lon,max_lon`. Add ±0.01° around the
gap dot's lat/lon from the review map.

---

## Building routes (Phase 3)

Once gaps are reviewed and the active set is exported from the review map,
`build_routes.py` turns each gap into a buildable alignment along the road
network, classifying every segment with an OTM Book 18 facility recommendation
and an LTS score.

```bash
# Comfortable low-stress alignment (default)
python build_routes.py --gaps ./output/kitchener_gaps_finalised.geojson \
  --region "Kitchener" --output ./output_routes

# Most direct physical alignment (LTS still scored per segment)
python build_routes.py --gaps ... --strategy shortest

# Build both and report the trade-off per gap
python build_routes.py --gaps ... --strategy compare
```

Three strategies (`--strategy`):

| Strategy | Path chosen by | Use |
|----------|---------------|-----|
| `lts` (default) | LTS-weighted cost (length × stress penalty) | The comfortable route a planner would recommend |
| `shortest` | Physical distance | The most direct alignment; LTS scored along it to expose stress problems |
| `compare` | Both | Annotates the low-stress route with how it differs from the direct one |

In `compare` mode each route gets a verdict summarising the trade-off:

- `direct_is_comfortable` — the shortest path is already LTS ≤2; just retrofit it
- `comfort_for_free` — the direct path is high-stress but a comfortable route exists at nearly the same length (best case)
- `detour_buys_comfort` — a comfortable route exists but needs a meaningful detour
- `no_comfortable_route` — even the low-stress search stays high-stress (corridor needs a road diet / cycle track / new ROW)

Each run writes (suffixed by strategy): `_routes[...].geojson` (per-segment
alignments), `_routes[...]_summary.csv` (one row per gap with metrics, plus
`compare_*` columns in compare mode), and `_routes[...]_map.html` (interactive
map). `build_routes_shortest.py` is retained as a thin wrapper equivalent to
`--strategy shortest` (or `--compare`).

---



| Type | Detection method |
|------|-----------------|
| `island` | Isolated component not connected to the main network |
| `dangling` | Dead-end with no nearby cross-component connection |
| `detour` | Two connected points whose cycling route is >2× straight-line |
| `corridor` | Missing segment on a road that otherwise has continuous cycling infra |
| `near_miss` | Two separate components within 300m not caught by other methods |
| `connector` | Facility terminus near a different facility/street (Method 6, opt-in via `--junction-connectors`); tagged `quick_win` or `network_link` |

---

## Priority scoring (0–100)

| Dimension | Weight | Description |
|-----------|--------|-------------|
| Connectivity | 35% | Trip-pairs enabled by closing the gap |
| Buildability | 25% | Road type, width, gradient, barriers |
| Destination | 20% | Proximity to schools, transit, healthcare, parks |
| LTS | 10% | Traffic stress of the gap corridor |
| Equity | 10% | Income quintile of surrounding census areas |

High ≥ 66 · Medium 33–65 · Low < 33

---

## Project structure

```
main.py                     Entry point
diagnose.py                 Diagnostic tool (per-gap decision tracer)
analyze_dismissals.py       Threshold-calibration from review dismissals
build_routes.py             Phase 3 — route alignment (--strategy lts|shortest|compare)
build_routes_shortest.py    Phase 3 — thin wrapper (= build_routes.py --strategy shortest)
requirements.txt            Core dependencies
requirements-gis.txt        Optional GIS dependencies

core/
  osm_fetcher.py            Overpass API queries
  infra_loader.py           Municipal shapefile / GeoJSON loader
  graph_builder.py          NetworkX graph construction
  network_clean.py          Topology cleaning (degree-2 dissolve, T-junction snap)
  gap_finder.py             Six gap detection methods + separation analysis
  street_labels.py          Nearest-street labelling helpers

scoring/
  priority.py               Composite priority scoring
  lts.py                    Level-of-traffic-stress calculation
  otm18.py                  OTM Book 18 facility recommendation
  spines.py                 Cycling spine detection

output/
  report.py                 HTML map, CSV, and GeoJSON generation
  review_map.py             Interactive review and dismissal tool

routing/
  route_builder.py          Unified route alignment (LTS / shortest / compare)

data/
  field_maps/
    winnipeg.yaml           Winnipeg Open Data field map
    kitchener.yaml          Kitchener field map
    template.yaml           Template for other municipalities
```

---

## Topology cleaning (false-positive control)

Raw OSM represents one physical corridor as many ways and many degree-2
vertices. Before gap detection, the pipeline cleans topology
(`core/network_clean.py`):

1. **Endpoint-to-edge snap** — degree-1 dead-ends within `--edge-snap-m` (18 m
   default) of another edge's interior are connected (T-junction fragmentation
   that node snapping misses).
2. **Degree-2 dissolve** — pass-through vertices are collapsed so a corridor
   becomes one edge between meaningful nodes; geometry and length are preserved,
   and facility-type transitions are kept as nodes.

Disable with `--no-clean-topology`. This stage is the main reason a clean run
shows far fewer "dangling" gaps than an uncleaned one.

## Holistic gap ranking

Each gap gets a `separation_ratio` = current on-network cycling path ÷
straight-line distance (∞ when the endpoints are in different components). Gaps
that are effectively already connected (short and low-ratio) are suppressed, and
the ratio feeds the connectivity score so genuinely-separated groups rank
highest. The ratio and `current_network_m` appear as columns in the gaps CSV.

## Calibrating from review decisions

The review map records a structured dismiss reason per gap and exports
`dismissed.json`. Feed it back to tune thresholds:

```bash
python analyze_dismissals.py --dismissed output/<region>_dismissed.json \
                             --gaps output/<region>_gaps.csv
```

It reports dismissals by reason, separates false positives from planning
decisions, and suggests concrete threshold values (e.g. `--edge-snap-m`,
`ALREADY_CONNECTED_RATIO`) that would have caught the dismissed false positives.
