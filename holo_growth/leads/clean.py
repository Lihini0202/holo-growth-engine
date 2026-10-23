"""Normalise and validate raw signup rows.

Rules are deliberately conservative: a value we can't confidently fix is
dropped (set to None) and reported, never guessed.
"""

from __future__ import annotations

import re
from importlib import resources

from holo_growth.models import Lead, RawLead, RowIssue

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

# Common typos seen on event signup sheets. Only whole-domain matches are fixed.
DOMAIN_TYPOS = {
    "gmial.com": "gmail.com",
    "gmai.com": "gmail.com",
    "gamil.com": "gmail.com",
    "gmail.co": "gmail.com",
    "gmail.con": "gmail.com",
    "gmaill.com": "gmail.com",
    "yahoo.co": "yahoo.com",
    "yaho.com": "yahoo.com",
    "hotmial.com": "hotmail.com",
    "hotmail.co": "hotmail.com",
    "outlok.com": "outlook.com",
}

TRUTHY = {"yes", "y", "true", "1", "x", "✓", "checked", "agree", "i agree", "on", "ok"}

CITY_ALIASES = {
    "cmb": "Colombo",
    "colombo 3": "Colombo",
    "colombo 7": "Colombo",
    "kandy city": "Kandy",
    "galle fort": "Galle",
    "kurunagala": "Kurunegala",
    "kurunegla": "Kurunegala",
    "jaffna town": "Jaffna",
    "nugegoda": "Nugegoda",
    "moratuwa": "Moratuwa",
}


def _disposable_domains() -> frozenset[str]:
    text = resources.files("holo_growth.data").joinpath("disposable_domains.txt").read_text()
    return frozenset(line.strip() for line in text.splitlines() if line.strip())


DISPOSABLE = _disposable_domains()


def clean_email(value: str) -> tuple[str | None, str | None]:
    """Return (email, problem)."""
    v = value.strip().strip("<>").replace(" ", "").lower()
    v = v.rstrip(".,;")
    if not v:
        return None, "missing"
    if not EMAIL_RE.match(v):
        return None, "invalid format"
    local, domain = v.rsplit("@", 1)
    domain = DOMAIN_TYPOS.get(domain, domain)
    if domain in DISPOSABLE:
        return None, "disposable domain"
    return f"{local}@{domain}", None


def clean_phone(value: str) -> str | None:
    """Normalise Sri Lankan numbers to E.164 (+94XXXXXXXXX); keep other valid E.164 numbers."""
    digits = re.sub(r"[^\d+]", "", value)
    if not digits:
        return None
    if digits.startswith("+"):
        rest = digits[1:]
        return digits if rest.isdigit() and 8 <= len(rest) <= 15 else None
    if digits.startswith("0094"):
        digits = digits[4:]
    elif digits.startswith("94") and len(digits) == 11:
        digits = digits[2:]
    if digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 9 and digits.isdigit():
        return "+94" + digits
    return None


def _title(part: str) -> str:
    # "o'neil" -> "O'Neil", "de silva" handled per word by caller
    return "-".join(
        "'".join(p[:1].upper() + p[1:].lower() for p in seg.split("'")) for seg in part.split("-")
    )


LOWER_PARTICLES = {"de", "da", "van", "von", "bin", "binti"}


def split_name(value: str) -> tuple[str, str]:
    words = [w for w in re.split(r"\s+", value.strip()) if w]
    if not words:
        return "", ""
    words = [w.lower() if w.lower() in LOWER_PARTICLES and i > 0 else _title(w) for i, w in enumerate(words)]
    if len(words) == 1:
        return words[0], ""
    return words[0], " ".join(words[1:])


def clean_city(value: str) -> str | None:
    v = re.sub(r"\s+", " ", value.strip())
    if not v:
        return None
    alias = CITY_ALIASES.get(v.lower())
    if alias:
        return alias
    return " ".join(w[:1].upper() + w[1:].lower() for w in v.split(" "))


def clean_age(value: str) -> tuple[int | None, str | None]:
    m = re.search(r"\d{1,3}", value)
    if not m:
        return None, ("unparseable" if value.strip() else None)
    age = int(m.group())
    if age < 18:
        return None, "under 18"
    if age > 100:
        return None, "implausible"
    return age, None


def clean_row(raw: RawLead, source_event: str) -> tuple[Lead | None, list[RowIssue]]:
    issues: list[RowIssue] = []
    email, problem = clean_email(raw.email)
    if problem:
        issues.append(RowIssue(row_number=raw.row_number, field="email", problem=problem, value=raw.email))
        return None, issues

    age, age_problem = clean_age(raw.age)
    if age_problem == "under 18":
        # Holo is an adult dating-safety app. Minors are rejected outright and
        # their details are not stored anywhere, including the issue log.
        issues.append(
            RowIssue(row_number=raw.row_number, field="age", problem="under 18, row discarded", value="")
        )
        return None, issues
    if age_problem:
        issues.append(RowIssue(row_number=raw.row_number, field="age", problem=age_problem, value=raw.age))

    first, last = split_name(raw.full_name)
    if not first:
        issues.append(RowIssue(row_number=raw.row_number, field="full_name", problem="missing"))

    phone = clean_phone(raw.phone) if raw.phone else None
    if raw.phone and phone is None:
        issues.append(
            RowIssue(row_number=raw.row_number, field="phone", problem="could not normalise", value=raw.phone)
        )

    lead = Lead(
        row_number=raw.row_number,
        first_name=first,
        last_name=last,
        email=email or "",
        phone=phone,
        organisation=raw.organisation.strip() or None,
        city=clean_city(raw.city),
        age=age,
        heard_from=raw.heard_from.strip() or None,
        reason=raw.reason.strip()[:500] or None,
        marketing_opt_in=raw.marketing_opt_in.strip().lower() in TRUTHY,
        source_event=source_event,
    )
    return lead, issues
