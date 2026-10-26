"""OpenAI-compatible chat client with tool calling. Works with Groq, OpenAI, Ollama (/v1) and vLLM."""
import asyncio
import logging

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)


class LLMError(Exception):
    pass


class ChatProvider:
    def __init__(self, name: str, base_url: str, api_key: str, model: str, timeout: float):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    async def chat(self, messages: list[dict], tools: list[dict] | None = None, temperature: float = 0.3) -> dict:
        s = get_settings()
        body: dict = {"model": self.model, "messages": messages, "temperature": temperature,
                      "max_completion_tokens": s.llm_max_completion_tokens}
        if s.llm_reasoning_effort and "gpt-oss" in self.model:
            body["reasoning_effort"] = s.llm_reasoning_effort
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        last: Exception | None = None
        for attempt in range(3):  # bounded retries (NFR-07)
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as c:
                    r = await c.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
                if r.status_code == 429:
                    # Rate limited (e.g. Groq tokens-per-minute). Wait as told if it's short; otherwise give up
                    # quickly and let the scripted agent answer this turn.
                    try:
                        wait = float(r.headers.get("retry-after", "2"))
                    except ValueError:
                        wait = 2.0
                    log.warning("LLM rate limited: retry after %.1fs", wait)
                    if wait > 8 or attempt == 2:
                        raise LLMError(f"{self.name} rate limited")
                    await asyncio.sleep(wait)
                    continue
                if r.status_code >= 500:
                    log.warning("LLM provider failing: HTTP %s", r.status_code)
                    raise LLMError(f"{self.name} HTTP {r.status_code}")
                if r.status_code >= 400:
                    try:
                        err = r.json().get("error", {})
                    except ValueError:
                        err = {}
                    # Log the provider's error code only; messages can echo conversation content.
                    log.warning("LLM rejected request: HTTP %s %s", r.status_code, err.get("code") or err.get("type"))
                    raise LLMError(f"{self.name} HTTP {r.status_code}: {err.get('code')}: {str(err.get('message'))[:300]}")
                msg = r.json()["choices"][0]["message"]
                return {k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")}
            except (httpx.HTTPError, LLMError, KeyError) as e:
                last = e
                log.warning("LLM call failed (attempt %d): %s %s", attempt + 1, type(e).__name__,
                            "" if isinstance(e, LLMError) else str(e)[:120])
                await asyncio.sleep(0.5 * (attempt + 1))
        raise LLMError(str(last))


def get_provider() -> ChatProvider | None:
    s = get_settings()
    name, base, key, model = s.resolved_llm
    if name == "scripted":
        return None
    return ChatProvider(name, base, key, model, s.llm_timeout_s)
