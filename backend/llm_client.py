"""
Tynd wrapper omkring Anthropic's Messages API.

Vi isolerer LLM-kaldet bag en lille klasse for at gøre det nemt at bytte
provider, mocke det i test, og holde fejlhåndtering ét sted.

Reliability-features:
- Retry med exponential backoff på transiente fejl.
- HTTP-timeout, så hængende API-kald ikke hænger backend'en.
- Strukturet logging med request-id, så hver vurdering kan spores.
"""

from __future__ import annotations

import logging
import os
import random
import time
import uuid
from typing import Any

import anthropic

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """Generel fejlklasse for problemer ved LLM-integrationen."""


# Disse fejl er værd at retry på - midlertidige problemer i netværk eller server.
_RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    anthropic.APIConnectionError,
    anthropic.RateLimitError,
    anthropic.APITimeoutError,
    anthropic.InternalServerError,
)


class AnthropicClient:
    """Lille wrapper omkring Anthropic's SDK."""

    def __init__(
        self,
        api_key: str | None = None,
        default_model: str | None = None,
        timeout_seconds: float = 60.0,
        max_retries: int = 3,
    ) -> None:
        api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise LLMError(
                "ANTHROPIC_API_KEY ikke sat. Sæt environment-variablen "
                "før du starter backend'en."
            )
        # SDK-niveauets retry sættes til 0 - vi styrer selv retry-loopet
        # for at få konsistent logging og kontrol over backoff.
        self._client = anthropic.Anthropic(
            api_key=api_key,
            timeout=timeout_seconds,
            max_retries=0,
        )
        self._default_model = default_model or os.getenv(
            "CLAUDE_MODEL", "claude-opus-4-7"
        )
        self._max_retries = max_retries

    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        max_tokens: int = 4000,
        temperature: float = 0.2,
        request_id: str | None = None,
    ) -> str:
        """
        Send en chat-prompt til modellen og returnér rå tekst fra første
        text-blok i svaret.

        Vi holder temperaturen lav fordi vi vil have stabile, struktur-
        konsistente vurderinger frem for kreative variationer.

        Retry-strategi: op til ``max_retries`` forsøg ved transiente fejl.
        Backoff er eksponentielt med jitter for at undgå at flere parallelle
        kald rammer API'et samtidig efter en fejl.
        """
        model = model or self._default_model
        rid = request_id or uuid.uuid4().hex[:8]

        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            t0 = time.monotonic()
            try:
                message = self._client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_prompt}],
                )
            except _RETRYABLE_EXCEPTIONS as e:
                last_error = e
                if attempt >= self._max_retries:
                    log.error(
                        "[%s] LLM-kald fejlede efter %d forsøg: %s",
                        rid, attempt, e,
                    )
                    raise LLMError(
                        f"LLM utilgængelig efter {attempt} forsøg: {e}"
                    ) from e
                # Exponential backoff: 1s, 2s, 4s ... + 0-500ms jitter
                wait = (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                log.warning(
                    "[%s] forsøg %d/%d fejlede (%s) - venter %.1fs",
                    rid, attempt, self._max_retries, type(e).__name__, wait,
                )
                time.sleep(wait)
                continue
            except anthropic.BadRequestError as e:
                # 4xx-fejl er ikke værd at retry på - de er deterministiske.
                log.error("[%s] dårlig request: %s", rid, e)
                raise LLMError(f"Ugyldig request til LLM: {e}") from e
            except anthropic.APIStatusError as e:
                # Andre 4xx/5xx vi ikke har en specifik handler til.
                log.error("[%s] API-fejl %s: %s", rid, e.status_code, e.message)
                raise LLMError(
                    f"API-fejl ({e.status_code}): {e.message}"
                ) from e
            except Exception as e:  # noqa: BLE001 - sidste-udvejs fallback
                log.exception("[%s] uventet fejl ved LLM-kald", rid)
                raise LLMError(f"Uventet fejl ved LLM-kald: {e}") from e

            elapsed = time.monotonic() - t0
            log.info(
                "[%s] LLM-kald OK (model=%s, forsoeg=%d, %.2fs)",
                rid, model, attempt, elapsed,
            )

            for block in message.content:
                if getattr(block, "type", None) == "text":
                    return block.text  # type: ignore[attr-defined]

            raise LLMError(f"[{rid}] Modellen returnerede ingen tekstblok.")

        # Skulle aldrig nås (loop returnerer eller kaster)
        raise LLMError(
            f"LLM-kald slog fejl uden specifik aarsag: {last_error}"
        )
