"""Lead scoring: a transparent, additive points table.

Weights are defined as data so they can be reviewed and adjusted without logic
changes.
"""

from __future__ import annotations

from holo_growth.models import Enrichment, Intent, Lead, Persona

INTENT_POINTS: dict[Intent, int] = {Intent.HIGH: 40, Intent.MEDIUM: 20, Intent.LOW: 0}
PERSONA_POINTS: dict[Persona, int] = {
    Persona.YOUNG_ADULT: 15,
    Persona.YOUNG_PROFESSIONAL: 15,
    Persona.OTHER: 5,
}
OPTED_IN = 20
HAS_PHONE = 5
REFERRAL = 10  # Referred by a friend
CORE_INTEREST = 10  # Interest in ghosting detection or safety

HOT_THRESHOLD = 70


def score(lead: Lead, enrichment: Enrichment | None) -> tuple[int, list[str]]:
    points = 0
    reasons: list[str] = []
    if enrichment:
        p = INTENT_POINTS[enrichment.intent]
        points += p
        reasons.append(f"intent {enrichment.intent.value} +{p}")
        p = PERSONA_POINTS[enrichment.persona]
        points += p
        reasons.append(f"persona {enrichment.persona.value} +{p}")
        if {"ghosting", "safety"} & set(enrichment.interests):
            points += CORE_INTEREST
            reasons.append(f"core interest +{CORE_INTEREST}")
    if lead.marketing_opt_in:
        points += OPTED_IN
        reasons.append(f"opted in +{OPTED_IN}")
    if lead.phone:
        points += HAS_PHONE
        reasons.append(f"phone +{HAS_PHONE}")
    if lead.heard_from and any(w in lead.heard_from.lower() for w in ("friend", "referr", "word")):
        points += REFERRAL
        reasons.append(f"referral +{REFERRAL}")
    return min(points, 100), reasons
