"""Trade engine: SIZER, EXECUTOR, TP LADDER, STOP GUARD. All paper in dry_run.
The EXECUTOR is deliberately a thin interface so a live on-chain filler can slot
in later — but it stays simulated until dry_run is explicitly turned off."""

from __future__ import annotations
import time
from . import store


# ── equity accounting ─────────────────────────────────────────────────────
def current_equity(cfg: dict) -> float:
    """Starting bankroll + realized P&L (closed) + unrealized (open at last_price)."""
    with store.db() as conn:
        realized = conn.execute(
            "SELECT COALESCE(SUM(realized_usd),0) r FROM positions").fetchone()["r"]
        open_rows = conn.execute(
            "SELECT usd, tokens, entry_price, last_price, realized_usd "
            "FROM positions WHERE status='open'").fetchall()
    unreal = 0.0
    for p in open_rows:
        last = p["last_price"] or p["entry_price"]
        # value of tokens still held = remaining tokens * last price
        # remaining tokens tracked implicitly via realized; approximate with mark
        unreal += (last - p["entry_price"]) * (p["tokens"] or 0)
    return cfg["starting_equity_usd"] + realized + unreal


def open_position_count() -> int:
    with store.db() as conn:
        return conn.execute("SELECT COUNT(*) n FROM positions WHERE status='open'").fetchone()["n"]


# ── 8. SIZER ──────────────────────────────────────────────────────────────
def size_trade(cfg: dict) -> float:
    """USD to deploy = equity_frac_per_trade × current equity. Returns 0 if the
    concurrent-position cap is hit (SIZER refuses to over-deploy)."""
    if open_position_count() >= cfg["max_concurrent_positions"]:
        return 0.0
    eq = current_equity(cfg)
    return round(eq * cfg["equity_frac_per_trade"], 2)


# ── 9. EXECUTOR ───────────────────────────────────────────────────────────
def execute_buy(cand: dict, cfg: dict) -> dict | None:
    """Open a paper position. In live mode this is where the on-chain swap goes;
    here it fills at the candidate's price (optionally a slippage bump)."""
    usd = size_trade(cfg)
    sym, mint = cand["symbol"], cand["mint"]
    if usd <= 0:
        store.scribe("Sizer", "skip", f"{sym or mint[:8]}: position cap reached, no size", mint=mint, symbol=sym)
        return None
    price = cand["price"]
    if price <= 0:
        store.scribe("Executor", "skip", f"{sym or mint[:8]}: no price, cannot fill", mint=mint, symbol=sym)
        return None
    # simulated fill (≈100ms) — slippage applied on the buy
    fill = price * (1 + cfg.get("sim_slippage", 0.01))
    tokens = usd / fill
    with store.db() as conn:
        conn.execute("""INSERT INTO positions
            (ts,mint,symbol,mode,entry_price,usd,tokens,equity_frac,peak_price,last_price,pnl,status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?, 'open')""",
            (time.time(), mint, sym, "dry_run" if cfg["dry_run"] else "live",
             fill, usd, tokens, cfg["equity_frac_per_trade"], fill, fill, 0.0))
    store.scribe("Executor", "act",
                 f"BUY {sym or mint[:8]} ${usd:.2f} @ {fill:.8f} ({tokens:.0f} tokens)",
                 mint=mint, symbol=sym, data={"usd": usd, "fill": fill})
    return {"mint": mint, "usd": usd, "fill": fill, "tokens": tokens}


def _sell(conn, pos, frac: float, price: float, reason: str):
    """Sell `frac` of the ORIGINAL token amount at price; bank realized P&L."""
    sell_tokens = pos["tokens"] * frac
    proceeds = sell_tokens * price
    cost = sell_tokens * pos["entry_price"]
    realized = proceeds - cost
    store.scribe("TpLadder" if "TP" in reason else "StopGuard", "act",
                 f"SELL {frac*100:.0f}% {pos['symbol'] or pos['mint'][:8]} @ {price:.8f} ({reason}) P&L ${realized:+.2f}",
                 mint=pos["mint"], symbol=pos["symbol"])
    return realized


# ── 10+11. TP LADDER + STOP GUARD (run on every monitor tick) ─────────────
def manage_positions(feed, cfg: dict):
    """For each open position: refresh price, apply TP ladder (2x/3x) and stop
    guard (-12% hard, 25% trail). Updates peak for the trailing stop."""
    with store.db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM positions WHERE status='open'").fetchall()]
    for pos in rows:
        mk = feed.token_market(pos["mint"])
        price = mk.get("price") or pos["last_price"] or pos["entry_price"]
        entry = pos["entry_price"]
        peak = max(pos["peak_price"] or entry, price)
        realized_add = 0.0
        closed = False
        close_reason = None

        # TP LADDER — sell 50% at 2x, then 50% of remainder at 3x
        with store.db() as conn:
            if not pos["tp1_done"] and price >= entry * cfg["tp1_mult"]:
                realized_add += _sell(conn, pos, cfg["tp1_sell_frac"], price, "TP1 2x")
                conn.execute("UPDATE positions SET tp1_done=1 WHERE id=?", (pos["id"],))
                pos["tp1_done"] = 1
            if pos["tp1_done"] and not pos["tp2_done"] and price >= entry * cfg["tp2_mult"]:
                # sell tp2_sell_frac of the ORIGINAL (the remaining half)
                realized_add += _sell(conn, pos, cfg["tp2_sell_frac"] * (1 - cfg["tp1_sell_frac"]) / 1, price, "TP2 3x")
                conn.execute("UPDATE positions SET tp2_done=1 WHERE id=?", (pos["id"],))
                pos["tp2_done"] = 1

            # STOP GUARD — hard stop from entry, trailing stop from peak
            hard_stop = entry * (1 - cfg["hard_stop_pct"])
            trail_stop = peak * (1 - cfg["trail_pct"])
            stop_level = max(hard_stop, trail_stop)
            if price <= stop_level:
                # sell everything still held
                sold_frac = 0.0
                if pos["tp1_done"]: sold_frac += cfg["tp1_sell_frac"]
                if pos["tp2_done"]: sold_frac += cfg["tp2_sell_frac"] * (1 - cfg["tp1_sell_frac"])
                remaining = 1 - sold_frac
                if remaining > 0.001:
                    reason = "hard stop" if price <= hard_stop else "trail stop"
                    realized_add += _sell(conn, pos, remaining, price, reason)
                closed = True
                close_reason = "hard stop" if price <= hard_stop else "trail stop"

            # if both TPs done, close the runner out at price (nothing left meaningful)
            if pos["tp1_done"] and pos["tp2_done"] and not closed:
                closed = True; close_reason = "TP ladder complete"

            new_realized = (pos["realized_usd"] or 0) + realized_add
            unreal = (price - entry) * pos["tokens"]
            conn.execute("""UPDATE positions SET last_price=?, peak_price=?, realized_usd=?,
                            pnl=?, status=?, close_reason=?, closed_ts=? WHERE id=?""",
                (price, peak, new_realized,
                 new_realized if closed else new_realized + unreal,
                 "closed" if closed else "open",
                 close_reason, time.time() if closed else None, pos["id"]))


# ── equity snapshot for the dashboard ─────────────────────────────────────
def snapshot_equity(cfg: dict):
    with store.db() as conn:
        realized = conn.execute("SELECT COALESCE(SUM(realized_usd),0) r FROM positions").fetchone()["r"]
        open_n = conn.execute("SELECT COUNT(*) n FROM positions WHERE status='open'").fetchone()["n"]
    eq = current_equity(cfg)
    with store.db() as conn:
        conn.execute("INSERT INTO pnl_snapshots (ts,mode,equity,realized,unrealized,open_n) VALUES (?,?,?,?,?,?)",
                     (time.time(), "dry_run" if cfg["dry_run"] else "live",
                      eq, realized, eq - cfg["starting_equity_usd"] - realized, open_n))
