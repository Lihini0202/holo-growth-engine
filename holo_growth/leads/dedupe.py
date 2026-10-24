"""Within-file lead deduplication.

Email is the identity key (also the HubSpot upsert key); rows sharing an email are
merged field by field. Rows sharing only a phone number are flagged, not merged,
because a shared phone does not establish identity.
"""

from __future__ import annotations

from collections import defaultdict

from holo_growth.models import Lead, RowIssue


def _merge(a: Lead, b: Lead) -> Lead:
    data = a.model_dump()
    for key, value in b.model_dump().items():
        if key == "marketing_opt_in":
            # On conflicting answers, keep the more conservative value.
            data[key] = a.marketing_opt_in and b.marketing_opt_in
        elif key == "row_number":
            continue
        elif data.get(key) in (None, "") and value not in (None, ""):
            data[key] = value
        elif key == "reason" and value and data.get(key) and value not in data[key]:
            data[key] = f"{data[key]} | {value}"[:500]
    return Lead(**data)


def dedupe(leads: list[Lead]) -> tuple[list[Lead], list[RowIssue], int]:
    """Return (unique leads, issues, number of rows merged away)."""
    by_email: dict[str, Lead] = {}
    merged = 0
    for lead in leads:
        if lead.email in by_email:
            by_email[lead.email] = _merge(by_email[lead.email], lead)
            merged += 1
        else:
            by_email[lead.email] = lead

    issues: list[RowIssue] = []
    by_phone: dict[str, list[Lead]] = defaultdict(list)
    for lead in by_email.values():
        if lead.phone:
            by_phone[lead.phone].append(lead)
    for phone, group in by_phone.items():
        if len(group) > 1:
            rows = ", ".join(str(g.row_number) for g in group)
            for g in group:
                issues.append(
                    RowIssue(
                        row_number=g.row_number,
                        field="phone",
                        problem=f"shared with rows {rows}; kept separate",
                        value=phone,
                    )
                )
    return list(by_email.values()), issues, merged
