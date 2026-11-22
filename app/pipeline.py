"""The 12-module pipeline. A sweep runs PUMP SCOUT, then for each fresh token
runs the signal + gate modules, SCORES it, and (if it passes) hands it to the
SIZER/EXECUTOR. STOP GUARD + TP LADDER run continuously on open positions.
SCRIBE records every decision.

Modules map 1:1 to the user's 12:
  1 PumpScout  2 BounceFinder  3 WhaleWatch  4 KolWatch  5 HolderFlow
  6 RugGate    7 BundleHunter  8 Sizer       9 Executor 10 TpLadder
 11 StopGuard 12 Scribe (store.scribe, woven throughout)
"""

from __future__ import annotations
import time
from . import store
from .adapters import get_feed


# ── WALLET DISCOVERY (auto-populate the 210) ──────────────────────────────
def discover_wallets(feed, cfg: dict) -> int:
    """Pull top-PnL wallets from SolanaTracker into watch_wallets. Only ADDS /
    refreshes discovered ('smart') wallets; never removes manually-added ones."""
    want = int(cfg.get("watch_wallet_target", 210))
    found = feed.discover_top_wallets(
        limit=want,
        min_win_rate=cfg.get("discover_min_win_rate", 0.55),
        min_trades=cfg.get("discover_min_trades", 20),
        days=cfg.get("discover_days", 30))
    if not found:
        store.scribe("Discover", "error", feed.last_error or "no wallets returned")
        return 0
    added = 0
    with store.db() as conn:
        for w in found:
            conn.execute("""INSERT INTO watch_wallets (address,label,kind,added)
                VALUES (?,?,?,?) ON CONFLICT(address) DO UPDATE SET label=excluded.label""",
                (w["address"], w.get("label",""), w.get("kind","smart"), time.time()))
            added += 1
    store.scribe("Discover", "act", f"watch list refreshed: {added} top-PnL wallets")
    return added


# ── 1. PUMP SCOUT ─────────────────────────────────────────────────────────
def pump_scout(feed, since_ts: float) -> list:
    toks = feed.new_tokens(since_ts)
    fresh = []
    with store.db() as conn:
        for t in toks:
            exists = conn.execute("SELECT 1 FROM tokens WHERE mint=?", (t.mint,)).fetchone()
            if exists:
                continue
            conn.execute("""INSERT OR IGNORE INTO tokens
                (mint,symbol,name,first_seen,launch_ts,pool,dev_wallet,raw)
                VALUES (?,?,?,?,?,?,?,?)""",
                (t.mint, t.symbol, t.name, time.time(), t.launch_ts, t.pool,
                 t.dev_wallet, __import__("json").dumps(t.raw)[:4000]))
            fresh.append(t)
    store.scribe("PumpScout", "info", f"{len(fresh)} new tokens (of {len(toks)} seen)")
    if feed.last_error:
        store.scribe("PumpScout", "error", feed.last_error)
    return fresh


# ── 2. BOUNCE FINDER ──────────────────────────────────────────────────────
def bounce_finder(feed, mint: str) -> int:
    """1 if price is recovering off a recent low (last point > trough and
    trough was below the window's start), else 0."""
    prices = feed.price_history(mint, points=30)
    if len(prices) < 6:
        return 0
    trough = min(prices)
    last = prices[-1]
    # bounced if current is >=12% above the trough and the trough is in the past
    if trough > 0 and last >= trough * 1.12 and prices.index(trough) < len(prices) - 1:
        return 1
    return 0


# ── 3+4. WHALE WATCH + KOL WATCH ──────────────────────────────────────────
def wallet_signals(feed, mint: str, window_secs: int = 300) -> dict:
    since = time.time() - window_secs
    wallets = store_watch_wallets()
    whale_set = {w["address"] for w in wallets if w["kind"] in ("whale", "fund", "smart")}
    kol_set = {w["address"] for w in wallets if w["kind"] == "kol"}
    buys = feed.wallet_buys(list(whale_set | kol_set), since)
    whale_buys = sum(1 for b in buys if b["mint"] == mint and b["wallet"] in whale_set)
    kol_buys = sum(1 for b in buys if b["mint"] == mint and b["wallet"] in kol_set)
    return {"whale_buys": whale_buys, "kol_buys": kol_buys}


def store_watch_wallets() -> list[dict]:
    with store.db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM watch_wallets").fetchall()]


# ── 5. HOLDER FLOW ────────────────────────────────────────────────────────
def holder_flow(feed, mint: str) -> dict:
    h = feed.holders(mint)
    return {"new_holders": h.get("new_recent", 0), "buy_pressure": h.get("buy_pressure", 0.0)}


# ── 6. RUG GATE ───────────────────────────────────────────────────────────
def rug_gate(feed, mint: str, dev_wallet: str) -> dict:
    r = feed.rug_report(mint, dev_wallet)
    return {"pass": bool(r.get("pass")), "reason": r.get("reason", "")}


# ── 7. BUNDLE HUNTER ──────────────────────────────────────────────────────
def bundle_hunter(feed, mint: str) -> int:
    b = feed.bundle_report(mint)
    return int(b.get("clusters", 0))


# ── SCORING + DECISION ────────────────────────────────────────────────────
def score_candidate(sig: dict, cfg: dict) -> tuple[float, str, str]:
    """Combine module signals into a score + buy/skip decision. Gates (Rug,
    Bundle) can veto regardless of score."""
    # hard gates first
    if cfg["require_rug_pass"] and not sig.get("rug_pass"):
        return 0.0, "skip", f"RUG GATE blocked: {sig.get('rug_reason','')}"
    if (sig.get("bundle_clusters") or 0) > cfg["max_bundle_clusters"]:
        return 0.0, "skip", f"BUNDLE HUNTER: {sig['bundle_clusters']} clusters > {cfg['max_bundle_clusters']}"

    # additive score from the signal modules
    score = 0.0
    if sig.get("bounce"): score += 1.0
    score += min(sig.get("whale_buys", 0), 3) * 1.0      # up to +3
    score += min(sig.get("kol_buys", 0), 3) * 0.75       # up to +2.25
    if (sig.get("new_holders") or 0) >= 20: score += 1.0
    if (sig.get("buy_pressure") or 0) >= 0.6: score += 1.0

    if score >= cfg["min_score"]:
        return score, "buy", f"score {score:.1f} ≥ {cfg['min_score']}"
    return score, "skip", f"score {score:.1f} < {cfg['min_score']}"


def evaluate_token(feed, tok, cfg: dict) -> dict:
    """Run modules 2-7 on one token, score it, log the candidate."""
    mint, sym = tok.mint, tok.symbol
    mk = feed.token_market(mint)
    rg = rug_gate(feed, mint, tok.dev_wallet)
    ws = wallet_signals(feed, mint, cfg.get("kol_window_secs", 300))
    hf = holder_flow(feed, mint)
    sig = {
        "bounce": bounce_finder(feed, mint),
        "whale_buys": ws["whale_buys"], "kol_buys": ws["kol_buys"],
        "new_holders": hf["new_holders"], "buy_pressure": hf["buy_pressure"],
        "rug_pass": rg["pass"], "rug_reason": rg["reason"],
        "bundle_clusters": bundle_hunter(feed, mint),
    }
    score, decision, reason = score_candidate(sig, cfg)
    with store.db() as conn:
        conn.execute("""INSERT INTO candidates
            (ts,mint,symbol,age_secs,price,mcap,liquidity,bounce,whale_buys,kol_buys,
             new_holders,buy_pressure,rug_pass,rug_reason,bundle_clusters,score,decision,decision_reason)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (time.time(), mint, sym, max(0, time.time() - (tok.launch_ts or time.time())),
             mk["price"], mk["mcap"], mk["liquidity"],
             sig["bounce"], sig["whale_buys"], sig["kol_buys"], sig["new_holders"],
             sig["buy_pressure"], int(sig["rug_pass"]), sig["rug_reason"],
             sig["bundle_clusters"], score, decision, reason))
    store.scribe("Score", "act" if decision == "buy" else "skip",
                 f"{sym or mint[:8]} → {decision} ({reason})", mint=mint, symbol=sym, data=sig)
    return {"mint": mint, "symbol": sym, "price": mk["price"],
            "score": score, "decision": decision, "reason": reason, "sig": sig}
