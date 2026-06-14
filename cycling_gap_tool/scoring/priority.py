"""
priority.py
Composite priority scoring for cycling network gaps.

Weights (agreed in design session):
  Connectivity improvement         40%
  Gap length / buildability + type 20%
  Destination proximity            20%
  LTS context (Furth 2012)         15%
  Equity                            5%

Each criterion returns a 0-1 score. The composite is a weighted sum.
Final score is normalized to 0-100 for readability.

Output includes a per-criterion breakdown so engineers can understand
and defend the scoring in a client or council context.
"""

import math
import logging
from scoring.lts import score_gap_lts_context, lts_priority_contribution
from scoring.otm18 import recommend_facility, buildability_score
from scoring.spines import score_spine_proximity

logger = logging.getLogger(__name__)

WEIGHTS = {
    "connectivity":   0.40,
    "buildability":   0.20,
    "destination":    0.20,
    "lts":            0.15,
    "equity":         0.05,
}

# Destination category weights for proximity scoring
DESTINATION_WEIGHTS = {
    "school":             1.0,
    "university":         1.0,
    "college":            1.0,
    "station":            1.0,   # transit
    "stop_position":      0.7,
    "tram_stop":          0.8,
    "library":            0.6,
    "community_centre":   0.6,
    "hospital":           0.8,
    "clinic":             0.5,
    "supermarket":        0.5,
    "park":               0.4,
    "commercial":         0.4,
    "industrial":         0.3,
}

# Distance bands for proximity scoring (metres)
PROXIMITY_BAND_1 = 500   # high influence
PROXIMITY_BAND_2 = 1000  # moderate influence


def score_all_gaps(
    gaps: list,
    road_graph,
    destinations: list,
    equity_data: dict,
    network_baseline: dict,
    spines: list = None,
) -> list:
    """
    Score all gaps and return them sorted by priority score descending.

    Args:
      gaps: list of Gap objects
      road_graph: networkx Graph of road network
      destinations: list of destination dicts (from osm_fetcher)
      equity_data: dict mapping (lat, lon) grid cells to income quintile
      network_baseline: dict with baseline connectivity metrics

    Returns list of ScoredGap dicts sorted by composite_score descending.
    """
    scored = []

    for gap in gaps:
        result = score_gap(gap, road_graph, destinations, equity_data, network_baseline, spines or [])
        scored.append(result)

    scored.sort(key=lambda x: x["composite_score"], reverse=True)

    # Assign rank after sorting
    for i, s in enumerate(scored):
        s["rank"] = i + 1

    logger.info(f"Scored {len(scored)} gaps. Top gap: {scored[0]['gap_id'] if scored else 'none'}")
    return scored


def score_gap(
    gap,
    road_graph,
    destinations: list,
    equity_data: dict,
    network_baseline: dict,
    spines: list = None,
) -> dict:
    """Score a single gap across all criteria. Returns a scored gap dict."""

    # ── 1. LTS context ───────────────────────────────────────────────────────
    lts_ctx = score_gap_lts_context(
        gap.start_facility,
        gap.end_facility,
        gap.candidate_roads,
    )
    lts_score = lts_priority_contribution(lts_ctx)

    # ── 2. OTM Book 18 facility recommendation ───────────────────────────────
    recommendation = recommend_facility(gap, lts_ctx, gap.candidate_roads)

    # ── 3. Buildability (gap length + facility type) ──────────────────────────
    build_score = buildability_score(gap, recommendation)

    # ── 4. Destination proximity ──────────────────────────────────────────────
    dest_score = _score_destination_proximity(gap, destinations)

    # ── 5. Connectivity improvement (blended with spine proximity) ──────────────
    base_conn = _score_connectivity(gap, network_baseline)
    spine_score = score_spine_proximity(gap, spines or [])
    sep_score = _score_separation(gap)
    # Blend: connectivity signal is the gap-type base, the spine proximity, and
    # the holistic separation ratio. Separation gets real weight because it is
    # the most direct measure of "do these two groups actually fail to connect":
    #   55% gap-type base · 20% spine proximity · 25% separation ratio.
    conn_score = 0.55 * base_conn + 0.20 * spine_score + 0.25 * sep_score

    # ── 6. Equity ─────────────────────────────────────────────────────────────
    equity_score = _score_equity(gap, equity_data)

    # ── 7. Master plan alignment bonus ──────────────────────────────────────────
    plan_bonus = 0.05 if gap.master_plan_match else 0.0

    # ── 8. Continuity bonus ───────────────────────────────────────────────────
    # Corridor gaps (same street name on both sides) and dangling endpoint gaps
    # connecting same-facility-type endpoints get a significant bonus.
    # Rationale: filling a hole in an existing corridor is the highest-value
    # investment — it completes a route people are already using.
    continuity_bonus = 0.0
    if gap.gap_type == "corridor":
        continuity_bonus = 0.12  # same street, known gap
    elif gap.gap_type == "dangling":
        if (gap.start_facility == gap.end_facility and
                gap.start_facility in ("protected_track", "cycle_lane", "shared_path")):
            continuity_bonus = 0.08  # same facility type — likely same corridor
        else:
            continuity_bonus = 0.04
    elif gap.gap_type == "connector":
        # Junction connectors: a quick-win short link completes a near-connection
        # and earns a modest continuity bonus; longer network links earn less
        # (they are real projects, ranked mainly on connectivity/destination).
        if getattr(gap, "connector_class", None) == "quick_win":
            continuity_bonus = 0.06
        else:
            continuity_bonus = 0.02

    # ── Composite ─────────────────────────────────────────────────────────────
    raw = (
        WEIGHTS["connectivity"] * conn_score +
        WEIGHTS["buildability"] * build_score +
        WEIGHTS["destination"] * dest_score +
        WEIGHTS["lts"]         * lts_score +
        WEIGHTS["equity"]      * equity_score
    )
    composite = min(1.0, raw + plan_bonus + continuity_bonus)

    return {
        "gap_id": gap.gap_id,
        "gap_type": gap.gap_type,
        "connector_class": getattr(gap, "connector_class", None),
        "rank": None,  # assigned after sorting
        "composite_score": round(composite * 100, 1),
        "composite_raw": round(composite, 4),

        # Per-criterion breakdown
        "scores": {
            "connectivity": round(conn_score * 100, 1),
            "buildability": round(build_score * 100, 1),
            "destination":  round(dest_score * 100, 1),
            "lts":          round(lts_score * 100, 1),
            "equity":       round(equity_score * 100, 1),
        },

        # Gap geometry
        "start_lat": gap.start_coord[0] if gap.start_coord else None,
        "start_lon": gap.start_coord[1] if gap.start_coord else None,
        "end_lat":   gap.end_coord[0] if gap.end_coord else None,
        "end_lon":   gap.end_coord[1] if gap.end_coord else None,
        "straight_line_m": round(gap.straight_line_m, 0),
        "detour_factor": gap.detour_factor,

        # Facility recommendation
        "recommended_facility": recommendation["facility_short"],
        "recommended_facility_type": recommendation["facility_type"],
        "facility_description": recommendation["facility_description"],
        "lts_achievable": recommendation["lts_achievable"],
        "cost_tier": recommendation["cost_tier"],
        "otm18_basis": recommendation["otm18_basis"],

        # Context
        "start_facility": gap.start_facility,
        "end_facility":   gap.end_facility,
        "crosses_barrier": gap.crosses_barrier,
        "master_plan_match": gap.master_plan_match,
        # Holistic separation signal
        "separation_ratio": (
            None if getattr(gap, "separation_ratio", None) is None
            else ("inf" if gap.separation_ratio == float("inf")
                  else round(gap.separation_ratio, 2))
        ),
        "current_network_m": (
            round(gap.current_network_m, 0)
            if getattr(gap, "current_network_m", None) is not None else None
        ),
        "separation_score": round(_score_separation(gap) * 100, 1),
        "plan_bonus_applied": plan_bonus > 0,
        "continuity_bonus_applied": continuity_bonus > 0,
        "continuity_bonus": round(continuity_bonus, 3),

        # LTS detail
        "lts_context": lts_ctx["gap_lts_context"],
        "corridor_lts": lts_ctx.get("corridor_lts"),

        # Street labels
        "from_street": getattr(gap, "from_street", None),
        "to_street": getattr(gap, "to_street", None),

        # Notes (all warnings, caveats, data quality flags)
        "notes": (
            lts_ctx.get("notes", []) +
            recommendation.get("notes", [])
        ),

        # Raw gap data for GeoJSON export
        "candidate_roads": gap.candidate_roads,

        # Spine context
        "spine_proximity_score": round(spine_score, 3),
        "connects_to_spine": spine_score >= 0.7,

        # Human-readable label (street keys already set above)
        "gap_label": (getattr(gap, "from_street", "") or "Unknown") + " to " + (getattr(gap, "to_street", "") or "Unknown"),
    }


# ─── Criterion scoring functions ──────────────────────────────────────────────

def _score_separation(gap) -> float:
    """
    Score how separated the two groups of links are on the EXISTING network
    (0-1). This is the holistic "visually obvious gap" signal.

    separation_ratio = current best on-network path ÷ straight-line distance.
      - inf  (different components, no path)      → 1.0  (maximally separated)
      - >= 4 (long detour to get between them)    → ~1.0
      - 2.0  (moderate detour)                    → ~0.33
      - <= 1.0 (basically adjacent already)       → 0.0

    A gap with a high separation ratio heals a genuine network break and should
    rank above a gap whose endpoints are already nearly connected.
    """
    ratio = getattr(gap, "separation_ratio", None)
    if ratio is None:
        return 0.5  # not computed — neutral
    if ratio == float("inf"):
        return 1.0
    return max(0.0, min(1.0, (ratio - 1.0) / 3.0))


def _score_connectivity(gap, network_baseline: dict) -> float:
    """
    Score the connectivity improvement from filling this gap (0-1).

    For island gaps: score by the size of the isolated component
    (larger island = more connectivity gained by connecting it).

    For detour gaps: score by the detour factor
    (higher detour = more benefit from filling the gap).

    Normalized against network baseline values.
    """
    if gap.gap_type == "island":
        # Normalize component size: 50 nodes = perfect score
        component_size = gap.component_size or 1
        max_size = network_baseline.get("max_island_size", 50)
        return min(1.0, component_size / max_size)

    elif gap.gap_type == "detour":
        # Normalize detour factor: 5.0+ = perfect score
        df = gap.detour_factor or 2.0
        return min(1.0, (df - 2.0) / 3.0)  # scale 2.0 (min) to 5.0 (max)

    elif gap.gap_type in ("dangling", "corridor", "connector"):
        # Dangling, corridor, near-miss, and connector gaps join two separate
        # components but have no component-size or detour-factor signal.  Their
        # connectivity value scales inversely with the physical gap length: a
        # 30 m break between two real facilities is a near-certain, high-value
        # connection, while a 600 m break needs substantial new build and is
        # less of a pure connectivity win.
        #
        # Previously these gap types fell through to a flat 0.5, which meant the
        # 40%-weighted connectivity criterion contributed an identical constant
        # for most detected gaps — effectively disabling the most important
        # scoring dimension for them.
        gap_len = gap.straight_line_m or 0.0
        max_gap = network_baseline.get("max_short_gap_m", 600.0)
        proximity = max(0.0, 1.0 - (gap_len / max_gap))  # 1.0 at 0 m → 0.0 at max_gap
        # Floor of 0.35 so even a long-but-real connection still registers as
        # meaningful connectivity, and a small "continuity" premium for in-corridor
        # gaps which by definition complete a route already in use.
        base = 0.35 + 0.65 * proximity
        if gap.gap_type == "corridor":
            base = min(1.0, base + 0.10)
        return round(min(1.0, base), 4)

    return 0.5


def _score_destination_proximity(gap, destinations: list) -> float:
    """
    Score based on density and importance of destinations within
    500m and 1000m of the gap midpoint.

    Uses weighted destination count with distance decay.
    """
    if not destinations or not gap.start_coord or not gap.end_coord:
        return 0.0

    mid_lat = (gap.start_coord[0] + gap.end_coord[0]) / 2
    mid_lon = (gap.start_coord[1] + gap.end_coord[1]) / 2

    score = 0.0
    max_possible = 5.0  # normalization ceiling

    for dest in destinations:
        dest_lat = dest.get("lat")
        dest_lon = dest.get("lon")
        if dest_lat is None or dest_lon is None:
            continue

        dist = _haversine_m(mid_lon, mid_lat, dest_lon, dest_lat)

        if dist > PROXIMITY_BAND_2:
            continue

        # Destination type weight
        dest_type = dest.get("type", "unknown")
        weight = DESTINATION_WEIGHTS.get(dest_type, 0.3)

        # Distance decay: full weight within band 1, half weight in band 2
        if dist <= PROXIMITY_BAND_1:
            score += weight
        else:
            score += weight * 0.5

    return min(1.0, score / max_possible)


def _score_equity(gap, equity_data: dict) -> float:
    """
    Score based on whether the gap is in a low-income or underserved area.
    Uses StatsCan Dissemination Area (DA) income quintile data.

    equity_data: dict mapping DA identifier to income quintile (1=lowest, 5=highest)
    Lower income quintile = higher equity priority score.

    If equity data is unavailable, returns 0.5 (neutral) with a flag.
    This ensures the tool degrades gracefully without census data.
    """
    if not equity_data or not gap.start_coord:
        return 0.5  # neutral default — no penalty for missing data

    mid_lat = (gap.start_coord[0] + gap.end_coord[0]) / 2
    mid_lon = (gap.start_coord[1] + gap.end_coord[1]) / 2

    # Find nearest DA centroid
    best_quintile = 3  # default to middle
    best_dist = float("inf")

    for (da_lat, da_lon), quintile in equity_data.items():
        dist = _haversine_m(mid_lon, mid_lat, da_lon, da_lat)
        if dist < best_dist:
            best_dist = dist
            best_quintile = quintile

    # Invert: quintile 1 (lowest income) → score 1.0, quintile 5 → score 0.2
    return max(0.1, (6 - best_quintile) / 5.0)


def _haversine_m(lon1, lat1, lon2, lat2) -> float:
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def compute_network_baseline(cycling_graph, gaps: list) -> dict:
    """
    Compute baseline metrics for normalizing connectivity scores.
    Run once before scoring all gaps.
    """
    island_sizes = [g.component_size for g in gaps if g.gap_type == "island"]
    detour_factors = [g.detour_factor for g in gaps if g.gap_type == "detour" and g.detour_factor]
    short_gap_lengths = [
        g.straight_line_m for g in gaps
        if g.gap_type in ("dangling", "corridor", "connector") and g.straight_line_m
    ]

    return {
        "max_island_size": max(island_sizes) if island_sizes else 50,
        "max_detour_factor": max(detour_factors) if detour_factors else 5.0,
        "mean_detour_factor": (
            sum(detour_factors) / len(detour_factors) if detour_factors else 2.5
        ),
        # Normalizer for the dangling/corridor connectivity branch.  Falls back
        # to 600 m (the MAX_GAP_STRAIGHT_LINE_M default in gap_finder) so the
        # score is stable even when no short gaps are present in this run.
        "max_short_gap_m": max(short_gap_lengths) if short_gap_lengths else 600.0,
        "total_gaps": len(gaps),
    }
