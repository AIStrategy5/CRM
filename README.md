# Medical Affairs KOL CRM

A lightweight CRM for pharmaceutical **Medical Affairs / field medical (MSL)** teams to:

- **Organise Key Opinion Leader (KOL) relationships**: profiles, tiering, therapeutic area, publications and trials.
- **Log scientific interactions**: 1:1s, advisory boards, congresses, speaker programs, medical information requests and investigator meetings.
- **Turn KOL insights into actionable outcomes with AI.** Claude synthesises interaction notes into themes, owner-assigned actions and compliance flags. A rule-based engine runs when no API key is set.
- **Run statistical analyses on stakeholder engagement**: trends, sentiment comparisons, influence/engagement alignment, and insight associations.
- **Stay compliant**:
  - role-based logins and a tamper-evident audit trail with reasons for change;
  - a pharmacovigilance referral workflow;
  - consent tracking, data-subject export and retention purge.

## Quick start

```bash
pip install -r requirements.txt
python -m app.seed                      # optional: FICTITIOUS demo data + demo logins (replaces the DB)
uvicorn app.main:app --reload           # open http://localhost:8000
```

The seed creates the demo logins `admin`, `lead`, `msl` and `viewer`, all with the password `demo-password-123`. They are for local demos only.

On a fresh database without the seed, an `admin` user is created on first start:
- If `CRM_ADMIN_PASSWORD` is set, it uses that password.
- Otherwise it generates a password and prints it once to the server log.

To enable Claude-powered synthesis:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export CRM_CLAUDE_MODEL=claude-opus-5-5   # optional, this is the default
```

Other environment variables:

| Variable | Purpose |
|---|---|
| `CRM_DB_PATH` | SQLite file (default `data/crm.db`) |
| `CRM_ADMIN_USER` / `CRM_ADMIN_PASSWORD` | Initial admin account on an empty database |
| `CRM_CLAUDE_MODEL` | Claude model (default `claude-opus-5-5`) |
| `CRM_CLAUDE_FALLBACKS` | `1` (default) enables server-side refusal fallback. Set to `0` on Bedrock, Vertex or Foundry |

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
| PV referrals | A potential adverse event in a note opens a referral, due in 24h by default. Closing one requires either a safety-database case reference or a written justification |
| Admin | User management, scoring and compliance settings, retention purge, and an audit-trail viewer with an integrity check |

### Roles

| Permission | admin | lead | msl | viewer |
|---|:-:|:-:|:-:|:-:|
| View KOLs, interactions, insights, actions, analytics | ✓ | ✓ | ✓ | ✓ |
| Create/edit KOLs, log interactions and insights, run AI synthesis | ✓ | ✓ | ✓ | |
| Update action status | ✓ | ✓ | ✓ | |
| View PV referrals | ✓ | ✓ | ✓ | |
| Save AI results, manage actions, resolve PV referrals, delete interactions/insights | ✓ | ✓ | | |
| Read the audit trail, CSV exports | ✓ | ✓ | | |
| Delete KOLs, data-subject export, users, settings, retention purge | ✓ | | | |

### Compliance controls

- **Audit trail.** Every create, update and delete is recorded, along with logins, failed logins, exports, AI runs and purges. Each record holds the user, a UTC timestamp, before/after values and the reason for change.
  - **Append-only:** SQLite triggers reject `UPDATE`/`DELETE` on the log.
  - **Hash-chained:** each record includes a SHA-256 of the previous one. `GET /api/audit/verify` finds the first altered record.
- **Reason for change.** Edits and deletes require an `X-Change-Reason`. An admin can turn this off.
- **Authentication.**
  - Passwords are hashed with scrypt; the minimum length is 10.
  - Bearer sessions last 12h and are stored hashed.
  - An account locks for 15 min after 5 failed logins.
  - Deactivating a user or resetting their password ends their sessions.
  - The last active admin can't be removed.
- **PV referrals.** These are a screening aid. The keyword scan runs on every logged interaction, whether or not AI is used.
- **Data protection.**
  - Each KOL has a consent status and date. KOLs who withdrew consent are excluded from AI processing.
  - Admins can export a KOL's full record as JSON (data-subject access).
  - The retention purge deletes old interactions and their insights. It has a dry-run preview, and PV referrals are kept.

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
- **Refusal fallback:** requests use server-side `fallbacks: "default"` (beta `server-side-fallback-2026-07-01`). If a safety classifier declines, the API re-runs the request on a fallback model, and the UI shows which model served it.
- **Graceful degradation:** a missing key, a network error, a refusal across the whole fallback chain, or the admin "AI disabled" setting all fall back to the rule engine, with a visible warning.

## API

The REST API lives under `/api`, with interactive docs at `/docs`. All endpoints except login require `Authorization: Bearer <token>`. Main endpoints:

- **Auth:** `/api/auth/{login,logout,me,password}`
- **Core data:** `/api/kols` (plus `/{id}/export`), `/api/interactions`, `/api/insights`, `/api/actions`
- **AI:** `/api/ai/{synthesize,accept}`
- **PV and audit:** `/api/pv`, `/api/audit` (plus `/verify`)
- **Admin:** `/api/users`, `/api/settings`, `/api/admin/retention`
- **Reporting:** `/api/analytics`, `/api/export/{kols|interactions|insights|actions|pv_referrals|engagement}.csv`

## Project layout

```
app/
  main.py      FastAPI routes + static UI
  security.py  auth, RBAC, hash-chained audit trail, settings
  db.py        SQLite schema/connection
  models.py    Pydantic schemas & controlled vocabularies
  stats.py     engagement analytics & statistical tests
  ai.py        Claude + rule-based insight synthesis, compliance scan
  seed.py      fictitious demo data generator
  static/      single-page UI (vanilla JS, bundled Chart.js 4.4.1, MIT)
tests/         pytest suite (stats, AI engine, API, security/compliance)
```

## Before production use

The controls above are building blocks, not a validated system. Before using it with real HCP data you still need:

- **Computer system validation.** 21 CFR Part 11 / EU Annex 11 requires validation (IQ/OQ/PQ), SOPs and validated change control. Electronic signatures as defined in Part 11 are not implemented.
- **Enterprise identity.** Add SSO/MFA through your identity provider. Run behind HTTPS.
- **Data protection review.** GDPR/HIPAA review, a legal basis for HCP data, a DPA with the LLM vendor, and a decision on data residency.
- **PV integration.** Connect referrals to your safety database and the SOP-defined reporting timelines. The keyword screen will both over-flag and miss events.
- **Compliance sign-off** on interaction categories and the non-promotional guardrails.
- **A production database and multi-worker deployment.**
  - Use Postgres instead of SQLite.
  - Login lockout counters are in-memory per process. Move them to shared storage if you run several workers.
- **Rule-engine scope.** The rule engine is English-only.
