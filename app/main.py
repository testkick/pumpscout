"""PumpScout server: FastAPI + a background scheduler. PUMP SCOUT runs every
scout_interval_secs; STOP GUARD / TP LADDER manage open positions on a faster
tick. Dashboard + /healthz reuse WhaleMirror's honest-diagnostics pattern."""

from __future__ import annotations
import asyncio, os, time, json
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from . import store, trade, pipeline
from .adapters import get_feed

app = FastAPI(title="PumpScout")
_STATE = {"last_scout": 0.0, "last_manage": 0.0, "scheduler_alive": 0.0,
          "last_scout_result": {}, "last_error": None, "feed_error": None}


# ── background scheduler ───────────────────────────────────────────────────
async def scheduler():
    store.init()
    while True:
        _STATE["scheduler_alive"] = time.time()
        try:
            cfg = store.get_settings()
            feed = get_feed(cfg)
            now = time.time()

            # WALLET DISCOVERY: seed on first run if empty, then refresh daily.
            # Back off on failure — if the list is still empty, retry at most
            # every 15 min (not every loop), so a bad key can't cause a retry storm.
            with store.db() as conn:
                wn = conn.execute("SELECT COUNT(*) n FROM watch_wallets").fetchone()["n"]
            since_last = now - _STATE.get("last_discover", 0)
            need_discover = (wn == 0 and since_last >= 900) or \
                            (wn > 0 and since_last >= 86400) or \
                            _STATE.get("last_discover", 0) == 0
            if need_discover and os.environ.get("SOLANATRACKER_API_KEY"):
                _STATE["last_discover"] = now
                got = await asyncio.to_thread(pipeline.discover_wallets, feed, cfg)
                _STATE["last_discover_result"] = got

            # CREDIT GATE: Free tier is ~2.5K/month. Track calls per UTC day and
            # pause sweeps once the daily budget is spent, so it lasts the month.
            import datetime
            today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
            if _STATE.get("credit_day") != today:
                _STATE["credit_day"] = today
                _STATE["credit_used"] = 0
            budget = cfg.get("daily_credit_budget", 70)
            credit_ok = _STATE.get("credit_used", 0) < budget

            # PUMP SCOUT + evaluate, every scout_interval_secs (if budget remains)
            if credit_ok and now - _STATE["last_scout"] >= cfg["scout_interval_secs"]:
                _STATE["last_scout"] = now
                since = now - cfg["scout_interval_secs"] * 2
                # refresh the whale-buy index on its own cadence (bulk, cheap)
                if now - _STATE.get("last_whale_index", 0) >= cfg.get("whale_index_refresh_secs", 1800):
                    _STATE["last_whale_index"] = now
                    await asyncio.to_thread(pipeline.refresh_whale_index, feed, cfg)

                fresh = await asyncio.to_thread(pipeline.pump_scout, feed, since)
                bought = 0
                cap = cfg.get("max_candidates_per_sweep", 8)
                for tok in fresh[:cap]:                 # cap ST calls per sweep
                    res = await asyncio.to_thread(pipeline.evaluate_token, feed, tok, cfg)
                    if res["decision"] == "buy":
                        if await asyncio.to_thread(trade.execute_buy, res, cfg):
                            bought += 1
                _STATE["credit_used"] = _STATE.get("credit_used", 0) + getattr(feed, "st_calls", 0)
                _STATE["last_scout_result"] = {"fresh": len(fresh), "bought": bought,
                                               "credits_today": _STATE["credit_used"], "ts": now}
                _STATE["feed_error"] = feed.last_error
                if _STATE["credit_used"] >= budget:
                    store.scribe("Credit", "info",
                        f"daily credit budget ({budget}) reached — pausing sweeps until tomorrow (UTC)")

            # STOP GUARD + TP LADDER + equity snapshot, every ~20s
            if now - _STATE["last_manage"] >= 20:
                _STATE["last_manage"] = now
                await asyncio.to_thread(trade.manage_positions, feed, cfg)
                await asyncio.to_thread(trade.snapshot_equity, cfg)

            _STATE["last_error"] = None
        except Exception as e:  # never let the loop die silently
            _STATE["last_error"] = f"{type(e).__name__}: {e}"
            store.scribe("Scheduler", "error", _STATE["last_error"])
        await asyncio.sleep(5)


@app.on_event("startup")
async def _startup():
    store.init()
    asyncio.create_task(scheduler())


# ── auth ───────────────────────────────────────────────────────────────────
def require(request: Request):
    tok = request.cookies.get("ps_session")
    if not store.valid_session(tok):
        raise HTTPException(401, "login required")

@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    if body.get("password") != os.environ.get("APP_PASSWORD", "changeme"):
        raise HTTPException(401, "bad password")
    tok = store.new_session()
    r = JSONResponse({"ok": True})
    r.set_cookie("ps_session", tok, httponly=True, samesite="lax",
                 secure=os.environ.get("COOKIE_SECURE", "true") == "true")
    return r


# ── health (honest diagnostics) ────────────────────────────────────────────
@app.get("/healthz")
def healthz():
    cfg = store.get_settings()
    with store.db() as conn:
        snaps = conn.execute("SELECT COUNT(*) n FROM pnl_snapshots").fetchone()["n"]
        cands = conn.execute("SELECT COUNT(*) n FROM candidates").fetchone()["n"]
        open_n = conn.execute("SELECT COUNT(*) n FROM positions WHERE status='open'").fetchone()["n"]
        tokens = conn.execute("SELECT COUNT(*) n FROM tokens").fetchone()["n"]
        wallets = conn.execute("SELECT COUNT(*) n FROM watch_wallets").fetchone()["n"]
    now = time.time()
    return {
        "ok": True,
        "dry_run": cfg["dry_run"],
        "data_provider": "helius" + ("+solanatracker" if os.environ.get("SOLANATRACKER_API_KEY") else ""),
        "helius_key_set": bool(os.environ.get("HELIUS_API_KEY")),
        "solanatracker_key_set": bool(os.environ.get("SOLANATRACKER_API_KEY")),
        "scheduler_alive_secs_ago": round(now - _STATE["scheduler_alive"]) if _STATE["scheduler_alive"] else None,
        "last_scout_secs_ago": round(now - _STATE["last_scout"]) if _STATE["last_scout"] else None,
        "last_scout_result": _STATE["last_scout_result"],
        "feed_error": _STATE["feed_error"],
        "last_error": _STATE["last_error"],
        "counts": {"tokens_seen": tokens, "candidates": cands, "open_positions": open_n,
                   "snapshots": snaps, "watch_wallets": wallets},
        "equity": round(trade.current_equity(cfg), 2),
        "credits_used_today": _STATE.get("credit_used", 0),
        "daily_credit_budget": cfg.get("daily_credit_budget", 70),
    }


# ── data APIs for the dashboard ────────────────────────────────────────────
@app.get("/api/settings")
def api_settings(request: Request):
    require(request); return {"settings": store.get_settings()}

@app.post("/api/settings")
async def api_save_settings(request: Request):
    require(request); body = await request.json()
    store.save_settings(body.get("settings") or body); return {"ok": True}

@app.get("/api/candidates")
def api_candidates(request: Request, limit: int = 100):
    require(request)
    with store.db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM candidates ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
    return {"candidates": rows}

@app.get("/api/positions")
def api_positions(request: Request):
    require(request)
    with store.db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM positions ORDER BY id DESC LIMIT 300").fetchall()]
    return {"positions": rows}

@app.get("/api/scribe")
def api_scribe(request: Request, limit: int = 200):
    require(request); return {"scribe": store.recent_scribe(limit)}

@app.get("/api/performance")
def api_performance(request: Request):
    require(request)
    cfg = store.get_settings()
    with store.db() as conn:
        snaps = [dict(r) for r in conn.execute(
            "SELECT ts,equity,realized FROM pnl_snapshots ORDER BY ts ASC").fetchall()]
        closed_rows = [dict(r) for r in conn.execute(
            "SELECT pnl, close_reason FROM positions WHERE status='closed'").fetchall()]
        open_n = conn.execute("SELECT COUNT(*) n FROM positions WHERE status='open'").fetchone()["n"]
        # candidate funnel (SCRIBE / candidates give the whole decision story)
        scouted = conn.execute("SELECT COUNT(DISTINCT mint) n FROM tokens").fetchone()["n"]
        evaluated = conn.execute("SELECT COUNT(*) n FROM candidates").fetchone()["n"]
        bought = conn.execute("SELECT COUNT(*) n FROM candidates WHERE decision='buy'").fetchone()["n"]
        rug_blocked = conn.execute("SELECT COUNT(*) n FROM candidates WHERE rug_pass=0").fetchone()["n"]
        bundle_blocked = conn.execute(
            "SELECT COUNT(*) n FROM candidates WHERE decision='skip' AND decision_reason LIKE '%BUNDLE%'").fetchone()["n"]

    pnls = [c["pnl"] or 0 for c in closed_rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    from collections import Counter
    reasons = Counter(c["close_reason"] or "?" for c in closed_rows)
    return {
        "snapshots": snaps,
        "equity": round(trade.current_equity(cfg), 2),
        "starting_equity": cfg["starting_equity_usd"],
        "realized": round(sum(pnls), 2),
        "closed": len(pnls), "open": open_n,
        "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / len(pnls) * 100, 1) if pnls else None,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "best": round(max(pnls), 2) if pnls else 0.0,
        "worst": round(min(pnls), 2) if pnls else 0.0,
        "close_reasons": dict(reasons),
        "funnel": {"scouted": scouted, "evaluated": evaluated, "bought": bought,
                   "rug_blocked": rug_blocked, "bundle_blocked": bundle_blocked},
    }


# ── wallet management (the 210) ────────────────────────────────────────────
@app.post("/api/wallets/import")
async def api_import_wallets(request: Request):
    require(request); body = await request.json()
    rows = body.get("wallets", [])   # [{address,label,kind}]
    with store.db() as conn:
        for w in rows:
            if not w.get("address"): continue
            conn.execute("INSERT OR REPLACE INTO watch_wallets (address,label,kind,added) VALUES (?,?,?,?)",
                         (w["address"].strip(), w.get("label",""), w.get("kind","whale"), time.time()))
    return {"ok": True, "count": len(rows)}

@app.post("/api/wallets/discover")
async def api_discover(request: Request):
    require(request)
    cfg = store.get_settings()
    feed = get_feed(cfg)
    n = await asyncio.to_thread(pipeline.discover_wallets, feed, cfg)
    return {"ok": True, "discovered": n, "error": feed.last_error}

@app.get("/api/wallets")
def api_wallets(request: Request):
    require(request)
    with store.db() as conn:
        return {"wallets": [dict(r) for r in conn.execute("SELECT * FROM watch_wallets").fetchall()]}


_STATIC = os.path.join(os.path.dirname(__file__), "static")
@app.get("/")
def index():
    idx = os.path.join(_STATIC, "index.html")
    return FileResponse(idx) if os.path.exists(idx) else HTMLResponse("<h1>PumpScout</h1>")
if os.path.isdir(_STATIC):
    app.mount("/static", StaticFiles(directory=_STATIC), name="static")
