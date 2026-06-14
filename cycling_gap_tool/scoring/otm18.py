"""
otm18.py
Cycling facility recommendations based on Ontario Traffic Manual Book 18
(Cycling Facilities, MTO).

OTM Book 18 defines facility selection based primarily on:
  - Operating speed (posted speed or 85th percentile)
  - Volume (AADT) — proxy used where counts unavailable
  - Roadway context (urban/rural, arterial/collector/local)

Key OTM Book 18 facility hierarchy (Section 3):
  ┌─────────────────────────────────────────────────────────────┐
  │ Speed ≤ 30 km/h, low volume → Shared roadway / bike route  │
  │ Speed 40-50 km/h, <3000 AADT → Painted cycle lane          │
  │ Speed 40-50 km/h, 3000-8000 AADT → Protected cycle lane    │
  │ Speed 50-60 km/h, any volume → Cycle track / protected lane │
  │ Speed > 60 km/h OR >8000 AADT → Grade-separated path       │
  │ Barrier crossing → Grade separation + signalized crossing   │
  └─────────────────────────────────────────────────────────────┘

Additionally, the gap endpoint context is used to determine what facility
type is needed to match or improve the existing network (from gap_finder.py).

Reference: OTM Book 18, 2nd Ed., Ministry of Transportation Ontario
Section 3: Facility Selection; Table 3-1 and Figure 3-1
"""

import logging

logger = logging.getLogger(__name__)

# AADT proxy midpoint values for threshold comparison
# (from graph_builder._aadt_proxy categories)
AADT_PROXY_MIDPOINTS = {
    ">40000":       40000,
    "20000-40000":  30000,
    "10000-25000":  17500,
    "5000-15000":   10000,
    "1000-5000":     3000,
    "500-2000":      1250,
    "<1000":          500,
    "<200":           100,
    "unknown":       2500,  # conservative default
}

FACILITY_DESCRIPTIONS = {
    "grade_separated_path": {
        "short": "Multi-use path (grade separated)",
        "description": (
            "Off-road multi-use path physically separated from motor traffic. "
            "Required where speeds exceed 60 km/h or AADT > 8,000. "
            "Barrier crossings require dedicated grade separation structure or "
            "signalized at-grade crossing. "
            "OTM Book 18 Section 3.4 / Figure 3-1."
        ),
        "lts_achievable": 1,
        "cost_tier": "high",
    },
    "protected_cycle_track": {
        "short": "Protected cycle track (physically separated)",
        "description": (
            "Bidirectional or one-way cycle track with physical separation "
            "(flexible delineators, raised surface, planter, or curb). "
            "Recommended for speeds 50-60 km/h or AADT 3,000-8,000. "
            "OTM Book 18 Section 3.3.3."
        ),
        "lts_achievable": 1,
        "cost_tier": "medium-high",
    },
    "painted_cycle_lane": {
        "short": "Painted cycle lane",
        "description": (
            "Dedicated lane marked with paint and signage, no physical barrier. "
            "Suitable for speeds 40-50 km/h and AADT < 3,000. "
            "Buffer striping recommended where right-of-way permits. "
            "OTM Book 18 Section 3.3.2."
        ),
        "lts_achievable": 2,
        "cost_tier": "low",
    },
    "shared_roadway": {
        "short": "Signed shared roadway / neighbourhood greenway",
        "description": (
            "Shared use of roadway with wayfinding signage and pavement markings. "
            "Suitable for speeds ≤ 30 km/h and AADT < 1,000. "
            "Traffic calming measures recommended to maintain low-stress environment. "
            "OTM Book 18 Section 3.3.1."
        ),
        "lts_achievable": 2,
        "cost_tier": "low",
    },
    "barrier_crossing": {
        "short": "Barrier crossing — engineering review required",
        "description": (
            "Gap crosses a major barrier (highway, railway, waterway). "
            "Requires site-specific engineering assessment. Options include: "
            "grade separation (underpass/overpass), signalized at-grade crossing, "
            "or realignment to existing crossing point. "
            "Cost and feasibility highly site-specific. "
            "Flag for detailed study before priority scoring."
        ),
        "lts_achievable": 1,
        "cost_tier": "very high",
    },
}


def recommend_facility(
    gap,
    lts_context: dict,
    candidate_roads: list,
) -> dict:
    """
    Recommend a cycling facility type for a gap based on OTM Book 18.

    Inputs:
      gap: Gap object (from gap_finder.py)
      lts_context: dict from lts.score_gap_lts_context()
      candidate_roads: list of road dicts from gap_finder._find_candidate_roads()

    Returns dict:
      facility_type: str key from FACILITY_DESCRIPTIONS
      facility_short: str display name
      facility_description: str full description
      lts_achievable: int
      cost_tier: str
      otm18_basis: str (which table/section drove the decision)
      endpoint_context: str (what the gap needs to match)
      notes: list of str
    """
    notes = []

    # Barrier crossing overrides everything
    if gap.crosses_barrier:
        return _build_result("barrier_crossing", notes + [
            "Barrier crossing detected — facility type requires site-specific study."
        ], otm18_basis="Barrier crossing — OTM Book 18 Section 3.5")

    # Get best candidate road attributes
    road = candidate_roads[0] if candidate_roads else {}
    speed = road.get("maxspeed", 0) or 50  # default 50 if unknown
    aadt_proxy = road.get("aadt_proxy", "unknown")
    aadt = AADT_PROXY_MIDPOINTS.get(aadt_proxy, 2500)
    highway = road.get("highway", "unclassified")
    aadt_is_proxy = road.get("aadt_is_proxy", True)

    if aadt_is_proxy:
        notes.append(
            f"AADT estimated from road class '{highway}' ({aadt_proxy}). "
            "Verify with municipal traffic model. Facility recommendation may change."
        )

    # OTM Book 18 facility selection logic
    if speed > 60 or aadt > 8000:
        facility = "grade_separated_path"
        basis = "OTM Book 18 Fig 3-1: Speed >60 km/h or AADT >8,000 → grade-separated path"

    elif speed >= 50 or aadt >= 3000:
        facility = "protected_cycle_track"
        basis = "OTM Book 18 Fig 3-1: Speed 50-60 km/h or AADT 3,000-8,000 → protected cycle track"

    elif speed >= 40 or aadt >= 1000:
        # Additional check: does endpoint context require upgrading?
        endpoint_needs_protection = _endpoint_requires_protection(
            gap.start_facility, gap.end_facility
        )
        if endpoint_needs_protection:
            facility = "protected_cycle_track"
            basis = (
                "OTM Book 18 Fig 3-1: Speed 40-50 km/h + endpoint context "
                "(connecting to protected track) → protected cycle track recommended"
            )
            notes.append(
                "Endpoint facility context: connecting to protected track. "
                "Protected facility recommended to maintain network comfort level."
            )
        else:
            facility = "painted_cycle_lane"
            basis = "OTM Book 18 Fig 3-1: Speed 40-50 km/h, AADT <3,000 → painted cycle lane"

    else:
        facility = "shared_roadway"
        basis = "OTM Book 18 Fig 3-1: Speed ≤30 km/h, low volume → shared roadway"

    # Endpoint context note
    endpoint_note = _endpoint_context_note(gap.start_facility, gap.end_facility)
    if endpoint_note:
        notes.append(endpoint_note)

    return _build_result(facility, notes, otm18_basis=basis)


def _build_result(facility_key: str, notes: list, otm18_basis: str = "") -> dict:
    info = FACILITY_DESCRIPTIONS.get(facility_key, {})
    return {
        "facility_type": facility_key,
        "facility_short": info.get("short", facility_key),
        "facility_description": info.get("description", ""),
        "lts_achievable": info.get("lts_achievable", 2),
        "cost_tier": info.get("cost_tier", "unknown"),
        "otm18_basis": otm18_basis,
        "notes": notes,
    }


def _endpoint_requires_protection(start_facility: str, end_facility: str) -> bool:
    """
    Returns True if either endpoint is a protected facility,
    meaning the gap fill should also be protected to maintain network comfort.
    """
    protected = {"protected_track", "shared_path"}
    return start_facility in protected or end_facility in protected


def _endpoint_context_note(start_facility: str, end_facility: str) -> str:
    """Generate a human-readable note about the endpoint infrastructure context."""
    if start_facility == end_facility and start_facility not in (None, "unknown"):
        return f"Gap connects two '{start_facility}' segments — facility type should match."
    if start_facility and end_facility and start_facility != end_facility:
        return (
            f"Gap endpoints differ: '{start_facility}' → '{end_facility}'. "
            "Facility transition zone recommended at junction points."
        )
    return ""


def buildability_score(gap, recommendation: dict) -> float:
    """
    Score the buildability of a gap (0-1, higher = easier to build).
    Contributes to the gap length/buildability/type criterion (20% of composite).

    Factors:
      - Gap length (shorter = more buildable)
      - Facility type complexity (shared roadway easiest, grade separation hardest)
      - Barrier crossing penalty
      - Existing ROW (inferred from candidate road presence)

    Returns a 0-1 score.
    """
    score = 1.0

    # Length penalty: normalize against 800m max gap
    length_factor = max(0, 1 - (gap.straight_line_m / 800))
    score *= 0.5 + 0.5 * length_factor  # length accounts for half of buildability

    # Facility type complexity
    complexity = {
        "shared_roadway":        1.0,
        "painted_cycle_lane":    0.85,
        "protected_cycle_track": 0.55,
        "grade_separated_path":  0.25,
        "barrier_crossing":      0.10,
    }
    facility = recommendation.get("facility_type", "painted_cycle_lane")
    score *= complexity.get(facility, 0.5)

    # Barrier penalty
    if gap.crosses_barrier:
        score *= 0.3

    # Candidate road bonus: if gap follows an existing road, ROW likely exists
    if gap.candidate_roads:
        score *= 1.15  # small bonus for having a road corridor to build within

    return min(1.0, max(0.0, score))
