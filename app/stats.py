"""Statistical analyses of stakeholder (KOL) engagement.

All functions are pure: they take plain lists of dicts (as returned from the DB)
so they are easy to test. Every inferential test reports its sample size and
returns ``None`` for the statistic when assumptions/minimum sizes are not met,
rather than producing a misleading number.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import date, datetime

import numpy as np
from scipy import stats as sps

# Relative weight of an interaction type when scoring engagement depth.
TYPE_WEIGHTS = {
    "advisory_board": 3.0,
    "investigator_meeting": 2.5,
    "one_to_one": 2.0,
    "speaker_program": 1.5,
    "congress": 1.0,
    "medical_info_request": 1.0,
}
RECENCY_HALF_LIFE_DAYS = 90.0
ALPHA = 0.05


def _d(value) -> date:
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value)[:10]).date()


def _round(x, n=3):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    return round(float(x), n)


def _p(p):
    """Report p-values to 3 significant figures (never rounds a tiny p to 0)."""
    if p is None or math.isnan(p):
        return None
    return float(f"{p:.3g}")


def mean_ci(values: list[float], confidence: float = 0.95) -> dict:
    """Mean with a t-distribution confidence interval."""
    vals = [v for v in values if v is not None]
    n = len(vals)
    if n == 0:
        return {"n": 0, "mean": None, "ci_low": None, "ci_high": None}
    m = float(np.mean(vals))
    if n < 2:
        return {"n": n, "mean": _round(m), "ci_low": None, "ci_high": None}
    sem = sps.sem(vals)
    if sem == 0:
        return {"n": n, "mean": _round(m), "ci_low": _round(m), "ci_high": _round(m)}
    lo, hi = sps.t.interval(confidence, n - 1, loc=m, scale=sem)
    return {"n": n, "mean": _round(m), "ci_low": _round(lo), "ci_high": _round(hi)}


def influence_score(kol: dict) -> float:
    """0-100 proxy for scientific influence from publications, trials and tier.

    Log-scaled so a handful of prolific authors don't dominate. This is a
    heuristic, not a validated index - adjust weights to your organisation.
    """
    pubs = math.log1p(kol.get("publications") or 0) / math.log1p(300)
    trials = math.log1p(kol.get("trials_involved") or 0) / math.log1p(40)
    tier = {1: 1.0, 2: 0.6, 3: 0.3}.get(kol.get("tier") or 2, 0.6)
    return round(100 * min(1.0, 0.45 * pubs + 0.30 * trials + 0.25 * tier), 1)


def kol_engagement(kols: list[dict], interactions: list[dict], insights: list[dict],
                   today: date | None = None) -> list[dict]:
    """Per-KOL engagement metrics with a recency-decayed, type-weighted score."""
    today = today or date.today()
    by_kol: dict[int, list[dict]] = defaultdict(list)
    for it in interactions:
        by_kol[it["kol_id"]].append(it)
    insight_counts = Counter(i["kol_id"] for i in insights if i.get("kol_id"))

    raw = {}
    out = []
    for k in kols:
        its = by_kol.get(k["id"], [])
        score = 0.0
        for it in its:
            age = max(0, (today - _d(it["date"])).days)
            decay = 0.5 ** (age / RECENCY_HALF_LIFE_DAYS)
            score += TYPE_WEIGHTS.get(it["type"], 1.0) * decay
        raw[k["id"]] = score
        last = max((_d(it["date"]) for it in its), default=None)
        sents = [it["sentiment"] for it in its if it.get("sentiment") is not None]
        out.append({
            "kol_id": k["id"],
            "name": k["name"],
            "tier": k["tier"],
            "therapeutic_area": k["therapeutic_area"],
            "interactions": len(its),
            "last_interaction": last.isoformat() if last else None,
            "days_since_last": (today - last).days if last else None,
            "avg_sentiment": _round(np.mean(sents)) if sents else None,
            "insights": insight_counts.get(k["id"], 0),
            "influence": influence_score(k),
            "_raw": score,
        })
    top = max(raw.values(), default=0) or 1.0
    for row in out:
        row["engagement"] = round(100 * row.pop("_raw") / top, 1)
    # Segment relative to the cohort: "high" means at or above the median.
    med_inf = float(np.median([r["influence"] for r in out])) if out else 0.0
    med_eng = float(np.median([r["engagement"] for r in out])) if out else 0.0
    for row in out:
        row["segment"] = _segment(row["influence"] >= med_inf and row["influence"] > 0,
                                  row["engagement"] >= med_eng and row["engagement"] > 0)
    out.sort(key=lambda r: (-r["engagement"], -r["influence"]))
    return out


def _segment(hi_inf: bool, hi_eng: bool) -> str:
    if hi_inf and hi_eng:
        return "strategic_partner"
    if hi_inf and not hi_eng:
        return "priority_gap"        # influential but under-engaged -> plan outreach
    if not hi_inf and hi_eng:
        return "engaged_advocate"
    return "monitor"


def monthly_trend(interactions: list[dict]) -> dict:
    """Interaction volume and sentiment per month, plus a linear trend test."""
    buckets: dict[str, list[dict]] = defaultdict(list)
    for it in interactions:
        buckets[str(it["date"])[:7]].append(it)
    if not buckets:
        return {"months": [], "trend": None}
    # Fill gaps so months with zero activity count as zero, not missing.
    first, last = min(buckets), max(buckets)
    y, m = map(int, first.split("-"))
    months = []
    while f"{y:04d}-{m:02d}" <= last:
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    series = []
    for key in months:
        its = buckets.get(key, [])
        sents = [i["sentiment"] for i in its if i.get("sentiment") is not None]
        series.append({
            "month": key,
            "interactions": len(its),
            "unique_kols": len({i["kol_id"] for i in its}),
            "avg_sentiment": _round(np.mean(sents)) if sents else None,
        })
    trend = None
    if len(series) >= 3:
        x = np.arange(len(series))
        counts = np.array([s["interactions"] for s in series], dtype=float)
        if np.ptp(counts) > 0:
            lr = sps.linregress(x, counts)
            trend = {
                "test": "OLS linear regression of monthly interaction count on time",
                "n_months": len(series),
                "slope_per_month": _round(lr.slope),
                "r_squared": _round(lr.rvalue ** 2),
                "p_value": _p(lr.pvalue),
                "significant": bool(lr.pvalue < ALPHA),
                "interpretation": _trend_text(lr.slope, lr.pvalue),
            }
    return {"months": series, "trend": trend}


def _trend_text(slope: float, p: float) -> str:
    direction = "increasing" if slope > 0 else "decreasing"
    if p < ALPHA:
        return f"Engagement volume is {direction} (~{abs(slope):.1f} interactions/month, p={p:.3g})."
    return f"No statistically significant trend in engagement volume (p={p:.3g})."


def sentiment_by_group(interactions: list[dict], kols: list[dict], key: str) -> dict:
    """Compare sentiment across KOL groups (e.g. tier) with Kruskal-Wallis."""
    kol_map = {k["id"]: k for k in kols}
    groups: dict[str, list[float]] = defaultdict(list)
    for it in interactions:
        if it.get("sentiment") is None:
            continue
        if key in it:
            g = it[key]
        else:
            g = kol_map.get(it["kol_id"], {}).get(key)
        if g is not None:
            groups[str(g)].append(it["sentiment"])
    summary = {g: mean_ci(v) for g, v in sorted(groups.items())}
    eligible = [v for v in groups.values() if len(v) >= 3]
    test = None
    if len(eligible) >= 2 and len({x for v in eligible for x in v}) > 1:
        h, p = sps.kruskal(*eligible)
        test = {
            "test": "Kruskal-Wallis H (non-parametric, groups with n>=3)",
            "statistic": _round(h),
            "p_value": _p(p),
            "significant": bool(p < ALPHA),
            "interpretation": (
                f"Sentiment differs significantly by {key} (p={p:.3g})." if p < ALPHA
                else f"No significant sentiment difference by {key} (p={p:.3g})."
            ),
        }
    return {"groups": summary, "test": test}


def influence_engagement_correlation(engagement_rows: list[dict]) -> dict | None:
    """Spearman correlation between influence and engagement scores."""
    xs = [r["influence"] for r in engagement_rows]
    ys = [r["engagement"] for r in engagement_rows]
    if len(xs) < 5 or np.ptp(xs) == 0 or np.ptp(ys) == 0:
        return None
    rho, p = sps.spearmanr(xs, ys)
    strength = "weak" if abs(rho) < 0.3 else "moderate" if abs(rho) < 0.6 else "strong"
    return {
        "test": "Spearman rank correlation (influence vs engagement)",
        "n": len(xs),
        "rho": _round(rho),
        "p_value": _p(p),
        "significant": bool(p < ALPHA),
        "interpretation": (
            f"{strength.capitalize()} {'positive' if rho > 0 else 'negative'} association "
            f"(rho={rho:.2f}, p={p:.3g}). "
            + ("Engagement effort is aligned with influence." if rho >= 0.3 and p < ALPHA
               else "Engagement is not clearly aligned with influence - review 'priority_gap' KOLs.")
        ),
    }


def insight_category_association(insights: list[dict]) -> dict | None:
    """Chi-square test: are insight categories independent of therapeutic area?"""
    pairs = [(i["category"], i.get("therapeutic_area")) for i in insights if i.get("therapeutic_area")]
    cats = sorted({c for c, _ in pairs})
    tas = sorted({t for _, t in pairs})
    if len(cats) < 2 or len(tas) < 2:
        return None
    table = np.zeros((len(tas), len(cats)))
    for c, t in pairs:
        table[tas.index(t), cats.index(c)] += 1
    # Drop all-zero rows/cols (can't happen by construction but keeps chi2 safe).
    chi2, p, dof, expected = sps.chi2_contingency(table)
    n = table.sum()
    cramers_v = math.sqrt(chi2 / (n * (min(table.shape) - 1))) if n else None
    low_expected = float((expected < 5).mean())
    return {
        "test": "Chi-square test of independence (category x therapeutic area)",
        "n": int(n),
        "chi2": _round(chi2),
        "dof": int(dof),
        "p_value": _p(p),
        "cramers_v": _round(cramers_v),
        "significant": bool(p < ALPHA),
        "pct_cells_expected_lt5": _round(100 * low_expected, 1),
        "warning": ("More than 20% of cells have expected count <5; treat p-value with caution."
                    if low_expected > 0.2 else None),
        "rows": tas,
        "cols": cats,
        "table": table.astype(int).tolist(),
    }


def overview(kols: list[dict], interactions: list[dict], insights: list[dict],
             actions: list[dict], today: date | None = None, window_days: int = 90) -> dict:
    today = today or date.today()
    recent = [i for i in interactions if 0 <= (today - _d(i["date"])).days <= window_days]
    engaged = {i["kol_id"] for i in recent}
    tier1 = [k for k in kols if k["tier"] == 1]
    tier1_cov = (sum(1 for k in tier1 if k["id"] in engaged) / len(tier1)) if tier1 else None
    sents = [i["sentiment"] for i in interactions if i.get("sentiment") is not None]
    open_actions = [a for a in actions if a["status"] != "done"]
    overdue = [a for a in open_actions if a.get("due_date") and _d(a["due_date"]) < today]
    return {
        "kols": len(kols),
        "interactions": len(interactions),
        "insights": len(insights),
        "window_days": window_days,
        "interactions_in_window": len(recent),
        "kols_engaged_in_window": len(engaged),
        "tier1_coverage_pct": _round(100 * tier1_cov, 1) if tier1_cov is not None else None,
        "sentiment": mean_ci(sents),
        "open_actions": len(open_actions),
        "overdue_actions": len(overdue),
        "by_type": dict(Counter(i["type"] for i in interactions).most_common()),
        "by_category": dict(Counter(i["category"] for i in insights).most_common()),
    }


def full_report(kols, interactions, insights, actions, today: date | None = None) -> dict:
    eng = kol_engagement(kols, interactions, insights, today)
    return {
        "overview": overview(kols, interactions, insights, actions, today),
        "engagement": eng,
        "segments": dict(Counter(r["segment"] for r in eng)),
        "monthly": monthly_trend(interactions),
        "sentiment_by_tier": sentiment_by_group(interactions, kols, "tier"),
        "sentiment_by_type": sentiment_by_group(interactions, kols, "type"),
        "influence_vs_engagement": influence_engagement_correlation(eng),
        "insight_association": insight_category_association(insights),
        "notes": [
            "Analyses are observational and descriptive; associations are not causal.",
            "Sentiment scores are MSL-recorded ratings (-1..1) and subject to rater bias.",
            "Influence and engagement scores are configurable heuristics, not validated indices.",
        ],
    }
