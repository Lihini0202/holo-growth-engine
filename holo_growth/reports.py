"""Reporting: plain SQL over the local store, shared by the CLI, MCP server and dashboard."""

from __future__ import annotations

import json
import sqlite3
from typing import Any


def _rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def recent_runs(conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
    rows = _rows(
        conn,
        "SELECT id, kind, label, status, started_at, finished_at, stats_json "
        "FROM runs ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    for r in rows:
        r["stats"] = json.loads(r.pop("stats_json") or "{}")
    return rows


def event_funnel(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Per event: leads uploaded, hot leads, and how many later became Holo users."""
    return _rows(
        conn,
        """
        SELECT l.source_event                                   AS event,
               COUNT(*)                                         AS leads,
               SUM(CASE WHEN l.score >= 70 AND l.needs_review = 0 THEN 1 ELSE 0 END) AS hot,
               SUM(l.needs_review)                              AS in_review,
               SUM(CASE WHEN lc.holo_user_id IS NOT NULL THEN 1 ELSE 0 END) AS became_users,
               SUM(CASE WHEN lc.stage IN ('activated', 'engaged') THEN 1 ELSE 0 END) AS active_users,
               ROUND(AVG(l.score), 1)                           AS avg_score
        FROM leads l
        LEFT JOIN lifecycle lc ON lc.email = l.email
        GROUP BY l.source_event
        ORDER BY leads DESC
    """,
    )


def lifecycle_counts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return _rows(
        conn,
        """
        SELECT stage, COUNT(*) AS users, SUM(pending_validations) AS pending_validations
        FROM lifecycle GROUP BY stage
        ORDER BY CASE stage WHEN 'new' THEN 1 WHEN 'activated' THEN 2 WHEN 'engaged' THEN 3
                 WHEN 'at_risk' THEN 4 WHEN 'dormant' THEN 5 ELSE 6 END
    """,
    )


def hot_leads(conn: sqlite3.Connection, event: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    sql = (
        "SELECT first_name, last_name, email, source_event, persona, intent, score "
        "FROM leads WHERE score >= 70 AND needs_review = 0"
    )
    params: tuple[Any, ...] = ()
    if event:
        sql += " AND source_event = ?"
        params = (event,)
    sql += " ORDER BY score DESC, updated_at DESC LIMIT ?"
    return _rows(conn, sql, params + (limit,))


def persona_mix(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return _rows(
        conn,
        "SELECT CASE WHEN needs_review = 1 THEN 'in review' ELSE persona END AS persona, "
        "COUNT(*) AS leads FROM leads GROUP BY 1 ORDER BY 2 DESC",
    )


def open_reviews(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    return _rows(
        conn,
        "SELECT id, kind, ref, reason, created_at FROM review_queue "
        "WHERE status = 'open' ORDER BY id LIMIT ?",
        (limit,),
    )


def geo_trend(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return _rows(
        conn,
        """
        SELECT date(checked_at) AS day, provider,
               COUNT(*) AS checks, SUM(mentioned) AS mentions,
               ROUND(100.0 * SUM(mentioned) / COUNT(*), 1) AS mention_rate_pct
        FROM geo_results GROUP BY 1, 2 ORDER BY 1, 2
    """,
    )


def row_issues(conn: sqlite3.Connection, run_id: int) -> list[dict[str, Any]]:
    return _rows(
        conn,
        "SELECT row_number, field, problem, value FROM row_issues WHERE run_id = ? ORDER BY row_number",
        (run_id,),
    )


def weekly_brief(conn: sqlite3.Connection) -> str:
    """A markdown brief a growth lead can read in two minutes."""
    lines = ["# Holo growth: weekly brief", ""]
    funnel = event_funnel(conn)
    lines.append("## Events")
    if funnel:
        lines += ["| Event | Leads | Hot | In review | Became users | Active |", "|---|---|---|---|---|---|"]
        lines += [
            f"| {f['event']} | {f['leads']} | {f['hot']} | {f['in_review']} | {f['became_users']} | "
            f"{f['active_users']} |"
            for f in funnel
        ]
    else:
        lines.append("No event leads processed yet.")
    lines += ["", "## Users by lifecycle stage"]
    lc = lifecycle_counts(conn)
    if lc:
        lines += [f"- **{r['stage']}**: {r['users']} users" for r in lc]
        pending = sum(r["pending_validations"] or 0 for r in lc)
        lines.append(f"- Predictions awaiting an outcome from users: **{pending}**")
    else:
        lines.append("No retention sync has run yet.")
    lines += ["", "## AI search visibility"]
    trend = geo_trend(conn)
    if trend:
        latest_day = trend[-1]["day"]
        for t in (t for t in trend if t["day"] == latest_day):
            label = " (demo data)" if t["provider"] == "demo" else ""
            lines.append(
                f"- {t['provider']}{label}: mentioned in {t['mentions']}/{t['checks']} "
                f"answers ({t['mention_rate_pct']}%)"
            )
    else:
        lines.append("No GEO checks yet.")
    reviews = open_reviews(conn, 500)
    lines += [
        "",
        "## Needs a human",
        f"- {sum(1 for r in reviews if r['kind'] == 'enrichment')} leads with low-confidence enrichment",
        f"- {sum(1 for r in reviews if r['kind'] == 'content')} email drafts that failed checks",
    ]
    return "\n".join(lines) + "\n"
