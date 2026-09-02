"""Deepgram Speech-to-Text client (Nova 3, diarized, one chunk per call).

The worker decodes the recording to 16 kHz mono WAV and slices it into
fixed-length chunks (app.audio.split_wav), then calls this client once per
chunk against Deepgram's pre-recorded endpoint:

    POST https://api.deepgram.com/v1/listen
    ?model=nova-3        (Nova 3: ASR + diarization in one request)
    &diarize=true        (speaker labels on words/paragraphs/utterances)
    &utterances=true     (speaker-turn grouping — the cleanest segment source)
    &smart_format=true   (numbers/dates/currency normalization)
    &punctuate=true      (punctuation)
    &language=<hint>     (BCP-47, or `multi` = Nova auto-detects)

Chunk timestamps are relative to the chunk; the worker offsets them back
into the session timeline (app.artifacts.offset_segments) and merges the
diarized segments in sequence. Speaker identification then happens locally
(speaker-service), unchanged.

Failure policy (per chunk), mirroring the OpenAI variant:
- transient failures (429 honoring Retry-After, 5xx, network errors) retry
  client-side with exponential backoff up to DEEPGRAM_MAX_RETRIES;
- permanent 4xx (bad key, ...) fail fast: the worker then marks the session
  failed and the broker redelivery is a no-op (ConflictTransition), so a
  failed job is never re-run.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.artifacts import normalize_segments, normalize_speaker
from app.core.config import ServiceSettings

logger = logging.getLogger(__name__)


class DeepgramAPIError(RuntimeError):
    """Permanent API error (4xx other than 429) — fail the job, do not retry."""


class TranscriptionError(RuntimeError):
    """Any failure transcribing one recording (network, API, parsing, ...)."""


@dataclass
class DiarizedTranscription:
    """Parsed diarized transcription of one chunk (app segment shape)."""

    segments: list[dict[str, Any]] = field(default_factory=list)
    language: str | None = None


class DeepgramTranscriber:
    """Async client for Deepgram diarized transcription (one WAV chunk per call)."""

    def __init__(self, settings: ServiceSettings) -> None:
        self._settings = settings

    def _endpoint(self) -> str:
        return f"{self._settings.deepgram_api_base.rstrip('/')}/listen"

    def _params(self, language: str | None) -> dict[str, Any]:
        settings = self._settings
        params: dict[str, Any] = {
            "model": settings.deepgram_transcription_model,
            "diarize": "true",
            # utterances=true groups consecutive words of the same speaker into
            # turns (each carrying start/end/transcript/speaker) — the cleanest
            # source for app segments; the response also keeps words/paragraphs
            # so the parser can fall back if utterances are ever missing.
            "utterances": "true",
            "smart_format": "true" if settings.deepgram_smart_format else "false",
            "punctuate": "true" if settings.deepgram_punctuate else "false",
        }
        # Empty hint = auto-detect. Deepgram's default when `language` is
        # omitted is ENGLISH, so auto-detection must be requested explicitly.
        params["language"] = language or "multi"
        # Echo the detected language on every utterance so the worker can
        # record the session language (Deepgram never returns it in metadata).
        params["detect_language"] = "true"
        return params

    async def _post_once(
        self,
        client: httpx.AsyncClient,
        wav_path: str,
        language: str | None,
    ) -> httpx.Response:
        with open(wav_path, "rb") as fh:  # noqa: ASYNC230
            return await client.post(
                self._endpoint(),
                params=self._params(language),
                headers={
                    "Authorization": f"Token {self._settings.deepgram_api_key}",
                    "Content-Type": "audio/wav",
                },
                content=fh.read(),
            )

    async def transcribe(
        self,
        wav_path: str,
        language: str | None = None,
    ) -> DiarizedTranscription:
        """Transcribe one WAV chunk; returns normalized segments.

        ``language`` is an optional BCP-47 hint (config or detected from an
        earlier chunk); timestamps in the result are relative to the chunk.
        """
        settings = self._settings
        if not settings.deepgram_api_key:
            raise TranscriptionError(
                "DEEPGRAM_API_KEY is not configured — set DEEPGRAM_API_KEY in .env and "
                "start the stack with the Deepgram transcription variant: "
                "docker compose -f docker-compose.yml -f docker-compose.transcription.yml "
                "up -d --build "
                "(a container started without that override never receives the key, "
                "even if .env has it)"
            )

        timeout = settings.deepgram_request_timeout_sec
        attempts = max(1, settings.deepgram_max_retries)
        async with httpx.AsyncClient(timeout=timeout) as client:
            for attempt in range(1, attempts + 1):
                try:
                    resp = await self._post_once(client, wav_path, language)
                except httpx.HTTPError as exc:
                    if attempt >= attempts:
                        raise TranscriptionError(
                            f"Deepgram request failed after {attempts} attempt(s): {exc}"
                        ) from exc
                    delay = min(2 ** (attempt - 1), 30)
                    logger.warning(
                        "Deepgram request error (attempt %d/%d): %s; retrying in %.1fs",
                        attempt, attempts, exc, delay,
                    )
                    await asyncio.sleep(delay)
                    continue

                if resp.status_code == 429 or resp.status_code >= 500:
                    if attempt >= attempts:
                        raise TranscriptionError(
                            f"Deepgram returned {resp.status_code} after {attempts} "
                            f"attempt(s): {resp.text[:500]}"
                        )
                    retry_after = resp.headers.get("retry-after", "")
                    delay = float(retry_after) if retry_after.isdigit() else min(2 ** (attempt - 1), 30)
                    logger.warning(
                        "Deepgram %s (attempt %d/%d); retrying in %.1fs",
                        resp.status_code, attempt, attempts, delay,
                    )
                    await asyncio.sleep(delay)
                    continue

                if resp.status_code >= 400:
                    raise DeepgramAPIError(
                        f"Deepgram returned {resp.status_code}: {resp.text[:500]}"
                    )
                return self._parse(resp)

        raise TranscriptionError("unreachable")  # pragma: no cover

    def _parse(self, resp: httpx.Response) -> DiarizedTranscription:
        try:
            payload = resp.json()
        except Exception as exc:  # non-JSON body
            raise TranscriptionError(
                f"Deepgram returned a non-JSON response: {resp.text[:300]}"
            ) from exc
        segments = normalize_segments(_extract_segments(payload))
        language = _detected_language(payload) or self._settings.deepgram_language.strip() or None
        return DiarizedTranscription(segments=segments, language=language)


def _extract_segments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull speaker-turn segments out of a Deepgram listen response.

    Preference order:
    1. results.utterances — speaker turns (start/end/transcript/words/speaker)
       when `utterances=true` (what we request);
    2. paragraphs.paragraphs — speaker-grouped paragraphs
       (sentences -> text, speaker, start/end) when utterances are absent;
    3. alternatives.words — last resort: group consecutive same-speaker words
       into runs (works whenever diarize=true returned word-level speakers).
    """
    results = payload.get("results") or {}
    utterances = results.get("utterances") or []
    if utterances:
        return [dict(u) for u in utterances]

    channels = results.get("channels") or []
    if channels:
        alternatives = channels[0].get("alternatives") or []
        if alternatives:
            alt = alternatives[0]
            paragraphs = (alt.get("paragraphs") or {}).get("paragraphs") or []
            if paragraphs:
                return [_paragraph_segment(p) for p in paragraphs]
            words = alt.get("words") or []
            if words:
                return _group_words_by_speaker(words)
    return []


def _paragraph_segment(paragraph: dict[str, Any]) -> dict[str, Any]:
    """Paragraph -> {start, end, text, speaker, words: []} (no word timings)."""
    sentences = paragraph.get("sentences") or []
    text = " ".join(s.get("text", "") for s in sentences).strip()
    return {
        "start": paragraph.get("start"),
        "end": paragraph.get("end"),
        "text": text,
        "speaker": normalize_speaker(paragraph.get("speaker")),
        "words": [],
    }


def _group_words_by_speaker(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Consecutive same-speaker word runs -> {start, end, text, speaker, words}.

    The run's `speaker_confidence` is the mean of its words'
    `speaker_confidence` (the fallback path has no segment-level field).
    """
    segments: list[dict[str, Any]] = []
    run_words: list[dict[str, Any]] = []
    for word in words:
        speaker = normalize_speaker(word.get("speaker"))
        if segments and segments[-1]["speaker"] == speaker:
            seg = segments[-1]
            seg["end"] = word.get("end")
            seg["text"] = f"{seg['text']} {word.get('word', '')}".strip()
            run_words.append(word)
            seg["speaker_confidence"] = _mean_confidence(run_words)
        else:
            run_words = [word]
            segments.append(
                {
                    "start": word.get("start"),
                    "end": word.get("end"),
                    "text": word.get("word", ""),
                    "speaker": speaker,
                    "speaker_confidence": _mean_confidence(run_words),
                    "words": [],
                }
            )
        segments[-1]["words"].append(_word_meta(word))
    return segments


def _mean_confidence(items: list[dict[str, Any]]) -> float | None:
    """Mean of word-level speaker_confidence values (None when absent)."""
    values = [
        v
        for v in (w.get("speaker_confidence") for w in items)
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    ]
    if not values:
        return None
    return sum(values) / len(values)


def _word_meta(word: dict[str, Any]) -> dict[str, Any]:
    """Word -> {word, start, end} (the transcript shape used by the on-prem worker)."""
    return {
        "word": word.get("word", ""),
        "start": word.get("start"),
        "end": word.get("end"),
    }


def _detected_language(payload: dict[str, Any]) -> str | None:
    """Best-effort detected language from the response (utterance-level).

    Deepgram does not echo the detected language in metadata; it only carries
    it on utterances when `detect_language` is enabled. Fall back to the
    configured hint (handled by the caller).
    """
    results = payload.get("results") or {}
    utterances = results.get("utterances") or []
    for utterance in utterances:
        lang = utterance.get("language")
        if lang:
            return str(lang)
    return None
