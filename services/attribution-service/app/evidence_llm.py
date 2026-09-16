"""LiteLLM client for the identity-evidence pass.

litellm is imported lazily (same as content-service and the refiner) so the
module can be imported and unit-tested without it installed. Parsing is lenient
in the same two stages they use: json_repair first, then a corrective retry that
shows the model its own bad output.

The pass is BEST-EFFORT by design. If every chunk fails, the engine still runs on
the voice channel and the timing channels alone and reports a lower coverage -
which is strictly better than failing a four-hour session because an LLM was
briefly unavailable (docs/attribution-model.md S16).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from json_repair import loads as repair_loads

from app.core.config import ServiceSettings
from app.evidence import (
    EvidencePass,
    build_chunk_message,
    parse_evidence,
    render_view,
    split_view,
    system_message,
)
from app.scenes import (
    SceneMap,
    build_scene_message,
    merge_scene_parts,
    parse_scenes,
    render_moment_view,
    scene_system_message,
)

logger = logging.getLogger(__name__)

#: LiteLLM provider prefix -> (API key env var, base URL env var or None).
PROVIDER_ENV: dict[str, tuple[str, str | None]] = {
    "deepseek": ("DEEPSEEK_API_KEY", None),
    "openai": ("OPENAI_API_KEY", None),
    "anthropic": ("ANTHROPIC_API_KEY", None),
    "ollama": ("OLLAMA_API_KEY", "OLLAMA_BASE_URL"),
}

CORRECT_JSON_MESSAGE = (
    "Your previous response was not valid evidence JSON.\n"
    "Error: {error}\n"
    "Respond again with ONLY a single JSON object matching the schema exactly."
)

#: Rough chars-per-token estimate, matching content-service's chunker.
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def chunk_lines(
    lines: list[str], max_tokens: int, *, max_items: int | None = None
) -> list[list[str]]:
    """Greedy grouping (no overlap: evidence rows are independent).

    Bounded by BOTH tokens and item count, and the item bound is the one that
    guarantees coverage. A chunk that asks about more utterances than the model
    will answer for is a chunk that silently loses the rest: this used to group
    by tokens alone (312 utterances in the first chunk of a real session) while
    the prompt asked for "at most 40", so the pass returned 40 rows for the first
    312 utterances and 272 of 419 ended up with no kind, no gist and no stakes -
    and therefore no content question the review could ever ask about them.

    Every line still lands in exactly one chunk: the grouping never drops an
    utterance, only decides which request carries it.
    """
    if not lines:
        return []
    budget = max_items if max_items and max_items > 0 else len(lines)
    chunks: list[list[str]] = []
    current: list[str] = []
    used = 0
    for line in lines:
        cost = estimate_tokens(line)
        if current and (used + cost > max_tokens or len(current) >= budget):
            chunks.append(current)
            current, used = [], 0
        current.append(line)
        used += cost
    if current:
        chunks.append(current)
    return chunks


class EvidenceLLM:
    """Async wrapper around litellm.acompletion for one chunk of the view."""

    def __init__(self, settings: ServiceSettings) -> None:
        self._settings = settings

    def _export_provider_env(self) -> None:
        model = self._settings.effective_evidence_model
        for provider, (key_env, base_env) in PROVIDER_ENV.items():
            if not model.startswith(provider + "/"):
                continue
            key = getattr(self._settings, provider + "_api_key", "") or ""
            if key:
                os.environ.setdefault(key_env, key)
            if base_env:
                base = getattr(self._settings, provider + "_base_url", "") or ""
                if base:
                    os.environ.setdefault(base_env, base)

    async def extract(
        self,
        lines: list[str],
        *,
        window: int,
        total: int,
        header: str | None = None,
    ) -> EvidencePass:
        """One chunk -> evidence (raises on unusable output)."""
        raw = await self._json_response(
            system=system_message(),
            user=build_chunk_message(
                "\n".join(lines),
                window=window,
                total=total,
                header=header,
                expected=len(lines),
            ),
            what=f"evidence window {window + 1}/{total}",
        )
        return parse_evidence(raw)

    async def scene_response(
        self,
        lines: list[str],
        *,
        window: int,
        total: int,
        header: str | None = None,
    ) -> Any:
        """One part of the session -> the RAW scene JSON (raises when unusable).

        Raw, not parsed: only the caller knows the session's reference list and
        the roster's own spellings, and a scene that names a character the
        evidence never saw must be dropped by the side that can tell.
        """
        return await self._json_response(
            system=scene_system_message(),
            user=build_scene_message(lines, header=header, window=window, total=total),
            what=f"scene reading {window + 1}/{total}",
        )

    async def _json_response(self, *, system: str, user: str, what: str) -> Any:
        """One JSON response, with the corrective retry the pass already used.

        Shared by both passes: the retry is about the model returning unusable
        JSON, which is a property of the model and not of the question.
        """
        import litellm  # lazy: heavy dependency, only needed at runtime

        litellm.drop_params = True
        self._export_provider_env()

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        for attempt in range(self._settings.evidence_json_retries + 1):
            response = await litellm.acompletion(
                model=self._settings.effective_evidence_model,
                temperature=self._settings.evidence_temperature,
                response_format={"type": "json_object"},
                messages=messages,
            )
            content = response.choices[0].message.content
            try:
                return self._load_json(content)
            except ValueError as exc:
                if attempt >= self._settings.evidence_json_retries:
                    raise
                logger.warning("%s: %s; asking the model to fix the JSON", what, exc)
                messages = [
                    *messages,
                    {"role": "assistant", "content": content or ""},
                    {"role": "user", "content": CORRECT_JSON_MESSAGE.format(error=str(exc))},
                ]
        raise ValueError("unreachable")  # pragma: no cover

    def _load_json(self, content: str | None) -> Any:
        if not content or not content.strip():
            raise ValueError("empty evidence response")
        text = content.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        try:
            repaired = repair_loads(text)
        except Exception as exc:  # json_repair.JsonRepairError and friends
            raise ValueError(f"invalid evidence JSON: {exc}") from exc
        if isinstance(repaired, str):
            # ValueError, not TypeError: nothing about the CALLER's argument is
            # wrong - the model returned something we could not parse.
            raise ValueError("unparseable evidence output")  # noqa: TRY004
        return repaired


def merge_passes(passes: list[EvidencePass]) -> EvidencePass:
    """Combine per-chunk results: items union, notes concatenated."""
    merged = EvidencePass()
    for partial in passes:
        merged.language = partial.language or merged.language
        merged.items.update(partial.items)
        merged.notes.extend(partial.notes)
    return merged


async def run_evidence_pass(
    *,
    utterances: list[Any],
    voice_of: dict[str, str],
    member_of: dict[str, str],
    roster: list[str],
    settings: ServiceSettings,
    llm: EvidenceLLM | None = None,
) -> EvidencePass:
    """Render, chunk and run the whole pass; never raises.

    A failed chunk is logged and dropped rather than failing the run: the engine
    falls back to voice and continuity, and the coverage it reports will simply
    be lower and honest about it.
    """
    if not utterances:
        return EvidencePass()
    view = render_view(utterances, voice_of=voice_of, member_of=member_of, roster=roster)
    # The header (the roster) does NOT belong to any one chunk: every chunk needs
    # it, so it is carried separately instead of being the first lines of the
    # first chunk and the lost lines of all the others.
    header, lines = split_view(view)
    chunks = chunk_lines(
        lines,
        settings.evidence_chunk_tokens,
        max_items=settings.evidence_max_items_per_chunk,
    )
    if not chunks:
        return EvidencePass()

    llm = llm or EvidenceLLM(settings)
    semaphore = asyncio.Semaphore(max(1, settings.evidence_concurrency))

    async def _one(index: int, chunk: list[str]) -> EvidencePass | None:
        async with semaphore:
            try:
                return await llm.extract(
                    chunk, window=index, total=len(chunks), header=header
                )
            except Exception:
                logger.exception("evidence pass failed for chunk %d/%d", index + 1, len(chunks))
                return None

    results = await asyncio.gather(*(_one(i, chunk) for i, chunk in enumerate(chunks)))
    kept = [result for result in results if result is not None]
    if not kept:
        logger.warning("the identity-evidence pass produced nothing; continuing text+voice only")
    merged = merge_passes(kept)
    _report_coverage(merged, utterances)
    return merged


async def run_scene_pass(
    *,
    utterances: list[Any],
    roster: list[str],
    roster_names: dict[str, str],
    settings: ServiceSettings,
    llm: EvidenceLLM | None = None,
) -> SceneMap:
    """Read the session's stretches: where, and who is there; never raises.

    One call over the WHOLE session's text rather than one per chunk: a stretch is
    a property of the session, and the per-utterance pass has no way to know where
    the previous chunk ended (see app.scenes).

    A failed reading is not a failed session: with no stretches the engine simply
    has no presence evidence, exactly as it did before this existed.
    """
    if not utterances:
        return SceneMap()
    ordinals = {
        str(getattr(utterance, "ref", "")): int(getattr(utterance, "ordinal", 0))
        for utterance in utterances
    }
    lines = render_moment_view(utterances)
    if not lines:
        return SceneMap()
    header = "\n".join(roster) if roster else None
    chunks = chunk_lines(lines, settings.scene_chunk_tokens)
    if not chunks:
        return SceneMap()

    llm = llm or EvidenceLLM(settings)
    semaphore = asyncio.Semaphore(max(1, settings.evidence_concurrency))

    async def _one(index: int, chunk: list[str]) -> tuple[Any, ...]:
        async with semaphore:
            try:
                raw = await llm.scene_response(
                    chunk, window=index, total=len(chunks), header=header
                )
            except Exception:
                logger.exception(
                    "scene reading failed for part %d/%d", index + 1, len(chunks)
                )
                return ()
            return parse_scenes(raw, ordinals=ordinals, roster_names=roster_names)

    parts = await asyncio.gather(*(_one(i, chunk) for i, chunk in enumerate(chunks)))
    scenes = merge_scene_parts(parts)
    scene_map = SceneMap.build(scenes)
    if not scene_map:
        logger.warning(
            "the scene reading produced nothing; the session runs without presence "
            "evidence and without presence questions"
        )
        return scene_map
    logger.info(
        "scene reading: %d scene(s) over %d moment(s); %d with a cast, %d naming "
        "somebody absent, %d with NPCs in play",
        len(scene_map),
        sum(scene.moment_count for scene in scene_map.scenes),
        sum(1 for scene in scene_map.scenes if scene.present),
        sum(1 for scene in scene_map.scenes if scene.absent),
        sum(1 for scene in scene_map.scenes if scene.npcs),
    )
    return scene_map


def _report_coverage(merged: EvidencePass, utterances: list[Any]) -> None:
    """Say out loud how much of the session the pass actually described.

    An utterance with no evidence item is not a small loss: it has no kind and no
    gist, so no who_did/who_said question can be built from it and the review can
    never ask about it. That is exactly what happened silently - 272 of 419
    utterances, because the model was told to return at most 40 rows per chunk -
    and a pass that is 35% covered must not look like a pass that is healthy.
    """
    expected = {getattr(utterance, "ref", "") for utterance in utterances}
    missing = expected - set(merged.items)
    if not expected or not missing:
        logger.info(
            "the identity-evidence pass described all %d utterances", len(expected)
        )
        return
    logger.warning(
        "the identity-evidence pass returned %d of %d utterances (%d missing): "
        "an utterance without an item has no kind, no gist and no stakes, so no "
        "question about it can ever be asked. A truncated response is the usual "
        "cause; lower EVIDENCE_MAX_ITEMS_PER_CHUNK or EVIDENCE_CHUNK_TOKENS.",
        len(expected) - len(missing),
        len(expected),
        len(missing),
    )
