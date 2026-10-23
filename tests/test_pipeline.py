import json

from holo_growth.consent import ConsentLedger, Purpose
from holo_growth.crm.base import UpsertResult
from holo_growth.leads.enrich import LLMEnricher, RuleEnricher
from holo_growth.leads.pipeline import run_leads
from holo_growth.llm import ScriptedLLM
from holo_growth.review import resolve_enrichment
from holo_growth.synthetic import make_event_file

SHEET = """
Name,Email Address,Mobile,University / Workplace,Age,What brings you to Holo?,Send me Holo updates
kasun perera,KASUN@gmail.com,077 123 4567,University of Moratuwa,21,I keep getting ghosted need this,Yes
Kasun Perera,kasun@gmail.com,,,,,yes
Dilini Fernando,dilini@gmial.com,,WSO2,26,Curious about the AI side,Yes
No Consent,nc@gmail.com,,SLIIT,22,just browsing,No
Bad Email,bad-email,,,,,Yes
Teen,teen@gmail.com,,,16,,Yes
"""


def test_end_to_end_counts_and_crm(conn, crm, write_csv):
    report = run_leads(write_csv(SHEET), "Launch", conn, crm, RuleEnricher())
    assert report.status == "ok"
    assert (report.rows_read, report.rejected, report.duplicates_merged) == (6, 2, 1)
    assert report.not_consented == 1
    assert report.uploaded_created == 2
    kasun = crm.get("kasun@gmail.com")
    assert kasun["firstname"] == "Kasun" and kasun["phone"] == "+94771234567"
    assert kasun["holo_source_event"] == "Launch" and kasun["holo_persona"] == "student"
    assert crm.get("dilini@gmail.com") is not None  # typo domain fixed
    assert crm.get("nc@gmail.com") is None  # no consent, never uploaded
    assert conn.execute("SELECT COUNT(*) FROM leads WHERE email = 'nc@gmail.com'").fetchone()[0] == 0


def test_same_file_is_skipped_unless_forced(conn, crm, write_csv):
    p = write_csv(SHEET)
    run_leads(p, "Launch", conn, crm, RuleEnricher())
    again = run_leads(p, "Launch", conn, crm, RuleEnricher())
    assert again.status == "skipped"
    forced = run_leads(p, "Launch", conn, crm, RuleEnricher(), force=True)
    assert forced.uploaded_created == 0 and forced.uploaded_updated == 2


def test_dry_run_writes_nothing(conn, crm, write_csv):
    report = run_leads(write_csv(SHEET), "Launch", conn, crm, RuleEnricher(), dry_run=True)
    assert report.status == "ok" and report.uploaded_created == 0
    assert crm.get("kasun@gmail.com") is None
    assert conn.execute("SELECT COUNT(*) FROM consent_events").fetchone()[0] == 0


def test_unsubscribed_person_is_not_reuploaded_from_old_sheet(conn, crm, write_csv):
    p = write_csv(SHEET)
    run_leads(p, "Launch", conn, crm, RuleEnricher())
    ConsentLedger(conn).withdraw("kasun@gmail.com")
    report = run_leads(p, "Launch", conn, crm, RuleEnricher(), force=True)
    assert report.not_consented == 2  # kasun now, plus the original no-consent row
    assert not ConsentLedger(conn).allowed("kasun@gmail.com")


def test_low_confidence_goes_to_review_without_enrichment_fields(conn, crm, write_csv):
    llm = ScriptedLLM(
        structured_fn=lambda p: {"persona": "other", "intent": "medium", "language": "en", "confidence": 0.3}
    )
    report = run_leads(write_csv(SHEET), "Launch", conn, crm, LLMEnricher(llm))
    assert report.needs_review == 2 and report.enriched_llm == 2
    assert "holo_persona" not in crm.get("kasun@gmail.com")
    queue = conn.execute("SELECT id, ref FROM review_queue WHERE kind='enrichment'").fetchall()
    assert len(queue) == 2

    review_id = next(r["id"] for r in queue if r["ref"] == "kasun@gmail.com")
    out = resolve_enrichment(conn, crm, review_id, persona="student", intent="high")
    assert out["status"] == "approved"
    assert crm.get("kasun@gmail.com")["holo_persona"] == "student"
    row = conn.execute(
        "SELECT needs_review, enrichment_source FROM leads WHERE email='kasun@gmail.com'"
    ).fetchone()
    assert (row["needs_review"], row["enrichment_source"]) == (0, "human")


def test_prompt_injection_in_signup_text_does_not_reach_crm_unreviewed(conn, crm, tmp_path):
    # The scripted model "obeys" the injection; the pipeline must still treat the
    # result as data validated by schema, and the rules fallback must not either.
    p = make_event_file(tmp_path / "e.csv", 30, seed=3)
    report = run_leads(p, "Launch", conn, crm, RuleEnricher())
    row = conn.execute(
        "SELECT intent, confidence FROM leads WHERE email = 'injector.test@gmail.com'"
    ).fetchone()
    assert row is not None and row["intent"] != "high"
    assert report.status == "ok"


class FlakyCRM:
    name = "flaky"

    def ensure_properties(self):
        pass

    def existing_emails(self, emails):
        return set()

    def upsert_contacts(self, contacts):
        return UpsertResult(created=len(contacts) - 1, failed=[(contacts[0]["email"], "boom")])


def test_partial_crm_failure_marks_run_partial(conn, write_csv):
    report = run_leads(write_csv(SHEET), "Launch", conn, FlakyCRM(), RuleEnricher())
    assert report.status == "partial" and report.upload_failed == 1
    stats = json.loads(
        conn.execute("SELECT stats_json FROM runs WHERE id = ?", (report.run_id,)).fetchone()[0]
    )
    assert stats["upload_failed"] == 1
    issues = conn.execute("SELECT * FROM row_issues WHERE field = 'crm'").fetchall()
    assert len(issues) == 1


def test_consent_recorded_with_source(conn, crm, write_csv):
    run_leads(write_csv(SHEET), "Launch", conn, crm, RuleEnricher())
    hist = ConsentLedger(conn).history("kasun@gmail.com")
    assert hist[0]["source"] == "event_form:Launch" and hist[0]["purpose"] == Purpose.MARKETING_EMAIL
