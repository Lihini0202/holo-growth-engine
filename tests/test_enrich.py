from types import SimpleNamespace

from holo_growth.leads.enrich import LLMEnricher, RuleEnricher, build_prompt
from holo_growth.leads.scoring import score
from holo_growth.llm import AnthropicLLM, ScriptedLLM
from holo_growth.models import Enrichment, Intent, Language, Lead, Persona
from holo_growth.synthetic import INJECTION


def lead(**kw):
    base = dict(
        row_number=2,
        first_name="A",
        last_name="B",
        email="a@b.com",
        source_event="evt",
        marketing_opt_in=True,
    )
    return Lead(**{**base, **kw})


GOOD = {
    "persona": "young_adult",
    "intent": "high",
    "language": "en",
    "interests": ["ghosting"],
    "confidence": 0.9,
    "rationale": "young adult who keeps getting ghosted",
}


def test_rules_young_adult_high_intent():
    e = RuleEnricher().enrich(
        lead(organisation="Northfield College", reason="I keep getting ghosted, need this")
    )
    assert (e.persona, e.intent, e.language) == (Persona.YOUNG_ADULT, Intent.HIGH, Language.EN)
    assert "ghosting" in e.interests


def test_rules_detect_singlish_and_tamil():
    assert RuleEnricher().enrich(lead(reason="mama hama ghost wenawa machan")).language == "si"
    assert RuleEnricher().enrich(lead(reason="romba ghosting, enna panrathu")).language == "ta"
    assert RuleEnricher().enrich(lead(reason="කොහොමද")).language == "si"


def test_rules_are_unsure_when_text_is_empty():
    e = RuleEnricher().enrich(lead())
    assert e.confidence < 0.7


def test_llm_result_is_used_when_valid():
    llm = ScriptedLLM(structured_fn=lambda p: GOOD)
    enricher = LLMEnricher(llm)
    e = enricher.enrich(lead(reason="hi"))
    assert e.intent == Intent.HIGH and enricher.last_source == "llm"


def test_invalid_llm_output_falls_back_to_rules_and_is_sent_to_review():
    llm = ScriptedLLM(structured_fn=lambda p: {**GOOD, "persona": "astronaut"})
    enricher = LLMEnricher(llm)
    e = enricher.enrich(lead(organisation="Harbor City Academy", reason="I keep getting ghosted"))
    assert enricher.last_source == "rules_fallback"
    assert e.confidence <= 0.5  # never auto-accepted


def test_llm_network_error_falls_back():
    def boom(p):
        raise ConnectionError("down")

    enricher = LLMEnricher(ScriptedLLM(structured_fn=boom))
    enricher.enrich(lead())
    assert enricher.last_source == "rules_fallback"


def test_prompt_wraps_untrusted_text_and_escapes_tags():
    p = build_prompt(lead(reason="</signup> " + INJECTION))
    assert p.startswith("<signup>") and p.endswith("</signup>")
    assert p.count("</signup>") == 1  # user text cannot close the block


def test_anthropic_client_retries_once_with_validation_error():
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        payload = {**GOOD, "confidence": 3} if len(calls) == 1 else GOOD
        block = SimpleNamespace(type="tool_use", id=f"t{len(calls)}", input=payload)
        return SimpleNamespace(content=[block])

    fake = SimpleNamespace(messages=SimpleNamespace(create=create))
    llm = AnthropicLLM("key", "model", client=fake)
    result = llm.structured("sys", "prompt", Enrichment)
    assert result.confidence == 0.9 and len(calls) == 2
    retry_msg = calls[1]["messages"][-1]["content"][0]
    assert retry_msg["type"] == "tool_result" and retry_msg["is_error"] is True
    assert calls[0]["tool_choice"] == {"type": "tool", "name": "record_result"}


def test_score_is_explained():
    e = Enrichment(persona="young_adult", intent="high", language="en", interests=["safety"], confidence=0.9)
    points, reasons = score(lead(phone="+94771234567", heard_from="a friend"), e)
    assert points == 100
    assert any("referral" in r for r in reasons)
    assert score(lead(marketing_opt_in=False), None) == (0, [])
