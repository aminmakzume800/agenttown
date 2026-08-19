"""Base agent class that all agents inherit from."""
from typing import Optional

from app.llm_client import AGENT_MODEL_MAP, chat_completion, model_for
from app.db import log_event
from app.memory import save_message, get_history
from app.market_data import format_market_context


class BaseAgent:
    """Base class for all trading agents."""

    agent_key: str = ""
    name: str = ""
    role: str = ""
    system_prompt: str = ""
    status: str = "idle"
    # Override in subclasses to inject market data for specific symbols
    market_symbols: list[str] = []

    def __init__(self):
        self._provider, self._model = AGENT_MODEL_MAP.get(
            self.agent_key, ("nvidia", "nvidia/nemotron-3-nano-30b-a3b")
        )

    def chat(self, user_message: str, lang: str = "en") -> str:
        """Send a message to the agent and get a response."""
        self.status = "thinking"
        try:
            # Build system prompt with market context if relevant
            prompt = self.system_prompt

            # Replies are read aloud, so keep them speakable: no scratchpad,
            # no markdown scaffolding, conversational length.
            prompt += (
                "\n\nOUTPUT RULES:\n"
                "- Reply directly. Never show your reasoning process or restate the question.\n"
                "- Do not open with phrases like 'The user asks' or 'We need to answer'.\n"
                "- Keep it under 120 words unless asked for detail.\n"
                "- Write in plain prose that sounds natural read aloud. "
                "Avoid tables, headers and bullet lists unless quoting exact trade levels."
            )

            if lang == "bn":
                prompt += "\n\nIMPORTANT: Respond in Bengali (বাংলা)."

            # Inject live market data if agent has market_symbols
            if self.market_symbols:
                for sym in self.market_symbols:
                    market_ctx = format_market_context(sym)
                    if "[Market data unavailable" not in market_ctx:
                        prompt += f"\n\n{market_ctx}"

            # Get conversation history for context
            history = get_history(self.agent_key, limit=6)

            # Build messages with history
            messages = [{"role": "system", "content": prompt}]
            for msg in history:
                messages.append(msg)
            messages.append({"role": "user", "content": user_message})

            # Resolved per call, not at startup, so switching to scalping takes
            # effect immediately instead of after a restart.
            provider, model = model_for(self.agent_key)

            response = chat_completion(
                provider=provider,
                model=model,
                system_prompt=prompt,
                user_message=user_message,
                history=history,
            )

            if response is None:
                response = self.get_canned_response(user_message, lang)

            # Save to memory
            save_message(self.agent_key, "user", user_message)
            save_message(self.agent_key, "assistant", response)

            # Log the interaction
            log_event(
                agent_key=self.agent_key,
                action_type="chat",
                detail=f"user: {user_message[:200]}",
                metadata=f"response: {response[:200]}",
            )

            return response
        finally:
            self.status = "idle"

    def get_canned_response(self, user_message: str, lang: str = "en") -> str:
        """Fallback response when no API key or API fails."""
        if lang == "bn":
            return f"[{self.name}] আমি এখন ডেমো মোডে আছি। API কী সেট করুন।"
        return f"[{self.name}] I'm in demo mode. Set API keys in .env for real responses."

    def to_dict(self) -> dict:
        """Serialize agent info for API responses."""
        return {
            "agent_key": self.agent_key,
            "name": self.name,
            "role": self.role,
            "status": self.status,
        }
