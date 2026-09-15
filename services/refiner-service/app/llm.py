"""LiteLLM refinement client (lazy import), one call per turn window.

litellm is imported lazily so the module can be imported and unit-tested
without it installed (mirrors content-service extraction). Provider abstraction
is the same: LiteLLM routes by the model string's provider/model prefix and
reads the provider's API key from the environment, so switching providers is a
config change, not a code change.

Parsing is lenient in two stages, like content-service:
1. Local repair via json_repair (markdown fences, missing/trailing commas,
   unquoted keys, ...).
2. Corrective retry (refiner_json_retries): the model is re-asked with its own
   bad output and the parse error appended, asking for corrected JSON only.
If both fail, RefineError is raised and the worker fails the session.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from json_repair import loads as repair_loads

from app.core.config import ServiceSettings
from app.prompts import (
    REFINE_SCHEMA,
    TEXT_ONLY_SCHEMA,
    build_window_message,
    system_prompt,
)
from app.refine import RefineError, parse_decisions

logger = logging.getLogger(__name__)

#: LiteLLM provider prefix -> (API key env var, base URL env var or None).
#: Add new providers here; the matching settings fields live in app/core/config.py.
PROVIDER_ENV: dict[str, tuple[str, str | None]] = {
    "deepseek": ("DEEPSEEK_API_KEY", None),
    "openai": ("OPENAI_API_KEY", None),
    "anthropic": ("ANTHROPIC_API_KEY", None),
    "ollama": ("OLLAMA_API_KEY", "OLLAMA_BASE_URL"),
}

#: Follow-up message on a corrective retry: the model sees its own (bad) output
#: as the assistant turn, then this user message with the concrete parse error.
CORRECT_JSON_MESSAGE = (
    "Your previous response was not valid refinement JSON.\n"
    "Error: {error}\n"
    "Respond again with ONLY a single JSON object matching the schema exactly "
    "(no markdown, no commentary outside the JSON)."
)


class RefinerLLM:
    """Async wrapper around litellm.acompletion for one window of turns."""

    def __init__(self, settings: ServiceSettings) -> None:
        self._settings = settings

    def _export_provider_env(self) -> None:
        """Export the configured provider keys into the environment for LiteLLM."""
        for provider, (key_env, base_env) in PROVIDER_ENV.items():
            if not self._settings.effective_model.startswith(provider + "/"):
                continue
            key = getattr(self._settings, provider + "_api_key", "") or ""
            if key:
                os.environ.setdefault(key_env, key)
            if base_env:
                base = getattr(self._settings, provider + "_base_url", "") or ""
                if base:
                    os.environ.setdefault(base_env, base)

    async def refine_window(
        self,
        turns: list[dict],
        *,
        context_blocks: list[str],
        fixed_count: int,
        window_index: int,
        total_windows: int,
        cast_lines: list[str] | None = None,
        member_count: int | None = None,
        include_speakers: bool = True,
    ) -> dict[int, tuple[str, str]]:
        """Refine one window of turns (raises RefineError on unusable output).

        'include_speakers' selects the prompt AND the schema together: the model
        must never be shown a speaker field it has been told not to fill, and
        must never be asked for one it was told not to produce.
        """
        import litellm  # lazy: heavy dependency, only needed at runtime

        litellm.drop_params = True  # ignore params unsupported by the provider
        self._export_provider_env()

        schema = REFINE_SCHEMA if include_speakers else TEXT_ONLY_SCHEMA
        messages = [
            {
                "role": "system",
                "content": system_prompt(include_speakers=include_speakers).format(
                    schema=json.dumps(schema)
                ),
            },
            {
                "role": "user",
                "content": build_window_message(
                    turns,
                    context_blocks=context_blocks,
                    fixed_count=fixed_count,
                    window_index=window_index,
                    total_windows=total_windows,
                    cast_lines=cast_lines,
                    member_count=member_count,
                    include_speakers=include_speakers,
                ),
            },
        ]
        for attempt in range(self._settings.refiner_json_retries + 1):
            response = await litellm.acompletion(
                model=self._settings.effective_model,
                temperature=self._settings.refiner_temperature,
                max_tokens=self._settings.refiner_max_tokens,
                response_format={"type": "json_object"},
                messages=messages,
            )
            content = response.choices[0].message.content
            try:
                return parse_decisions(
                    self._load_json(content), include_speakers=include_speakers
                )
            except RefineError as exc:
                if attempt >= self._settings.refiner_json_retries:
                    raise
                logger.warning(
                    "window %d: %s (attempt %d/%d); asking the model to fix the JSON",
                    window_index + 1,
                    exc,
                    attempt + 1,
                    self._settings.refiner_json_retries,
                )
                messages = [
                    *messages,
                    {"role": "assistant", "content": content or ""},
                    {
                        "role": "user",
                        "content": CORRECT_JSON_MESSAGE.format(error=str(exc)),
                    },
                ]
        raise RefineError("unreachable")  # pragma: no cover

    def _load_json(self, content: str | None) -> Any:
        """Parse LLM output, repairing common JSON mistakes when needed."""
        if not content or not content.strip():
            raise RefineError("empty LLM response")
        text = content.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass  # fall through to lenient repair
        try:
            repaired = repair_loads(text)
        except Exception as exc:  # json_repair.JsonRepairError and friends
            raise RefineError(f"LLM returned invalid JSON: {exc}") from exc
        if isinstance(repaired, str):
            raise RefineError("LLM returned invalid JSON: unparseable output")
        return repaired