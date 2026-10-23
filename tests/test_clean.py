import pytest

from holo_growth.leads.clean import (
    clean_age,
    clean_city,
    clean_email,
    clean_phone,
    clean_row,
    split_name,
)
from holo_growth.models import RawLead


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("  Kasun.Perera@GMAIL.com ", "kasun.perera@gmail.com"),
        ("dilini@gmial.com", "dilini@gmail.com"),
        ("a.b@hotmail.co", "a.b@hotmail.com"),
        ("<ruwan@outlook.com>", "ruwan@outlook.com"),
        ("x@uni.ac.lk.", "x@uni.ac.lk"),
    ],
)
def test_clean_email_normalises_and_fixes_typos(raw, expected):
    assert clean_email(raw) == (expected, None)


@pytest.mark.parametrize(
    "raw,problem",
    [
        ("", "missing"),
        ("not-an-email", "invalid format"),
        ("a@b", "invalid format"),
        ("someone@mailinator.com", "disposable domain"),
    ],
)
def test_clean_email_rejects(raw, problem):
    assert clean_email(raw) == (None, problem)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("077 123 4567", "+94771234567"),
        ("+94771234567", "+94771234567"),
        ("94 77-123-4567", "+94771234567"),
        ("0094771234567", "+94771234567"),
        ("(077) 1234567", "+94771234567"),
        ("771234567", "+94771234567"),
        ("+447700900123", "+447700900123"),
        ("12345", None),
        ("call me", None),
    ],
)
def test_clean_phone(raw, expected):
    assert clean_phone(raw) == expected


def test_split_name_handles_case_and_particles():
    assert split_name("ARJUN DE SILVA") == ("Arjun", "de Silva")
    assert split_name("nethmi") == ("Nethmi", "")
    assert split_name("  mary-jane  o'neil ") == ("Mary-Jane", "O'Neil")


def test_clean_city_aliases():
    assert clean_city("cmb") == "Colombo"
    assert clean_city("kurunagala") == "Kurunegala"
    assert clean_city("NEGOMBO") == "Negombo"
    assert clean_city("") is None


def test_clean_age():
    assert clean_age("23") == (23, None)
    assert clean_age("23 years") == (23, None)
    assert clean_age("16") == (None, "under 18")
    assert clean_age("") == (None, None)


def test_minor_is_discarded_without_storing_details():
    raw = RawLead(row_number=5, full_name="Young Person", email="young@gmail.com", age="16")
    lead, issues = clean_row(raw, "evt")
    assert lead is None
    assert issues[0].problem.startswith("under 18")
    assert issues[0].value == ""  # nothing identifying in the issue log


def test_opt_in_variants():
    for value, expected in [("Yes", True), ("TRUE", True), ("✓", True), ("No", False), ("", False)]:
        lead, _ = clean_row(
            RawLead(row_number=2, full_name="A B", email="a@b.com", marketing_opt_in=value), "evt"
        )
        assert lead is not None and lead.marketing_opt_in is expected
