"""
Streamlit dashboard for pm_bot.

Reads both:
  data/pm_bot.db        — main bot trade log (signals, orders, fills, P&L)
  data/cross_scan.db    — cross-exchange scanner observations

Shows:
  * Status panel — what's running, when last seen
  * Candidates over time — flagged arb opportunities
  * Edge persistence — for each leg, does the gap survive?
  * Signal acceptance breakdown — what's the risk manager rejecting?
  * Daily P&L (real or simulated) — by strategy
  * Recent activity log

Run:
  pip install streamlit pandas plotly
  streamlit run scripts/dashboard.py

Then open http://localhost:8501
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

# Configuration ---------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
PM_BOT_DB = Path(os.environ.get("PM_BOT_DB", REPO_ROOT / "data" / "pm_bot.db"))
CROSS_SCAN_DB = Path(os.environ.get("CROSS_SCAN_DB", REPO_ROOT / "data" / "cross_scan.db"))

st.set_page_config(
    page_title="pm_bot dashboard",
    page_icon="📈",
    layout="wide",
)

# Cached query helpers --------------------------------------------------


@st.cache_data(ttl=10)
def query(db_path: Path, sql: str, params: tuple = ()) -> pd.DataFrame:
    """Run a query, return DataFrame. Returns empty DF if DB doesn't exist."""
    if not db_path.exists():
        return pd.DataFrame()
    try:
        with sqlite3.connect(db_path) as conn:
            return pd.read_sql_query(sql, conn, params=params)
    except (sqlite3.OperationalError, pd.errors.DatabaseError):
        return pd.DataFrame()


def file_age_seconds(p: Path) -> float | None:
    """Seconds since file was last modified, or None if missing."""
    if not p.exists():
        return None
    return (datetime.now().timestamp() - p.stat().st_mtime)


# Header ----------------------------------------------------------------

st.title("📈 pm_bot dashboard")
st.caption(f"Reading from `{PM_BOT_DB}` and `{CROSS_SCAN_DB}`")


# Status panel ----------------------------------------------------------

status_cols = st.columns(4)

# pm_bot status
log_path = REPO_ROOT / "logs" / "pm_bot.log"
log_age = file_age_seconds(log_path)
if log_age is None:
    bot_status = "❌ never run"
elif log_age > 600:
    bot_status = f"⚠️ stale ({log_age // 60:.0f}m ago)"
else:
    bot_status = f"✅ active ({log_age // 60:.0f}m ago)"
status_cols[0].metric("pm_bot", bot_status)

# Scanner status
scan_age = file_age_seconds(CROSS_SCAN_DB)
if scan_age is None:
    scan_status = "❌ never run"
elif scan_age > 600:
    scan_status = f"⚠️ stale ({scan_age // 60:.0f}m ago)"
else:
    scan_status = f"✅ active ({scan_age // 60:.0f}m ago)"
status_cols[1].metric("cross-scanner", scan_status)

# Total signals today
today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
sig_today = query(PM_BOT_DB, "SELECT COUNT(*) AS n FROM signals WHERE ts LIKE ?", (f"{today}%",))
n_sig = int(sig_today["n"].iloc[0]) if not sig_today.empty else 0
status_cols[2].metric("signals today", n_sig)

# Total candidates today (cross scanner)
cand_today = query(CROSS_SCAN_DB, "SELECT COUNT(*) AS n FROM candidates WHERE ts LIKE ?", (f"{today}%",))
n_cand = int(cand_today["n"].iloc[0]) if not cand_today.empty else 0
status_cols[3].metric("candidates today", n_cand)


# Tabs ------------------------------------------------------------------

tab_cross, tab_bot, tab_pnl, tab_raw = st.tabs([
    "🔀 Cross-exchange",
    "🤖 Bot signals",
    "💰 P&L",
    "🗃 Raw data",
])

# ----- Cross-exchange tab ---------------------------------------------

with tab_cross:
    st.subheader("Live candidates (gaps that exceeded threshold)")

    candidates = query(
        CROSS_SCAN_DB,
        """SELECT ts, pair_name, leg_name, direction, edge_cents,
                  poly_price, kalshi_price, kalshi_ticker
           FROM candidates
           ORDER BY ts DESC
           LIMIT 100""",
    )
    if candidates.empty:
        st.info("No candidates logged yet. Run `python scripts/cross_exchange_scanner.py` to start collecting.")
    else:
        candidates["ts"] = pd.to_datetime(candidates["ts"])
        st.dataframe(candidates, use_container_width=True, hide_index=True)

    st.subheader("Edge persistence over time")
    st.caption("Track whether specific gaps survive. Stable gaps = structural; bouncy = noise.")

    gaps = query(
        CROSS_SCAN_DB,
        """SELECT ts, pair_name, leg_name,
                  edge_PYK_NO_cents AS edge_PY_KN,
                  edge_KYP_NO_cents AS edge_KY_PN
           FROM gaps
           ORDER BY ts DESC
           LIMIT 5000""",
    )
    if gaps.empty:
        st.info("No gap data yet.")
    else:
        gaps["ts"] = pd.to_datetime(gaps["ts"])
        gaps["leg_full"] = gaps["pair_name"] + " | " + gaps["leg_name"]

        # Limit to the leg with the most observations for default view
        top_legs = gaps["leg_full"].value_counts().head(10).index.tolist()
        selected_legs = st.multiselect(
            "Legs to plot",
            options=top_legs,
            default=top_legs[:4],
        )

        if selected_legs:
            filtered = gaps[gaps["leg_full"].isin(selected_legs)].copy()
            # Take the better direction's edge for each row
            filtered["best_edge"] = filtered[["edge_PY_KN", "edge_KY_PN"]].max(axis=1)
            # Clip to reasonable display range
            filtered = filtered[filtered["best_edge"] > -10]
            chart_df = filtered.pivot_table(
                index="ts", columns="leg_full", values="best_edge", aggfunc="last"
            )
            st.line_chart(chart_df, height=350)

        # Summary stats per leg
        st.subheader("Edge summary by leg")
        gaps["best_edge"] = gaps[["edge_PY_KN", "edge_KY_PN"]].max(axis=1)
        summary = (
            gaps.groupby("leg_full")["best_edge"]
            .agg(
                observations="count",
                mean_edge_c="mean",
                median_edge_c="median",
                pct_above_1c=lambda s: (s > 1.0).mean() * 100,
                max_edge_c="max",
            )
            .sort_values("observations", ascending=False)
        )
        st.dataframe(summary.round(2), use_container_width=True)


# ----- Bot signals tab ------------------------------------------------

with tab_bot:
    st.subheader("Signal flow: accepted vs rejected")

    sig_breakdown = query(
        PM_BOT_DB,
        """SELECT strategy, outcome, COUNT(*) AS n
           FROM signals
           GROUP BY strategy, outcome
           ORDER BY n DESC""",
    )
    if sig_breakdown.empty:
        st.info("No signals yet. Run `python run_bot.py` to start the bot.")
    else:
        st.bar_chart(sig_breakdown.pivot(index="strategy", columns="outcome", values="n").fillna(0))

    st.subheader("Top rejection reasons")
    rejections = query(
        PM_BOT_DB,
        """SELECT rejection_reason, COUNT(*) AS n
           FROM signals
           WHERE outcome='rejected'
           GROUP BY rejection_reason
           ORDER BY n DESC
           LIMIT 15""",
    )
    if not rejections.empty:
        st.dataframe(rejections, use_container_width=True, hide_index=True)
        st.caption(
            "These are signals the strategy emitted but the risk manager refused. "
            "Big numbers in `edge_below_min_*` are healthy — means the strategy is "
            "spotting candidates but the threshold is filtering aggressively."
        )

    st.subheader("Recent signals")
    recent = query(
        PM_BOT_DB,
        """SELECT ts, strategy, ticker, side, action, price, size,
                  edge_bps, outcome, rejection_reason, reasoning
           FROM signals
           ORDER BY id DESC
           LIMIT 50""",
    )
    if not recent.empty:
        recent["ts"] = pd.to_datetime(recent["ts"])
        st.dataframe(recent, use_container_width=True, hide_index=True)


# ----- P&L tab --------------------------------------------------------

with tab_pnl:
    st.subheader("Daily P&L by strategy")
    pnl = query(
        PM_BOT_DB,
        """SELECT day, strategy, realized_pnl, unrealized_pnl, trade_count, fee_total
           FROM daily_pnl
           ORDER BY day DESC""",
    )
    if pnl.empty:
        st.info("No P&L data yet (paper trading hasn't logged any closed positions).")
    else:
        # Cumulative realized P&L line chart
        pnl["day"] = pd.to_datetime(pnl["day"])
        cumulative = (
            pnl.sort_values("day")
            .groupby("strategy")["realized_pnl"]
            .cumsum()
            .reset_index()
            .merge(pnl[["day", "strategy"]].reset_index(), left_on="index", right_index=True)
            [["day", "strategy", "realized_pnl"]]
            .pivot(index="day", columns="strategy", values="realized_pnl")
        )
        st.line_chart(cumulative, height=350)

        st.dataframe(pnl, use_container_width=True, hide_index=True)

    st.subheader("Fills today")
    fills_today = query(
        PM_BOT_DB,
        """SELECT ts, ticker, side, action, price, size, fee, paper
           FROM fills
           WHERE ts LIKE ?
           ORDER BY ts DESC""",
        (f"{today}%",),
    )
    if not fills_today.empty:
        st.dataframe(fills_today, use_container_width=True, hide_index=True)
    else:
        st.caption("No fills today.")


# ----- Raw data tab ---------------------------------------------------

with tab_raw:
    st.subheader("SQL playground")
    st.caption("Run arbitrary queries against either database for ad-hoc exploration.")

    db_choice = st.radio("Database", ["pm_bot.db", "cross_scan.db"], horizontal=True)
    db_path = PM_BOT_DB if db_choice == "pm_bot.db" else CROSS_SCAN_DB

    # Show table list
    tables = query(db_path, "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    if tables.empty:
        st.warning(f"{db_path} doesn't exist or has no tables.")
    else:
        st.caption("Available tables: " + ", ".join(tables["name"].tolist()))

        default_sql = (
            "SELECT * FROM gaps ORDER BY ts DESC LIMIT 50"
            if db_choice == "cross_scan.db"
            else "SELECT * FROM signals ORDER BY id DESC LIMIT 50"
        )
        sql = st.text_area("SQL", value=default_sql, height=120)
        if st.button("Run query"):
            result = query(db_path, sql)
            st.dataframe(result, use_container_width=True)


# Footer ----------------------------------------------------------------

st.caption(
    f"Auto-refreshes every 10s (cached). "
    f"Last loaded: {datetime.now().strftime('%H:%M:%S')}. "
    f"Press `R` to force refresh."
)
