"""Enforcement targeting - which 20 individual sources to send teams to first.

The previous enforcement output ranked *cells*: "cell 544, traffic, priority 69.8".
An inspector cannot act on a grid cell. This ranks the actual physical sources -
a named road, a specific industrial polygon, a construction site - using Parth's
source-attribution engine, which computes per-cell contributions for ~200 candidate
sources each across all 1,600 cells.

HOW PRIORITY IS BUILT
---------------------
A source earns priority three ways, and it needs more than one of them:

    IMPACT    how much it contributes, summed over every cell it reaches,
              weighted by how bad those cells already are
    SEVERITY  the worst AQI among the cells it affects
    REACH     how many cells it shows up in at all

Weighted 50/30/20. Impact dominates because a strong contribution to one very
polluted ward matters more than a faint contribution to twenty clean ones - but
reach is kept because a source touching many wards is a better use of one
inspection visit.

THE UNITS PROBLEM, AND WHY THIS RANKS WITHIN TYPE
-------------------------------------------------
Parth's engine is careful about something that is easy to get wrong: traffic sources
carry `contribution_score` derived from g PM2.5/hour, while industry and construction
carry `proxy_influence_score`, an unbounded presence index with **no mass units at
all**. Adding those together produces a number that looks authoritative and means
nothing.

So sources are scored **within their own type first** (percentile rank against
their peers), and only then compared across types. A "top 20" therefore reads as
"the most significant road, the most significant industrial site..." rather than
pretending a factory and a junction were measured on the same instrument.

Every row carries `evidence_basis` saying which it is.

HONEST LIMITS, carried through from the engine
----------------------------------------------
* Traffic emission factors are placeholders (`PLACEHOLDER_NOT_FOR_REPORTING`).
  Traffic rankings are RELATIVE, not reference-grade PM2.5 mass.
* Industry and construction have no emissions data at all - presence and proximity
  only.
* Confidence comes from the engine's wind reliability and sits at 0.30-0.45 here,
  which is low. It is reported, not hidden.

Usage:
    python scripts/build_enforcement_targets.py            # writes the CSV
    python scripts/build_enforcement_targets.py --top 20   # how many to emit
"""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

PARTH = (REPO / "Bind's Workspace" / "Parth's Work" / "source_attribution"
         / "airgrid" / "data" / "outputs")
ATTRIBUTION = PARTH / "airgrid_attribution.csv"
HOTSPOTS = PARTH / "source_hotspots.csv"          # ~654 MB - streamed, never loaded whole
WARD_MAP = REPO / "data" / "future_aqi_forecast_ward.csv"
OUT = REPO / "data" / "enforcement_targets_v2.csv"

RANKS = (1, 2, 3, 4, 5)

# What to actually do about each source type, and who does it. Deliberately concrete:
# "investigate" is not a dispatchable instruction.
ACTIONS = {
    "Traffic/Road": ("Traffic Enforcement Team",
                     "Deploy traffic marshals; enforce lane discipline and idling ban"),
    "Intersection": ("Traffic Enforcement Team",
                     "Signal-timing review and anti-idling enforcement at the junction"),
    "Industry": ("Industrial Compliance Team",
                 "Stack inspection and consent-to-operate verification"),
    "Construction/Dust": ("Dust Control Team",
                          "Verify dust screens, water sprinkling and covered material storage"),
    "Biomass/Open Burning": ("Rapid Response Team",
                             "Locate and extinguish; issue burning violation notice"),
}

# CPCB bands. A source feeding an already-Poor ward outranks one feeding a clean ward,
# so cells are weighted by the band they are in rather than counted equally.
def severity_weight(aqi: float) -> float:
    if aqi > 400: return 5.0
    if aqi > 300: return 4.0
    if aqi > 200: return 3.0
    if aqi > 100: return 2.0
    if aqi > 50:  return 1.2
    return 1.0


def log(m: str) -> None:
    print(m, flush=True)


def load_cell_impacts() -> pd.DataFrame:
    """Explode the per-cell top-5 into one row per (source, cell) pair."""
    att = pd.read_csv(ATTRIBUTION)
    frames = []
    for r in RANKS:
        sid, stype = f"rank{r}_source_id", f"rank{r}_source_type"
        contrib, proxy = f"rank{r}_contribution_score", f"rank{r}_proxy_influence_score"
        if sid not in att.columns:
            continue
        part = att[["cell_id", "forecast_aqi", "confidence", sid, stype,
                    contrib, proxy]].copy()
        part.columns = ["cell_id", "cell_aqi", "confidence",
                        "source_id", "source_type", "contribution", "proxy"]
        frames.append(part)
    long = pd.concat(frames, ignore_index=True).dropna(subset=["source_id"])

    # One score column. They are NOT interchangeable - `basis` records which, and
    # ranking never mixes them (see module docstring).
    long["score"] = long["contribution"].fillna(long["proxy"])
    long["basis"] = np.where(long["contribution"].notna(),
                             "modelled_pm25_contribution", "proxy_influence_index")
    long = long.dropna(subset=["score"])
    log(f"[1/4] {len(long):,} source-cell impacts across "
        f"{long.source_id.nunique():,} distinct sources")
    return long


def aggregate_sources(long: pd.DataFrame) -> pd.DataFrame:
    """Collapse to one row per source, with impact / severity / reach."""
    long = long.copy()
    long["weighted"] = long["score"] * long["cell_aqi"].map(severity_weight)

    agg = long.groupby(["source_id", "source_type", "basis"], as_index=False).agg(
        impact=("weighted", "sum"),
        raw_score=("score", "sum"),
        reach=("cell_id", "nunique"),
        peak_aqi=("cell_aqi", "max"),
        mean_aqi=("cell_aqi", "mean"),
        confidence=("confidence", "mean"),
    )

    # Rank WITHIN type, so a presence index is never compared against a mass rate.
    for col, name in (("impact", "impact_pct"), ("reach", "reach_pct")):
        agg[name] = agg.groupby("source_type")[col].rank(pct=True)
    agg["severity_pct"] = agg["peak_aqi"].rank(pct=True)      # AQI is comparable everywhere

    agg["priority"] = (0.50 * agg["impact_pct"]
                       + 0.30 * agg["severity_pct"]
                       + 0.20 * agg["reach_pct"]) * 100.0
    log(f"[2/4] aggregated to {len(agg):,} sources")
    return agg.sort_values("priority", ascending=False)


def attach_source_details(agg: pd.DataFrame, wanted: set[str]) -> pd.DataFrame:
    """Stream the 654 MB hotspots file, keeping only the sources we care about.

    Read in chunks deliberately: loading it whole would need several GB, and we need
    a few hundred rows out of millions.
    """
    if not HOTSPOTS.exists():
        log("[3/4] hotspots file missing - no coordinates or names available")
        return agg

    keep = []
    cols = ["source_id", "latitude", "longitude", "source_strength",
            "source_strength_unit", "source_strength_type", "metadata"]
    for chunk in pd.read_csv(HOTSPOTS, usecols=cols, chunksize=200_000):
        hit = chunk[chunk["source_id"].isin(wanted)]
        if not hit.empty:
            keep.append(hit)
    if not keep:
        log("[3/4] no matching sources found in hotspots")
        return agg

    det = pd.concat(keep, ignore_index=True).drop_duplicates("source_id")

    def _name(meta) -> str | None:
        try:
            d = ast.literal_eval(meta) if isinstance(meta, str) else None
            return (d or {}).get("name") or (d or {}).get("highway")
        except Exception:
            return None

    det["source_name"] = det["metadata"].map(_name)
    det["placeholder_factors"] = det["metadata"].astype(str).str.contains(
        "PLACEHOLDER_NOT_FOR_REPORTING")
    log(f"[3/4] matched {len(det):,} sources to coordinates")
    return agg.merge(det.drop(columns=["metadata"]), on="source_id", how="left")


def attach_wards(df: pd.DataFrame, long: pd.DataFrame) -> pd.DataFrame:
    """Name the ward a team should be sent to: the worst cell this source reaches."""
    if not WARD_MAP.exists():
        return df
    wmap = (pd.read_csv(WARD_MAP, usecols=["cell_id", "Ward_No", "Ward_Name"])
              .dropna(subset=["Ward_Name"]).drop_duplicates("cell_id"))
    worst = (long.sort_values("cell_aqi", ascending=False)
                 .drop_duplicates("source_id")[["source_id", "cell_id"]])
    worst = worst.merge(wmap, on="cell_id", how="left")
    return df.merge(worst[["source_id", "Ward_Name", "Ward_No"]],
                    on="source_id", how="left")


def build_evidence(r: pd.Series) -> str:
    bits = []
    if pd.notna(r.get("source_name")):
        bits.append(str(r["source_name"]))
    bits.append(f"affects {int(r['reach'])} cell(s)")
    bits.append(f"peak AQI {r['peak_aqi']:.0f}")
    if r["basis"] == "modelled_pm25_contribution":
        bits.append(f"modelled contribution {r['raw_score']:.2f}"
                    + (" (placeholder emission factors)"
                       if r.get("placeholder_factors") else ""))
    else:
        bits.append(f"proxy influence index {r['raw_score']:.1f} - presence, not emissions")
    bits.append(f"engine confidence {r['confidence']:.0%}")
    return "; ".join(bits)


# Adjacent segments of the same physical thing must not each become a dispatch. The
# first run produced four separate KARALA junctions and "Delhi-Gurugram Expressway"
# twice inside one top-20 - four vans to the same corner. Sources of a type are
# clustered onto a ~300 m grid and only the strongest survives, carrying a count of
# what it absorbed.
CLUSTER_DEG = 0.003          # ~330 m at Delhi's latitude
MAX_PER_WARD = 2             # keep the list spread across the city

# Balance the dispatch list across source types. Without this, junctions swamp it -
# there are 822,352 intersections against 342 industrial sites, so even after
# ranking within type the sheer count wins the tie-breaks and 14 of 20 slots go to
# junctions. A city sends different teams; the queue should reflect that.
MAX_PER_TYPE = 7


def deduplicate(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse co-located same-type sources, then cap how many land in one ward."""
    df = df.copy()
    if {"latitude", "longitude"}.issubset(df.columns):
        df["_gx"] = (df["latitude"] / CLUSTER_DEG).round()
        df["_gy"] = (df["longitude"] / CLUSTER_DEG).round()
        df = df.sort_values("priority", ascending=False)
        grouped = df.groupby(["source_type", "_gx", "_gy"], dropna=False)
        df["merged_sources"] = grouped["source_id"].transform("size")
        df = grouped.head(1).drop(columns=["_gx", "_gy"])
    else:
        df["merged_sources"] = 1

    # A source we cannot place in a ward cannot be dispatched to, so it is dropped
    # rather than shown as "nan".
    if "Ward_Name" in df.columns:
        df = df[df["Ward_Name"].notna()]
        df = df.sort_values("priority", ascending=False)
        # Spread across wards: one ward monopolising the queue is not a city-wide plan.
        df = df.groupby("Ward_Name", dropna=False).head(MAX_PER_WARD)

    df = df.sort_values("priority", ascending=False)
    df = df.groupby("source_type", dropna=False).head(MAX_PER_TYPE)
    return df.sort_values("priority", ascending=False)


def label(r: pd.Series) -> str:
    """A name a team can be sent to. Raw ids like INTERSECTION_476342 are useless."""
    name = r.get("source_name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    ward = r.get("Ward_Name")
    where = f" - {ward}" if isinstance(ward, str) and ward.strip() else ""
    kind = {"Intersection": "Road junction",
            "Industry": "Industrial site",
            "Construction/Dust": "Construction site",
            "Traffic/Road": "Road segment",
            "Biomass/Open Burning": "Open burning"}.get(r["source_type"], "Source")
    return f"{kind}{where}"


def main() -> None:
    ap = argparse.ArgumentParser(description="Rank sources for enforcement dispatch")
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()

    if not ATTRIBUTION.exists():
        raise SystemExit(f"Attribution output not found: {ATTRIBUTION}")

    long = load_cell_impacts()
    agg = aggregate_sources(long)

    # Detail-lookup only for the shortlist we might emit - a wide margin over --top
    # so ties and later filtering still have coordinates.
    shortlist = set(agg.head(max(args.top * 40, 800))["source_id"])
    agg = attach_source_details(agg, shortlist)
    agg = attach_wards(agg, long)

    agg = deduplicate(agg)
    top = agg.head(args.top).copy().reset_index(drop=True)
    top["source_name"] = top.apply(label, axis=1)
    top.insert(0, "rank", range(1, len(top) + 1))
    top["recommended_team"] = top["source_type"].map(lambda t: ACTIONS.get(t, ("Field Team", ""))[0])
    top["action"] = top["source_type"].map(lambda t: ACTIONS.get(t, ("", "Investigate"))[1])
    top["evidence"] = top.apply(build_evidence, axis=1)
    top["generated_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    cols = ["rank", "source_id", "source_type", "source_name", "Ward_Name", "Ward_No",
            "merged_sources",
            "latitude", "longitude", "priority", "impact", "reach", "peak_aqi",
            "mean_aqi", "confidence", "basis", "recommended_team", "action",
            "evidence", "generated_at"]
    top = top[[c for c in cols if c in top.columns]]
    top.to_csv(OUT, index=False)

    log(f"[4/4] wrote {OUT.name} - top {len(top)}")
    log("")
    log(f"{'#':>2}  {'TYPE':<18} {'WARD':<20} {'PRI':>5}  {'REACH':>5} {'PEAK':>5}  SOURCE")
    for _, r in top.iterrows():
        log(f"{int(r['rank']):>2}  {r['source_type']:<18} "
            f"{str(r.get('Ward_Name'))[:20]:<20} {r['priority']:>5.1f}  "
            f"{int(r['reach']):>5} {r['peak_aqi']:>5.0f}  "
            f"{str(r.get('source_name') or r['source_id'])[:34]}")
    log("")
    log("type mix in the top list: "
        + str(top["source_type"].value_counts().to_dict()))


if __name__ == "__main__":
    main()
