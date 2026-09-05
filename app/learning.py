"""Feed each agent its own track record before it proposes again.

Without this the desk has no memory of outcomes. An agent that lost four times
running on the same setup proposes it a fifth time with equal confidence,
because nothing ever told it what happened. The positions table already holds
every closed trade and who opened it, so the record exists — it simply was not
being read back.

This is not machine learning. No weights change. It is in-context learning: the
agent's own results are summarised into its prompt so it can adjust. That is the
honest kind of "learning" available here, and it is worth being clear about the
difference.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def agent_record(agent_key: str, limit: Optional[int] = None) -> dict:
    """Closed-trade statistics for one agent."""
    limit = limit or int(settings.LEARNING_LOOKBACK)
    conn = _conn()
    try:
        rows = conn.execute(
            """SELECT symbol, direction, size, entry_price, exit_price, pnl,
                      opened_at, closed_at
               FROM positions
               WHERE agent_key = ? AND status = 'closed' AND pnl IS NOT NULL
               ORDER BY closed_at DESC LIMIT ?""",
            (agent_key, limit),
        ).fetchall()
    except sqlite3.Error as exc:
        logger.warning("Could not read record for %s: %s", agent_key, exc)
        return {"trades": 0}
    finally:
        conn.close()

    if not rows:
        return {"trades": 0}

    trades = [dict(r) for r in rows]
    wins = [t for t in trades if float(t["pnl"]) > 0]
    losses = [t for t in trades if float(t["pnl"]) <= 0]
    total = sum(float(t["pnl"]) for t in trades)

    # Per-symbol breakdown: an agent may be fine on one instrument and poor on
    # another, which a single win rate hides.
    by_symbol: dict[str, dict] = {}
    for t in trades:
        s = by_symbol.setdefault(t["symbol"], {"n": 0, "wins": 0, "pnl": 0.0})
        s["n"] += 1
        s["pnl"] += float(t["pnl"])
        if float(t["pnl"]) > 0:
            s["wins"] += 1

    # Losing streak, most recent first — the signal that something is off now
    # rather than on average.
    streak = 0
    for t in trades:
        if float(t["pnl"]) <= 0:
            streak += 1
        else:
            break

    # A per-instrument streak matters more than the overall one. An agent can be
    # net positive from one lucky trade elsewhere while losing every single time
    # on a given pair, and the headline streak hides exactly that.
    symbol_streaks: dict[str, int] = {}
    for symbol in by_symbol:
        run = 0
        for t in trades:
            if t["symbol"] != symbol:
                continue
            if float(t["pnl"]) <= 0:
                run += 1
            else:
                break
        symbol_streaks[symbol] = run
    worst_streak = max(symbol_streaks.items(), key=lambda kv: kv[1],
                       default=("", 0))

    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trades) * 100, 1),
        "net_pnl": round(total, 2),
        "avg_win": round(sum(float(t["pnl"]) for t in wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(float(t["pnl"]) for t in losses) / len(losses), 2) if losses else 0.0,
        "losing_streak": streak,
        "symbol_streaks": symbol_streaks,
        "worst_symbol_streak": {"symbol": worst_streak[0], "losses": worst_streak[1]},
        "by_symbol": {
            k: {"trades": v["n"],
                "win_rate": round(v["wins"] / v["n"] * 100, 1),
                "pnl": round(v["pnl"], 2)}
            for k, v in by_symbol.items()
        },
        "recent": [
            {"symbol": t["symbol"], "direction": t["direction"],
             "pnl": round(float(t["pnl"]), 2),
             "entry": t["entry_price"], "exit": t["exit_price"]}
            for t in trades[:5]
        ],
    }


def desk_record(limit: int = 50) -> dict:
    """Whole-desk statistics, for the Manager and the UI."""
    conn = _conn()
    try:
        rows = conn.execute(
            """SELECT agent_key, symbol, pnl FROM positions
               WHERE status = 'closed' AND pnl IS NOT NULL
               ORDER BY closed_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    except sqlite3.Error:
        return {"trades": 0}
    finally:
        conn.close()

    if not rows:
        return {"trades": 0}

    trades = [dict(r) for r in rows]
    per_agent: dict[str, dict] = {}
    for t in trades:
        a = per_agent.setdefault(t["agent_key"], {"n": 0, "wins": 0, "pnl": 0.0})
        a["n"] += 1
        a["pnl"] += float(t["pnl"])
        if float(t["pnl"]) > 0:
            a["wins"] += 1

    wins = sum(1 for t in trades if float(t["pnl"]) > 0)
    return {
        "trades": len(trades),
        "win_rate": round(wins / len(trades) * 100, 1),
        "net_pnl": round(sum(float(t["pnl"]) for t in trades), 2),
        "by_agent": {
            k: {"trades": v["n"],
                "win_rate": round(v["wins"] / v["n"] * 100, 1),
                "pnl": round(v["pnl"], 2)}
            for k, v in sorted(per_agent.items(), key=lambda kv: -kv[1]["pnl"])
        },
    }


def format_record(agent_key: str) -> str:
    """Track-record block for an agent's prompt.

    Deliberately blunt. A soft summary would let a losing pattern continue; the
    point is that the agent sees the losing streak and the per-symbol split, and
    is told plainly to change approach or stand aside.
    """
    if not settings.LEARNING_ENABLED:
        return ""

    rec = agent_record(agent_key)
    if not rec.get("trades"):
        return ("YOUR TRACK RECORD: no closed trades yet. Be conservative until "
                "there is evidence your read works.")

    lines = [
        f"YOUR TRACK RECORD (last {rec['trades']} closed trades — your own results):",
        f"  Win rate {rec['win_rate']}%  ({rec['wins']}W / {rec['losses']}L)",
        f"  Net P&L {rec['net_pnl']:+.2f} USD  "
        f"(avg win {rec['avg_win']:+.2f}, avg loss {rec['avg_loss']:+.2f})",
    ]

    if rec["by_symbol"]:
        parts = [f"{s} {v['win_rate']}% ({v['pnl']:+.2f})"
                 for s, v in rec["by_symbol"].items()]
        lines.append("  By instrument: " + ", ".join(parts))

    if rec["recent"]:
        lines.append("  Most recent: " + ", ".join(
            f"{t['symbol']} {t['direction']} {t['pnl']:+.2f}" for t in rec["recent"]))

    # The instruction has to be specific, or the model reads the numbers and
    # carries on unchanged.
    worst = rec.get("worst_symbol_streak") or {}
    if rec["losing_streak"] >= 3:
        lines.append(
            f"  WARNING: {rec['losing_streak']} losses in a row. Whatever you have "
            f"been doing is not working in this market. Either change the approach "
            f"or answer NO-TRADE — do not repeat the same setup again."
        )
    elif worst.get("losses", 0) >= 3:
        lines.append(
            f"  WARNING: {worst['losses']} losses in a row on {worst['symbol']} "
            f"specifically. Your overall numbers hide this. Change your read on "
            f"{worst['symbol']} or answer NO-TRADE there."
        )
    elif rec["win_rate"] < 40 and rec["trades"] >= 5:
        lines.append(
            f"  Your win rate is {rec['win_rate']}%. Raise your bar: only take the "
            f"clearest setups, and prefer NO-TRADE when unsure."
        )
    elif rec["net_pnl"] < 0:
        lines.append(
            "  You are net negative. Favour wider targets or tighter entries, and "
            "be more selective."
        )
    else:
        lines.append("  You are net positive. Keep doing what has been working.")

    worst = [s for s, v in rec["by_symbol"].items()
             if v["trades"] >= 3 and v["win_rate"] < 35]
    if worst:
        lines.append(
            f"  You have been consistently wrong on {', '.join(worst)} — treat "
            f"setups there with extra suspicion."
        )
    return "\n".join(lines)
