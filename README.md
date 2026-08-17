# Trading Agent Starter — Multi-Agent Trading System

A multi-agent AI trading chatbot with NVIDIA + DeepSeek free APIs.

English / বাংলা instructions included.

## Quick Start (English)

### Option A: Run with Python (simplest)

1. Install Python 3.11+
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Copy config:
   ```bash
   cp .env.example .env
   ```
4. (Optional) Add your free API keys in `.env`:
   - NVIDIA: https://build.nvidia.com/settings/api-keys
   - DeepSeek: https://platform.deepseek.com/api_keys
5. Run:
   ```bash
   uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
   ```
6. Open: http://localhost:8000

### Option B: Run with Docker

1. Copy config:
   ```bash
   cp .env.example .env
   ```
2. Run:
   ```bash
   docker compose up --build
   ```
3. Open: http://localhost:8000

## দ্রুত শুরু (বাংলা)

1. Python 3.11+ ইনস্টল করুন
2. ডিপেন্ডেন্সি ইনস্টল: `pip install -r requirements.txt`
3. কনফিগ কপি: `cp .env.example .env`
4. চালান: `uvicorn app.main:app --reload --host 0.0.0.0 --port 8000`
5. ব্রাউজারে খুলুন: http://localhost:8000

## Features

- 8 AI agents (Manager, Super Trader, Risk Manager, Computer Scientist, 4 Trader Bots)
- Voice chat (microphone + text-to-speech)
- Bilingual UI (English + Bengali)
- Paper-mode trading simulation
- Risk management (drawdown, position sizing, max trades)
- Kill switch for emergency stop
- Real-time WebSocket updates
- Audit trail for all decisions

## API Keys (Free)

| Provider | Free Tier | Get Key |
|----------|-----------|---------|
| NVIDIA | 1000-5000 free credits | https://build.nvidia.com |
| DeepSeek | 5M free tokens | https://platform.deepseek.com |

The system works WITHOUT API keys (uses demo responses). Add keys for real AI responses.

## Security

- Live trading DISABLED by default (paper mode only)
- API keys stored in .env (never committed to git)
- All decisions logged to audit trail
