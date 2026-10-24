"""Lifecycle stage classification from Holo usage. Pure functions.

Outcome thresholds match the Holo app's follow-up schedule (FOLLOW_UP_AFTER_DAYS = 14
and FOLLOW_UP_EXPIRES_DAYS = 60 in tasks.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from holo_growth.models import LifecycleStage
from holo_growth.retention.usage import UserActivity

ENGAGED_MIN_ANALYSES_14D = 3
AT_RISK_AFTER_DAYS = 14
DORMANT_AFTER_DAYS = 30
OUTCOME_RIPE_DAYS = 14
OUTCOME_EXPIRES_DAYS = 60


@dataclass(frozen=True)
class LifecycleSnapshot:
    user_id: str
    stage: LifecycleStage
    analyses_14d: int
    analyses_total: int
    pending_validations: int
    last_active: datetime
    days_inactive: int


def pending_validations(act: UserActivity, now: datetime) -> int:
    """Count analyses inside the outcome window whose outcome has not been recorded."""
    ripe = now - timedelta(days=OUTCOME_RIPE_DAYS)
    stale = now - timedelta(days=OUTCOME_EXPIRES_DAYS)
    return sum(
        1
        for t, done in zip(act.analyses, act.outcome_recorded, strict=True)
        if not done and stale < t <= ripe
    )


def classify(act: UserActivity, now: datetime, consent_withdrawn: bool = False) -> LifecycleSnapshot:
    last_active = max(act.analyses) if act.analyses else act.signed_up
    days_inactive = max(0, (now - last_active).days)
    recent = sum(1 for t in act.analyses if t > now - timedelta(days=14))

    if consent_withdrawn:
        stage = LifecycleStage.OPTED_OUT
    elif days_inactive >= DORMANT_AFTER_DAYS:
        stage = LifecycleStage.DORMANT
    elif days_inactive >= AT_RISK_AFTER_DAYS:
        stage = LifecycleStage.AT_RISK
    elif recent >= ENGAGED_MIN_ANALYSES_14D:
        stage = LifecycleStage.ENGAGED
    elif act.analyses:
        stage = LifecycleStage.ACTIVATED
    else:
        stage = LifecycleStage.NEW

    return LifecycleSnapshot(
        user_id=act.user_id,
        stage=stage,
        analyses_14d=recent,
        analyses_total=len(act.analyses),
        pending_validations=pending_validations(act, now),
        last_active=last_active,
        days_inactive=days_inactive,
    )
