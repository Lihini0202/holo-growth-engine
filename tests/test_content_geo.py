import pytest

from holo_growth.content.campaigns import TEMPLATES, draft_campaign
from holo_growth.content.checks import check_copy
from holo_growth.geo.tracker import CannedProvider, GeoConfig, detect, run_geo
from holo_growth.llm import ScriptedLLM

ALLOWED = {"holo-tan.vercel.app"}
BODY = (
    "Hi {{first_name}}, "
    + "this is a calm and friendly note about Holo. " * 6
    + "{{app_link}} {{unsubscribe_link}}"
)


@pytest.mark.parametrize("segment", sorted(TEMPLATES))
def test_every_template_passes_its_own_checks(segment):
    subject, body = TEMPLATES[segment]
    result = check_copy(subject, body, ALLOWED)
    assert result.passed, result.errors


def test_clean_copy_passes():
    assert check_copy("A quick note", BODY, ALLOWED).passed


@pytest.mark.parametrize(
    "bad,expect",
    [
        ("You'll never get ghosted again.", "promises"),
        ("Guaranteed results, 100% of the time.", "guarantee"),
        ("Last chance to join!", "urgency"),
        ("Holo is 92% accurate.", "accuracy"),
        ("They're going to ghost you.", "fear"),
    ],
)
def test_tone_rules(bad, expect):
    r = check_copy("Subject", BODY + " " + bad, ALLOWED)
    assert not r.passed and any(expect in e for e in r.errors)


@pytest.mark.parametrize(
    "leak",
    [
        "Your conversation with Nimal looks high risk.",
        "Your risk score went up this week.",
        "Hi {{partner_name}} might ghost you",
    ],
)
def test_privacy_rules(leak):
    r = check_copy("Subject", BODY + " " + leak, ALLOWED)
    assert any(e.startswith("privacy") for e in r.errors)


def test_links_must_be_allowlisted_and_https():
    r = check_copy("S", BODY + " https://bit.ly/x http://holo-tan.vercel.app", ALLOWED)
    assert any("bit.ly" in e for e in r.errors)
    assert any("insecure" in e for e in r.errors)


def test_unsubscribe_link_required_and_length_limits():
    r = check_copy("x" * 70, "too short", ALLOWED)
    assert "missing unsubscribe link" in r.errors
    assert any("subject" in e for e in r.errors) and any("words" in e for e in r.errors)


def test_non_english_always_needs_review():
    assert not check_copy("S", BODY, ALLOWED, language="si").passed


def test_llm_tone_review_can_only_add_errors():
    llm = ScriptedLLM(
        structured_fn=lambda p: {
            "kind_and_non_judgmental": False,
            "reveals_private_information": False,
            "notes": "condescending",
        }
    )
    r = check_copy("A quick note", BODY, ALLOWED, llm=llm)
    assert not r.passed and "condescending" in r.errors[0]


def test_draft_with_llm_that_breaks_rules_goes_to_review(conn):
    llm = ScriptedLLM(
        structured_fn=lambda p: (
            {"subject": "Last chance!", "body": BODY}
            if "Brief" in p
            else {"kind_and_non_judgmental": True, "reveals_private_information": False}
        )
    )
    d = draft_campaign(conn, "at_risk", "en", llm, ALLOWED)
    assert d.status == "needs_review" and d.source == "llm"
    assert conn.execute("SELECT COUNT(*) FROM review_queue WHERE kind='content'").fetchone()[0] == 1


def test_unknown_segment(conn):
    with pytest.raises(ValueError):
        draft_campaign(conn, "vip", "en")


# -- GEO -------------------------------------------------------------------------

CFG = GeoConfig.load()


def test_detect_brand_and_position():
    m = detect("Try Rizz for replies, or Holo to see if a chat is cooling.", CFG)
    assert m.mentioned and m.position == 2 and m.competitors == ["Rizz"]


def test_detect_ignores_lookalike_words():
    m = detect("A holographic display and hologram tech; holo means whole.", CFG)
    assert not m.mentioned and m.position is None


def test_run_geo_records_results_and_survives_provider_errors(conn):
    class Broken:
        name = "broken"

        def ask(self, prompt):
            raise TimeoutError

    report = run_geo(conn, [CannedProvider(), Broken()], CFG)
    assert report.checks == len(CFG.prompts) and report.errors == len(CFG.prompts)
    rows = conn.execute("SELECT COUNT(*) FROM geo_results").fetchone()[0]
    assert rows == len(CFG.prompts)
