# Trading Agent Starter — Multi-Agent Trading System

A multi-agent AI trading chatbot powered by NVIDIA's free model APIs, with
optional real MT5 broker execution on any OS.

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
4. (Optional) Add your free NVIDIA API key in `.env`:
   - NVIDIA: https://build.nvidia.com/settings/api-keys
5. Run:
   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```
   Do not add `--reload`: it watches the whole folder (including the virtual
   environment) and can get stuck reloading. Use plain uvicorn as above.
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
4. চালান: `uvicorn app.main:app --host 0.0.0.0 --port 8000` (`--reload` ব্যবহার করবেন না)
5. ব্রাউজারে খুলুন: http://localhost:8000

## Features

- 8 AI agents (Manager, Super Trader, Risk Manager, Computer Scientist, 4 Trader Bots)
- Voice chat (microphone + text-to-speech, hands-free Converse mode)
- Bilingual UI (English + Bengali)
- Pixel-art "Agent Town" office with walkable characters
- Full trade pipeline: agent proposal → risk gate → manager approval → execution
- Paper-mode simulation, or real MT5 execution on any OS (see Broker Setup)
- Autopilot: unattended scanning + trading with hard guardrails
- Risk management (drawdown, position sizing, max trades, correlation, exposure)
- Kill switch for emergency stop
- Real-time WebSocket updates and audit trail

## Trading modes

Set `TRADING_MODE` in `.env`:

| Mode | What it does | Where it runs |
|------|--------------|---------------|
| `paper` (default) | Simulated fills stored locally. No broker, no money. | Any OS |
| `broker` | Real orders on your MT5 account via the MetaApi cloud bridge. | macOS, Linux, Windows |
| `live` | Local MetaTrader 5 terminal via the MetaTrader5 package. | Windows only |

It ships in `paper` mode. Nothing reaches a broker until you deliberately switch.

## Broker Setup (optional — for real MT5 execution on any OS)

The official MetaTrader5 Python package is Windows-only, so on a Mac the app
reaches MT5 through **MetaApi**, a hosted bridge that runs the terminal in the
cloud and exposes it over HTTPS. This lets Trading.com (or any MT5 broker) be
traded from macOS, Linux or Windows.

1. Open a demo account with an MT5 broker (e.g. Trading.com, or the built-in
   MetaQuotes-Demo). Note the login, **master** password and server name.
2. Sign up at https://app.metaapi.cloud (free tier: one account), add that MT5
   login, and copy the account id (a UUID).
3. Generate a token at https://app.metaapi.cloud/token; note the region.
4. In the app, open the **AUTOPILOT** tab → **BROKER CONNECTION** form. Paste
   the token, account id and region, choose mode `broker`, and click
   **SAVE & TEST**. It writes them to `.env` and shows the connection result.
   (You can also fill `METAAPI_*` in `.env` by hand.)
5. Once it shows *connected*, tick **Allow real orders** to enable execution.

Notes:
- Use the **master** MT5 password, not the investor (read-only) one.
- MetaApi is a paid service (~$0.0126/hr while the account is deployed).
  A few dollars covers plenty of demo testing. Undeploy the account from the
  MetaApi dashboard when idle to pause billing. This cost is on the account
  owner, not the app.
- Everything is validated read-only first (`GET /broker/status`), so the
  connection can be confirmed before any order is sent.
- For an Indian broker like 5paisa (not MetaTrader), a separate adapter would
  be needed — MetaApi only bridges MT4/MT5 brokers.

## Autopilot (optional — unattended trading)

The Autopilot scans the configured markets on a timer, asks the trader bots for
setups, runs them through the risk gate and the Manager, and either queues them
for your approval (default) or places them itself. Guardrails in `.env`:

- `AUTOPILOT_REQUIRE_APPROVAL=true` — queue for a human click (recommended)
- `AUTOPILOT_ALLOW_LIVE=false` — extra lock before it can touch a real account
- `AUTOPILOT_MAX_TRADES_PER_HOUR` / `_PER_DAY`, `AUTOPILOT_MAX_SIZE`,
  `AUTOPILOT_MIN_RR`, `AUTOPILOT_HALT_DRAWDOWN` — throughput and loss limits
- Optional Telegram alerts via `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`

Start and stop it from the **AUTOPILOT** tab. It is off by default and never
runs on its own until started.

## API Keys (Free)

| Provider | Free Tier | Get Key |
|----------|-----------|---------|
| NVIDIA | free credits, no card | https://build.nvidia.com/settings/api-keys |

One NVIDIA key powers all 8 agents (Nemotron / Llama models). The system works
WITHOUT a key (falls back to demo responses); add the key for real AI replies.

## Security

- Real trading DISABLED by default — paper mode only out of the box
- Broker execution sits behind two locks: `TRADING_MODE=broker` **and**
  `BROKER_TRADING_ENABLED=true`
- Keys and tokens live in `.env`, which is never committed to git
- Kill switch flattens all positions and halts trading instantly
- All decisions logged to the audit trail
