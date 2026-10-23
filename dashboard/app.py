"""Growth dashboard: `streamlit run dashboard/app.py`.

Reads the same SQLite store the pipeline writes, through the same report queries
the CLI and MCP server use, so every surface shows the same numbers.
"""

from __future__ import annotations

import os
import sqlite3

import altair as alt
import pandas as pd
import streamlit as st

from holo_growth import db, reports

SERIES = "#2a78d6"  # single-series charts: one hue, the title names the series

st.set_page_config(page_title="Holo Growth", layout="wide")
db_path = os.environ.get("HOLO_GROWTH_DB", "holo_growth.db")


@st.cache_resource
def _conn(path: str) -> sqlite3.Connection:
    conn = db.connect(path)
    conn.execute("PRAGMA query_only = ON")  # the dashboard never writes
    return conn


conn = _conn(db_path)
st.title("Holo growth")
st.caption(f"Data: {db_path}")

funnel = pd.DataFrame(reports.event_funnel(conn))
lifecycle = pd.DataFrame(reports.lifecycle_counts(conn))
reviews = reports.open_reviews(conn, 1000)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Consented leads", int(funnel["leads"].sum()) if not funnel.empty else 0)
c2.metric("Hot leads", int(funnel["hot"].sum()) if not funnel.empty else 0)
c3.metric("Holo users tracked", int(lifecycle["users"].sum()) if not lifecycle.empty else 0)
c4.metric("Waiting for review", len(reviews))


def bar(df: pd.DataFrame, x: str, y: str, title: str, sort: list[str] | str = "-x") -> None:
    chart = (
        alt.Chart(df, title=title)
        .mark_bar(color=SERIES, cornerRadiusEnd=4, size=18)
        .encode(
            x=alt.X(f"{x}:Q", title=None, axis=alt.Axis(grid=True, tickCount=5)),
            y=alt.Y(f"{y}:N", title=None, sort=sort),
            tooltip=[alt.Tooltip(f"{y}:N"), alt.Tooltip(f"{x}:Q")],
        )
        .configure_axis(gridOpacity=0.3, domain=False)
    )
    st.altair_chart(chart, width="stretch")


left, right = st.columns(2)
with left:
    st.subheader("Events")
    if funnel.empty:
        st.info("No events processed yet. Run `holo-growth leads process`.")
    else:
        st.dataframe(funnel, hide_index=True, width="stretch")
        bar(funnel, "leads", "event", "Consented leads per event")
with right:
    st.subheader("Holo users by lifecycle stage")
    if lifecycle.empty:
        st.info("No retention sync yet. Run `holo-growth retention sync`.")
    else:
        order = ["new", "activated", "engaged", "at_risk", "dormant", "opted_out"]
        bar(lifecycle, "users", "stage", "Users per stage", sort=order)
        pending = int(lifecycle["pending_validations"].fillna(0).sum())
        st.caption(f"{pending} predictions are waiting for users to confirm the outcome.")

st.subheader("Hot leads")
hot = pd.DataFrame(reports.hot_leads(conn, limit=25))
if hot.empty:
    st.write("None yet.")
else:
    st.dataframe(hot, hide_index=True, width="stretch")

st.subheader("AI search visibility")
geo = pd.DataFrame(reports.geo_trend(conn))
if geo.empty:
    st.write("No GEO checks yet. Run `holo-growth geo run`.")
else:
    if (geo["provider"] == "demo").any():
        st.warning("Includes canned demo answers, not real assistant responses.")
    st.dataframe(geo, hide_index=True, width="stretch")

st.subheader("Review queue")
if reviews:
    st.dataframe(pd.DataFrame(reviews), hide_index=True, width="stretch")
else:
    st.write("Nothing waiting.")

st.subheader("Recent runs")
runs = pd.DataFrame(reports.recent_runs(conn, 15))
if not runs.empty:
    st.dataframe(runs.drop(columns=["stats"]), hide_index=True, width="stretch")
