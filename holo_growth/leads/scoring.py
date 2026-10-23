"""Lead score: a transparent points system the growth team can read and argue with.

Kept as data rather than code so a non-engineer can change weights in a PR.
"""

from __future__ import annotations

from holo_growth.models import Enrichment, Intent, Lead, Persona

INTENT_POINTS: dict[Intent, int] = {Intent.HIGH: 40, Intent.MEDIUM: 20, Intent.LOW: 0}
PERSONA_POINTS: dict[Persona, int] = {
    Persona.STUDENT: 15,
    Persona.YOUNG_PROFESSIONAL: 15,
    Persona.OTHER: 5,
}
OPTED_IN = 20
HAS_PHONE = 5
REFERRAL = 10  # heard about Holo from a friend: the strongest acquisition channel
CORE_INTEREST = 10  # ghosting or safety

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
