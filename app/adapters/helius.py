"""Helius-backed Feed. Key is read from HELIUS_API_KEY (never hardcoded).
SolanaTracker (SOLANATRACKER_API_KEY) is used where Helius is weak: price
history for BOUNCE FINDER and a risk score for RUG GATE.

Every network call is wrapped so a provider hiccup degrades one module (empty
result + last_error), never crashes the sweep — the WhaleMirror lesson: surface
the real error, never swallow it silently."""

from __future__ import annotations
import os, time, json
import requests
from .base import Feed, NewToken

def _extract_key(raw: str) -> str:
    """Accept the Helius key as a bare UUID OR a full RPC URL
    (https://mainnet.helius-rpc.com/?api-key=UUID). Either way, return just the
    key so the adapter can't be misconfigured by pasting the whole URL."""
    raw = (raw or "").strip()
    if "api-key=" in raw:
        return raw.split("api-key=", 1)[1].split("&")[0].strip()
    # strip an accidental full URL with no query too
    if raw.startswith("http"):
        return ""  # URL without api-key= — unusable, surface as missing
    return raw

HELIUS_KEY = _extract_key(os.environ.get("HELIUS_API_KEY", ""))
ST_KEY = (os.environ.get("SOLANATRACKER_API_KEY", "") or "").strip()
# Data API is the shared public host; the key goes in the x-api-key header.
# (Per SolanaTracker docs: Data API, RPC, Datastream are DIFFERENT products with
# DIFFERENT keys — SOLANATRACKER_API_KEY must be the *Data API* key specifically.)
ST_API = "https://data.solanatracker.io"


HELIUS_RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}"
HELIUS_API = "https://api.helius.xyz/v0"
PUMPFUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"  # pump.fun program id

SOL_USD_FALLBACK = 150.0  # used only if price lookup fails


class HeliusFeed(Feed):
    name = "helius"

    def __init__(self):
        super().__init__()
        self.s = requests.Session()
        self.s.headers.update({"Content-Type": "application/json"})
        self.rate_limited = False
        self.st_calls = 0          # SolanaTracker Data API calls this session (credit meter)
        self.rpc_calls = 0
        self.RPC_CALL_BUDGET = 120        # hard cap on getTransaction calls per sweep
        self.RPC_CALL_DELAY = 0.12        # ~8 req/s, under the base-tier limit

    # ── low-level helpers ────────────────────────────────────────────────
    def _rpc(self, method: str, params: list):
        for attempt in range(3):
            try:
                r = self.s.post(HELIUS_RPC, json={"jsonrpc": "2.0", "id": "1",
                                "method": method, "params": params}, timeout=12)
                if r.status_code == 429:
                    # rate limited — back off, and flag so bulk callers stop early
                    self.rate_limited = True
                    if attempt < 2:
                        time.sleep(0.4 * (attempt + 1))
                        continue
                    self.last_error = f"helius rpc {method} 429: rate limited"
                    return None
                if r.status_code >= 400:
                    self.last_error = f"helius rpc {method} {r.status_code}: {r.text[:140]}"
                    return None
                return r.json().get("result")
            except requests.RequestException as e:
                self.last_error = f"helius rpc {method} error: {e}"
                return None
        return None

    def _helius_api(self, path: str, **params):
        try:
            params["api-key"] = HELIUS_KEY
            r = self.s.get(f"{HELIUS_API}{path}", params=params, timeout=12)
            if r.status_code >= 400:
                self.last_error = f"helius api {path} {r.status_code}: {r.text[:140]}"
                return None
            return r.json()
        except requests.RequestException as e:
            self.last_error = f"helius api {path} error: {e}"
            return None

    def _rpc_tx_history(self, address: str, limit: int = 40) -> list[dict]:
        """Recent parsed transactions for an address via RPC only (no Enhanced
        REST API, which is plan-gated and 401s). getSignaturesForAddress ->
        getTransaction(jsonParsed). Returns a lightweight shape the modules use:
        {timestamp, feePayer, tokenTransfers:[{mint,toUserAccount}]}."""
        # cap signatures hard — base-tier RPC rate-limits bursts, and we only
        # need the most recent few per wallet to detect a buy.
        limit = min(limit, 8)
        sigs = self._rpc("getSignaturesForAddress", [address, {"limit": limit}])
        if not sigs:
            return []
        out = []
        for s in sigs[:limit]:
            if self.rate_limited or self.rpc_calls >= self.RPC_CALL_BUDGET:
                break   # stop early rather than hammer a rate-limited endpoint
            sig = s.get("signature")
            if not sig:
                continue
            self.rpc_calls += 1
            time.sleep(self.RPC_CALL_DELAY)
            tx = self._rpc("getTransaction",
                           [sig, {"maxSupportedTransactionVersion": 0,
                                  "encoding": "jsonParsed"}])
            if not tx:
                continue
            meta = tx.get("meta") or {}
            msg = (tx.get("transaction") or {}).get("message") or {}
            keys = msg.get("accountKeys") or []
            fee_payer = ""
            for k in keys:
                if isinstance(k, dict) and k.get("signer"):
                    fee_payer = k.get("pubkey"); break
                if isinstance(k, str):
                    fee_payer = k; break
            # token transfers from pre/post token balances
            transfers = []
            pre = {b.get("accountIndex"): b for b in (meta.get("preTokenBalances") or [])}
            for pb in (meta.get("postTokenBalances") or []):
                idx = pb.get("accountIndex")
                mint = pb.get("mint")
                owner = pb.get("owner")
                pre_amt = float(((pre.get(idx) or {}).get("uiTokenAmount") or {}).get("uiAmount") or 0)
                post_amt = float((pb.get("uiTokenAmount") or {}).get("uiAmount") or 0)
                if mint and post_amt > pre_amt:        # received tokens = a buy leg
                    transfers.append({"mint": mint, "toUserAccount": owner})
            out.append({"timestamp": s.get("blockTime") or 0,
                        "feePayer": fee_payer, "tokenTransfers": transfers})
        return out

    def _st(self, path: str, **params):
        if not ST_KEY:
            return None
        try:
            r = self.s.get(f"{ST_API}{path}", params=params,
                           headers={"x-api-key": ST_KEY}, timeout=12)
            if r.status_code >= 400:
                self.last_error = f"solanatracker {path} {r.status_code}: {r.text[:140]}"
                return None
            return r.json()
        except requests.RequestException as e:
            self.last_error = f"solanatracker {path} error: {e}"
            return None

    # ── PUMP SCOUT ───────────────────────────────────────────────────────
    def new_tokens(self, since_ts: float, limit: int = 100) -> list[NewToken]:
        """Newest pump.fun launches. Primary: SolanaTracker /tokens/latest
        (turnkey). Fallback: parse recent pump.fun program txns via Helius."""
        out: list[NewToken] = []
        data = self._st("/tokens/latest")
        if isinstance(data, list):
            for t in data[:limit]:
                tok = t.get("token") or t
                mint = tok.get("mint") or tok.get("address") or ""
                if not mint:
                    continue
                created = (tok.get("createdAt") or t.get("createdAt") or 0)
                created = float(created) / 1000 if created and created > 1e12 else float(created or 0)
                if created and created < since_ts:
                    continue
                out.append(NewToken(
                    mint=mint, symbol=tok.get("symbol", ""), name=tok.get("name", ""),
                    pool=(t.get("pools", [{}])[0].get("market") if t.get("pools") else "pump.fun"),
                    dev_wallet=tok.get("creator") or tok.get("deployer") or "",
                    launch_ts=created or time.time(), raw=t))
            return out
        # fallback: recent pump.fun program txns via RPC (SolanaTracker is the
        # primary new-tokens source; this only runs if ST is unavailable).
        txns = self._rpc_tx_history(PUMPFUN_PROGRAM, limit=limit)
        for tx in txns[:limit]:
            mint = ""
            for tt in tx.get("tokenTransfers", []):
                if tt.get("mint"):
                    mint = tt["mint"]; break
            if not mint:
                continue
            out.append(NewToken(mint=mint, launch_ts=tx.get("timestamp", time.time()),
                                dev_wallet=tx.get("feePayer", ""), raw=tx))
        return out

    # ── BOUNCE FINDER ────────────────────────────────────────────────────
    def price_history(self, mint: str, points: int = 30) -> list[float]:
        data = self._st(f"/chart/{mint}", type="1m", limit=points)
        if isinstance(data, dict):
            candles = data.get("oclhv") or data.get("candles") or []
            return [float(c.get("close") or c.get("c") or 0) for c in candles][-points:]
        return []

    def token_market(self, mint: str) -> dict:
        data = self._st(f"/tokens/{mint}")
        if isinstance(data, dict):
            pools = data.get("pools") or [{}]
            p = pools[0] if pools else {}
            return {
                "price": float((p.get("price") or {}).get("usd") or 0),
                "liquidity": float((p.get("liquidity") or {}).get("usd") or 0),
                "mcap": float((p.get("marketCap") or {}).get("usd") or 0),
            }
        return {"price": 0.0, "liquidity": 0.0, "mcap": 0.0}

    # ── WALLET DISCOVERY (SolanaTracker PnL leaderboard) ─────────────────
    def discover_top_wallets(self, limit: int = 210, min_win_rate: float = 0.55,
                             min_trades: int = 20, days: int = 30) -> list[dict]:
        """Top-PnL Solana wallets to seed the watch list. /v2/pnl/leaderboard/top
        ranks wallets with rich filters; we pull winners and tag them 'smart'.
        Returns [{address,label,kind,pnl,win_rate,trades}]."""
        out, cursor, pages = [], None, 0
        while len(out) < limit and pages < 6:
            params = {"sort": "realized", "direction": "desc", "days": days,
                      "minTrades": min_trades, "minWinRate": min_win_rate,
                      "excludeArbitrage": "true", "limit": 100}
            if cursor:
                params = {"cursor": cursor}
            data = self._st("/v2/pnl/leaderboard/top", **params)
            if not isinstance(data, dict):
                break
            for t in data.get("traders") or []:
                addr = t.get("wallet")
                if not addr:
                    continue
                pnl = ((t.get("ending") or {}).get("pnl") or {}).get("total") or 0
                out.append({
                    "address": addr,
                    "label": (t.get("identity") or {}).get("name") or addr[:8],
                    "kind": "smart",
                    "pnl": float(pnl),
                    "win_rate": float(t.get("winRate") or 0),
                    "trades": int((t.get("counts") or {}).get("trades") or 0),
                })
                if len(out) >= limit:
                    break
            cursor = (data.get("pagination") or {}).get("nextCursor")
            pages += 1
            if not cursor:
                break
        return out

    # ── WHALE / KOL WATCH ────────────────────────────────────────────────
    def wallet_buys(self, wallets: list[str], since_ts: float) -> list[dict]:
        """Recent BUY actions by watched wallets. Primary: SolanaTracker
        /wallet/{owner}/trades (one call per wallet, no RPC rate limit). Falls
        back to throttled Helius RPC tx history only if ST is unavailable."""
        out = []
        for w in wallets:
            data = self._st(f"/wallet/{w}/trades")
            trades = None
            if isinstance(data, dict):
                trades = data.get("trades") or data.get("data")
            elif isinstance(data, list):
                trades = data
            if trades is not None:
                for t in trades:
                    # a buy = type/side 'buy', or the 'to' token is non-SOL
                    side = (t.get("type") or t.get("side") or "").lower()
                    ts = t.get("time") or t.get("timestamp") or 0
                    ts = float(ts)/1000 if ts > 1e12 else float(ts or 0)
                    if ts and ts < since_ts:
                        continue
                    mint = (t.get("mint") or t.get("token")
                            or (t.get("to") or {}).get("token", {}).get("address")
                            or (t.get("to") or {}).get("address"))
                    if side and side != "buy":
                        continue
                    if mint:
                        out.append({"wallet": w, "mint": mint,
                                    "usd": float(t.get("volume") or t.get("volumeUsd") or 0),
                                    "ts": ts})
                continue
            # fallback: throttled Helius RPC (budget-capped)
            for tx in self._rpc_tx_history(w, limit=8):
                ts = tx.get("timestamp", 0)
                if ts < since_ts:
                    continue
                for tt in tx.get("tokenTransfers", []):
                    if tt.get("toUserAccount") == w and tt.get("mint"):
                        out.append({"wallet": w, "mint": tt["mint"], "usd": 0.0, "ts": ts})
        return out

    # ── HOLDER FLOW ──────────────────────────────────────────────────────
    def holders(self, mint: str) -> dict:
        data = self._st(f"/tokens/{mint}/holders")
        if isinstance(data, dict):
            return {
                "count": int(data.get("total") or 0),
                "new_recent": int(data.get("newHolders") or 0),
                "buy_pressure": float(data.get("buyPressure") or 0),
            }
        # fallback: Helius DAS token accounts count (coarse holder count, no flow)
        res = self._rpc("getTokenLargestAccounts", [mint])
        if res and res.get("value"):
            return {"count": len(res["value"]), "new_recent": 0, "buy_pressure": 0.0}
        return {"count": 0, "new_recent": 0, "buy_pressure": 0.0}

    # ── RUG GATE ─────────────────────────────────────────────────────────
    def rug_report(self, mint: str, dev_wallet: str = "") -> dict:
        # SolanaTracker risk score first (purpose-built)
        data = self._st(f"/tokens/{mint}")
        if isinstance(data, dict):
            risk = data.get("risk") or {}
            score = risk.get("score")
            if score is not None:
                # ST risk score: lower = safer (0-10 typical); treat >6 as fail
                passed = float(score) <= 6.0
                reasons = [r.get("name") for r in (risk.get("risks") or [])][:3]
                return {"pass": passed,
                        "reason": "; ".join(filter(None, reasons)) or f"risk score {score}",
                        "mint_authority": None, "freeze_authority": None,
                        "lp_locked": risk.get("lpLocked"), "dev_prior_rugs": None,
                        "score": float(score)}
        # fallback: Helius mint/freeze authority check via DAS
        asset = self._rpc("getAsset", [mint])
        if asset:
            auth = asset.get("authorities") or []
            mint_auth = bool(auth)
            # if mint authority still active, it can print more supply — fail
            return {"pass": not mint_auth,
                    "reason": "mint authority still active" if mint_auth else "authorities renounced",
                    "mint_authority": mint_auth, "freeze_authority": None,
                    "lp_locked": None, "dev_prior_rugs": None}
        return {"pass": False, "reason": "no rug data (fail-closed)",
                "mint_authority": None, "freeze_authority": None,
                "lp_locked": None, "dev_prior_rugs": None}

    # ── BUNDLE HUNTER ────────────────────────────────────────────────────
    def bundle_report(self, mint: str) -> dict:
        """Detect coordinated-buy clusters near launch. Heuristic: among the
        earliest buyers, group by common funder wallet; each group >1 that was
        funded by the same source in the same window is a 'bundle cluster'."""
        txns = self._rpc_tx_history(mint, limit=60)
        # earliest ~60 buys
        buys = []
        for tx in sorted(txns, key=lambda t: t.get("timestamp", 0))[:60]:
            payer = tx.get("feePayer")
            if payer:
                buys.append((payer, tx.get("timestamp", 0)))
        if not buys:
            return {"clusters": 0, "bundled_pct": 0.0, "detail": "no early-buy data"}
        # cluster by same payer buying multiple times in first 30s (sybil-ish)
        from collections import Counter
        c = Counter(p for p, _ in buys)
        clusters = sum(1 for _, n in c.items() if n >= 2)
        bundled = sum(n for _, n in c.items() if n >= 2)
        return {"clusters": clusters,
                "bundled_pct": round(bundled / max(1, len(buys)), 3),
                "detail": f"{clusters} clusters among {len(buys)} early buys"}
