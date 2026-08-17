"""NVIDIA Magpie TTS — neural speech synthesis using the existing NVIDIA key.

Verified working against the hosted NVCF endpoint with encoding=LINEAR_PCM,
which returns a ready-to-play WAV. No separate signup or key is needed.

The frontend falls back to the browser's built-in voice whenever this returns
an error, so a credit exhaustion or outage degrades quality instead of
silencing the agents.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

import urllib.error
import urllib.request

from app.config import settings

logger = logging.getLogger(__name__)

MAGPIE_URL = (
    "https://877104f7-e885-42b9-8de8-f6e4c6303969"
    ".invocation.api.nvcf.nvidia.com/v1/audio/synthesize"
)

# Distinct voices so the agents don't all sound like one person.
AGENT_VOICES = {
    "manager":            "Magpie-Multilingual.EN-US.Sofia",
    "risk_manager":       "Magpie-Multilingual.EN-US.Sofia",
    "super_trader":       "Magpie-Multilingual.EN-US.Aria",
    "computer_scientist": "Magpie-Multilingual.EN-US.Aria",
    "trader_bot_1":       "Magpie-Multilingual.EN-US.Sofia",
    "trader_bot_2":       "Magpie-Multilingual.EN-US.Aria",
    "trader_bot_3":       "Magpie-Multilingual.EN-US.Sofia",
    "trader_bot_4":       "Magpie-Multilingual.EN-US.Aria",
}
DEFAULT_VOICE = "Magpie-Multilingual.EN-US.Sofia"

LANG_TAGS = {"en": "en-US", "bn": "hi-IN"}  # no bn voice yet; hi-IN is closest

MAX_CHARS = 800   # one sentence-ish chunk; longer text is split by the caller


def _multipart(fields: dict[str, str]) -> tuple[str, bytes]:
    boundary = uuid.uuid4().hex
    body = b""
    for key, value in fields.items():
        body += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
            f"{value}\r\n"
        ).encode()
    body += f"--{boundary}--\r\n".encode()
    return boundary, body


def is_available() -> bool:
    """True when a key is configured. Does not prove the service is up."""
    return bool(settings.NVIDIA_API_KEY)


def synthesize(text: str, agent_key: str = "", lang: str = "en") -> Optional[bytes]:
    """Return WAV bytes for `text`, or None so the caller can fall back.

    Never raises — a voice failure must not break the chat.
    """
    if not settings.NVIDIA_API_KEY:
        return None

    clean = (text or "").strip()
    if not clean:
        return None
    if len(clean) > MAX_CHARS:
        clean = clean[:MAX_CHARS]

    fields = {
        "text": clean,
        "language": LANG_TAGS.get(lang, "en-US"),
        "voice": AGENT_VOICES.get(agent_key, DEFAULT_VOICE),
        "encoding": "LINEAR_PCM",   # the only value this endpoint accepts
    }
    boundary, body = _multipart(fields)

    req = urllib.request.Request(
        MAGPIE_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {settings.NVIDIA_API_KEY}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            audio = resp.read()
        if len(audio) < 512:
            logger.warning("Magpie returned %d bytes — treating as failure", len(audio))
            return None
        return audio
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read()[:160].decode("utf-8", "ignore")
        except Exception:
            pass
        logger.warning("Magpie TTS HTTP %s: %s", exc.code, detail)
        return None
    except Exception as exc:
        logger.warning("Magpie TTS failed: %s", exc)
        return None
