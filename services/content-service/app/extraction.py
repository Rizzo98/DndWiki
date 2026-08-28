"""LLM extraction client (LiteLLM), one call per transcript chunk.

litellm is imported lazily so the module can be imported and unit-tested
without it installed (mirrors how transcription-service keeps whisperx lazy);
the real call only happens in the running worker/Docker image where litellm
is a declared dependency.

Provider abstraction: LiteLLM routes by the model string's provider/model
prefix (deepseek/*, openai/*, anthropic/*, ollama/*, ...). The provider's API
key (and optional base URL) are read from settings and exported to the
environment under the env var LiteLLM expects, so switching providers is a
config change, not a code change. Each provider's key is a placeholder in
.env.example until the DM fills it in.

Every chunk must produce JSON matching EXTRACTION_SCHEMA. Because LLMs
occasionally emit slightly malformed JSON (missing commas, trailing commas,
markdown fences, ...), parsing is lenient in two stages:

1. Local repair: the raw json.loads is tried first; on failure the output
   goes through json_repair (handles missing/trailing commas, unquoted
   keys, fences, single quotes). The result must be an OBJECT, and it must
   be recognizable as an extraction (at least one schema key). Category
   keys a model omits because the category is empty (events,
   timeline_entries, ...) are tolerated and default to empty lists — an
   omitted empty category is a benign variation, not a broken response.
2. Corrective retry: if local repair cannot produce a valid extraction, the
   model is asked once more (LLM_JSON_RETRIES times) with its own bad
   output and the parse error appended, asking for corrected JSON only.

If both stages fail the chunk raises ExtractionError, which the worker treats
as a job failure (the session lands on 'failed' and the message retries are
no-ops — see workers/generate.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from json_repair import loads as repair_loads

from app.core.config import ServiceSettings
from app.prompts import SYSTEM_PROMPT, build_chunk_message

logger = logging.getLogger(__name__)

#: Schema keys recognized in per-chunk extractions. Missing CATEGORY keys
#: (characters/locations/events/timeline_entries) default to empty lists —
#: models often omit an empty category despite the schema's 'required' list —
#: while a dict carrying NONE of these keys is not an extraction at all and
#: is rejected (corrective retry).
_REQUIRED_KEYS = (
    "session_summary",
    "characters",
    "locations",
    "events",
    "timeline_entries",
)

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
    "Your previous response was not valid extraction JSON.\n"
    "Error: {error}\n"
    "Respond again with ONLY a single JSON object matching the schema exactly "
    "(no markdown, no commentary outside the JSON)."
)


class ExtractionError(Exception):
    """The LLM returned something that is not valid extraction JSON."""


class LLMClient:
    """Async wrapper around litellm.acompletion for chunk extraction."""

    def __init__(self, settings: ServiceSettings) -> None:
        self._settings = settings

    def _export_provider_env(self) -> None:
        """Export the configured provider keys into the environment for LiteLLM.

        os.environ is only touched when a value is set, so unset placeholders
        never clobber an already-configured environment.
        """
        for provider, (key_env, base_env) in PROVIDER_ENV.items():
            if not self._settings.llm_model.startswith(provider + "/"):
                continue
            key = getattr(self._settings, provider + "_api_key", "") or ""
            if key:
                os.environ.setdefault(key_env, key)
            if base_env:
                base = getattr(self._settings, provider + "_base_url", "") or ""
                if base:
                    os.environ.setdefault(base_env, base)

    async def extract_chunk(
        self,
        chunk_view: str,
        chunk_index: int,
        total_chunks: int,
        out_of_world: list[str] | None = None,
    ) -> dict[str, Any]:
        """Extract structured facts from one chunk view (raises ExtractionError).

        On malformed output, local repair is tried first; if that fails, the
        call is retried up to llm_json_retries times with the bad output
        and the parse error appended as a corrective prompt.
        'out_of_world' names narrators (the DM) the model must never turn
        into characters.
        """
        import litellm  # lazy: heavy dependency, only needed at runtime

        litellm.drop_params = True  # ignore params unsupported by the provider
        self._export_provider_env()

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": build_chunk_message(
                    chunk_view, chunk_index, total_chunks, out_of_world=out_of_world
                ),
            },
        ]
        for attempt in range(self._settings.llm_json_retries + 1):
            response = await litellm.acompletion(
                model=self._settings.llm_model,
                temperature=self._settings.llm_temperature,
                max_tokens=self._settings.llm_max_tokens,
                response_format={"type": "json_object"},
                messages=messages,
            )
            content = response.choices[0].message.content
            try:
                return self._parse(content, chunk_index)
            except ExtractionError as exc:
                if attempt >= self._settings.llm_json_retries:
                    raise
                logger.warning(
                    "chunk %d: %s (attempt %d/%d); asking the model to fix the JSON",
                    chunk_index + 1,
                    exc,
                    attempt + 1,
                    self._settings.llm_json_retries,
                )
                messages = [
                    *messages,
                    {"role": "assistant", "content": content or ""},
                    {"role": "user", "content": CORRECT_JSON_MESSAGE.format(error=str(exc))},
                ]

    def _load_json(self, content: str, chunk_index: int) -> Any:
        """Parse LLM output, repairing common JSON mistakes when needed."""
        text = content.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass  # fall through to lenient repair
        try:
            repaired = repair_loads(text)
        except Exception as exc:  # json_repair.JsonRepairError and friends
            raise ExtractionError(
                f"LLM returned invalid JSON for chunk {chunk_index + 1}: {exc}"
            ) from exc
        if isinstance(repaired, str):
            # json_repair returns the input unchanged when it cannot parse it
            # (e.g. plain prose) — treat as invalid rather than silently accept.
            raise ExtractionError(
                f"LLM returned invalid JSON for chunk {chunk_index + 1}: unparseable output"
            )
        return repaired

    def _parse(self, content: str | None, chunk_index: int) -> dict[str, Any]:
        if not content:
            raise ExtractionError(f"empty LLM response for chunk {chunk_index + 1}")
        data = self._load_json(content, chunk_index)
        if not isinstance(data, dict):
            raise ExtractionError(f"LLM returned non-object JSON for chunk {chunk_index + 1}")
        # Recognizable-extraction guard: the object must carry at least one
        # schema key. A dict that is unrelated JSON is not an extraction and
        # goes through the corrective retry. Missing CATEGORY keys, however,
        # are tolerated (models omit empty categories) and default to []
        # below — an omitted 'events'/'timeline_entries' must never fail a
        # whole chunk, let alone the generation job.
        if not any(key in data for key in _REQUIRED_KEYS):
            raise ExtractionError(
                f"LLM output for chunk {chunk_index + 1} has no extraction keys: "
                f"{sorted(data)[:5]}"
            )
        # Normalize: ensure lists/ints where the schema expects them.
        for key in ("characters", "locations", "events", "timeline_entries"):
            if not isinstance(data.get(key), list):
                data[key] = []
        # v2/v3 fields are coerced leniently: a model that ignores 'language',
        # 'session_facts' or the category hints still yields a valid
        # extraction (empty defaults).
        for key in ("characters", "locations"):
            for entity in data[key]:
                if not isinstance(entity, dict):
                    continue
                if not isinstance(entity.get("session_facts"), list):
                    entity["session_facts"] = []
                if not isinstance(entity.get("facts"), list):
                    entity["facts"] = []
        for character in data["characters"]:
            if not isinstance(character, dict):
                continue
            character["is_party"] = bool(character.get("is_party"))
            for key in ("physical_look", "personality"):
                value = character.get(key)
                character[key] = value.strip() if isinstance(value, str) else ""
            # v5 static info fields: free text, empty when the model skipped them
            for key in ("race", "class", "gender", "height", "weight", "age"):
                value = character.get(key)
                character[key] = value.strip() if isinstance(value, str) else ""
            # v5 durable relationships: type -> list of proper names
            rels = character.get("relationships")
            if not isinstance(rels, dict):
                character["relationships"] = {}
            else:
                character["relationships"] = {
                    str(k): [n for n in (v if isinstance(v, list) else []) if isinstance(n, str)]
                    for k, v in rels.items()
                }
        for location in data["locations"]:
            if not isinstance(location, dict):
                continue
            for hint in ("place_type", "part_of"):
                value = location.get(hint)
                location[hint] = value.strip() if isinstance(value, str) else ""
        if not isinstance(data.get("session_summary"), str):
            data["session_summary"] = ""
        if not isinstance(data.get("language"), str):
            data["language"] = ""
        return data

    async def extract_many(
        self,
        chunk_views: list[str],
        *,
        concurrency: int,
        out_of_world: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Extract every chunk with a bounded number of in-flight calls."""
        semaphore = asyncio.Semaphore(concurrency)

        async def _one(index: int, view: str) -> dict[str, Any]:
            async with semaphore:
                logger.info("extracting chunk %d/%d", index + 1, len(chunk_views))
                return await self.extract_chunk(
                    view, index, len(chunk_views), out_of_world=out_of_world
                )

        return list(await asyncio.gather(*(_one(i, v) for i, v in enumerate(chunk_views))))