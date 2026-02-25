"""
Streamlit dashboard for the Matchbook trading system.
Dark-mode UI with header metrics, goal tracker, active positions, panic hedge, and equity chart.
"""

import time
from datetime import datetime

import plotly.graph_objects as go
import streamlit as st

import db
from matchbook_api import MatchbookClient, MatchbookAPIError, greening_up_lay_stake, lay_liability

# Bot considered "running" if last snapshot within this many seconds
BOT_ACTIVE_THRESHOLD_SEC = 120

# Page config - dark theme
st.set_page_config(
    page_title="Matchbook Trading Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Dark mode CSS
st.markdown(
    """
    <style>
    .stApp {
        background-color: #0e1117;
    }
    .metric-card {
        background: linear-gradient(135deg, #1e2130 0%, #252938 100%);
        padding: 1rem 1.5rem;
        border-radius: 8px;
        border: 1px solid #31333f;
        margin-bottom: 1rem;
    }
    .metric-label {
        color: #8b8fa3;
        font-size: 0.85rem;
    }
    .metric-value {
        color: #fafafa;
        font-size: 1.5rem;
        font-weight: 600;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# Constants
TARGET_BANKROLL = 5000.0
STARTING_BANKROLL = 25.0
REFRESH_INTERVAL = 30  # seconds


def get_api_client():
    """Return authenticated Matchbook client or None if not configured."""
    try:
        client = MatchbookClient()
        client.login()
        return client
    except MatchbookAPIError:
        return None


def get_balance_from_api() -> tuple[float | None, float | None, int | None]:
    """
    Fetch balance, exposure, and phase from Matchbook API.
    Returns (balance, exposure, phase) or (None, None, None) on failure.
    """
    client = get_api_client()
    if not client:
        return None, None, None
    try:
        account = client.get_account()
        balance = float(account.get("balance", 0) or 0)
        exposure = float(account.get("exposure", 0) or 0)
        phase = 1 if 25 <= balance < 200 else 2
        return balance, exposure, phase
    except Exception:
        return None, None, None


def get_offers_from_api() -> list[dict]:
    """Fetch open and matched offers from Matchbook API."""
    client = get_api_client()
    if not client:
        return []
    try:
        data = client.get_offers(status="open,matched", per_page=50)
        return data.get("offers", [])
    except MatchbookAPIError:
        return []


def get_connection_status() -> tuple[bool, str]:
    """Return (connected, message) for Matchbook API connection status."""
    client = get_api_client()
    if not client:
        return False, "Failed — check .env credentials"
    try:
        client.get_account()
        return True, "Connected"
    except Exception as e:
        return False, f"Failed — {str(e)[:50]}"


def get_bot_status() -> tuple[str, str]:
    """Return (status, detail) for bot. Uses last bankroll snapshot timestamp."""
    last_ts = db.get_last_snapshot_time()
    if not last_ts:
        return "Unknown", "No snapshots yet"
    try:
        ts_compact = last_ts.replace("T", " ").replace("Z", "")[:19]
        dt = datetime.strptime(ts_compact, "%Y-%m-%d %H:%M:%S")
        age_sec = (datetime.utcnow() - dt).total_seconds()
        if age_sec < BOT_ACTIVE_THRESHOLD_SEC:
            return "Running", f"Last snapshot {int(age_sec)}s ago"
        return "Offline or idle", f"Last snapshot {int(age_sec // 60)}m ago"
    except Exception:
        return "Unknown", last_ts[:30]


def panic_hedge() -> tuple[bool, str]:
    """
    Emergency close: for each matched position, place offsetting order at market.
    Returns (success, message).
    """
    client = get_api_client()
    if not client:
        return False, "Not logged in. Check .env credentials."

    try:
        offers = client.get_offers(status="matched", per_page=50)
        matched = [o for o in offers.get("offers", []) if o.get("status") == "matched"]
        if not matched:
            return True, "No matched positions to hedge."

        events_data = client.get_events(
            include_prices=True,
            price_depth=1,
            states="open,suspended",
            per_page=50,
        )
        events_by_id = {e["id"]: e for e in events_data.get("events", [])}

        for offer in matched:
            side = offer.get("side")
            runner_id = offer.get("runner-id")
            back_odds = offer.get("decimal-odds") or offer.get("odds")
            back_stake = offer.get("stake", 0)
            event_id = offer.get("event-id")
            market_id = offer.get("market-id")

            # Find current best price for offsetting
            best_lay = None
            ev = events_by_id.get(event_id)
            if ev:
                for mkt in ev.get("markets", []):
                    if mkt.get("id") != market_id:
                        continue
                    for r in mkt.get("runners", []):
                        if r.get("id") == runner_id:
                            for p in r.get("prices", []):
                                if p.get("side") == "lay":
                                    best_lay = p.get("decimal-odds") or p.get("odds")
                                    break
                            break

            if side == "back" and best_lay:
                # We're long (Back matched) - Lay to close
                lay_stake = greening_up_lay_stake(back_stake, back_odds, best_lay)
                client.submit_offers(
                    offers=[
                        {
                            "runner-id": runner_id,
                            "side": "lay",
                            "odds": best_lay,
                            "stake": round(lay_stake, 2),
                            "keep-in-play": False,
                        }
                    ]
                )
            elif side == "lay":
                # We're short (Lay matched) - Back to close at best back
                best_back = None
                ev = events_by_id.get(event_id)
                if ev:
                    for mkt in ev.get("markets", []):
                        for r in mkt.get("runners", []):
                            if r.get("id") == runner_id:
                                for p in r.get("prices", []):
                                    if p.get("side") == "back":
                                        best_back = p.get("decimal-odds") or p.get("odds")
                                        break
                                break
                if best_back:
                    # Greening: Back_stake = Lay_stake * Lay_odds / Back_odds
                    lay_stake = offer.get("stake", 0)
                    lay_odds = offer.get("decimal-odds") or offer.get("odds")
                    back_close_stake = greening_up_lay_stake(lay_stake, lay_odds, best_back)
                    client.submit_offers(
                        offers=[
                            {
                                "runner-id": runner_id,
                                "side": "back",
                                "odds": best_back,
                                "stake": round(back_close_stake, 2),
                                "keep-in-play": False,
                            }
                        ]
                    )

        return True, "Panic hedge orders submitted."
    except MatchbookAPIError as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)


def main():
    st.title("Matchbook Trading Dashboard")
    st.caption("Automated trading system — £25 → £5,000 target")

    # Initialize DB
    db.init_db()

    # Bot control - must enable trading before bot places any orders
    st.subheader("Bot Control")
    trading_enabled = db.is_trading_enabled()
    event_id = db.get_event_id() or ""

    col_ctrl1, col_ctrl2, col_ctrl3 = st.columns([1, 1, 2])
    with col_ctrl1:
        if trading_enabled:
            if st.button("Disable Trading", type="secondary"):
                db.set_trading_enabled(False)
                st.success("Trading disabled. Bot will not place new orders.")
                st.rerun()
        else:
            if st.button("Enable Trading", type="primary"):
                db.set_trading_enabled(True)
                st.success("Trading enabled. Bot will place orders on next cycle.")
                st.rerun()
    with col_ctrl2:
        st.metric("Trading", "ON" if trading_enabled else "OFF")
    with col_ctrl3:
        new_event_id = st.text_input(
            "Event ID (focus on single event)",
            value=event_id,
            placeholder="e.g. 32363927044601045 — leave empty for all events",
            help="Enter a Matchbook event ID to trade only that event.",
        )
        if new_event_id != event_id:
            db.set_event_id(new_event_id)
            st.caption(f"Event filter: {new_event_id or 'All events'}")

    # Force Phase 1: use Phase 1 until you've grown to £200 and are ready for Phase 2
    force_phase1 = db.is_force_phase1()
    new_force = st.checkbox(
        "Force Phase 1 (start with scalping until £200)",
        value=force_phase1,
        help="When checked, bot uses Phase 1 strategy regardless of balance. Uncheck to allow Phase 2 when balance reaches £200.",
    )
    if new_force != force_phase1:
        db.set_force_phase1(new_force)
        st.rerun()

    st.divider()

    # Status bar: Connection, Bot, Manual refresh
    conn_ok, conn_msg = get_connection_status()
    bot_status, bot_detail = get_bot_status()
    col_status1, col_status2, col_status3, col_status4 = st.columns([1, 1, 1, 2])
    with col_status1:
        st.caption("Matchbook")
        if conn_ok:
            st.success(conn_msg)
        else:
            st.error(conn_msg)
    with col_status2:
        st.caption("Bot")
        if bot_status == "Running":
            st.success(bot_status)
        elif bot_status == "Offline or idle":
            st.warning(bot_status)
        else:
            st.info(bot_status)
        st.caption(bot_detail)
    with col_status3:
        st.caption("Refresh")
        if st.button("Refresh now"):
            st.session_state.last_refresh = time.time()
            st.rerun()

    # Header metrics
    api_balance, api_exposure, api_phase = get_balance_from_api()
    db_balance = db.get_latest_balance()

    balance = api_balance if api_balance is not None else db_balance or STARTING_BANKROLL
    exposure = api_exposure if api_exposure is not None else 0.0
    # Phase: use Force Phase 1 setting, else balance-based
    if db.is_force_phase1():
        phase = 1
    else:
        phase = api_phase if api_phase is not None else (1 if balance < 200 else 2)

    daily_start = db.get_daily_start_balance()
    if daily_start and daily_start > 0:
        daily_roi = (balance - daily_start) / daily_start * 100
    else:
        daily_roi = 0.0

    cumulative_pnl = balance - STARTING_BANKROLL

    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        st.metric("Current Bankroll (£)", f"£{balance:.2f}")
    with col2:
        st.metric("Cumulative P&L (£)", f"£{cumulative_pnl:+.2f}")
    with col3:
        st.metric("Daily ROI (%)", f"{daily_roi:.2f}%")
    with col4:
        st.metric("Open Exposure (£)", f"£{exposure:.2f}")
    with col5:
        st.metric("Phase", f"Phase {phase}")

    # Goal tracker
    st.subheader("Goal Tracker")
    progress = min(100.0, max(0.0, (balance - STARTING_BANKROLL) / (TARGET_BANKROLL - STARTING_BANKROLL) * 100))
    st.progress(progress / 100)
    st.caption(f"£25 → £5,000 | Progress: {progress:.1f}% (£{balance:.2f})")

    # Active positions table
    st.subheader("Active Positions")
    offers = get_offers_from_api()
    if offers:
        rows = []
        for o in offers:
            rows.append({
                "Market": o.get("market-name", ""),
                "Selection": o.get("runner-name", ""),
                "Side": o.get("side", "").upper(),
                "Odds": o.get("decimal-odds") or o.get("odds", 0),
                "Stake": o.get("stake", 0),
                "Status": o.get("status", ""),
            })
        st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        st.info("No active positions. Connect to Matchbook (check .env) or bot not running.")

    # Trade history
    st.subheader("Trade History")
    trades = db.get_trades(limit=50)
    if trades:
        trade_rows = []
        for t in trades:
            trade_rows.append({
                "Date": t.get("matched_at", "")[:19].replace("T", " ") if t.get("matched_at") else "",
                "Event ID": t.get("event_id", ""),
                "Runner ID": t.get("runner_id", ""),
                "Side": (t.get("side", "") or "").upper(),
                "Odds": t.get("odds", 0),
                "Stake": t.get("stake", 0),
                "Profit (£)": t.get("profit") if t.get("profit") is not None else "—",
                "Phase": t.get("phase", ""),
            })
        st.dataframe(trade_rows, use_container_width=True, hide_index=True)
    else:
        st.info("No trades yet. Completed trades will appear here.")

    # Emergency control
    st.subheader("Emergency Control")
    if st.button("Panic Hedge / Close Position", type="primary"):
        with st.spinner("Submitting hedge orders..."):
            ok, msg = panic_hedge()
        if ok:
            st.success(msg)
        else:
            st.error(msg)

    # Analytics - equity curve
    st.subheader("Bankroll Equity Curve")
    timestamps, balances = db.get_equity_curve()
    if timestamps and balances:
        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=timestamps,
                y=balances,
                mode="lines",
                name="Balance",
                line=dict(color="#00d4aa", width=2),
                fill="tozeroy",
                fillcolor="rgba(0, 212, 170, 0.2)",
            )
        )
        fig.update_layout(
            template="plotly_dark",
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            xaxis_title="Time",
            yaxis_title="Balance (£)",
            margin=dict(l=40, r=40, t=40, b=40),
            height=400,
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No bankroll data yet. Run the bot to record snapshots.")

    # Auto-refresh
    if "last_refresh" not in st.session_state:
        st.session_state.last_refresh = time.time()
    if time.time() - st.session_state.last_refresh > REFRESH_INTERVAL:
        st.session_state.last_refresh = time.time()
        st.rerun()


if __name__ == "__main__":
    main()
