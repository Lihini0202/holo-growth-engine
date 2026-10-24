"""Growth dashboard (Streamlit).

Run from the repository root so that .streamlit/config.toml is applied:

    streamlit run dashboard/app.py

Uses the same report queries as the CLI and MCP server.

Theming: charts use the theme's categorical palette; custom HTML uses CSS
`light-dark()`, resolved against the `color-scheme` Streamlit sets for the active
theme.
"""

from __future__ import annotations

import html
import os
import sqlite3
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager

import altair as alt
import pandas as pd
import streamlit as st

from holo_growth import db, reports
from holo_growth.models import Intent, Persona

NEUTRAL = "#8a8886"  # Neutral colour for non-emphasised bars; legible in both themes
HOLO_BLUE = "#2a78d6"
STAGE_ORDER = ["new", "activated", "engaged", "at_risk", "dormant", "opted_out"]
PAGES = ["Overview", "Leads", "Holo users", "Website", "AI visibility", "Operations"]
LANGUAGES = {"en": "English", "si": "Sinhala", "ta": "Tamil"}
PERSONA_ORDER = [p.value for p in Persona] + ["in review"]
INTENT_ORDER = [i.value for i in Intent] + ["in review"]

# KPI tile accents: (light, dark) steps from the categorical palette.
ACCENTS = {
    "blue": ("#2a78d6", "#3987e5"),
    "orange": ("#eb6834", "#d95926"),
    "aqua": ("#1baf7a", "#199e70"),
    "violet": ("#6250d6", "#9085e9"),
    "magenta": ("#e87ba4", "#d55181"),
    "yellow": ("#eda100", "#c98500"),
    "red": ("#e34948", "#e66767"),
}

# Lucide outline icons (ISC licence), rendered in the tile accent colour.
ICONS = {
    "users": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/>'
    '<path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    "flame": '<path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.07-2.14-.22-4.05 2-6 .5 2.5 2 4.9 4 '
    '6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.15.43-2.29 1-3a2.5 2.5 0 0 0 2.5 2.5z"/>',
    "check": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/>'
    '<polyline points="16 11 18 13 22 9"/>',
    "activity": '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
    "star": '<polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 '
    '2 9.27 8.91 8.26 12 2"/>',
    "alert": '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 '
    '0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
    "eye": '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>',
    "search": '<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
    "award": '<circle cx="12" cy="8" r="7"/><polyline points="8.21 13.89 7 23 12 20 17 23 15.79 13.88"/>',
    "layers": '<polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/>'
    '<polyline points="2 12 12 17 22 12"/>',
    "inbox": '<polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 '
    '2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
    "mail": '<path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/>'
    '<polyline points="22,6 12,13 2,6"/>',
    "refresh": '<polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>',
    "x": '<circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/>'
    '<line x1="9" y1="9" x2="15" y2="15"/>',
}

st.set_page_config(page_title="Holo Growth", page_icon="📈", layout="wide")
db_path = os.environ.get("HOLO_GROWTH_DB", "holo_growth.db")

st.markdown(
    """
<style>
[data-testid="stMainBlockContainer"] { padding-top: 1.6rem; padding-bottom: 3rem; max-width: 1480px; }
header[data-testid="stHeader"] { background: transparent; }

.banner { border-radius: 14px; padding: 22px 26px; margin-bottom: 14px; color: #fff;
          background: linear-gradient(115deg, #184f95 0%, #2a78d6 38%, #6250d6 72%, #b4467a 100%);
          box-shadow: 0 6px 20px light-dark(rgba(42,120,214,.25), rgba(0,0,0,.45));
          display: flex; justify-content: space-between; align-items: flex-end; gap: 16px; flex-wrap: wrap; }
.banner .eyebrow { font-size: 12px; font-weight: 600; letter-spacing: .08em; text-transform: uppercase;
                   opacity: .85; }
.banner h1 { font-size: 28px; font-weight: 700; margin: 2px 0 0; padding: 0; color: #fff; }
.banner .meta { font-size: 12.5px; text-align: right; line-height: 1.6; opacity: .92; }
.badge { display: inline-block; background: rgba(255,255,255,.18); border: 1px solid rgba(255,255,255,.35);
         border-radius: 999px; padding: 2px 10px; font-size: 12px; font-weight: 600; margin-left: 10px;
         vertical-align: middle; }

.kpi { --accent: #2a78d6; position: relative; overflow: hidden; border-radius: 12px;
       padding: 16px 18px 14px; margin-bottom: 12px;
       background: light-dark(#ffffff, #161d2a); border: 1px solid light-dark(#e1e6ee, #263042);
       box-shadow: 0 1px 3px light-dark(rgba(16,24,40,.06), rgba(0,0,0,.35)); }
.kpi::before { content: ""; position: absolute; inset: 0 0 auto 0; height: 4px; background: var(--accent); }
.kpi-top { display: flex; justify-content: space-between; align-items: center; }
.kpi-label { font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: .05em;
             color: light-dark(#5b6576, #9aa5b8); }
.kpi-icon { width: 34px; height: 34px; border-radius: 10px; display: grid; place-items: center;
            color: var(--accent); background: color-mix(in srgb, var(--accent) 14%, transparent); }
.kpi-icon svg { width: 18px; height: 18px; fill: none; stroke: currentColor; stroke-width: 2;
                stroke-linecap: round; stroke-linejoin: round; }
.kpi-value { font-size: 32px; font-weight: 700; line-height: 1.15; margin-top: 4px;
             color: light-dark(#1b2230, #f2f4f8); font-variant-numeric: tabular-nums; }
.kpi-note { font-size: 12.5px; color: light-dark(#5b6576, #9aa5b8); margin-top: 2px; }
.kpi-note b { color: var(--accent); font-weight: 700; }

[class*="st-key-card"] { border-radius: 12px; padding: 16px 18px 10px;
                         background: light-dark(#ffffff, #161d2a);
                         border: 1px solid light-dark(#e1e6ee, #263042);
                         box-shadow: 0 1px 3px light-dark(rgba(16,24,40,.06), rgba(0,0,0,.35)); }
.card-title { font-size: 15px; font-weight: 650; color: light-dark(#1b2230, #f2f4f8); }
.card-sub { font-size: 12.5px; color: light-dark(#5b6576, #9aa5b8); margin-bottom: 6px; }

.brand { display: flex; align-items: center; gap: 10px; padding: 4px 0 20px; }
.brand .logo { width: 36px; height: 36px; border-radius: 10px; display: grid; place-items: center;
               font-weight: 800; color: #fff; font-size: 18px;
               background: linear-gradient(135deg, #2a78d6, #6250d6 60%, #d55181); }
.brand .name { font-size: 18px; font-weight: 700; color: light-dark(#1b2230, #fff); line-height: 1.1; }
.brand .tag { font-size: 12px; color: light-dark(#5b6576, #9aa5b8); }
section[data-testid="stSidebar"] { border-right: 1px solid light-dark(#e1e6ee, transparent); }
section[data-testid="stSidebar"] [data-testid="stRadio"],
section[data-testid="stSidebar"] [data-testid="stRadio"] > div,
section[data-testid="stSidebar"] [role="radiogroup"],
section[data-testid="stSidebar"] [role="radiogroup"] > div { width: 100%; gap: 3px; }
section[data-testid="stSidebar"] [data-testid="stRadioOption"] { width: 100%; padding: 9px 12px;
                                                               border-radius: 8px; margin: 0; }
section[data-testid="stSidebar"] [data-testid="stRadioOption"]:hover {
    background: light-dark(#eef1f6, #222b3c); }
section[data-testid="stSidebar"] [data-testid="stRadioOption"][data-selected="true"] {
    background: linear-gradient(90deg, light-dark(rgba(42,120,214,.14), rgba(57,135,229,.28)),
                                light-dark(rgba(98,80,214,.06), rgba(98,80,214,.18)));
    box-shadow: inset 3px 0 0 light-dark(#2a78d6, #6da7ec); font-weight: 600;
    color: light-dark(#1c5cab, #f2f4f8); }
[data-testid="stRadioOption"] > div > div:not([data-testid="stMarkdownContainer"]) { display: none; }
.side-foot { font-size: 11.5px; color: light-dark(#5b6576, #9aa5b8); margin-top: 18px; line-height: 1.6;
             overflow-wrap: anywhere; }
</style>
""",
    unsafe_allow_html=True,
)


# Not cached: Streamlit runs each script execution on a separate thread, and
# sqlite3 connections are bound to the thread that created them.
def _conn(path: str) -> sqlite3.Connection:
    conn = db.connect(path)
    conn.execute("PRAGMA query_only = ON")  # the dashboard never writes
    return conn


# -- building blocks ------------------------------------------------------------


def esc(value: object) -> str:
    return html.escape(str(value))


def nice(value: object) -> str:
    """Format a category code for display (`at_risk` -> `At risk`, `si` -> `Sinhala`)."""
    text = LANGUAGES.get(str(value), str(value)).replace("_", " ")
    return text[:1].upper() + text[1:]


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def pct(part: float, whole: float) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "–"


def kpi(col, label: str, value: object, note: str = "", accent: str = "blue", icon: str = "activity") -> None:  # type: ignore[no-untyped-def]
    """Render a KPI tile. `note` may contain <b> markup; all other values are escaped."""
    light, dark = ACCENTS[accent]
    col.markdown(
        f'<div class="kpi" style="--accent: light-dark({light}, {dark})">'
        f'<div class="kpi-top"><div class="kpi-label">{esc(label)}</div>'
        f'<div class="kpi-icon"><svg viewBox="0 0 24 24">{ICONS[icon]}</svg></div></div>'
        f'<div class="kpi-value">{esc(value)}</div><div class="kpi-note">{note}&nbsp;</div></div>',
        unsafe_allow_html=True,
    )


@contextmanager
def card(key: str, title: str, subtitle: str = "") -> Iterator[None]:
    with st.container(key=f"card-{key}"):
        st.markdown(
            f'<div class="card-title">{esc(title)}</div><div class="card-sub">{esc(subtitle)}&nbsp;</div>',
            unsafe_allow_html=True,
        )
        yield


def ink() -> str:
    """Text colour for chart labels, which are not styleable through CSS."""
    return "#e6e9ef" if st.context.theme.type == "dark" else "#1b2230"


def show(chart: alt.TopLevelMixin) -> None:
    # Axis and grid colours are inherited from the Streamlit theme.
    chart = chart.configure(background="transparent").configure_view(stroke=None)
    chart = chart.configure_axis(domain=False, ticks=False, labelPadding=6, labelFontSize=12)
    st.altair_chart(chart, width="stretch")


def hbar(
    df: pd.DataFrame,
    cat: str,
    val: str,
    order: list[str] | None = None,
    label: str | None = None,
    height: int = 110,
    color: alt.Color | None = None,
    pretty: bool = True,
) -> None:
    """Horizontal bar chart with value labels.

    The colour domain is the full `order`, so category colours stay stable under
    filtering.
    """
    if df.empty:
        st.caption("No data yet.")
        return
    if pretty:  # Format category codes (e.g. at_risk); keep proper names unchanged
        df = df.assign(**{cat: df[cat].map(nice)})
        order = [nice(o) for o in order] if order else None
    order = order or list(df.sort_values(val, ascending=False)[cat])
    y = alt.Y(f"{cat}:N", title=None, sort=order, axis=alt.Axis(labelLimit=230))
    x = alt.X(f"{val}:Q", title=None, axis=alt.Axis(grid=True, tickCount=4, labels=False))
    base = alt.Chart(df).encode(y=y, x=x, tooltip=[alt.Tooltip(f"{cat}:N"), alt.Tooltip(f"{val}:Q")])
    fill = color or alt.Color(f"{cat}:N", scale=alt.Scale(domain=order), legend=None)
    bars = base.mark_bar(cornerRadiusEnd=5, height={"band": 0.64}).encode(color=fill)
    text = base.mark_text(align="left", dx=6, fontSize=12.5, fontWeight=600, color=ink()).encode(
        text=alt.Text(f"{label or val}:N")
    )
    show((bars + text).properties(height=max(height, 38 * len(df))))


def donut(
    df: pd.DataFrame,
    cat: str,
    val: str,
    order: list[str] | None = None,
    height: int = 220,
    legend: str = "right",
) -> None:
    """Donut chart with a legend, so categories are not identified by colour alone."""
    if df.empty:
        st.caption("No data yet.")
        return
    df = df.assign(**{cat: df[cat].map(nice)})
    order = [nice(o) for o in order] if order else list(df.sort_values(val, ascending=False)[cat])
    df["share"] = (100 * df[val] / df[val].sum()).round(0).astype(int).astype(str) + "%"
    present = [o for o in order if o in set(df[cat])]
    # The chart height includes the legend; reserve space for a bottom legend.
    outer = (height - (34 if legend == "bottom" else 0)) / 2 - 6
    chart = (
        alt.Chart(df)
        .mark_arc(innerRadius=outer * 0.6, outerRadius=outer, padAngle=0.015, cornerRadius=3)
        .encode(
            theta=alt.Theta(f"{val}:Q", stack=True),
            color=alt.Color(
                f"{cat}:N",
                scale=alt.Scale(domain=order),
                sort=order,
                # The full domain keeps colours stable; the legend lists present categories only.
                legend=alt.Legend(
                    title=None, orient=legend, labelFontSize=12.5, symbolType="circle", values=present
                ),
            ),
            order=alt.Order(f"{val}:Q", sort="descending"),
            tooltip=[alt.Tooltip(f"{cat}:N"), alt.Tooltip(f"{val}:Q"), alt.Tooltip("share:N", title="share")],
        )
        .properties(height=height)
    )
    show(chart)


def table(df: pd.DataFrame, **kwargs) -> None:  # type: ignore[no-untyped-def]
    labels = {c: nice(c) for c in df.columns} | kwargs.pop("column_config", {})
    st.dataframe(df, hide_index=True, width="stretch", column_config=labels, **kwargs)


def banner(eyebrow: str, title: str, scope: str, badge: str = "") -> None:
    tag = f'<span class="badge">{esc(badge)}</span>' if badge else ""
    st.markdown(
        f'<div class="banner"><div><div class="eyebrow">{esc(eyebrow)}</div><h1>{esc(title)}{tag}</h1></div>'
        f'<div class="meta">{esc(scope)}<br>{esc(updated)}</div></div>',
        unsafe_allow_html=True,
    )


def empty(message: str) -> None:
    st.info(message, icon="ℹ️")


def b(text: object) -> str:
    return f"<b>{esc(text)}</b>"


def theme_switch() -> None:
    """Dark-mode toggle.

    Streamlit has no API for setting the theme at runtime. The toggle writes the theme
    to the localStorage key used by Streamlit's settings menu and reloads the page.
    Page and filter state persist through query parameters.
    """
    is_dark = st.context.theme.type == "dark"
    # Keyed on the current theme so the toggle resyncs after the reload.
    want_dark = st.toggle(":material/dark_mode: Dark mode", value=is_dark, key=f"dark-{is_dark}")
    if want_dark != is_dark:
        choice = "Dark" if want_dark else "Light"
        st.html(
            "<script>"
            'const key = "stActiveTheme-" + window.location.pathname + "-v2";'
            f'window.localStorage.setItem(key, JSON.stringify("{choice}"));'
            "window.location.reload();"
            "</script>",
            unsafe_allow_javascript=True,
        )


# -- data -------------------------------------------------------------------------

conn = _conn(db_path)
leads_all = pd.DataFrame(reports.lead_rows(conn))
lifecycle = pd.DataFrame(reports.lifecycle_counts(conn))
reviews = reports.open_reviews(conn, 1000)
geo = reports.geo_answers(conn)
runs = pd.DataFrame(reports.recent_runs(conn, 25))
channels = pd.DataFrame(reports.traffic_by_channel(conn))
daily = pd.DataFrame(reports.traffic_daily(conn))
web_provider = reports.traffic_provider(conn)
updated = (
    f"Last run {runs['finished_at'].iloc[0] or runs['started_at'].iloc[0]} UTC" if not runs.empty else ""
)
events = sorted(leads_all["event"].dropna().unique()) if not leads_all.empty else []

with st.sidebar:
    st.markdown(
        '<div class="brand"><div class="logo">h</div><div><div class="name">Holo</div>'
        '<div class="tag">Growth Engine</div></div></div>',
        unsafe_allow_html=True,
    )
    # Initialise from query parameters so reloads and shared links restore the view.
    if "page" not in st.session_state:
        st.session_state.page = (
            st.query_params.get("page") if st.query_params.get("page") in PAGES else PAGES[0]
        )
    if "events" not in st.session_state:
        st.session_state.events = [e for e in st.query_params.get_all("event") if e in events]
    page = st.radio("Page", PAGES, key="page", label_visibility="collapsed")
    st.divider()
    chosen = st.multiselect("Events", events, key="events", placeholder="All events")
    st.query_params["page"] = page
    st.query_params["event"] = chosen
    st.divider()
    theme_switch()
    st.markdown(
        f'<div class="side-foot">Data: {esc(db_path)}<br>Event filter applies to lead figures. '
        "Holo user stages cover every user.</div>",
        unsafe_allow_html=True,
    )

leads = leads_all[leads_all["event"].isin(chosen)] if chosen and not leads_all.empty else leads_all
scope = ", ".join(chosen) if chosen else "All events"
demo_geo = any(r["provider"] == "demo" for r in geo)


def _counts(df: pd.DataFrame, col: str) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    return df.groupby(col).size().reset_index(name="leads").sort_values("leads", ascending=False)


# -- pages ------------------------------------------------------------------------


def overview() -> None:
    banner("Holo growth", "Growth overview", scope)
    if leads_all.empty and lifecycle.empty:
        empty(
            "No data yet. Run `holo-growth demo`, or process an event sheet with `holo-growth leads process`."
        )
        return
    n = len(leads)
    hot, users, active = (int(leads[c].sum()) if n else 0 for c in ("hot", "became_user", "active_user"))
    c = st.columns(4)
    kpi(
        c[0],
        "Consented leads",
        n,
        "from " + b(plural(leads["event"].nunique() if n else 0, "event")),
        "blue",
        "users",
    )
    kpi(c[1], "Hot leads", hot, f"{b(pct(hot, n))} of leads, ready for follow-up", "orange", "flame")
    kpi(c[2], "Became Holo users", users, f"{b(pct(users, n))} lead-to-user conversion", "aqua", "check")
    kpi(c[3], "Active users", active, f"{b(pct(active, users))} of converted leads", "violet", "activity")

    left, right = st.columns([3, 2])
    with left, card("funnel", "Event funnel", "From signup sheet to active Holo user"):
        stages = pd.DataFrame({"stage": ["Consented leads", "Became Holo users", "Active users"]})
        stages["count"] = [n, users, active]
        stages["label"] = [f"{v}  ({pct(v, n)})" for v in stages["count"]]
        hbar(stages, "stage", "count", order=list(stages["stage"]), label="label", height=210)
    with right, card("lifecycle", "Holo users by stage", "Every Holo user, from product usage"):
        donut(lifecycle, "stage", "users", order=STAGE_ORDER, height=210)

    left, right = st.columns(2)
    with left, card("events", "Leads by event", "Consented leads, counted at their first event"):
        hbar(_counts(leads, "event"), "event", "leads", height=190, pretty=False)
    with (
        right,
        card("persona", "Who signed up", "Persona from enrichment; unreviewed guesses shown as 'In review'"),
    ):
        donut(_counts(leads, "persona"), "persona", "leads", order=PERSONA_ORDER, height=190)


def leads_page() -> None:
    banner("Lead pipeline", "Leads", scope)
    if leads.empty:
        empty("No leads yet. Run `holo-growth leads process <sheet> --event <name>`.")
        return
    n = len(leads)
    in_review = int(leads["needs_review"].sum())
    c = st.columns(4)
    kpi(c[0], "Consented leads", n, b(plural(leads["event"].nunique(), "event")), "blue", "users")
    kpi(c[1], "Hot leads", int(leads["hot"].sum()), "score " + b("70+") + " and reviewed", "orange", "flame")
    kpi(
        c[2],
        "Waiting for review",
        in_review,
        f"{b(pct(in_review, n))} not enriched yet",
        "yellow",
        "clock",
    )
    kpi(c[3], "Average score", f"{leads['score'].mean():.0f}", "out of " + b(100), "magenta", "star")

    left, mid, right = st.columns(3)
    with left, card("score", "Lead score distribution", "Number of leads per 10-point band"):
        hist = (
            alt.Chart(leads)
            .mark_bar(cornerRadiusEnd=5)
            .encode(
                x=alt.X("score:Q", bin=alt.Bin(step=10, extent=[0, 100]), title="Score"),
                y=alt.Y("count():Q", title=None, axis=alt.Axis(tickCount=4)),
                tooltip=[
                    alt.Tooltip("score:Q", bin=alt.Bin(step=10)),
                    alt.Tooltip("count():Q", title="leads"),
                ],
            )
            .properties(height=230)
        )
        show(hist)
    with mid, card("intent", "Intent", "How ready they sounded to try Holo"):
        donut(_counts(leads, "intent"), "intent", "leads", order=INTENT_ORDER, height=190, legend="bottom")
    with right, card("language", "Language", "Sinhala and Tamil copy needs native review"):
        donut(
            _counts(leads, "language"),
            "language",
            "leads",
            order=["en", "si", "ta", "in review"],
            height=190,
            legend="bottom",
        )

    with card("hot", "Hot leads", "Highest scores first. Reviewed enrichment only."):
        hot = leads[leads["hot"] == 1].sort_values("score", ascending=False)
        if hot.empty:
            st.caption("No hot leads in this selection.")
        else:
            hot = hot.assign(
                name=hot["first_name"].fillna("") + " " + hot["last_name"].fillna(""),
                persona=hot["persona"].map(nice),
                intent=hot["intent"].map(nice),
            )
            table(
                hot[["name", "score", "event", "persona", "intent", "email"]],
                column_config={
                    "score": st.column_config.ProgressColumn(
                        "Score", min_value=0, max_value=100, format="%d", width="medium"
                    )
                },
            )


def users_page() -> None:
    banner("Retention", "Holo users", "Every Holo user")
    if lifecycle.empty:
        empty("No retention sync yet. Run `holo-growth retention sync`.")
        return
    by_stage = dict(zip(lifecycle["stage"], lifecycle["users"], strict=True))
    total = int(lifecycle["users"].sum())
    engaged = int(by_stage.get("engaged", 0))
    slipping = int(by_stage.get("at_risk", 0) + by_stage.get("dormant", 0))
    pending = int(lifecycle["pending_validations"].fillna(0).sum())
    c = st.columns(4)
    kpi(c[0], "Users tracked", total, "usage metadata " + b("only"), "blue", "users")
    kpi(c[1], "Engaged", engaged, f"{b(pct(engaged, total))} with 3+ analyses in 14 days", "aqua", "activity")
    kpi(
        c[2], "At risk or dormant", slipping, f"{b(pct(slipping, total))} no use for 14+ days", "red", "alert"
    )
    kpi(
        c[3],
        "Outcomes to confirm",
        pending,
        "predictions " + b("waiting") + " for the user",
        "violet",
        "clock",
    )

    left, right = st.columns(2)
    with left, card("stages", "Users by lifecycle stage", "Rules on activity, not the ghosting model"):
        hbar(lifecycle, "stage", "users", order=STAGE_ORDER, height=220)
    with (
        right,
        card("pending", "Outcomes waiting, by stage", "Analyses 14 to 60 days old with no outcome recorded"),
    ):
        hbar(
            lifecycle.assign(pending=lifecycle["pending_validations"].fillna(0).astype(int)),
            "stage",
            "pending",
            order=STAGE_ORDER,
            height=220,
        )


def line(df: pd.DataFrame, col: str, title: str, height: int = 200) -> None:
    """Single-measure line chart. Separate measures use separate charts, never a second axis."""
    chart = (
        alt.Chart(df)
        .mark_line(strokeWidth=2.5, point=alt.OverlayMarkDef(size=28, filled=True))
        .encode(
            x=alt.X("day:T", title=None, axis=alt.Axis(format="%d %b", tickCount=6, grid=False)),
            y=alt.Y(f"{col}:Q", title=None, axis=alt.Axis(tickCount=4)),
            tooltip=[
                alt.Tooltip("day:T", title="day", format="%a %d %b"),
                alt.Tooltip(f"{col}:Q", title=title),
            ],
        )
        .properties(height=height)
    )
    show(chart)


def web_page() -> None:
    synthetic = web_provider == "synthetic"
    banner(
        "Website", "Sign-ups by source", "Last 28 days of GA4", badge="Synthetic data" if synthetic else ""
    )
    if channels.empty:
        empty("No website data yet. Run `holo-growth web sync`.")
        return
    if synthetic:
        st.info(
            "Synthetic demo traffic. Set GA4_PROPERTY_ID and GA4_CREDENTIALS_FILE, then run "
            "`holo-growth web sync`, for real numbers.",
            icon="ℹ️",
        )
    sessions, signups = int(channels["sessions"].sum()), int(channels["signups"].sum())
    top = channels.iloc[0]
    c = st.columns(4)
    kpi(c[0], "Sessions", f"{sessions:,}", "website visits, " + b("28 days"), "blue", "eye")
    kpi(c[1], "Sign-ups", f"{signups:,}", f"{b(round(signups / 28, 1))} a day on average", "aqua", "check")
    kpi(
        c[2], "Sign-up rate", pct(signups, sessions), "of sessions end in a " + b("sign-up"), "violet", "star"
    )
    kpi(
        c[3],
        "Top channel",
        f"{top['source']}",
        f"{b(top['signups'])} sign-ups via {esc(top['medium'])}",
        "orange",
        "award",
    )

    left, right = st.columns([2, 3])
    with left, card("channels", "Sign-ups by channel", "Source / medium, as GA4 attributes the session"):
        top8 = channels.head(8).assign(channel=lambda d: d["source"] + " / " + d["medium"])
        hbar(top8, "channel", "signups", order=list(top8["channel"]), height=300, pretty=False)
    with right:
        with card("daily-sessions", "Sessions per day", "All channels"):
            line(daily, "sessions", "sessions", height=110)
        with card("daily-signups", "Sign-ups per day", "All channels"):
            line(daily, "signups", "sign-ups", height=110)

    with card(
        "channel-table", "All channels", "GA4 reports totals only: it can't say which lead came from where"
    ):
        table(
            channels,
            column_config={
                "signups": "Sign-ups",
                "signup_rate_pct": st.column_config.ProgressColumn(
                    "Sign-up rate",
                    min_value=0,
                    max_value=float(max(channels["signup_rate_pct"].max(), 1)),
                    format="%.1f%%",
                    width="medium",
                ),
            },
        )


def geo_page() -> None:
    banner(
        "Generative engine optimisation",
        "AI search visibility",
        "Latest GEO check",
        badge="Demo answers" if demo_geo else "",
    )
    if not geo:
        empty("No GEO checks yet. Run `holo-growth geo run`.")
        return
    if demo_geo:
        st.warning("These are canned demo answers, not real assistant responses.", icon="⚠️")
    mentioned = sum(r["mentioned"] for r in geo)
    rivals = Counter(name for r in geo for name in r["competitors"])
    top = rivals.most_common(1)[0] if rivals else ("–", 0)
    c = st.columns(4)
    kpi(
        c[0],
        "Holo mention rate",
        pct(mentioned, len(geo)),
        f"named in {b(mentioned)} of {len(geo)} answers",
        "blue",
        "eye",
    )
    kpi(
        c[1],
        "Answers checked",
        len(geo),
        b(plural(len({r["provider"] for r in geo}), "provider")),
        "aqua",
        "search",
    )
    kpi(c[2], "Top competitor", top[0], "in " + b(plural(top[1], "answer")), "orange", "award")
    kpi(c[3], "Competitors seen", len(rivals), b("distinct") + " brands named", "magenta", "layers")

    left, right = st.columns([2, 3])
    with left, card("brands", "Brands named in answers", "Holo in blue, competitors in grey"):
        brands = pd.DataFrame(
            [{"brand": "Holo", "answers": mentioned}]
            + [{"brand": k, "answers": v} for k, v in rivals.most_common(7)]
        )
        brands["who"] = ["Holo" if x == "Holo" else "Competitor" for x in brands["brand"]]
        hbar(
            brands,
            "brand",
            "answers",
            order=list(brands["brand"]),
            pretty=False,
            height=220,
            color=alt.Color(
                "who:N",
                scale=alt.Scale(domain=["Holo", "Competitor"], range=[HOLO_BLUE, NEUTRAL]),
                legend=None,
            ),
        )
    with right, card("answers", "Answers", "What each assistant said, question by question"):
        table(
            pd.DataFrame(geo).assign(competitors=lambda d: d["competitors"].map(", ".join)),
            column_order=["mentioned", "prompt", "competitors", "provider", "answer_excerpt"],
            column_config={
                "mentioned": st.column_config.CheckboxColumn("Holo named", width="small"),
                "prompt": st.column_config.TextColumn("Question", width="medium"),
                "answer_excerpt": st.column_config.TextColumn("Answer", width="large"),
            },
        )


def operations_page() -> None:
    banner("Operations", "Review queue, drafts and runs", "Everything waiting for a person")
    drafts = pd.DataFrame(reports.recent_drafts(conn))
    flagged = int((drafts["status"] != "passed").sum()) if not drafts.empty else 0
    failed = int(runs["status"].isin(["failed", "partial"]).sum()) if not runs.empty else 0
    c = st.columns(4)
    kpi(c[0], "Waiting for review", len(reviews), b("enrichment") + " and email drafts", "yellow", "inbox")
    kpi(c[1], "Drafts needing review", flagged, f"of {b(len(drafts))} recent drafts", "magenta", "mail")
    kpi(c[2], "Runs logged", len(runs), "most recent " + b(25), "blue", "refresh")
    kpi(
        c[3],
        "Failed or partial runs",
        failed,
        b("safe") + " to re-run",
        "red",
        "x",
    )

    with card("reviews", "Review queue", "Run `holo-growth review approve <id>` to accept"):
        if reviews:
            table(
                pd.DataFrame(reviews),
                column_config={"id": "#", "ref": "Lead or draft", "created_at": "Added"},
            )
        else:
            st.caption("Nothing waiting.")
    left, right = st.columns(2)
    with left, card("drafts", "Campaign drafts", "Never sent automatically"):
        if drafts.empty:
            st.caption("No drafts yet. Run `holo-growth content draft <segment>`.")
        else:
            table(drafts, column_config={"id": "#", "created_at": "Created"})
    with right, card("runs", "Recent runs", "Every pipeline run, newest first"):
        if runs.empty:
            st.caption("No runs yet.")
        else:
            table(
                runs[["id", "kind", "label", "status", "started_at"]],
                column_config={"id": "#", "started_at": "Started"},
            )


{
    "Overview": overview,
    "Leads": leads_page,
    "Holo users": users_page,
    "Website": web_page,
    "AI visibility": geo_page,
    "Operations": operations_page,
}[page]()
