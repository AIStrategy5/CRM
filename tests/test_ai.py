from app import ai


def test_classify_text():
    cats = ai.classify_text("Requested more real-world data; prior authorization delays access")
    assert "data_gap" in cats and "access_reimbursement" in cats
    assert ai.classify_text("") == []


def test_compliance_scan_flags_ae_and_off_label():
    its = [{"id": 1, "notes": "A patient was hospitalized after the second dose."},
           {"id": 2, "notes": "Asked about off-label use in children."},
           {"id": 3, "notes": "Discussed guideline updates."}]
    flags = {(f.interaction_id, f.flag_type) for f in ai.compliance_scan(its)}
    assert (1, "potential_adverse_event") in flags
    assert (2, "off_label_discussion") in flags
    assert not any(i == 3 for i, _ in flags)


def test_rules_synthesis_ranks_shared_themes_and_prioritises():
    kols = {i: {"id": i, "tier": 1, "therapeutic_area": "Oncology", "specialty": "Onc"} for i in range(1, 5)}
    its = [{"id": i, "kol_id": i, "date": "2026-01-01", "type": "one_to_one", "topics": "",
            "notes": "Need more real-world data in the elderly subgroup.", "sentiment": 0.1} for i in range(1, 5)]
    its.append({"id": 9, "kol_id": 1, "date": "2026-01-02", "type": "congress", "topics": "",
                "notes": "Formulary restrictions.", "sentiment": 0})
    r = ai.synthesize(its, kols, use_ai=False)
    assert r["engine"] == "rules"
    res = r["result"]
    assert res["themes"][0]["category"] == "data_gap" and res["themes"][0]["kol_count"] == 4
    top = res["actionable_outcomes"][0]
    assert top["priority"] == "high" and top["owner_function"] == "HEOR / RWE"


def test_synthesize_without_credentials_warns(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    r = ai.synthesize([{"id": 1, "kol_id": 1, "notes": "education needed"}], {1: {}}, use_ai=True)
    assert r["engine"] == "rules" and "credentials" in r["warning"]


def test_claude_failure_falls_back(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")

    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(ai, "synthesize_claude", boom)
    r = ai.synthesize([{"id": 1, "kol_id": 1, "notes": "x"}], {1: {}}, use_ai=True)
    assert r["engine"] == "rules" and "RuntimeError" in r["warning"]


def test_claude_payload_is_pseudonymised():
    kols = {7: {"id": 7, "name": "Dr. Secret Person", "email": "s@x.org", "institution": "Hidden Hosp",
                "specialty": "Cardiology", "tier": 1, "therapeutic_area": "Cardiometabolic"}}
    payload = ai._claude_payload([{"id": 1, "kol_id": 7, "date": "2026-01-01", "type": "one_to_one",
                                   "notes": "n", "topics": "t", "sentiment": 0.2}], kols)
    assert "KOL-7" in payload
    for secret in ("Secret", "s@x.org", "Hidden Hosp"):
        assert secret not in payload


def test_claude_path_filters_invalid_ids_and_unions_compliance(monkeypatch):
    """Exercise synthesize_claude with a stubbed SDK client (no network)."""
    import types

    import anthropic

    from app.models import ActionableOutcome, SynthesisResult, Theme

    fake = SynthesisResult(
        executive_summary="s",
        themes=[Theme(title="t", category="data_gap", summary="x", supporting_interaction_ids=[1, 999], kol_count=1)],
        actionable_outcomes=[ActionableOutcome(title="a", rationale="r", owner_function="HEOR / RWE",
                                               priority="high", supporting_interaction_ids=[999])],
        compliance_flags=[], knowledge_gaps=[])
    calls = {}

    class FakeClient:
        def __init__(self, *a, **k):
            self.messages = types.SimpleNamespace(parse=self.parse)
            self.beta = types.SimpleNamespace(messages=types.SimpleNamespace(parse=self.parse))

        def parse(self, **kwargs):
            calls.update(kwargs)
            return types.SimpleNamespace(stop_reason="end_turn", parsed_output=fake, model="claude-opus-4-8")

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    its = [{"id": 1, "kol_id": 1, "date": "2026-01-01", "type": "one_to_one", "topics": "",
            "notes": "Patient hospitalized after dose.", "sentiment": 0}]
    res, served = ai.synthesize_claude(its, {1: {"id": 1, "name": "Dr. X", "tier": 1}})
    assert served == "claude-opus-4-8"
    assert calls["model"] == ai.MODEL and calls["output_format"] is SynthesisResult
    assert calls["fallbacks"] == "default" and calls["betas"] == [ai.FALLBACK_BETA]
    assert "Dr. X" not in calls["messages"][0]["content"]
    assert res.themes[0].supporting_interaction_ids == [1]
    assert res.actionable_outcomes[0].supporting_interaction_ids == []
    assert any(f.flag_type == "potential_adverse_event" for f in res.compliance_flags)


def test_claude_refusal_raises(monkeypatch):
    import types

    import anthropic
    import pytest

    class FakeClient:
        def __init__(self, *a, **k):
            p = types.SimpleNamespace(
                parse=lambda **kw: types.SimpleNamespace(stop_reason="refusal", parsed_output=None))
            self.messages = p
            self.beta = types.SimpleNamespace(messages=p)

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    with pytest.raises(RuntimeError, match="refusal"):
        ai.synthesize_claude([{"id": 1, "kol_id": 1, "date": "2026-01-01", "type": "congress"}], {1: {}})


def test_fallbacks_can_be_disabled(monkeypatch):
    import types

    import anthropic

    from app.models import SynthesisResult
    used = {}

    class FakeClient:
        def __init__(self, *a, **k):
            ok = SynthesisResult(executive_summary="s", themes=[], actionable_outcomes=[],
                                 compliance_flags=[], knowledge_gaps=[])
            self.messages = types.SimpleNamespace(
                parse=lambda **kw: used.update(kw, path="ga") or types.SimpleNamespace(
                    stop_reason="end_turn", parsed_output=ok, model=ai.MODEL))
            self.beta = types.SimpleNamespace(messages=types.SimpleNamespace(parse=None))

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    monkeypatch.setattr(ai, "FALLBACKS_ENABLED", False)
    _, served = ai.synthesize_claude([{"id": 1, "kol_id": 1, "date": "d", "type": "t"}], {1: {}})
    assert used["path"] == "ga" and "fallbacks" not in used and served == ai.MODEL
