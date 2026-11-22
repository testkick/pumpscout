"""PumpScout store — SQLite on a Railway volume. Chassis reused from WhaleMirror
(WAL, settings kv, pnl snapshots, sessions auth, housekeeping) with a schema
shaped for Solana memecoin sniping: tokens seen, scored candidates, paper
positions with a TP ladder + trailing stop, and a decision log (Scribe)."""

from __future__ import annotations
import json, os, sqlite3, time, threading, secrets

DB_PATH = os.environ.get("DB_PATH", "/data/pumpscout.db")
_LOCAL = threading.local()

def db():
    conn = getattr(_LOCAL, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")      # reader + writer concurrency
        conn.execute("PRAGMA busy_timeout=10000")     # 10s before 'database is locked'
        _LOCAL.conn = conn
    return conn

def init():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, created REAL);

        -- every new token PUMP SCOUT sees (dedup by mint)
        CREATE TABLE IF NOT EXISTS tokens (
            mint TEXT PRIMARY KEY,
            symbol TEXT, name TEXT,
            first_seen REAL, launch_ts REAL,
            pool TEXT,                 -- pump.fun | raydium | ...
            dev_wallet TEXT,
            raw TEXT                   -- JSON blob of the launch event
        );

        -- a scored candidate: one row per (mint, time the pipeline scored it)
        CREATE TABLE IF NOT EXISTS candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, mint TEXT, symbol TEXT,
            age_secs REAL, price REAL, mcap REAL, liquidity REAL,
            -- per-module signals (NULL = module didn't run / no data)
            bounce INTEGER, whale_buys INTEGER, kol_buys INTEGER,
            new_holders INTEGER, buy_pressure REAL,
            rug_pass INTEGER, rug_reason TEXT,
            bundle_clusters INTEGER,
            score REAL, decision TEXT, decision_reason TEXT
        );

        -- paper positions with TP ladder + trailing stop
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, mint TEXT, symbol TEXT,
            mode TEXT,                 -- dry_run | live
            entry_price REAL, usd REAL, tokens REAL,
            equity_frac REAL,          -- SIZER: fraction of equity at entry
            peak_price REAL,           -- for the 25% trailing stop
            tp1_done INTEGER DEFAULT 0, tp2_done INTEGER DEFAULT 0,
            realized_usd REAL DEFAULT 0,
            last_price REAL, pnl REAL,
            status TEXT,               -- open | closed
            close_reason TEXT, closed_ts REAL
        );

        -- SCRIBE: every decision, verbatim, append-only
        CREATE TABLE IF NOT EXISTS scribe (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, module TEXT, mint TEXT, symbol TEXT,
            level TEXT,                -- info | act | skip | error
            message TEXT, data TEXT
        );

        -- equity curve snapshots for the dashboard
        CREATE TABLE IF NOT EXISTS pnl_snapshots (
            ts REAL, mode TEXT, equity REAL, realized REAL, unrealized REAL, open_n INTEGER
        );

        -- whale / fund / smart / KOL wallets we watch (the 210)
        CREATE TABLE IF NOT EXISTS watch_wallets (
            address TEXT PRIMARY KEY,
            label TEXT, kind TEXT,     -- whale | fund | smart | kol
            added REAL
        );
        CREATE INDEX IF NOT EXISTS idx_cand_mint ON candidates(mint);
        CREATE INDEX IF NOT EXISTS idx_cand_ts ON candidates(ts);
        CREATE INDEX IF NOT EXISTS idx_pos_status ON positions(status);
        CREATE INDEX IF NOT EXISTS idx_scribe_ts ON scribe(ts);
        """)

# ── settings ──────────────────────────────────────────────────────────────
DEFAULT_SETTINGS = {
    "dry_run": True,                   # paper by default — turn off only when you mean it
    "scout_interval_secs": 600,        # PUMP SCOUT every 10 min
    "starting_equity_usd": 1000.0,     # paper bankroll
    # SIZER
    "equity_frac_per_trade": 0.015,    # 1.5% of equity per trade (your "1-2%")
    "max_concurrent_positions": 8,
    # SCORE GATE — how many signals a candidate needs to get bought
    "min_score": 4.0,
    # RUG GATE
    "require_rug_pass": True,
    # BUNDLE HUNTER
    "max_bundle_clusters": 3,          # skip if more bundled clusters than this
    # TP LADDER
    "tp1_mult": 2.0, "tp1_sell_frac": 0.5,   # sell 50% at 2x
    "tp2_mult": 3.0, "tp2_sell_frac": 0.5,   # sell 50% of remainder at 3x
    # STOP GUARD
    "hard_stop_pct": 0.12,             # -12% hard stop from entry
    "trail_pct": 0.25,                 # 25% trailing stop from peak
    # data provider (bound once you name your key)
    "data_provider": "unset",          # helius | birdeye | moralis | unset
    # WALLET DISCOVERY (auto-populate the watch list from SolanaTracker)
    "watch_wallet_target": 210,
    "discover_min_win_rate": 0.55,
    "discover_min_trades": 20,
    "discover_days": 30,
}

def get_settings() -> dict:
    with db() as conn:
        row = conn.execute("SELECT v FROM kv WHERE k='settings'").fetchone()
    s = dict(DEFAULT_SETTINGS)
    if row:
        try: s.update(json.loads(row["v"]))
        except Exception: pass
    return s

def save_settings(patch: dict):
    s = get_settings(); s.update(patch or {})
    with db() as conn:
        conn.execute("INSERT INTO kv (k,v) VALUES ('settings',?) "
                     "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (json.dumps(s),))

# ── scribe (the logger every module writes to) ────────────────────────────
def scribe(module: str, level: str, message: str, mint: str = "", symbol: str = "", data: dict | None = None):
    with db() as conn:
        conn.execute("INSERT INTO scribe (ts,module,mint,symbol,level,message,data) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (time.time(), module, mint, symbol, level, message,
                      json.dumps(data) if data else None))

def recent_scribe(limit: int = 200) -> list[dict]:
    with db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM scribe ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

# ── auth (reused pattern) ─────────────────────────────────────────────────
def new_session() -> str:
    tok = secrets.token_urlsafe(32)
    with db() as conn:
        conn.execute("INSERT INTO sessions (token,created) VALUES (?,?)", (tok, time.time()))
    return tok

def valid_session(tok: str) -> bool:
    if not tok: return False
    with db() as conn:
        return conn.execute("SELECT 1 FROM sessions WHERE token=?", (tok,)).fetchone() is not None
