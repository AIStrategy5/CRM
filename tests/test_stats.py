from datetime import date, timedelta

import pytest

from app import stats

TODAY = date(2026, 6, 30)


def kol(i, tier=2, pubs=10, trials=2, ta="Oncology"):
    return {"id": i, "name": f"K{i}", "tier": tier, "publications": pubs,
            "trials_involved": trials, "therapeutic_area": ta}


def inter(i, kol_id, days_ago, type_="one_to_one", sentiment=0.0):
    return {"id": i, "kol_id": kol_id, "date": (TODAY - timedelta(days=days_ago)).isoformat(),
            "type": type_, "sentiment": sentiment}


def test_mean_ci_basic():
    r = stats.mean_ci([0.0, 0.5, 1.0])
    assert r["n"] == 3 and r["mean"] == 0.5
    assert r["ci_low"] < 0.5 < r["ci_high"]


def test_mean_ci_small_samples():
    assert stats.mean_ci([])["mean"] is None
    assert stats.mean_ci([0.4])["ci_low"] is None
    assert stats.mean_ci([0.2, 0.2])["ci_low"] == 0.2


def test_influence_score_monotonic_in_tier_and_bounded():
    hi = stats.influence_score(kol(1, tier=1, pubs=500, trials=100))
    lo = stats.influence_score(kol(2, tier=3, pubs=0, trials=0))
    assert 0 <= lo < hi <= 100


def test_engagement_recency_and_type_weighting():
    kols = [kol(1), kol(2), kol(3)]
    its = [inter(1, 1, 5, "advisory_board"),   # recent + heavy type
           inter(2, 2, 365, "advisory_board"),  # same type, a year old
           ]
    rows = {r["kol_id"]: r for r in stats.kol_engagement(kols, its, [], TODAY)}
    assert rows[1]["engagement"] == 100.0
    assert 0 < rows[2]["engagement"] < 10       # 0.5^(365/90) ~ 6%
    assert rows[3]["engagement"] == 0 and rows[3]["last_interaction"] is None
    assert rows[3]["segment"] in ("monitor", "priority_gap")


def test_priority_gap_segment():
    kols = [kol(1, tier=1, pubs=300, trials=40), kol(2, tier=3, pubs=1, trials=0),
            kol(3, tier=3, pubs=2, trials=0), kol(4, tier=1, pubs=250, trials=30)]
    its = [inter(i, 2, 1) for i in range(5)] + [inter(10 + i, 3, 1) for i in range(5)] + [inter(20 + i, 4, 2, "advisory_board") for i in range(6)]
    seg = {r["kol_id"]: r["segment"] for r in stats.kol_engagement(kols, its, [], TODAY)}
    assert seg[1] == "priority_gap"   # highly influential, never engaged
    assert seg[4] == "strategic_partner"


def test_monthly_trend_fills_gaps_and_detects_increase():
    its = []
    n = 0
    for month in range(1, 9):
        for _ in range(month * 2):  # strictly increasing volume
            n += 1
            its.append({"id": n, "kol_id": 1, "date": f"2026-{month:02d}-10", "type": "congress", "sentiment": 0.1})
    its = [i for i in its if not i["date"].startswith("2026-04")]  # gap month
    r = stats.monthly_trend(its)
    assert [m["month"] for m in r["months"]][:5] == ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05"]
    assert r["months"][3]["interactions"] == 0
    assert r["trend"]["slope_per_month"] > 0 and r["trend"]["significant"]


def test_sentiment_by_group_detects_difference():
    kols = [kol(1, tier=1), kol(2, tier=3)]
    its = [inter(i, 1, i, sentiment=0.8 + (i % 3) * 0.05) for i in range(10)] + \
          [inter(100 + i, 2, i, sentiment=-0.6 - (i % 3) * 0.05) for i in range(10)]
    r = stats.sentiment_by_group(its, kols, "tier")
    assert r["groups"]["1"]["mean"] > r["groups"]["3"]["mean"]
    assert r["test"]["significant"] and r["test"]["p_value"] > 0  # never rounded to 0


def test_sentiment_by_group_insufficient_data():
    r = stats.sentiment_by_group([inter(1, 1, 1, sentiment=0.5)], [kol(1)], "tier")
    assert r["test"] is None


def test_correlation_requires_variation():
    rows = [{"influence": 50, "engagement": 10}] * 6
    assert stats.influence_engagement_correlation(rows) is None
    rows = [{"influence": i * 10, "engagement": i * 9} for i in range(8)]
    r = stats.influence_engagement_correlation(rows)
    assert r["rho"] == pytest.approx(1.0) and r["significant"]


def test_chi_square_association():
    ins = ([{"category": "data_gap", "therapeutic_area": "Oncology"}] * 20 +
           [{"category": "access_reimbursement", "therapeutic_area": "Cardiometabolic"}] * 20 +
           [{"category": "data_gap", "therapeutic_area": "Cardiometabolic"}] * 2)
    r = stats.insight_category_association(ins)
    assert r["significant"] and r["n"] == 42 and r["cramers_v"] > 0.5
    assert stats.insight_category_association(ins[:5]) is None  # one category only


def test_overview_counts_overdue_and_tier1_coverage():
    kols = [kol(1, tier=1), kol(2, tier=1)]
    its = [inter(1, 1, 10), inter(2, 2, 200)]
    actions = [{"status": "open", "due_date": "2026-01-01"}, {"status": "done", "due_date": "2026-01-01"},
               {"status": "open", "due_date": None}]
    o = stats.overview(kols, its, [], actions, TODAY)
    assert o["tier1_coverage_pct"] == 50.0
    assert o["open_actions"] == 2 and o["overdue_actions"] == 1
