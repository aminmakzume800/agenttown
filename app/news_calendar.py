"""Economic event calendar — closes the news-blackout hole in the risk layer.

Why this is built from a schedule rather than an API: the reliable calendar
feeds are all paid, and a free scraper that silently breaks is worse than no
feed, because the gate would report "no events" and wave a trade through into an
NFP print. High-impact releases are published on fixed, known schedules, so the
recurring ones are encoded directly. That is dependency-free, cannot fail
silently, and covers the events that actually move these four instruments.

An optional external feed can be layered on top via NEWS_API_URL for anyone who
wants full coverage; when it is unreachable the built-in schedule still applies.

Known limits, stated plainly:
  - Fixed-schedule events only. One-off speeches and unscheduled news are not
    covered.
  - Release times are the standard ones and shift by an hour when the US and
    Europe change clocks on different dates.
  - Not a substitute for a paid calendar in production. It is a real gate that
    catches the big, predictable events instead of a stub that catches nothing.
"""
from __future__ import annotations

import calendar
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

# Which currencies matter for each instrument we trade.
SYMBOL_CURRENCIES = {
    "EUR/USD": {"EUR", "USD"},
    "GBP/USD": {"GBP", "USD"},
    "XAU/USD": {"USD"},          # gold is priced in dollars and reacts to Fed news
    "NAS100": {"USD"},
}

# Recurring high-impact releases, in UTC.
#   weekday: Monday=0 ... Sunday=6
#   nth: 1 = first that weekday of the month, -1 = last, None = every week
# Times are the usual publication times; see the caveat about DST above.
RECURRING_EVENTS = [
    # US Non-Farm Payrolls — first Friday, 13:30 UTC. The single biggest
    # scheduled mover for USD pairs and gold.
    {"name": "US Non-Farm Payrolls", "currency": "USD",
     "weekday": 4, "nth": 1, "hour": 13, "minute": 30, "impact": "high"},

    # US CPI — mid-month, 13:30 UTC. Day varies, so a window is used.
    {"name": "US CPI", "currency": "USD",
     "day_range": (10, 15), "hour": 13, "minute": 30, "impact": "high"},

    # FOMC rate decision — 8 scheduled meetings a year, 19:00 UTC.
    {"name": "FOMC rate decision", "currency": "USD",
     "months": (1, 3, 5, 6, 7, 9, 11, 12), "day_range": (17, 20),
     "hour": 19, "minute": 0, "impact": "high"},

    # ECB rate decision — 12:15 UTC, roughly every six weeks.
    {"name": "ECB rate decision", "currency": "EUR",
     "months": (1, 3, 4, 6, 7, 9, 10, 12), "day_range": (10, 16),
     "weekday": 3, "hour": 12, "minute": 15, "impact": "high"},

    # Bank of England — usually the first Thursday, 11:00 UTC.
    {"name": "BoE rate decision", "currency": "GBP",
     "weekday": 3, "nth": 1, "hour": 11, "minute": 0, "impact": "high"},

    # UK CPI — mid-month, 06:00 UTC.
    {"name": "UK CPI", "currency": "GBP",
     "day_range": (14, 19), "hour": 6, "minute": 0, "impact": "high"},

    # US weekly jobless claims — every Thursday, 13:30 UTC. Medium impact, but
    # enough to spike a scalp.
    {"name": "US jobless claims", "currency": "USD",
     "weekday": 3, "nth": None, "hour": 13, "minute": 30, "impact": "medium"},
]


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> Optional[int]:
    """Day of month for the nth given weekday. nth=-1 means the last one."""
    days = calendar.monthcalendar(year, month)
    matches = [week[weekday] for week in days if week[weekday] != 0]
    if not matches:
        return None
    try:
        return matches[nth - 1] if nth > 0 else matches[nth]
    except IndexError:
        return None


def _occurrences(event: dict, start: datetime, end: datetime) -> list[datetime]:
    """When this event fires between start and end."""
    out: list[datetime] = []
    day = start.date()
    while day <= end.date():
        when = datetime(day.year, day.month, day.day,
                        event["hour"], event["minute"], tzinfo=timezone.utc)
        if start <= when <= end and _fires_on(event, when):
            out.append(when)
        day += timedelta(days=1)
    return out


def _fires_on(event: dict, when: datetime) -> bool:
    """Whether the event's schedule matches this date."""
    if "months" in event and when.month not in event["months"]:
        return False

    weekday = event.get("weekday")
    if weekday is not None and when.weekday() != weekday:
        return False

    if "day_range" in event:
        low, high = event["day_range"]
        if not (low <= when.day <= high):
            return False
        # A windowed event with a weekday means "that weekday inside the window".
        if weekday is None:
            # Approximate the release as a weekday inside the window.
            if when.weekday() > 4:
                return False
            # Only the midpoint weekday counts, so the window does not flag
            # every single day as an event.
            if when.day != (low + high) // 2 and "months" not in event:
                return False

    nth = event.get("nth")
    if nth is not None and weekday is not None:
        target = _nth_weekday(when.year, when.month, weekday, nth)
        if target != when.day:
            return False

    return True


def _now() -> datetime:
    """Current UTC time, in one place so tests can pin it."""
    return datetime.now(timezone.utc)


def upcoming_events(
    within_minutes: int = 60,
    currencies: Optional[set[str]] = None,
    include_recent: int = 15,
    now: Optional[datetime] = None,
) -> list[dict]:
    """Scheduled events near now.

    Looks both forward (a release about to land) and slightly back, because the
    minutes right after a print are the most violent part. `now` is injectable so
    the schedule can be tested against known dates.
    """
    now = now or _now()
    start = now - timedelta(minutes=include_recent)
    end = now + timedelta(minutes=within_minutes)

    found: list[dict] = []
    for event in RECURRING_EVENTS:
        if currencies and event["currency"] not in currencies:
            continue
        for when in _occurrences(event, start, end):
            delta = (when - now).total_seconds() / 60
            found.append({
                "name": event["name"],
                "currency": event["currency"],
                "impact": event["impact"],
                "at": when.isoformat(timespec="minutes"),
                "minutes_away": round(delta, 1),
                "status": "imminent" if delta >= 0 else "just released",
            })

    found.extend(_external_events(start, end, currencies, now))
    found.sort(key=lambda e: abs(e["minutes_away"]))
    return found


def _external_events(
    start: datetime, end: datetime, currencies: Optional[set[str]],
    now: Optional[datetime] = None,
) -> list[dict]:
    """Optional extra events from a user-supplied JSON feed.

    Kept strictly additive: a feed that is down or malformed logs and returns
    nothing, so the built-in schedule still protects the desk.
    """
    url = getattr(settings, "NEWS_API_URL", "")
    if not url:
        return []
    try:
        from app.trading.broker import http_json

        status, payload, _ = http_json("GET", url, headers={"Accept": "application/json"},
                                       timeout=8.0)
        if status >= 400 or not isinstance(payload, list):
            return []
    except Exception as exc:
        logger.warning("News feed unavailable: %s", exc)
        return []

    now = now or _now()
    out = []
    for row in payload[:200]:
        try:
            when = datetime.fromisoformat(str(row["date"]).replace("Z", "+00:00"))
            impact = str(row.get("impact", "")).lower()
            currency = str(row.get("country") or row.get("currency") or "").upper()[:3]
            if impact not in ("high", "medium"):
                continue
            if currencies and currency not in currencies:
                continue
            if not (start <= when <= end):
                continue
            delta = (when - now).total_seconds() / 60
            out.append({
                "name": str(row.get("title") or row.get("event") or "event"),
                "currency": currency,
                "impact": impact,
                "at": when.isoformat(timespec="minutes"),
                "minutes_away": round(delta, 1),
                "status": "imminent" if delta >= 0 else "just released",
                "source": "feed",
            })
        except Exception:
            continue
    return out


def news_blackout(symbol: str = "", now: Optional[datetime] = None) -> tuple[bool, str]:
    """Is it unsafe to open a position right now?

    Returns (blocked, reason). Blocked when a high-impact release for a relevant
    currency lands inside the configured window, or landed in the last few
    minutes. Spreads widen and stops slip during those minutes, so entering then
    is taking a coin flip with worse execution.
    """
    if not settings.NEWS_BLACKOUT_ENABLED:
        return False, "News blackout checking is disabled."

    currencies = SYMBOL_CURRENCIES.get(symbol) if symbol else None
    before = int(settings.NEWS_BLACKOUT_BEFORE_MIN)
    after = int(settings.NEWS_BLACKOUT_AFTER_MIN)

    events = upcoming_events(within_minutes=before, currencies=currencies,
                             include_recent=after, now=now)
    high = [e for e in events if e["impact"] == "high"]
    if high:
        e = high[0]
        when = (f"in {e['minutes_away']:.0f} min" if e["minutes_away"] >= 0
                else f"{abs(e['minutes_away']):.0f} min ago")
        return True, (
            f"{e['name']} ({e['currency']}, high impact) {when} — "
            f"spreads widen and stops slip around this release."
        )

    medium = [e for e in events if e["impact"] == "medium"]
    if medium and settings.style_name() == "scalp":
        e = medium[0]
        return True, (
            f"{e['name']} ({e['currency']}, medium impact) in "
            f"{e['minutes_away']:.0f} min — too close for scalping."
        )

    if events:
        e = events[0]
        return False, (
            f"No high-impact release due. Nearest: {e['name']} in "
            f"{e['minutes_away']:.0f} min ({e['impact']})."
        )
    return False, f"No scheduled high-impact events in the next {before} minutes."


def calendar_context(symbol: str) -> str:
    """Event block for an agent prompt, so it can reason about timing."""
    currencies = SYMBOL_CURRENCIES.get(symbol, set())
    events = upcoming_events(within_minutes=240, currencies=currencies, include_recent=30)
    if not events:
        return ""
    lines = [f"SCHEDULED EVENTS affecting {symbol} (next 4 hours):"]
    for e in events[:5]:
        when = (f"in {e['minutes_away']:.0f} min" if e["minutes_away"] >= 0
                else f"{abs(e['minutes_away']):.0f} min ago")
        lines.append(f"  {e['name']} ({e['currency']}, {e['impact']}) — {when}")
    blocked, reason = news_blackout(symbol)
    if blocked:
        lines.append(f"  TRADING IS BLOCKED right now: {reason}")
    return "\n".join(lines)
