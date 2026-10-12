# Medical Affairs KOL CRM

A lightweight CRM for pharmaceutical **Medical Affairs / field medical (MSL)** teams to:

- **Organise Key Opinion Leader (KOL) relationships**: profiles, tiering, therapeutic area, publications and trials.
- **Log scientific interactions**: 1:1s, advisory boards, congresses, speaker programs, medical information requests and investigator meetings.
- **Turn KOL insights into actionable outcomes with AI.** Claude synthesises interaction notes into themes, owner-assigned actions and compliance flags. A rule-based engine runs when no API key is set.
- **Run statistical analyses on stakeholder engagement**: trends, sentiment comparisons, influence/engagement alignment, and insight associations.

## Quick start

```bash
pip install -r requirements.txt
python -m app.seed                      # optional: load a FICTITIOUS demo dataset
uvicorn app.main:app --reload           # open http://localhost:8000
```

To enable Claude-powered synthesis:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export CRM_CLAUDE_MODEL=claude-opus-5-5   # optional, this is the default
```

Other settings: `CRM_DB_PATH` sets the SQLite file (default `data/crm.db`).

Run the tests with `python -m pytest`.

## Features

| Area | What it does |
|---|---|
| Dashboard | KPIs (90-day activity, Tier 1 coverage, mean sentiment with a 95% CI, overdue actions), a monthly engagement chart, an influence-vs-engagement scatter plot and a "priority gap" outreach list |
| KOLs | Searchable, sortable directory with engagement and influence scores and segment labels. Each KOL has a detail page with their timeline and insights |
| Interactions | Structured logging. When you save, it suggests insight categories and runs a compliance check |
| AI Insights | Synthesis scoped by therapeutic area, KOL, date and size. You review the results, then save themes as insights and outcomes as tracked actions |
| Actions | Kanban board (open / in progress / done) with owner function, priority, due date and overdue highlighting |
| Analytics | The statistical tests below, a category × therapeutic area crosstab, the engagement table and CSV exports |

### Statistical methods (`app/stats.py`)

| Question | Method |
|---|---|
| Is engagement volume changing over time? | OLS linear regression of monthly counts (gap months filled with 0), reporting slope, R² and p |
| Does sentiment differ by KOL tier or by interaction type? | Kruskal–Wallis H (non-parametric; only groups with n ≥ 3), plus means with t-based 95% CIs |
| Is our effort aligned with KOL influence? | Spearman rank correlation between influence and engagement scores |
| Do insight themes depend on therapeutic area? | Chi-square test of independence with Cramér's V, warning when more than 20% of expected cells are below 5 |
| Who should we prioritise? | Segmentation against cohort medians: strategic partner / priority gap / engaged advocate / monitor |

**Engagement score:** Σ(interaction type weight × 0.5^(days ago / 90)), scaled 0–100.
**Influence score:** log-scaled publications (45%) + trials (30%) + tier (25%).

Both scores are **configurable heuristics, not validated indices**. Tune `TYPE_WEIGHTS`, `RECENCY_HALF_LIFE_DAYS` and `influence_score` to fit your organisation.

### AI synthesis (`app/ai.py`)

- Uses the Anthropic Python SDK, `messages.parse` and a Pydantic schema (`SynthesisResult`), so output is validated JSON with themes, actionable outcomes, compliance flags and knowledge gaps.
- **Privacy:** KOL names, emails and institutions are never sent to the model. KOLs are pseudonymised as `KOL-<id>`.
- **Grounding:** the model must cite interaction ids, and any citation to an id it was not given is removed.
- **Safety net:** a deterministic keyword scan for potential adverse events, off-label discussion and promotional language is *always* added to the model's flags. AE detection never depends on the LLM alone.
- **Graceful degradation:** a missing key, a network error or a refusal falls back to the rule engine and shows a visible warning.

## API

The REST API lives under `/api`, with interactive docs at `/docs`. Main endpoints:
`/api/kols`, `/api/interactions`, `/api/insights`, `/api/actions`, `/api/ai/synthesize`, `/api/ai/accept`, `/api/analytics`, `/api/export/{kols|interactions|insights|actions|engagement}.csv`.

## Project layout

```
app/
  main.py      FastAPI routes + static UI
  db.py        SQLite schema/connection
  models.py    Pydantic schemas & controlled vocabularies
  stats.py     engagement analytics & statistical tests
  ai.py        Claude + rule-based insight synthesis, compliance scan
  seed.py      fictitious demo data generator
  static/      single-page UI (vanilla JS, bundled Chart.js 4.4.1, MIT)
tests/         pytest suite (stats, AI engine, API)
```

## Before production use

This is a working foundation, not a validated system. Before using it with real HCP data you would need:

- **Authentication and role-based access.** There is none yet.
- **Audit trail.** For GxP / 21 CFR Part 11 contexts, you need an audit trail and validated change control.
- **Data protection review.** GDPR/HIPAA review, a legal basis for HCP data, retention policy, and a DPA covering any LLM vendor.
- **Pharmacovigilance SOP integration.** The adverse-event flags are a screening aid only. They do not replace your company's PV reporting process.
- **Compliance sign-off** on interaction categories and the non-promotional guardrails.
- **A production database.** For multi-user deployments, use Postgres instead of SQLite.
