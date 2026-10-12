"""AI insight synthesis: turns KOL interaction notes into themes and actionable outcomes.

Two engines:
  * ``claude``  - Claude via the Anthropic SDK with a structured-output schema
                  (used when ANTHROPIC_API_KEY or another Anthropic credential is configured).
  * ``rules``   - deterministic keyword engine; always available, used as fallback.

Privacy: KOL names, emails and institutions are never sent to the model. Each KOL is
pseudonymised as ``KOL-<id>`` and only specialty / tier / therapeutic area are shared.
"""
from __future__ import annotations

import logging
import os
import re
from collections import defaultdict

from .models import (ActionableOutcome, ComplianceFlag, SynthesisResult, Theme)

log = logging.getLogger(__name__)

MODEL = os.environ.get("CRM_CLAUDE_MODEL", "claude-opus-5-5")
# Server-side refusal fallback ("default" routing). Supported on the Claude API and Claude Platform
# on AWS; set CRM_CLAUDE_FALLBACKS=0 when pointing the SDK at Bedrock / Vertex / Foundry.
FALLBACKS_ENABLED = os.environ.get("CRM_CLAUDE_FALLBACKS", "1") != "0"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# --------------------------------------------------------------------------------------
# Rule-based engine
# --------------------------------------------------------------------------------------

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "unmet_need": ["unmet need", "no good option", "lack of", "no treatment", "refractory",
                   "limited options", "need better", "nothing works"],
    "clinical_practice": ["guideline", "in practice", "first-line", "second-line", "sequencing",
                          "dosing", "switch", "standard of care", "protocol"],
    "treatment_barrier": ["barrier", "adherence", "burden", "injection", "monitoring", "hesitant",
                          "reluctant", "workflow", "capacity"],
    "data_gap": ["need data", "more data", "real-world", "long-term", "subgroup", "head-to-head",
                 "evidence gap", "no data", "elderly", "comparative"],
    "safety_perception": ["safety", "tolerability", "side effect", "toxicity", "concern about",
                          "risk", "adverse"],
    "competitive_landscape": ["competitor", "biosimilar", "generic", "other agent", "alternative therapy",
                              "pipeline", "competing"],
    "access_reimbursement": ["reimburse", "payer", "formulary", "cost", "prior authorization",
                             "access", "coverage", "price"],
    "education_need": ["education", "awareness", "training", "unfamiliar", "confus", "misconception",
                       "not aware", "webinar"],
}

CATEGORY_ACTIONS: dict[str, tuple[str, str]] = {
    "unmet_need": ("Map unmet need into evidence-generation plan", "Clinical Development"),
    "clinical_practice": ("Develop practice-pattern resource / treatment-sequencing content", "Medical Affairs"),
    "treatment_barrier": ("Design HCP/patient support materials addressing practical barriers", "Medical Education"),
    "data_gap": ("Scope real-world evidence or post-hoc analysis to close data gap", "HEOR / RWE"),
    "safety_perception": ("Prepare balanced safety data summary and response document", "Medical Information"),
    "competitive_landscape": ("Update competitive intelligence brief for field medical", "Medical Affairs"),
    "access_reimbursement": ("Share access barriers with Market Access / HEOR value dossier team", "Market Access"),
    "education_need": ("Build CME-compliant educational program on identified topic", "Medical Education"),
}

AE_PATTERNS = [r"adverse event", r"\bAE\b", r"hospitali[sz]ed", r"side effect", r"toxicit",
               r"rash", r"discontinued due to", r"reaction", r"death", r"died", r"serious"]
OFF_LABEL_PATTERNS = [r"off[- ]label", r"unapproved", r"not (yet )?approved for", r"outside (the )?label",
                      r"unlicensed"]
PROMO_PATTERNS = [r"best drug", r"superior to", r"better than", r"\bmiracle\b", r"market share",
                  r"sell", r"\bsales\b"]


def classify_text(text: str) -> list[str]:
    """Return insight categories whose keywords appear in ``text``."""
    t = (text or "").lower()
    return [c for c, kws in CATEGORY_KEYWORDS.items() if any(k in t for k in kws)]


def _excerpt(text: str, pattern: str, width: int = 70) -> str:
    m = re.search(pattern, text, flags=re.I)
    if not m:
        return text[: 2 * width]
    s, e = max(0, m.start() - width), min(len(text), m.end() + width)
    return ("..." if s else "") + text[s:e].strip() + ("..." if e < len(text) else "")


def compliance_scan(interactions: list[dict]) -> list[ComplianceFlag]:
    flags: list[ComplianceFlag] = []
    for it in interactions:
        notes = it.get("notes") or ""
        for kind, pats, step in [
            ("potential_adverse_event", AE_PATTERNS,
             "Verify and report to Pharmacovigilance within the mandated timeframe (typically 24h)."),
            ("off_label_discussion", OFF_LABEL_PATTERNS,
             "Confirm the discussion was unsolicited/reactive and documented per SOP."),
            ("promotional_language", PROMO_PATTERNS,
             "Review with Compliance - field medical exchanges must remain non-promotional."),
        ]:
            hit = next((p for p in pats if re.search(p, notes, flags=re.I)), None)
            if hit:
                flags.append(ComplianceFlag(interaction_id=it["id"], flag_type=kind,
                                            excerpt=_excerpt(notes, hit), recommended_step=step))
    return flags


def synthesize_rules(interactions: list[dict], kols: dict[int, dict]) -> SynthesisResult:
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for it in interactions:
        for c in classify_text(f"{it.get('topics') or ''} {it.get('notes') or ''}"):
            by_cat[c].append(it)

    scope_kols = len({i["kol_id"] for i in interactions}) or 1
    ranked = sorted(by_cat.items(), key=lambda kv: (-len({i['kol_id'] for i in kv[1]}), -len(kv[1])))
    themes, outcomes = [], []
    for cat, its in ranked:
        kol_ids = {i["kol_id"] for i in its}
        tas = sorted({kols.get(i["kol_id"], {}).get("therapeutic_area", "?") for i in its})
        label = cat.replace("_", " ")
        themes.append(Theme(
            title=label.capitalize(),
            category=cat,
            summary=(f"{len(its)} interaction(s) from {len(kol_ids)} KOL(s) in {', '.join(tas)} "
                     f"raised {label} topics."),
            supporting_interaction_ids=sorted(i["id"] for i in its),
            kol_count=len(kol_ids),
        ))
        title, owner = CATEGORY_ACTIONS[cat]
        tier1 = sum(1 for k in kol_ids if kols.get(k, {}).get("tier") == 1)
        # Priority is relative to scope: share of engaged KOLs raising it, boosted by Tier 1 voices.
        share = len(kol_ids) / scope_kols
        priority = ("high" if share >= 0.33 or tier1 >= 3 else
                    "medium" if share >= 0.10 or len(kol_ids) >= 2 else "low")
        outcomes.append(ActionableOutcome(
            title=title,
            rationale=(f"Raised independently by {len(kol_ids)} of {scope_kols} KOL(s) in scope "
                       f"({share:.0%}; {tier1} Tier 1)."),
            owner_function=owner,
            priority=priority,
            supporting_interaction_ids=sorted(i["id"] for i in its),
        ))

    flags = compliance_scan(interactions)
    gaps = []
    covered_tas = {kols.get(i["kol_id"], {}).get("therapeutic_area") for i in interactions}
    all_tas = {k.get("therapeutic_area") for k in kols.values()}
    for ta in sorted(t for t in all_tas - covered_tas if t):
        gaps.append(f"No interactions in scope for therapeutic area: {ta}.")
    unclassified = [i["id"] for i in interactions
                    if not classify_text(f"{i.get('topics') or ''} {i.get('notes') or ''}")]
    if unclassified:
        gaps.append(f"{len(unclassified)} interaction(s) had notes too sparse to classify; "
                    "encourage structured insight capture.")

    top = ", ".join(t.title.lower() for t in themes[:3]) or "no recurring themes"
    summary = (f"Rule-based synthesis of {len(interactions)} interaction(s). "
               f"Most widely shared themes: {top}. "
               f"{len(flags)} compliance flag(s) require review.")
    return SynthesisResult(executive_summary=summary, themes=themes, actionable_outcomes=outcomes,
                           compliance_flags=flags, knowledge_gaps=gaps)


# --------------------------------------------------------------------------------------
# Claude engine
# --------------------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an analyst supporting a pharmaceutical Medical Affairs team.
You receive field medical (MSL) interaction records with Key Opinion Leaders (KOLs), pseudonymised as KOL-<id>.
Synthesise them into insights the medical strategy team can act on.

Guidelines:
- Ground every theme and outcome in the records: cite the interaction ids that support it and do not invent facts.
- Prefer themes raised independently by several KOLs; state kol_count accurately.
- Actionable outcomes must be specific, non-promotional and owned by one of these functions:
  {owners}.
- Use these theme categories where they fit: {categories}.
- Flag any text suggesting a potential adverse event, an off-label discussion, or promotional language,
  quoting a short excerpt. When in doubt about an adverse event, flag it - Pharmacovigilance decides.
- knowledge_gaps lists what the team cannot conclude from these records (missing data, thin coverage).
- Keep the executive summary under 120 words."""


def _claude_payload(interactions: list[dict], kols: dict[int, dict]) -> str:
    lines = []
    for it in interactions:
        k = kols.get(it["kol_id"], {})
        lines.append(
            f"[interaction {it['id']}] date={it['date']} type={it['type']} "
            f"KOL-{it['kol_id']} (specialty={k.get('specialty')}, tier={k.get('tier')}, "
            f"TA={k.get('therapeutic_area')}) sentiment={it.get('sentiment')}\n"
            f"topics: {it.get('topics') or '-'}\nnotes: {it.get('notes') or '-'}"
        )
    return "Interaction records:\n\n" + "\n\n".join(lines)


def claude_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def synthesize_claude(interactions: list[dict], kols: dict[int, dict]) -> tuple[SynthesisResult, str]:
    """Returns (result, model that served the response)."""
    import anthropic
    from .models import INSIGHT_CATEGORIES, OWNER_FUNCTIONS

    client = anthropic.Anthropic()
    kwargs = dict(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT.format(owners=", ".join(OWNER_FUNCTIONS),
                                    categories=", ".join(INSIGHT_CATEGORIES)),
        messages=[{"role": "user", "content": _claude_payload(interactions, kols)}],
        output_format=SynthesisResult,
    )
    if FALLBACKS_ENABLED:
        # On a safety-classifier refusal the API re-runs the request on a fallback model server-side.
        response = client.beta.messages.parse(**kwargs, betas=[FALLBACK_BETA], fallbacks="default")
    else:
        response = client.messages.parse(**kwargs)
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined the request (stop_reason=refusal)")
    if response.parsed_output is None:
        raise RuntimeError(f"No structured output returned (stop_reason={response.stop_reason})")
    result: SynthesisResult = response.parsed_output
    # Drop citations to interaction ids the model was not given.
    valid = {i["id"] for i in interactions}
    for item in [*result.themes, *result.actionable_outcomes]:
        item.supporting_interaction_ids = [i for i in item.supporting_interaction_ids if i in valid]
    result.compliance_flags = [f for f in result.compliance_flags if f.interaction_id in valid]
    # Always union with the deterministic compliance scan: AE detection must not depend on the LLM alone.
    seen = {(f.interaction_id, f.flag_type) for f in result.compliance_flags}
    result.compliance_flags += [f for f in compliance_scan(interactions)
                                if (f.interaction_id, f.flag_type) not in seen]
    return result, getattr(response, "model", None) or MODEL


def synthesize(interactions: list[dict], kols: dict[int, dict], use_ai: bool = True) -> dict:
    """Run synthesis, preferring Claude and falling back to the rule engine."""
    if not interactions:
        empty = SynthesisResult(executive_summary="No interactions match the selected scope.",
                                themes=[], actionable_outcomes=[], compliance_flags=[],
                                knowledge_gaps=["No data in scope."])
        return {"engine": "none", "warning": None, "result": empty.model_dump()}
    warning = None
    if use_ai and claude_available():
        try:
            result, served = synthesize_claude(interactions, kols)
            note = (f"Requested {MODEL}; served by fallback model {served}." if served != MODEL else None)
            return {"engine": "claude", "model": served, "warning": note, "result": result.model_dump()}
        except Exception as exc:  # network, auth, refusal, validation - degrade gracefully
            log.warning("Claude synthesis failed, using rule engine: %s", exc)
            warning = f"AI engine unavailable ({type(exc).__name__}); showing rule-based synthesis."
    elif use_ai:
        warning = "No Anthropic credentials configured; showing rule-based synthesis."
    return {"engine": "rules", "warning": warning,
            "result": synthesize_rules(interactions, kols).model_dump()}
