"""Generate a FICTITIOUS demo dataset. No real HCPs, institutions or products.

Usage:  python -m app.seed            (writes to CRM_DB_PATH or data/crm.db, replacing it)
"""
from __future__ import annotations

import json
import random
from datetime import date, timedelta

from .db import connect, db_path, init_db

FIRST = ["Amara", "Bruno", "Chen", "Dana", "Elif", "Farid", "Grace", "Hiro", "Ines", "Jonas",
         "Kavya", "Lena", "Mateo", "Nadia", "Omar", "Priya", "Quinn", "Rosa", "Sami", "Tara",
         "Uma", "Viktor", "Wen", "Yara", "Zoe", "Ali", "Beatriz", "Carlos", "Dmitri", "Eva"]
LAST = ["Abernathy", "Brightwell", "Castellan", "Dorner", "Eskilsen", "Falkner", "Greaves",
        "Holloway", "Ishikawa", "Jansen", "Kowalczyk", "Lindqvist", "Moreau", "Nakamura",
        "Okafor", "Petrov", "Quintero", "Rahman", "Sorensen", "Takahashi"]
INSTITUTIONS = ["Northbridge University Hospital", "Lakeside Cancer Institute", "Riverside Medical Center",
                "St. Aurelia Clinic", "Meridian Academic Health", "Harborview Research Hospital"]
REGIONS = ["North America", "Europe", "APAC", "MENA", "LATAM"]
TA_SPECIALTY = {
    "Oncology": ["Medical Oncology", "Hematology", "Radiation Oncology"],
    "Immunology": ["Rheumatology", "Dermatology", "Gastroenterology"],
    "Cardiometabolic": ["Cardiology", "Endocrinology", "Nephrology"],
}
NOTE_TEMPLATES = {
    "unmet_need": ["Highlighted a clear unmet need in refractory patients with limited options after second-line therapy.",
                   "Patients with comorbid renal impairment have no good option today; unmet need remains high."],
    "clinical_practice": ["Discussed treatment sequencing; in practice most centres switch after 3 months, ahead of guideline.",
                          "Asked about dosing in elderly patients and how the standard of care is evolving."],
    "treatment_barrier": ["Adherence is a barrier due to injection burden and monitoring requirements.",
                          "Clinic capacity and workflow make the monitoring schedule hard to follow."],
    "data_gap": ["Requested more real-world data and long-term outcomes in the elderly subgroup.",
                 "Wants head-to-head comparative evidence; noted the evidence gap in this subgroup."],
    "safety_perception": ["Raised concern about long-term tolerability and infection risk.",
                          "Peers perceive the safety profile as a concern; requested a balanced safety summary."],
    "competitive_landscape": ["Mentioned a competitor in the pipeline and growing biosimilar use at the centre.",
                              "Compared mechanism with an alternative therapy recently presented at congress."],
    "access_reimbursement": ["Prior authorization and formulary restrictions delay access for many patients.",
                             "Payer coverage and cost remain the main access hurdle in the region."],
    "education_need": ["Community physicians are unfamiliar with the biomarker testing pathway; education needed.",
                       "Observed misconception about the mechanism; suggested a webinar for fellows."],
}
COMPLIANCE_NOTES = [
    "Mentioned a patient who was hospitalized with a rash after starting therapy - possible adverse event.",
    "Asked unsolicited about off-label use in paediatric patients; responded with approved information only.",
]
TYPES = ["one_to_one"] * 6 + ["congress"] * 3 + ["advisory_board"] * 1 + ["speaker_program"] * 1 + \
        ["medical_info_request"] * 2 + ["investigator_meeting"] * 1
MSLS = ["MSL North", "MSL South", "MSL East", "MSL West"]


def seed(conn, today: date | None = None, n_kols: int = 36, months: int = 15, rng_seed: int = 7) -> dict:
    rnd = random.Random(rng_seed)
    today = today or date.today()
    for t in ("actions", "insights", "interactions", "kols"):
        conn.execute(f"DELETE FROM {t}")
    conn.execute("DELETE FROM sqlite_sequence")

    kol_ids = []
    used = set()
    for i in range(n_kols):
        while True:
            name = f"Dr. {rnd.choice(FIRST)} {rnd.choice(LAST)}"
            if name not in used:
                used.add(name)
                break
        ta = list(TA_SPECIALTY)[i % 3]
        tier = rnd.choices([1, 2, 3], weights=[2, 4, 3])[0]
        pubs = int(rnd.lognormvariate({1: 4.2, 2: 3.3, 3: 2.3}[tier], 0.6))
        trials = int(rnd.lognormvariate({1: 2.4, 2: 1.5, 3: 0.6}[tier], 0.6))
        cur = conn.execute(
            "INSERT INTO kols (name, specialty, institution, region, therapeutic_area, tier, publications,"
            " trials_involved, email) VALUES (?,?,?,?,?,?,?,?,?)",
            (name, rnd.choice(TA_SPECIALTY[ta]), rnd.choice(INSTITUTIONS), rnd.choice(REGIONS), ta, tier,
             pubs, trials, name.lower().replace("dr. ", "").replace(" ", ".") + "@example.org"))
        kol_ids.append((cur.lastrowid, ta, tier))

    cats = list(NOTE_TEMPLATES)
    n_inter = 0
    start = today - timedelta(days=30 * months)
    for kid, ta, tier in kol_ids:
        # Engagement intensity: mostly tier-driven, with deliberate under-engaged Tier 1 KOLs.
        rate = {1: 9, 2: 6, 3: 3}[tier] * rnd.uniform(0.2, 1.4)
        ta_bias = {"Oncology": ["unmet_need", "data_gap"], "Immunology": ["treatment_barrier", "safety_perception"],
                   "Cardiometabolic": ["access_reimbursement", "education_need"]}[ta]
        for _ in range(max(1, int(rate))):
            # Volume ramps up over time (later dates more likely).
            d = start + timedelta(days=int((30 * months) * (rnd.random() ** 0.7)))
            cat = rnd.choice(ta_bias) if rnd.random() < 0.55 else rnd.choice(cats)
            notes = rnd.choice(NOTE_TEMPLATES[cat])
            if rnd.random() < 0.03:
                notes += " " + rnd.choice(COMPLIANCE_NOTES)
            base = {1: 0.35, 2: 0.2, 3: 0.05}[tier] + (-0.25 if cat == "safety_perception" else 0)
            sentiment = max(-1.0, min(1.0, round(rnd.gauss(base, 0.3), 2)))
            cur = conn.execute(
                "INSERT INTO interactions (kol_id, date, type, channel, msl_owner, topics, notes, sentiment,"
                " duration_min) VALUES (?,?,?,?,?,?,?,?,?)",
                (kid, d.isoformat(), rnd.choice(TYPES), rnd.choice(["in_person", "in_person", "virtual", "phone"]),
                 rnd.choice(MSLS), cat.replace("_", " "), notes, sentiment, rnd.choice([20, 30, 45, 60, 90])))
            n_inter += 1
            if rnd.random() < 0.35:
                conn.execute(
                    "INSERT INTO insights (kol_id, interaction_id, category, therapeutic_area, text, created_at)"
                    " VALUES (?,?,?,?,?,?)", (kid, cur.lastrowid, cat, ta, notes, d.isoformat()))

    for title, owner, prio, status, due in [
        ("Scope RWE study on long-term outcomes in elderly subgroup", "HEOR / RWE", "high", "in_progress", 30),
        ("Biomarker testing pathway webinar for community oncologists", "Medical Education", "medium", "open", 45),
        ("Update competitive intelligence brief for field medical", "Medical Affairs", "low", "done", -10),
        ("Balanced safety summary for immunology advisory board", "Medical Information", "high", "open", -5),
    ]:
        conn.execute("INSERT INTO actions (title, owner_function, priority, status, due_date, insight_ids)"
                     " VALUES (?,?,?,?,?,?)",
                     (title, owner, prio, status, (today + timedelta(days=due)).isoformat(), json.dumps([])))
    conn.commit()
    return {"kols": len(kol_ids), "interactions": n_inter}


if __name__ == "__main__":
    c = connect()
    init_db(c)
    print(f"Seeded {db_path()}:", seed(c))
