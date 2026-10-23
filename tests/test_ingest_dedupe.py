from openpyxl import Workbook

from holo_growth.leads.clean import clean_row
from holo_growth.leads.dedupe import dedupe
from holo_growth.leads.ingest import ingest, map_headers


def test_header_synonyms_map_and_unknown_columns_are_reported():
    mapping, unmapped = map_headers(
        ["E-mail", "Full Name", "Mobile No", "University", "Send me updates", "Timestamp"]
    )
    assert mapping == {
        "E-mail": "email",
        "Full Name": "full_name",
        "Mobile No": "phone",
        "University": "organisation",
        "Send me updates": "marketing_opt_in",
    }
    assert unmapped == ["Timestamp"]


def test_first_and_last_name_columns_are_joined(write_csv):
    p = write_csv("First Name,Last Name,Email\nKasun,Perera,k@gmail.com\n")
    lead = ingest(p).leads[0]
    assert lead.full_name == "Kasun Perera"


def test_semicolon_csv_and_blank_rows(write_csv):
    p = write_csv("Name;Email;Age\nA One;a@x.com;22\n;;\nB Two;b@x.com;30\n")
    result = ingest(p)
    assert [lead.email for lead in result.leads] == ["a@x.com", "b@x.com"]
    assert [lead.row_number for lead in result.leads] == [2, 4]


def test_missing_email_column_warns(write_csv):
    result = ingest(write_csv("Name,Phone\nA,0771234567\n"))
    assert any("no email column" in w for w in result.warnings)


def test_xlsx_is_read(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.append(["Email Address", "Name", "Age"])
    ws.append(["x@gmail.com", "X Y", 25])
    p = tmp_path / "signups.xlsx"
    wb.save(p)
    leads = ingest(p).leads
    assert leads[0].email == "x@gmail.com" and leads[0].age == "25"


def test_same_file_gives_same_hash(write_csv):
    p = write_csv("Email\na@x.com\n")
    assert ingest(p).input_hash == ingest(p).input_hash


def _lead(row, email, **kw):
    from holo_growth.models import RawLead

    lead, _ = clean_row(RawLead(row_number=row, email=email, full_name=kw.pop("name", "A B"), **kw), "evt")
    return lead


def test_dedupe_merges_by_email_and_keeps_most_complete():
    a = _lead(2, "a@x.com", phone="", city="Kandy", marketing_opt_in="yes")
    b = _lead(3, "A@X.COM", phone="0771234567", city="", marketing_opt_in="yes")
    unique, issues, merged = dedupe([a, b])
    assert merged == 1 and len(unique) == 1
    assert unique[0].phone == "+94771234567" and unique[0].city == "Kandy"
    assert unique[0].marketing_opt_in is True


def test_dedupe_conflicting_consent_is_conservative():
    a = _lead(2, "a@x.com", marketing_opt_in="yes")
    b = _lead(3, "a@x.com", marketing_opt_in="no")
    unique, _, _ = dedupe([a, b])
    assert unique[0].marketing_opt_in is False


def test_shared_phone_is_flagged_not_merged():
    a = _lead(2, "a@x.com", phone="0771234567")
    b = _lead(3, "b@x.com", phone="077 123 4567")
    unique, issues, merged = dedupe([a, b])
    assert len(unique) == 2 and merged == 0
    assert {i.row_number for i in issues} == {2, 3}
