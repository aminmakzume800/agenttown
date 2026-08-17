"""Application configuration loaded from environment variables."""
import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    """All application settings with safe defaults."""

    # LLM API Key (single NVIDIA key for all models including DeepSeek)
    NVIDIA_API_KEY: str = os.getenv("NVIDIA_API_KEY", "")

    # API Base URL (NVIDIA hosts DeepSeek, Llama, Nemotron, CodeLlama — all in one)
    NVIDIA_BASE_URL: str = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")

    # Trading config
    TRADING_MODE: str = os.getenv("TRADING_MODE", "paper")
    DAILY_DRAWDOWN_LIMIT: float = float(os.getenv("DAILY_DRAWDOWN_LIMIT", "1000.0"))
    MAX_POSITION_SIZE: float = float(os.getenv("MAX_POSITION_SIZE", "5.0"))
    MAX_CONCURRENT_TRADES: int = int(os.getenv("MAX_CONCURRENT_TRADES", "5"))

    # MT5 config (only used when TRADING_MODE=live)
    MT5_LOGIN: int = int(os.getenv("MT5_LOGIN", "0") or "0")
    MT5_PASSWORD: str = os.getenv("MT5_PASSWORD", "")
    MT5_SERVER: str = os.getenv("MT5_SERVER", "")

    # Database
    DB_PATH: str = os.getenv("DB_PATH", "./data/app.db")

    # Server
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8000"))


settings = Settings()
