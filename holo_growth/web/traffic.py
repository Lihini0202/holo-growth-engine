"""Website traffic and sign-ups by source from the GA4 Data API.

Data is aggregate (per day, source, medium and campaign). GA4 does not expose
per-user records, so traffic cannot be attributed to individual leads.

Authentication uses a service-account key with Viewer access to the property. The
JWT is signed with google-auth and exchanged for an access token over httpx.
"""

from __future__ import annotations

import json
import logging
import random
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import httpx

from holo_growth import db

log = logging.getLogger(__name__)

DATA_API = "https://analyticsdata.googleapis.com/v1beta"
SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
DIMENSIONS = ["date", "sessionSource", "sessionMedium", "sessionCampaignName"]
PAGE = 10_000


@dataclass(frozen=True)
class TrafficRow:
    day: date
    source: str
    medium: str
    campaign: str
    sessions: int
    signups: int


class TrafficSource(Protocol):
    name: str

    def rows(self, days: int) -> list[TrafficRow]: ...


class GA4Error(RuntimeError):
    pass


def service_account_token(credentials_file: str, client: httpx.Client) -> Callable[[], str]:
    """Return a token provider for a service-account key file. Tokens refresh one minute before expiry."""
    from google.auth import crypt, jwt  # Optional dependency: the `ga4` extra

    info = json.loads(Path(credentials_file).read_text(encoding="utf-8"))
    signer = crypt.RSASigner.from_service_account_info(info)
    cache: dict[str, Any] = {"token": "", "expires": 0.0}

    def token() -> str:
        if time.time() < cache["expires"] - 60:
            return str(cache["token"])
        now = int(time.time())
        claims = {
            "iss": info["client_email"],
            "scope": SCOPE,
            "aud": info["token_uri"],
            "iat": now,
            "exp": now + 3600,
        }
        assertion = jwt.encode(signer, claims).decode()
        resp = client.post(
            info["token_uri"],
            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
        )
        if resp.status_code != 200:
            raise GA4Error(f"Google token exchange failed ({resp.status_code}): {resp.text[:200]}")
        body = resp.json()
        cache.update(token=body["access_token"], expires=now + int(body.get("expires_in", 3600)))
        return str(cache["token"])

    return token


class GA4Source:
    name = "ga4"

    def __init__(
        self,
        property_id: str,
        token: Callable[[], str],
        signup_event: str = "sign_up",
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.property_id = property_id.removeprefix("properties/")
        self.token = token
        self.signup_event = signup_event
        self._client = client or httpx.Client(timeout=60)
        self._sleep = sleep

    def rows(self, days: int) -> list[TrafficRow]:
        sessions = self._report("sessions", days)
        signups = self._report("eventCount", days, event=self.signup_event)
        merged: dict[tuple[str, str, str, str], list[int]] = {}
        for key, value in sessions.items():
            merged.setdefault(key, [0, 0])[0] = value
        for key, value in signups.items():
            merged.setdefault(key, [0, 0])[1] = value
        return [
            TrafficRow(datetime.strptime(d, "%Y%m%d").date(), src, med, camp, s, u)
            for (d, src, med, camp), (s, u) in sorted(merged.items())
        ]

    def _report(
        self, metric: str, days: int, event: str | None = None
    ) -> dict[tuple[str, str, str, str], int]:
        body: dict[str, Any] = {
            "dateRanges": [{"startDate": f"{days}daysAgo", "endDate": "yesterday"}],
            "dimensions": [{"name": d} for d in DIMENSIONS],
            "metrics": [{"name": metric}],
            "limit": PAGE,
        }
        if event:
            body["dimensionFilter"] = {
                "filter": {"fieldName": "eventName", "stringFilter": {"matchType": "EXACT", "value": event}}
            }
        out: dict[tuple[str, str, str, str], int] = {}
        offset = 0
        while True:
            data = self._post({**body, "offset": offset})
            for row in data.get("rows", []):
                d, src, med, camp = (v["value"] for v in row["dimensionValues"])
                out[(d, src, med, camp)] = int(float(row["metricValues"][0]["value"]))
            offset += PAGE
            if offset >= int(data.get("rowCount", 0)):
                return out

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{DATA_API}/properties/{self.property_id}:runReport"
        for attempt in range(1, 5):
            resp = self._client.post(url, json=body, headers={"Authorization": f"Bearer {self.token()}"})
            if resp.status_code == 200:
                return dict(resp.json())
            if resp.status_code in (429, 500, 502, 503) and attempt < 4:
                self._sleep(2**attempt)
                continue
            try:
                message = resp.json().get("error", {}).get("message", resp.text)
            except ValueError:
                message = resp.text
            raise GA4Error(f"GA4 {resp.status_code}: {str(message)[:300]}")
        raise GA4Error("GA4: gave up after retries")


# Synthetic channel mix; event QR codes use utm_medium=qr.
_CHANNELS = [
    ("instagram", "social", "(not set)", 52, 0.09),
    ("google", "organic", "(organic)", 40, 0.06),
    ("(direct)", "(none)", "(not set)", 34, 0.05),
    ("tiktok", "social", "(not set)", 30, 0.07),
    ("safety_workshop", "qr", "colombo_workshop", 9, 0.22),
    ("chatgpt.com", "referral", "(not set)", 6, 0.12),
    ("facebook", "social", "(not set)", 14, 0.04),
    ("perplexity.ai", "referral", "(not set)", 2, 0.10),
]


class SyntheticTrafficSource:
    """Deterministic synthetic traffic for offline runs, labelled 'synthetic'."""

    name = "synthetic"

    def __init__(self, seed: int = 11, today: date | None = None) -> None:
        self.seed = seed
        self.today = today or datetime.now(UTC).date()

    def rows(self, days: int) -> list[TrafficRow]:
        rng = random.Random(self.seed)
        out = []
        for back in range(days, 0, -1):
            day = self.today - timedelta(days=back)
            weekend = 1.25 if day.weekday() >= 5 else 1.0
            growth = 1 + 0.6 * (days - back) / max(days, 1)  # Linear growth across the window
            for source, medium, campaign, base, rate in _CHANNELS:
                sessions = max(0, int(rng.gauss(base * weekend * growth, base * 0.2)))
                signups = sum(1 for _ in range(sessions) if rng.random() < rate)
                if sessions:
                    out.append(TrafficRow(day, source, medium, campaign, sessions, signups))
        return out


@dataclass
class WebSyncReport:
    run_id: int
    status: str
    provider: str = ""
    days: int = 0
    rows: int = 0
    sessions: int = 0
    signups: int = 0
    top_channel: str | None = None

    def summary(self) -> str:
        rate = f"{100 * self.signups / self.sessions:.1f}%" if self.sessions else "n/a"
        top = f" Top channel for sign-ups: {self.top_channel}." if self.top_channel else ""
        return (
            f"Website ({self.provider}, last {self.days} days): {self.sessions} sessions, "
            f"{self.signups} sign-ups ({rate}).{top}"
        )


def run_web_sync(conn: sqlite3.Connection, source: TrafficSource, days: int = 28) -> WebSyncReport:
    """Replace stored rows for the sync window with the source's current data.

    GA4 revises recent days, so the window is rewritten rather than appended.
    """
    run_id = db.start_run(conn, "web", source.name)
    report = WebSyncReport(run_id=run_id, status="running", provider=source.name, days=days)
    try:
        rows = source.rows(days)
        end = datetime.now(UTC).date() - timedelta(days=1)
        start = end - timedelta(days=days - 1)
        with db.transaction(conn):
            conn.execute(
                "DELETE FROM web_traffic WHERE provider != ?", (source.name,)
            )  # Never mix synthetic and real data
            conn.execute(
                "DELETE FROM web_traffic WHERE day BETWEEN ? AND ?", (start.isoformat(), end.isoformat())
            )
            conn.executemany(
                """
                INSERT INTO web_traffic
                    (day, source, medium, campaign, sessions, signups, provider, synced_run)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(day, source, medium, campaign) DO UPDATE SET sessions = excluded.sessions,
                    signups = excluded.signups, provider = excluded.provider, synced_run = excluded.synced_run
                """,
                [
                    (
                        r.day.isoformat(),
                        r.source,
                        r.medium,
                        r.campaign,
                        r.sessions,
                        r.signups,
                        source.name,
                        run_id,
                    )
                    for r in rows
                ],
            )
        report.rows = len(rows)
        report.sessions = sum(r.sessions for r in rows)
        report.signups = sum(r.signups for r in rows)
        by_channel: dict[str, int] = {}
        for r in rows:
            by_channel[f"{r.source} / {r.medium}"] = by_channel.get(f"{r.source} / {r.medium}", 0) + r.signups
        if by_channel and max(by_channel.values()) > 0:
            report.top_channel = max(by_channel, key=lambda k: by_channel[k])
        report.status = "ok"
    except Exception as exc:
        db.finish_run(conn, run_id, "failed", json.dumps({"error": str(exc)[:500]}))
        log.exception("web traffic sync failed")
        raise
    db.finish_run(conn, run_id, "ok", json.dumps(report.__dict__, default=str))
    return report
