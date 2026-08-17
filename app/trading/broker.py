"""Cross-platform broker bridge — real MT5 orders from macOS, Linux or Windows.

The official MetaTrader5 Python package only speaks to a terminal running on
the same Windows machine, which rules out a Mac. This module talks instead to
MetaApi, a hosted bridge that keeps the MT5 terminal in the cloud and exposes
it over plain HTTPS. Any OS that can make an HTTP request can therefore trade a
Trading.com account (or any other MT5 broker).

Set up once:
  1. Open a Trading.com demo account and note the MT5 login, password and
     server name (e.g. "Trading.com-Demo").
  2. Create a free account at https://app.metaapi.cloud, add that MT5 login,
     and copy the account id it gives you.
  3. Generate a token on https://app.metaapi.cloud/token.
  4. Put METAAPI_TOKEN, METAAPI_ACCOUNT_ID and METAAPI_REGION in .env, then set
     TRADING_MODE=broker and BROKER_TRADING_ENABLED=true.

Nothing here can place an order unless BROKER_TRADING_ENABLED is true. Reads
(prices, balance, open positions) work as soon as the token is present, so the
connection can be verified before any money is at stake.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from app.config import settings

logger = logging.getLogger(__name__)

# MetaApi treats these MT5 return codes as a completed request.
# 10009 TRADE_RETCODE_DONE, 10008 PLACED, 10010 DONE_PARTIAL, 10025 partial close.
OK_CODES = {0, 10008, 10009, 10010, 10025}

# Broker symbol candidates per canonical name, best guess first. Brokers rename
# instruments freely, so the list is matched against what the account actually
# offers rather than assumed.
SYMBOL_CANDIDATES: dict[str, list[str]] = {
    "EUR/USD": ["EURUSD"],
    "GBP/USD": ["GBPUSD"],
    "XAU/USD": ["XAUUSD", "GOLD", "XAUUSD.s"],
    "NAS100": ["NAS100", "USTEC", "US100", "NDX100", "NAS100.cash", "USTECH", "NASDAQ100"],
}


class BrokerError(RuntimeError):
    """A broker call failed in a way the caller needs to see."""


def http_json(
    method: str,
    url: str,
    headers: dict[str, str],
    body: Optional[dict] = None,
    timeout: float = 20.0,
) -> tuple[int, Any, str]:
    """One JSON round trip on the standard library.

    Deliberately not using a third-party HTTP client: this has to work on a
    fresh macOS or Linux checkout with nothing but `pip install -r
    requirements.txt`, and one fewer dependency is one fewer way for that to go
    wrong. Returns (status, parsed_body, raw_text).
    """
    import json as _json
    import urllib.error
    import urllib.request

    data = _json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method.upper())
    for key, value in headers.items():
        request.add_header(key, value)
    if data is not None:
        request.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            status = response.getcode()
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace") if exc.fp else ""
        status = exc.code
    except Exception as exc:
        raise BrokerError(f"Could not reach the broker bridge: {exc}") from exc

    try:
        parsed = _json.loads(raw) if raw else None
    except ValueError:
        parsed = None
    return status, parsed, raw


class BrokerBridge:
    """Thin, synchronous MetaApi client.

    Synchronous on purpose: the rest of the app is sync and the async paths
    (autopilot) already wrap blocking calls in asyncio.to_thread.
    """

    def __init__(self) -> None:
        self._symbols: Optional[list[str]] = None
        self._symbol_map: dict[str, Optional[str]] = {}
        self._lock = threading.Lock()

    def reset_cache(self) -> None:
        """Drop cached symbols and mappings.

        Called when the account or region changes at runtime, so a new account's
        symbol list is fetched fresh instead of serving the previous one's.
        """
        with self._lock:
            self._symbols = None
            self._symbol_map.clear()

    # ── configuration ───────────────────────────────────────

    @property
    def is_configured(self) -> bool:
        """True when there is a token and an account id to talk to."""
        return bool(settings.METAAPI_TOKEN and settings.METAAPI_ACCOUNT_ID)

    @property
    def trading_enabled(self) -> bool:
        """True only when orders are explicitly allowed through."""
        return self.is_configured and settings.BROKER_TRADING_ENABLED

    @property
    def client_base(self) -> str:
        return f"https://mt-client-api-v1.{settings.METAAPI_REGION}.agiliumtrade.ai"

    @property
    def provisioning_base(self) -> str:
        return "https://mt-provisioning-api-v1.agiliumtrade.agiliumtrade.ai"

    def _account_path(self, suffix: str = "") -> str:
        return f"/users/current/accounts/{settings.METAAPI_ACCOUNT_ID}{suffix}"

    # ── transport ───────────────────────────────────────────

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> Any:
        if not self.is_configured:
            raise BrokerError(
                "Broker not configured. Set METAAPI_TOKEN and METAAPI_ACCOUNT_ID in .env."
            )

        if params:
            from urllib.parse import urlencode

            url = f"{url}?{urlencode(params)}"

        status, parsed, raw = http_json(
            method,
            url,
            headers={
                "auth-token": settings.METAAPI_TOKEN,
                "Accept": "application/json",
            },
            body=json_body,
            timeout=settings.BROKER_TIMEOUT_SEC,
        )

        if status == 401:
            raise BrokerError("Broker rejected the token (401). Check METAAPI_TOKEN.")
        if status == 403:
            raise BrokerError(
                "Broker denied the request (403). The token may lack trading rights."
            )
        if status == 404:
            raise BrokerError(
                "Account or symbol not found (404). Check METAAPI_ACCOUNT_ID, "
                "METAAPI_REGION, and that the account is deployed."
            )
        if status == 429:
            raise BrokerError("Broker rate limit hit (429). Try again shortly.")
        if status >= 400:
            raise BrokerError(f"Broker returned {status}: {raw[:200]}")

        return parsed if parsed is not None else {}

    def _get(self, suffix: str, params: Optional[dict] = None) -> Any:
        return self._request("GET", self.client_base + self._account_path(suffix), params=params)

    def _post(self, suffix: str, body: dict) -> Any:
        return self._request("POST", self.client_base + self._account_path(suffix), json_body=body)

    # ── reads ───────────────────────────────────────────────

    def account_info(self) -> dict:
        """Balance, equity, margin and currency straight from the terminal."""
        data = self._get("/account-information") or {}
        return {
            "login": data.get("login"),
            "name": data.get("name"),
            "server": data.get("server"),
            "platform": data.get("platform"),
            "currency": data.get("currency"),
            "balance": data.get("balance"),
            "equity": data.get("equity"),
            "margin": data.get("margin"),
            "free_margin": data.get("freeMargin"),
            "leverage": data.get("leverage"),
            "type": data.get("type"),  # ACCOUNT_TRADE_MODE_DEMO / _REAL
        }

    @property
    def is_demo(self) -> Optional[bool]:
        """True for a demo account, False for real, None when unknown.

        Read from the broker rather than trusted from config, because this is
        the one fact worth being certain about before an order goes out.
        """
        try:
            mode = str(self.account_info().get("type") or "")
        except BrokerError:
            return None
        if not mode:
            return None
        return "DEMO" in mode.upper()

    def deployment(self) -> dict:
        """Provisioning-side view: is the cloud terminal up and connected?"""
        data = self._request(
            "GET", self.provisioning_base + self._account_path()
        ) or {}
        return {
            "name": data.get("name"),
            "server": data.get("server"),
            "platform": data.get("platform"),
            "state": data.get("state"),
            "connection_status": data.get("connectionStatus"),
            "region": data.get("region"),
        }

    def symbols(self, refresh: bool = False) -> list[str]:
        """Every instrument the account can trade. Cached — the list is static."""
        with self._lock:
            if self._symbols is not None and not refresh:
                return self._symbols
        found = self._get("/symbols") or []
        names = [str(s) for s in found if isinstance(s, str)]
        with self._lock:
            self._symbols = names
            if refresh:
                self._symbol_map.clear()
        return names

    def symbol_for(self, canonical: str) -> Optional[str]:
        """Map "XAU/USD" to whatever this broker calls gold.

        Order of preference: an explicit BROKER_SYMBOL_* override, an exact
        match on a known candidate, then the shortest symbol that starts with a
        candidate (which picks EURUSD over EURUSD.pro or EURUSDm).
        """
        with self._lock:
            if canonical in self._symbol_map:
                return self._symbol_map[canonical]

        override = settings.BROKER_SYMBOL_OVERRIDES.get(canonical, "")
        if override:
            with self._lock:
                self._symbol_map[canonical] = override
            return override

        candidates = SYMBOL_CANDIDATES.get(canonical, [canonical.replace("/", "")])
        try:
            available = self.symbols()
        except BrokerError:
            # Without the list, the plainest guess is better than nothing.
            return candidates[0]

        resolved: Optional[str] = None
        upper = {s.upper(): s for s in available}
        for candidate in candidates:
            if candidate.upper() in upper:
                resolved = upper[candidate.upper()]
                break
        if resolved is None:
            prefixed = [
                s for s in available
                if any(s.upper().startswith(c.upper()) for c in candidates)
            ]
            if prefixed:
                resolved = min(prefixed, key=len)

        with self._lock:
            self._symbol_map[canonical] = resolved
        if resolved is None:
            logger.warning("Broker has no symbol matching %s", canonical)
        return resolved

    def price(self, canonical: str) -> Optional[dict]:
        """Live bid/ask for one instrument, in our canonical naming."""
        symbol = self.symbol_for(canonical)
        if not symbol:
            return None
        data = self._get(f"/symbols/{symbol}/current-price", params={"keepSubscription": "true"})
        if not data or data.get("bid") is None:
            return None
        bid = float(data["bid"])
        ask = float(data.get("ask") or bid)
        return {
            "symbol": canonical,
            "broker_symbol": symbol,
            "bid": bid,
            "ask": ask,
            "last": round((bid + ask) / 2, 5),
            "time": data.get("time"),
        }

    def positions(self) -> list[dict]:
        """Open positions as the broker sees them, in our field names."""
        raw = self._get("/positions") or []
        reverse = {
            self.symbol_for(c): c
            for c in SYMBOL_CANDIDATES
        }
        out = []
        for p in raw:
            broker_symbol = p.get("symbol")
            out.append({
                "broker_position_id": str(p.get("id")),
                "symbol": reverse.get(broker_symbol) or broker_symbol,
                "broker_symbol": broker_symbol,
                "direction": "buy" if str(p.get("type", "")).endswith("BUY") else "sell",
                "size": float(p.get("volume") or 0),
                "entry_price": float(p.get("openPrice") or 0),
                "current_price": float(p.get("currentPrice") or 0) or None,
                "stop_loss": float(p.get("stopLoss") or 0),
                "take_profit": float(p.get("takeProfit") or 0),
                "profit": float(p.get("profit") or 0),
                "swap": float(p.get("swap") or 0),
                "commission": float(p.get("commission") or 0),
                "opened_at": p.get("time"),
            })
        return out

    def deals_for_position(self, broker_position_id: str) -> list[dict]:
        """Every deal the broker booked against one position id."""
        raw = self._get(f"/history-deals/position/{broker_position_id}") or []
        return raw if isinstance(raw, list) else []

    def position_outcome(self, broker_position_id: str) -> Optional[dict]:
        """How a closed position actually settled, per the broker's own books.

        Used when a position vanishes from the open list because the broker
        filled its stop or target server-side. The numbers here are the real
        ones — profit net of commission and swap — so they are preferred over
        anything recomputed locally.
        """
        try:
            deals = self.deals_for_position(broker_position_id)
        except BrokerError:
            return None
        if not deals:
            return None

        exits = [d for d in deals if str(d.get("entryType")) == "DEAL_ENTRY_OUT"]
        if not exits:
            return None

        pnl = sum(
            float(d.get("profit") or 0)
            + float(d.get("commission") or 0)
            + float(d.get("swap") or 0)
            for d in deals
        )
        last = exits[-1]
        return {
            "exit_price": float(last.get("price") or 0),
            "pnl": round(pnl, 2),
            "closed_at": last.get("time"),
        }

    # ── writes ──────────────────────────────────────────────

    def _trade(self, body: dict) -> dict:
        """Send one trade command and normalise the terminal's answer."""
        data = self._post("/trade", body) or {}
        code = data.get("numericCode")
        ok = code in OK_CODES
        result = {
            "ok": ok,
            "code": code,
            "status": data.get("stringCode"),
            "message": data.get("message"),
            "order_id": str(data["orderId"]) if data.get("orderId") else None,
            "broker_position_id": str(data["positionId"]) if data.get("positionId") else None,
            "raw": data,
        }
        if not ok:
            logger.warning("Broker rejected trade: %s", data)
        return result

    def place_market_order(
        self,
        canonical_symbol: str,
        side: str,
        volume: float,
        stop_loss: float = 0.0,
        take_profit: float = 0.0,
        comment: str = "",
    ) -> dict:
        """Market buy or sell with the stop and target attached.

        The stop and target are sent with the order rather than added after the
        fill, so there is no window where the position sits unprotected.
        """
        if not self.trading_enabled:
            raise BrokerError(
                "Broker trading is switched off. Set BROKER_TRADING_ENABLED=true to send orders."
            )
        symbol = self.symbol_for(canonical_symbol)
        if not symbol:
            raise BrokerError(f"Broker does not offer {canonical_symbol}.")

        body: dict = {
            "actionType": "ORDER_TYPE_BUY" if side.lower() in ("buy", "long") else "ORDER_TYPE_SELL",
            "symbol": symbol,
            "volume": round(float(volume), 2),
        }
        if stop_loss:
            body["stopLoss"] = float(stop_loss)
        if take_profit:
            body["takeProfit"] = float(take_profit)
        if comment:
            # MT5 truncates comments; keep it short and safe.
            body["comment"] = "".join(
                ch for ch in comment if ch.isalnum() or ch in "-_ "
            )[:24]

        result = self._trade(body)
        result["symbol"] = canonical_symbol
        result["broker_symbol"] = symbol
        result["direction"] = side.lower()
        result["size"] = body["volume"]
        return result

    def close_position(self, broker_position_id: str) -> dict:
        """Close one position at market."""
        if not self.trading_enabled:
            raise BrokerError("Broker trading is switched off — cannot close at the broker.")
        return self._trade({
            "actionType": "POSITION_CLOSE_ID",
            "positionId": str(broker_position_id),
        })

    def close_all(self) -> dict:
        """Flatten the account. Used by the kill switch."""
        if not self.trading_enabled:
            raise BrokerError("Broker trading is switched off — cannot flatten at the broker.")
        closed, failed = [], []
        for pos in self.positions():
            pid = pos["broker_position_id"]
            try:
                if self.close_position(pid).get("ok"):
                    closed.append(pid)
                else:
                    failed.append(pid)
            except BrokerError as exc:
                logger.warning("Close failed for %s: %s", pid, exc)
                failed.append(pid)
        return {"closed": closed, "failed": failed, "count": len(closed)}

    # ── diagnostics ─────────────────────────────────────────

    def health(self) -> dict:
        """One call the UI and the setup docs can both lean on."""
        report: dict = {
            "configured": self.is_configured,
            "trading_enabled": self.trading_enabled,
            "region": settings.METAAPI_REGION,
            "mode": settings.TRADING_MODE,
            "reachable": False,
            "account": None,
            "deployment": None,
            "is_demo": None,
            "symbols": {},
            "error": None,
        }
        if not self.is_configured:
            report["error"] = "METAAPI_TOKEN and METAAPI_ACCOUNT_ID are not set."
            return report

        try:
            report["deployment"] = self.deployment()
        except BrokerError as exc:
            report["error"] = str(exc)

        try:
            info = self.account_info()
            report["account"] = info
            report["reachable"] = info.get("balance") is not None
            mode = str(info.get("type") or "")
            report["is_demo"] = ("DEMO" in mode.upper()) if mode else None
        except BrokerError as exc:
            report["error"] = str(exc)
            return report

        for canonical in SYMBOL_CANDIDATES:
            try:
                report["symbols"][canonical] = self.symbol_for(canonical)
            except BrokerError:
                report["symbols"][canonical] = None
        return report


# Singleton — one bridge per process.
broker = BrokerBridge()
