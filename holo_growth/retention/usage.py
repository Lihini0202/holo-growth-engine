"""Read Holo product-usage data.

Only usage metadata is read: who ran an analysis, when, and whether they have
recorded the outcome. Risk scores, partner details and chat text are never
selected. A CRM has no business holding someone's dating conversations.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

import httpx

log = logging.getLogger(__name__)


@dataclass
class UserActivity:
    user_id: str
    email: str | None
    first_name: str | None
    signed_up: datetime
    analyses: list[datetime] = field(default_factory=list)
    outcome_recorded: list[bool] = field(default_factory=list)  # parallel to analyses
    marketing_opt_in: bool | None = None  # None = never asked
    marketing_opt_in_at: datetime | None = None  # when the user last changed the toggle


class UsageSource(Protocol):
    name: str

    def users(self) -> list[UserActivity]: ...


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class SupabaseUsageSource:
    """Reads Holo's Supabase with the service key, via PostgREST and the auth admin API."""

    name = "supabase"
    PAGE = 1000

    def __init__(self, url: str, service_key: str, client: httpx.Client | None = None) -> None:
        self._base = url.rstrip("/")
        self._headers = {"apikey": service_key, "Authorization": f"Bearer {service_key}"}
        self._client = client or httpx.Client(timeout=30)

    def _get(self, path: str, params: dict[str, str]) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        offset = 0
        while True:
            resp = self._client.get(
                f"{self._base}{path}",
                params={**params, "limit": str(self.PAGE), "offset": str(offset)},
                headers=self._headers,
            )
            resp.raise_for_status()
            page = resp.json()
            rows.extend(page)
            if len(page) < self.PAGE:
                return rows
            offset += self.PAGE

    def _auth_users(self) -> list[dict[str, object]]:
        users: list[dict[str, object]] = []
        page = 1
        while True:
            resp = self._client.get(
                f"{self._base}/auth/v1/admin/users",
                params={"page": str(page), "per_page": "200"},
                headers=self._headers,
            )
            resp.raise_for_status()
            batch = resp.json().get("users", [])
            users.extend(batch)
            if len(batch) < 200:
                return users
            page += 1

    def _consents(self) -> dict[str, tuple[bool, datetime]]:
        """Latest marketing choice per user, if the marketing_consents table exists."""
        try:
            rows = self._get(
                "/rest/v1/marketing_consents",
                {"select": "user_id,granted,recorded_at", "order": "recorded_at.asc"},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                log.warning(
                    "marketing_consents table not found; no Holo user is marketable "
                    "until it exists (see integrations/holo/marketing_consent.sql)"
                )
                return {}
            raise
        latest: dict[str, tuple[bool, datetime]] = {}
        for r in rows:
            latest[str(r["user_id"])] = (bool(r["granted"]), _parse_ts(str(r["recorded_at"])))
        return latest

    def users(self) -> list[UserActivity]:
        logs = self._get(
            "/rest/v1/analysis_logs",
            {
                # Deliberately narrow select: no risk_score, no partner fields.
                "select": "auth_user_id,created_at,actual_outcome",
                "auth_user_id": "not.is.null",
            },
        )
        profiles = {
            str(p["id"]): p.get("first_name")
            for p in self._get("/rest/v1/user_profiles", {"select": "id,first_name"})
        }
        consents = self._consents()

        by_user: dict[str, UserActivity] = {}
        for u in self._auth_users():
            uid = str(u["id"])
            choice = consents.get(uid)
            by_user[uid] = UserActivity(
                user_id=uid,
                email=(str(u.get("email") or "") or None),
                first_name=profiles.get(uid),  # type: ignore[arg-type]
                signed_up=_parse_ts(str(u["created_at"])),
                marketing_opt_in=choice[0] if choice else None,
                marketing_opt_in_at=choice[1] if choice else None,
            )
        for row in sorted(logs, key=lambda r: str(r["created_at"])):
            act = by_user.get(str(row["auth_user_id"]))
            if act is None:
                continue
            act.analyses.append(_parse_ts(str(row["created_at"])))
            act.outcome_recorded.append(row.get("actual_outcome") is not None)
        return list(by_user.values())


class SyntheticUsageSource:
    name = "synthetic"

    def __init__(
        self,
        n: int = 200,
        seed: int = 11,
        now: datetime | None = None,
        convert_emails: list[str] | None = None,
    ) -> None:
        from holo_growth.synthetic import make_holo_users

        self._users = make_holo_users(n, seed, now, convert_emails)

    def users(self) -> list[UserActivity]:
        return [
            UserActivity(
                user_id=u.user_id,
                email=u.email,
                first_name=u.first_name,
                signed_up=u.signed_up,
                analyses=[t for t, _ in u.analyses],
                outcome_recorded=[bool(o) for _, o in u.analyses],
                marketing_opt_in=u.marketing_opt_in,
            )
            for u in self._users
        ]
