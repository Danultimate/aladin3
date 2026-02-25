"""
SQLite database module for the Matchbook trading system.
Stores trade history, active positions, and bankroll snapshots for analytics.
"""

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

# Default database path - same directory as this module
DB_PATH = Path(__file__).parent / "trading.db"

# Starting bankroll for first-run seed
STARTING_BANKROLL = 25.0


def get_connection() -> sqlite3.Connection:
    """Return a connection to the SQLite database."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # Return dict-like rows
    return conn


def init_db() -> None:
    """
    Create all tables if they do not exist.
    On first run with empty DB, seeds bankroll_snapshots with starting £25.
    """
    conn = get_connection()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bankroll_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                balance REAL NOT NULL,
                phase INTEGER NOT NULL,
                daily_roi REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER,
                market_id INTEGER,
                runner_id INTEGER,
                side TEXT NOT NULL,
                odds REAL NOT NULL,
                stake REAL NOT NULL,
                matched_at TEXT NOT NULL,
                profit REAL,
                phase INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                offer_id INTEGER PRIMARY KEY,
                event_id INTEGER,
                market_name TEXT,
                selection TEXT,
                side TEXT NOT NULL,
                odds REAL NOT NULL,
                stake REAL NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_start (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                date TEXT NOT NULL,
                balance REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.commit()

        # Default: trading disabled until user enables via dashboard
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            ("trading_enabled", "0"),
        )
        conn.commit()

        # Seed bankroll_snapshots if empty (first run)
        cursor = conn.execute("SELECT COUNT(*) FROM bankroll_snapshots")
        if cursor.fetchone()[0] == 0:
            now = datetime.utcnow().isoformat()
            conn.execute(
                "INSERT INTO bankroll_snapshots (timestamp, balance, phase, daily_roi) VALUES (?, ?, ?, ?)",
                (now, STARTING_BANKROLL, 1, 0.0),
            )
            conn.execute(
                "INSERT OR REPLACE INTO daily_start (id, date, balance) VALUES (1, ?, ?)",
                (now[:10], STARTING_BANKROLL),
            )
            conn.commit()
    finally:
        conn.close()


def record_bankroll_snapshot(balance: float, phase: int, daily_roi: Optional[float] = None) -> None:
    """
    Record a bankroll snapshot for the equity curve.
    Called by the bot on each loop iteration.
    """
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO bankroll_snapshots (timestamp, balance, phase, daily_roi) VALUES (?, ?, ?, ?)",
            (datetime.utcnow().isoformat(), balance, phase, daily_roi),
        )
        conn.commit()
    finally:
        conn.close()


def record_trade(
    event_id: int,
    market_id: int,
    runner_id: int,
    side: str,
    odds: float,
    stake: float,
    matched_at: str,
    profit: Optional[float] = None,
    phase: Optional[int] = None,
) -> None:
    """Insert a completed trade into the trades table."""
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO trades (event_id, market_id, runner_id, side, odds, stake, matched_at, profit, phase)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, market_id, runner_id, side, odds, stake, matched_at, profit, phase),
        )
        conn.commit()
    finally:
        conn.close()


def get_equity_curve() -> tuple[list[str], list[float]]:
    """
    Return (timestamps, balances) for Plotly equity chart.
    """
    conn = get_connection()
    try:
        cursor = conn.execute(
            "SELECT timestamp, balance FROM bankroll_snapshots ORDER BY timestamp"
        )
        rows = cursor.fetchall()
        timestamps = [row["timestamp"] for row in rows]
        balances = [row["balance"] for row in rows]
        return timestamps, balances
    finally:
        conn.close()


def upsert_position(
    offer_id: int,
    event_id: int,
    market_name: str,
    selection: str,
    side: str,
    odds: float,
    stake: float,
    status: str,
) -> None:
    """Insert or update a position (sync with API offers)."""
    conn = get_connection()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO positions (offer_id, event_id, market_name, selection, side, odds, stake, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                offer_id,
                event_id,
                market_name,
                selection,
                side,
                odds,
                stake,
                status,
                datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def remove_position(offer_id: int) -> None:
    """Remove a position from the positions table."""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM positions WHERE offer_id = ?", (offer_id,))
        conn.commit()
    finally:
        conn.close()


def get_latest_balance() -> Optional[float]:
    """Return the most recent balance from bankroll_snapshots."""
    conn = get_connection()
    try:
        cursor = conn.execute(
            "SELECT balance FROM bankroll_snapshots ORDER BY timestamp DESC LIMIT 1"
        )
        row = cursor.fetchone()
        return row["balance"] if row else None
    finally:
        conn.close()


def get_daily_start_balance() -> Optional[float]:
    """Return the balance at start of current day (for daily ROI calc)."""
    conn = get_connection()
    try:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        cursor = conn.execute(
            "SELECT balance FROM daily_start WHERE date = ? ORDER BY id DESC LIMIT 1",
            (today,),
        )
        row = cursor.fetchone()
        if row:
            return row["balance"]
        # Fallback: first snapshot of today
        cursor = conn.execute(
            "SELECT balance FROM bankroll_snapshots WHERE timestamp LIKE ? ORDER BY timestamp ASC LIMIT 1",
            (f"{today}%",),
        )
        row = cursor.fetchone()
        return row["balance"] if row else None
    finally:
        conn.close()


def update_daily_start(date: str, balance: float) -> None:
    """Update the daily start balance for the given date."""
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO daily_start (id, date, balance) VALUES (1, ?, ?)",
            (date, balance),
        )
        conn.commit()
    finally:
        conn.close()


def clear_positions() -> None:
    """Clear all positions (e.g. after sync with API)."""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM positions")
        conn.commit()
    finally:
        conn.close()


def _ensure_settings_table() -> None:
    """Create settings table if missing (migration for existing DBs)."""
    conn = get_connection()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            ("trading_enabled", "0"),
        )
        conn.commit()
    finally:
        conn.close()


def is_trading_enabled() -> bool:
    """Return True if trading is enabled via dashboard. Default False."""
    try:
        conn = get_connection()
        try:
            cursor = conn.execute(
                "SELECT value FROM settings WHERE key = ?", ("trading_enabled",)
            )
            row = cursor.fetchone()
            return row and row["value"] == "1"
        finally:
            conn.close()
    except sqlite3.OperationalError:
        _ensure_settings_table()
        return False


def set_trading_enabled(enabled: bool) -> None:
    """Enable or disable trading. Bot only places orders when enabled."""
    try:
        conn = get_connection()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                ("trading_enabled", "1" if enabled else "0"),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        _ensure_settings_table()
        set_trading_enabled(enabled)  # Retry


def get_event_id() -> Optional[str]:
    """Return the event ID to focus on, or None for all events."""
    try:
        conn = get_connection()
        try:
            cursor = conn.execute(
                "SELECT value FROM settings WHERE key = ?", ("event_id",)
            )
            row = cursor.fetchone()
            if row and row["value"]:
                return row["value"]
            return None
        finally:
            conn.close()
    except sqlite3.OperationalError:
        _ensure_settings_table()
        return None


def set_event_id(event_id: str) -> None:
    """Set the event ID to focus on. Empty string = all events."""
    try:
        conn = get_connection()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                ("event_id", event_id.strip() if event_id else ""),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        _ensure_settings_table()
        set_event_id(event_id)  # Retry


def is_force_phase1() -> bool:
    """Return True if Phase 1 is forced (ignore balance for phase selection). Default True."""
    try:
        conn = get_connection()
        try:
            cursor = conn.execute(
                "SELECT value FROM settings WHERE key = ?", ("force_phase1",)
            )
            row = cursor.fetchone()
            # Default True: start with Phase 1 until user switches
            return row is None or row["value"] == "1"
        finally:
            conn.close()
    except sqlite3.OperationalError:
        _ensure_settings_table()
        return True


def set_force_phase1(force: bool) -> None:
    """Force Phase 1 (True) or use balance-based phase (False)."""
    try:
        conn = get_connection()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                ("force_phase1", "1" if force else "0"),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        _ensure_settings_table()
        set_force_phase1(force)  # Retry
