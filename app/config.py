"""Application configuration loaded from environment variables."""
import os
from dotenv import load_dotenv

load_dotenv()


def _flag(name: str, default: bool = False) -> bool:
    """Read a boolean env var. Accepts 1/true/yes/on, case-insensitive."""
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# Absolute path to the .env the UI writes back to. Resolved from this file so it
# is correct no matter which directory the server was started from.
ENV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
)


def persist_env(updates: dict[str, str]) -> None:
    """Write key=value pairs into the .env file, in place.

    Existing keys are updated where they sit; new keys are appended. This is
    what lets someone paste their broker credentials into the app once and have
    them survive a restart, instead of hand-editing a file. Values are written
    verbatim, so a token with '=' in it is preserved.
    """
    lines: list[str] = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()

    remaining = dict(updates)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in remaining:
            lines[i] = f"{key}={remaining.pop(key)}"

    for key, value in remaining.items():
        lines.append(f"{key}={value}")

    with open(ENV_PATH, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


class Settings:
    """All application settings with safe defaults."""

    # LLM API Key (single NVIDIA key for all models including DeepSeek)
    NVIDIA_API_KEY: str = os.getenv("NVIDIA_API_KEY", "")

    # API Base URL (NVIDIA hosts DeepSeek, Llama, Nemotron, CodeLlama — all in one)
    NVIDIA_BASE_URL: str = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")

    # ── Trading config ──────────────────────────────────────
    # paper  — simulated fills in SQLite, no broker involved (default)
    # broker — real orders on your MT5 account through the MetaApi cloud
    #          bridge. Works on macOS, Linux and Windows alike.
    # live   — local MetaTrader 5 terminal via the MetaTrader5 package.
    #          Windows only; kept for anyone already running that setup.
    TRADING_MODE: str = os.getenv("TRADING_MODE", "paper").strip().lower()
    DAILY_DRAWDOWN_LIMIT: float = float(os.getenv("DAILY_DRAWDOWN_LIMIT", "1000.0"))
    MAX_POSITION_SIZE: float = float(os.getenv("MAX_POSITION_SIZE", "5.0"))
    MAX_CONCURRENT_TRADES: int = int(os.getenv("MAX_CONCURRENT_TRADES", "5"))

    # ── Broker bridge (MetaApi → MT5, any OS) ───────────────
    # Token from https://app.metaapi.cloud/token and the account id shown
    # after you add your Trading.com demo login there. One account is free.
    METAAPI_TOKEN: str = os.getenv("METAAPI_TOKEN", "")
    METAAPI_ACCOUNT_ID: str = os.getenv("METAAPI_ACCOUNT_ID", "")

    # Deployment region of that account — shown on the API access page.
    METAAPI_REGION: str = os.getenv("METAAPI_REGION", "new-york").strip()

    # Refuses to send orders unless this is explicitly true, so a copied .env
    # with real credentials still cannot trade by accident.
    BROKER_TRADING_ENABLED: bool = _flag("BROKER_TRADING_ENABLED", False)

    # Optional overrides when your broker names an instrument differently,
    # e.g. BROKER_SYMBOL_NAS100=USTEC. Blank means auto-detect.
    BROKER_SYMBOL_OVERRIDES: dict[str, str] = {
        canonical: os.getenv("BROKER_SYMBOL_" + env_key, "").strip()
        for canonical, env_key in (
            ("EUR/USD", "EURUSD"),
            ("GBP/USD", "GBPUSD"),
            ("XAU/USD", "XAUUSD"),
            ("NAS100", "NAS100"),
        )
    }

    # Seconds to wait on a broker call before giving up.
    BROKER_TIMEOUT_SEC: float = float(os.getenv("BROKER_TIMEOUT_SEC", "20"))

    # ── Autopilot (unattended trading) ──────────────────────
    # Off on boot by design: the loop only ever starts because someone asked
    # for it, either with this flag or the /autopilot/start button.
    AUTOPILOT_ENABLED: bool = _flag("AUTOPILOT_ENABLED", False)

    # Seconds between scans. 300 keeps well inside free-tier rate limits.
    AUTOPILOT_INTERVAL_SEC: int = int(os.getenv("AUTOPILOT_INTERVAL_SEC", "300"))

    # Instruments the loop is allowed to look at.
    AUTOPILOT_SYMBOLS: list[str] = [
        s.strip() for s in os.getenv(
            "AUTOPILOT_SYMBOLS", "EUR/USD,XAU/USD,GBP/USD,NAS100"
        ).split(",") if s.strip()
    ]

    # Throughput caps. Hitting either one parks the loop until the window rolls.
    AUTOPILOT_MAX_TRADES_PER_HOUR: int = int(os.getenv("AUTOPILOT_MAX_TRADES_PER_HOUR", "2"))
    AUTOPILOT_MAX_TRADES_PER_DAY: int = int(os.getenv("AUTOPILOT_MAX_TRADES_PER_DAY", "6"))

    # Largest size the autopilot may send, regardless of what an agent asks for.
    AUTOPILOT_MAX_SIZE: float = float(os.getenv("AUTOPILOT_MAX_SIZE", "0.10"))

    # Minimum reward:risk before an idea is worth taking unattended.
    AUTOPILOT_MIN_RR: float = float(os.getenv("AUTOPILOT_MIN_RR", "1.5"))

    # When true (default) the loop queues proposals for a human APPROVE click
    # and places nothing on its own. Set false for genuinely hands-off running.
    AUTOPILOT_REQUIRE_APPROVAL: bool = _flag("AUTOPILOT_REQUIRE_APPROVAL", True)

    # Second lock on real money: the autopilot refuses to trade a live account
    # unless this is explicitly true, even when TRADING_MODE=live.
    AUTOPILOT_ALLOW_LIVE: bool = _flag("AUTOPILOT_ALLOW_LIVE", False)

    # Realised loss (USD, positive number) that trips the kill switch and stops
    # the loop for the rest of the day.
    AUTOPILOT_HALT_DRAWDOWN: float = float(os.getenv("AUTOPILOT_HALT_DRAWDOWN", "500.0"))

    # Optional Telegram notifications. Blank disables them silently.
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # MT5 config (only used when TRADING_MODE=live)
    MT5_LOGIN: int = int(os.getenv("MT5_LOGIN", "0") or "0")
    MT5_PASSWORD: str = os.getenv("MT5_PASSWORD", "")
    MT5_SERVER: str = os.getenv("MT5_SERVER", "")

    # Database
    DB_PATH: str = os.getenv("DB_PATH", "./data/app.db")

    # Server
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8000"))

    def apply_broker_config(
        self,
        token: str | None = None,
        account_id: str | None = None,
        region: str | None = None,
        trading_enabled: bool | None = None,
        mode: str | None = None,
        persist: bool = True,
    ) -> dict:
        """Update broker settings at runtime, optionally writing them to .env.

        Only the fields that are provided change. Returns a redacted snapshot so
        the caller can confirm what took effect without echoing the token back.
        """
        env_updates: dict[str, str] = {}

        if token is not None:
            self.METAAPI_TOKEN = token.strip()
            env_updates["METAAPI_TOKEN"] = self.METAAPI_TOKEN
        if account_id is not None:
            self.METAAPI_ACCOUNT_ID = account_id.strip()
            env_updates["METAAPI_ACCOUNT_ID"] = self.METAAPI_ACCOUNT_ID
        if region is not None and region.strip():
            self.METAAPI_REGION = region.strip()
            env_updates["METAAPI_REGION"] = self.METAAPI_REGION
        if mode is not None and mode.strip():
            self.TRADING_MODE = mode.strip().lower()
            env_updates["TRADING_MODE"] = self.TRADING_MODE
        if trading_enabled is not None:
            self.BROKER_TRADING_ENABLED = bool(trading_enabled)
            env_updates["BROKER_TRADING_ENABLED"] = "true" if trading_enabled else "false"

        if persist and env_updates:
            persist_env(env_updates)

        return self.broker_config_snapshot()

    def broker_config_snapshot(self) -> dict:
        """Non-secret view of the broker config for the UI.

        The token is never returned in full — only whether one is set and its
        last four characters, so the form can show 'a token is saved' without
        leaking it back to the browser on every poll.
        """
        token = self.METAAPI_TOKEN or ""
        return {
            "mode": self.TRADING_MODE,
            "token_set": bool(token),
            "token_hint": ("…" + token[-4:]) if len(token) >= 4 else ("set" if token else ""),
            "account_id": self.METAAPI_ACCOUNT_ID,
            "region": self.METAAPI_REGION,
            "trading_enabled": self.BROKER_TRADING_ENABLED,
        }


settings = Settings()
