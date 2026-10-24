"""Report queries over the local store, shared by the CLI, MCP server and dashboard."""

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
    """Per-event funnel: leads, hot leads, leads in review, converted and active Holo users."""
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


def lead_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """One row per lead with the funnel flags used by `event_funnel`, for event filtering.

    Enrichment pending review is reported as 'in review', consistent with `persona_mix`.
    """
    return _rows(
        conn,
        """
        SELECT l.email, l.first_name, l.last_name, l.source_event AS event,
               CASE WHEN l.needs_review = 1 THEN 'in review' ELSE l.persona END  AS persona,
               CASE WHEN l.needs_review = 1 THEN 'in review' ELSE l.intent END   AS intent,
               CASE WHEN l.needs_review = 1 THEN 'in review' ELSE l.language END AS language,
               l.score, l.needs_review,
               CASE WHEN l.score >= 70 AND l.needs_review = 0 THEN 1 ELSE 0 END AS hot,
               CASE WHEN lc.holo_user_id IS NOT NULL THEN 1 ELSE 0 END AS became_user,
               CASE WHEN lc.stage IN ('activated', 'engaged') THEN 1 ELSE 0 END AS active_user
        FROM leads l
        LEFT JOIN lifecycle lc ON lc.email = l.email
    """,
    )


def geo_answers(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Answers from the latest GEO run, with competitors decoded."""
    rows = _rows(
        conn,
        """
        SELECT provider, prompt, mentioned, position, competitors, answer_excerpt, checked_at
        FROM geo_results WHERE run_id = (SELECT MAX(run_id) FROM geo_results)
        ORDER BY id
    """,
    )
    for r in rows:
        r["competitors"] = json.loads(r["competitors"] or "[]")
    return rows


def recent_drafts(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    return _rows(
        conn,
        "SELECT id, segment, language, subject, status, created_at FROM drafts ORDER BY id DESC LIMIT ?",
        (limit,),
    )


def _web_window(days: int) -> str:
    # The window is relative to the latest stored day, not the current date.
    return f"day > date((SELECT MAX(day) FROM web_traffic), '-{int(days)} days')"


def traffic_by_channel(conn: sqlite3.Connection, days: int = 28) -> list[dict[str, Any]]:
    """Sessions and sign-ups per source / medium over the last `days` stored days."""
    return _rows(
        conn,
        f"""
        SELECT source, medium, SUM(sessions) AS sessions, SUM(signups) AS signups,
               ROUND(100.0 * SUM(signups) / MAX(SUM(sessions), 1), 1) AS signup_rate_pct
        FROM web_traffic WHERE {_web_window(days)}
        GROUP BY source, medium ORDER BY signups DESC, sessions DESC
    """,
    )


def traffic_daily(conn: sqlite3.Connection, days: int = 28) -> list[dict[str, Any]]:
    return _rows(
        conn,
        f"""
        SELECT day, SUM(sessions) AS sessions, SUM(signups) AS signups
        FROM web_traffic WHERE {_web_window(days)} GROUP BY day ORDER BY day
    """,
    )


def traffic_provider(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT provider FROM web_traffic LIMIT 1").fetchone()
    return str(row["provider"]) if row else None


def row_issues(conn: sqlite3.Connection, run_id: int) -> list[dict[str, Any]]:
    return _rows(
        conn,
        "SELECT row_number, field, problem, value FROM row_issues WHERE run_id = ? ORDER BY row_number",
        (run_id,),
    )


def _count(n: int, noun: str, rest: str) -> str:
    return f"- {n} {noun}{'' if n == 1 else 's'} {rest}"


def weekly_brief(conn: sqlite3.Connection) -> str:
    """Render the weekly growth brief as Markdown."""
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
        lines += ["", "Each person counts once, under the first event they signed up at."]
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
    lines += ["", "## Website sign-ups by source (last 28 days)"]
    channels = traffic_by_channel(conn)
    if channels:
        sessions = sum(c["sessions"] for c in channels)
        signups = sum(c["signups"] for c in channels)
        label = " Synthetic demo data." if traffic_provider(conn) == "synthetic" else ""
        lines.append(
            f"{sessions} sessions, {signups} sign-ups ({100 * signups / max(sessions, 1):.1f}%).{label}"
        )
        lines += [
            f"- {c['source']} / {c['medium']}: {c['signups']} sign-ups from {c['sessions']} sessions "
            f"({c['signup_rate_pct']}%)"
            for c in channels[:5]
        ]
    else:
        lines.append("No website data yet. Run `holo-growth web sync`.")
    reviews = open_reviews(conn, 500)
    lines += [
        "",
        "## Needs a human",
        _count(
            sum(1 for r in reviews if r["kind"] == "enrichment"), "lead", "with low-confidence enrichment"
        ),
        _count(sum(1 for r in reviews if r["kind"] == "content"), "email draft", "waiting for review"),
    ]
    return "\n".join(lines) + "\n"
