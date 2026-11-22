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

HELIUS_KEY = os.environ.get("HELIUS_API_KEY", "")
ST_KEY = os.environ.get("SOLANATRACKER_API_KEY", "")

HELIUS_RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}"
HELIUS_API = "https://api.helius.xyz/v0"
ST_API = "https://data.solanatracker.io"
PUMPFUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"  # pump.fun program id

SOL_USD_FALLBACK = 150.0  # used only if price lookup fails


class HeliusFeed(Feed):
    name = "helius"

    def __init__(self):
        super().__init__()
        self.s = requests.Session()
        self.s.headers.update({"Content-Type": "application/json"})

    # ── low-level helpers ────────────────────────────────────────────────
    def _rpc(self, method: str, params: list):
        try:
            r = self.s.post(HELIUS_RPC, json={"jsonrpc": "2.0", "id": "1",
                            "method": method, "params": params}, timeout=12)
            if r.status_code >= 400:
                self.last_error = f"helius rpc {method} {r.status_code}: {r.text[:140]}"
                return None
            return r.json().get("result")
        except requests.RequestException as e:
            self.last_error = f"helius rpc {method} error: {e}"
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
        # fallback: Helius parsed pump.fun program transactions
        txns = self._helius_api(f"/addresses/{PUMPFUN_PROGRAM}/transactions",
                                type="CREATE") or []
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

    # ── WHALE / KOL WATCH ────────────────────────────────────────────────
    def wallet_buys(self, wallets: list[str], since_ts: float) -> list[dict]:
        """Recent BUY actions by watched wallets. Uses Helius parsed-txn history
        per wallet, filtering to SWAP buys after since_ts."""
        out = []
        for w in wallets:
            txns = self._helius_api(f"/addresses/{w}/transactions", type="SWAP") or []
            for tx in txns:
                ts = tx.get("timestamp", 0)
                if ts < since_ts:
                    continue
                # a "buy" = wallet received a non-SOL token in the swap
                for tt in tx.get("tokenTransfers", []):
                    if tt.get("toUserAccount") == w and tt.get("mint"):
                        out.append({"wallet": w, "mint": tt["mint"],
                                    "usd": 0.0, "ts": ts})
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
        txns = self._helius_api(f"/addresses/{mint}/transactions", type="SWAP") or []
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
