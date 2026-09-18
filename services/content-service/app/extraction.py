"""LLM client (LiteLLM): per-chunk transcript extraction + summary revision.

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

Two entry points share the same JSON contract and the same two-stage error
handling:

- extract_chunk: one call per transcript chunk (EXTRACTION_SCHEMA).
- revise_summary: ONE call that applies the DM's review feedback to an
  already extracted session. Since prompt v14 it answers with a PATCH - the
  complete summary plus the items the correction touches, addressed by the id
  every item is given - instead of echoing the whole extraction back; the
  echo did not fit the completion cap and its tail (locations, events,
  timeline entries) was silently lost (app/revision.py, 'Truncation' below).

JSON handling is lenient in two stages:

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

Truncation is NOT a repairable mistake. A response the provider cut off at
'max_tokens' parses perfectly - it is simply missing everything after the cut -
so repairing it turns "the model ran out of room" into a partial extraction
that looks like a complete one (that is how a DM's correction reached the
summary and never the events). A cut-off response is detected from the
provider's 'finish_reason' and goes through the corrective retry instead (the
model is asked for a shorter answer), and the job fails if the cap is really
too small: a loud failure, never a truncated draft.

An optional 'validate' check runs inside the same retry loop, so an answer
whose SHAPE is wrong (the revision patch of a model that answered with the
whole extraction, an update addressing an item the session does not have) is
re-asked with the concrete problem attached rather than accepted or dropped.

If both stages fail the call raises ExtractionError, which the worker treats
as a job failure (the session lands on 'failed' and the message retries are
no-ops — see workers/generate.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable
from typing import Any

from json_repair import loads as repair_loads

from app.core.config import ServiceSettings
from app.prompts import (
    SUMMARY_COMPOSE_SYSTEM_PROMPT,
    SUMMARY_REVISION_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_chunk_message,
    build_summary_compose_message,
    build_summary_revision_message,
)
from app.revision import SummaryRevisionError, apply_summary_revision
from app.summary import normalize_blocks

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

#: Follow-up message when the provider cut the answer off at the completion
#: cap: the fix is a SHORTER answer, not corrected syntax.
TRUNCATED_JSON_MESSAGE = (
    "Your previous response was cut off before the JSON was complete.\n"
    "Error: {error}\n"
    "Respond again with a SHORTER but COMPLETE JSON object: only the fields the "
    "request actually needs (no markdown, no commentary outside the JSON)."
)

#: finish_reason values meaning "the completion hit max_tokens".
TRUNCATED_FINISH_REASONS = frozenset({"length", "max_tokens"})


class ExtractionError(Exception):
    """The LLM returned something that is not valid extraction JSON."""


class TruncatedResponse(ExtractionError):
    """The provider cut the completion off at max_tokens: the JSON that came
    back parses, but everything after the cut is missing."""


def _narrative_check(payload: Any) -> None:
    """The compose answer must carry the story, as blocks or as plain prose.

    An empty narrative - or the beats quoted back as a list of separate
    statements - is refused here: the worker keeps the beats and the DM sees
    *something*, which beats storing a "story" that is not one.
    """
    summary = payload.get("session_summary") if isinstance(payload, dict) else None
    if not normalize_blocks(summary):
        raise ExtractionError("the composition returned no story for the session")


def _revision_patch_check(current: dict[str, Any]) -> Callable[[Any], None]:
    """The check a summary-revision answer must pass before it is accepted.

    app/revision.py owns the rules, so the check is simply "can this patch be
    merged into this extraction?": a patch that cannot is refused inside the
    retry loop (the model is told which id or field was wrong) instead of being
    applied halfway or dropped on the floor.
    """

    def check(patch: Any) -> None:
        try:
            apply_summary_revision(current, patch)
        except SummaryRevisionError as exc:
            raise ExtractionError(
                f"the revision patch does not fit the session's extraction: {exc}"
            ) from exc

    return check


class LLMClient:
    """Async wrapper around litellm.acompletion for the extraction prompts."""

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

        'out_of_world' names narrators (the DM) the model must never turn
        into characters.
        """
        return await self._complete_json(
            SYSTEM_PROMPT,
            build_chunk_message(
                chunk_view, chunk_index, total_chunks, out_of_world=out_of_world
            ),
            chunk_index,
        )
    async def compose_summary(
        self,
        current: dict[str, Any],
        *,
        language: str | None = None,
        scenes: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, str]]:
        """Write the session's STORY from the merged beats (raises ExtractionError).

        The beats were written one chunk at a time, each without sight of the
        others, so they read as separate moments and a being can be "una
        creatura" in one and named in the next. This is the only call that sees
        the whole session: it returns the narrative as scene blocks
        ([{location, text}], app/summary.py), which is what the DM reads and
        highlights portions of. 'scenes' is the record's own reading of where
        the session happens (place, who is there, who is elsewhere), when the
        attribution engine ran: it is what the block labels are grounded in.
        """
        payload = await self._complete_json(
            SUMMARY_COMPOSE_SYSTEM_PROMPT,
            build_summary_compose_message(current, language=language, scenes=scenes),
            "the session summary composition",
            validate=_narrative_check,
            extraction=False,
        )
        return normalize_blocks(payload.get("session_summary"))

    async def revise_summary(
        self,
        current: dict[str, Any],
        edits: list[dict[str, Any]] | None = None,
        summary_text_override: str | None = None,
    ) -> dict[str, Any]:
        """Apply the DM's review feedback to an extracted session.

        'current' is the persisted extraction (the narrative + entities +
        events + timeline entries), 'edits' the correction requests
        ({'targets': [passage, ...], 'instruction': str}) where the passages are
        the portions of the narrative the DM highlighted. Returns the PATCH
        app/revision.py merges into 'current' - the whole narrative in blocks
        plus the items the correction touches, never the whole extraction - and
        refuses, through the corrective retry, an answer that does not fit: an
        item id the session does not have, a revision without a narrative, or
        the old full-extraction echo. Raises ExtractionError when the model
        cannot do better.
        """
        return await self._complete_json(
            SUMMARY_REVISION_SYSTEM_PROMPT,
            build_summary_revision_message(
                current, edits, summary_text_override=summary_text_override
            ),
            "the session summary revision",
            validate=_revision_patch_check(current),
            extraction=False,
        )

    async def _complete_json(
        self,
        system: str,
        user: str,
        context: str | int,
        validate: Callable[[dict[str, Any]], Any] | None = None,
        *,
        extraction: bool = True,
    ) -> dict[str, Any]:
        """One JSON-mode completion with local repair + corrective retries.

        'context' labels the call in error messages: an int is a chunk index
        ('chunk 1'), a string is used as-is. 'validate' is an optional check of
        the parsed object that raises ExtractionError when the answer does not
        fit what the caller asked for; it runs INSIDE the retry loop, so the
        model is told what was wrong instead of the caller having to guess.
        'extraction' says whether the answer must have the extraction's shape
        (default) or is parsed raw and left to 'validate' - the summary
        revision answers with a patch, not with an extraction.
        """
        import litellm  # lazy: heavy dependency, only needed at runtime

        litellm.drop_params = True  # ignore params unsupported by the provider
        self._export_provider_env()

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        for attempt in range(self._settings.llm_json_retries + 1):
            response = await litellm.acompletion(
                model=self._settings.llm_model,
                temperature=self._settings.llm_temperature,
                max_tokens=self._settings.llm_max_tokens,
                response_format={"type": "json_object"},
                messages=messages,
            )
            choice = response.choices[0]
            content = choice.message.content
            try:
                self._assert_not_truncated(choice, context)
                data = self._parse(content, context, extraction=extraction)
                if validate is not None:
                    validate(data)
                return data
            except ExtractionError as exc:
                if attempt >= self._settings.llm_json_retries:
                    raise
                logger.warning(
                    "%s: %s (attempt %d/%d); asking the model to fix the JSON",
                    self._where(context),
                    exc,
                    attempt + 1,
                    self._settings.llm_json_retries,
                )
                messages = [
                    *messages,
                    {"role": "assistant", "content": content or ""},
                    {"role": "user", "content": self._corrective_message(exc)},
                ]
        raise ExtractionError(f"unreachable: no attempt made for {self._where(context)}")

    def _assert_not_truncated(self, choice: Any, context: str | int) -> None:
        """Refuse an answer the provider cut off at the completion cap.

        A truncated completion is not malformed JSON that json_repair can fix:
        what is there parses, and everything after the cut (the locations, the
        events, the timeline entries of an echoed extraction) is simply gone.
        Repairing it produced a partial extraction that looked complete and
        carried the PREVIOUS revision's text, so the cut is detected from the
        provider's 'finish_reason', re-asked as a shorter answer, and the job
        fails when the cap is really too small for the request.
        """
        reason = str(getattr(choice, "finish_reason", "") or "").lower()
        if reason in TRUNCATED_FINISH_REASONS:
            raise TruncatedResponse(
                f"the model ran out of output tokens (finish_reason={reason}, cap "
                f"{self._settings.llm_max_tokens}) for {self._where(context)}: the "
                "JSON was cut off and must not be repaired into a partial answer"
            )

    @staticmethod
    def _corrective_message(exc: ExtractionError) -> str:
        """What to tell the model about the answer that was refused."""
        if isinstance(exc, TruncatedResponse):
            return TRUNCATED_JSON_MESSAGE.format(error=str(exc))
        return CORRECT_JSON_MESSAGE.format(error=str(exc))

    @staticmethod
    def _where(context: str | int) -> str:
        """Human label of the call the error came from."""
        return f"chunk {context + 1}" if isinstance(context, int) else str(context)

    def _load_json(self, content: str, context: str | int) -> Any:
        """Parse LLM output, repairing common JSON mistakes when needed."""
        text = content.strip()
        where = self._where(context)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass  # fall through to lenient repair
        try:
            repaired = repair_loads(text)
        except Exception as exc:  # json_repair.JsonRepairError and friends
            raise ExtractionError(f"LLM returned invalid JSON for {where}: {exc}") from exc
        if isinstance(repaired, str):
            # json_repair returns the input unchanged when it cannot parse it
            # (e.g. plain prose) — treat as invalid rather than silently accept.
            raise ExtractionError(
                f"LLM returned invalid JSON for {where}: unparseable output"
            )
        return repaired

    def _parse(
        self, content: str | None, context: str | int, *, extraction: bool = True
    ) -> dict[str, Any]:
        """Validate + normalize one JSON answer.

        'extraction' tells which CONTRACT the answer has to satisfy: with it,
        the object must look like an extraction and is coerced into the
        schema's shape (missing categories become []). The summary revision is
        not an extraction - it is a patch over one - so it is parsed raw and
        left to its own validator: the category defaults below would INVENT
        the four category keys in every patch and make it look like the
        full-extraction echo the protocol refuses.
        """
        where = self._where(context)
        if not content:
            raise ExtractionError(f"empty LLM response for {where}")
        data = self._load_json(content, context)
        if not isinstance(data, dict):
            raise ExtractionError(f"LLM returned non-object JSON for {where}")
        if not extraction:
            return data
        # Recognizable-extraction guard: the object must carry at least one
        # schema key. A dict that is unrelated JSON is not an extraction and
        # goes through the corrective retry. Missing CATEGORY keys, however,
        # are tolerated (models omit empty categories) and default to []
        # below — an omitted 'events'/'timeline_entries' must never fail a
        # whole chunk, let alone the generation job.
        if not any(key in data for key in _REQUIRED_KEYS):
            raise ExtractionError(
                f"LLM output for {where} has no extraction keys: {sorted(data)[:5]}"
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
