"""OpenAI Speech-to-Text client (diarized transcription, one chunk per call).

The worker decodes the recording to 16 kHz mono WAV and slices it into
fixed-length chunks (app.audio.split_wav), then calls this client once per
chunk with:

    model             gpt-4o-transcribe-diarize
    response_format   diarized_json        (required for speaker annotations)
    chunking_strategy auto                 (server VAD-chunks long inputs)
    language          optional ISO-639-1 hint

Chunk timestamps are relative to the chunk; the worker offsets them back
into the session timeline (app.artifacts.offset_segments) and merges the
diarized segments in sequence. Speaker identification then happens locally
(speaker-service), unchanged.

Failure policy (per chunk):
- transient failures (429 honoring Retry-After, 5xx, network errors) retry
  client-side with exponential backoff up to OPENAI_MAX_RETRIES;
- permanent 4xx (bad key, oversized file, ...) fail fast: the worker then
  marks the session failed and the broker redelivery is a no-op
  (ConflictTransition), so a failed job is never re-run.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from app.artifacts import normalize_segments
from app.core.config import ServiceSettings

logger = logging.getLogger(__name__)


class OpenAIAPIError(RuntimeError):
    """Permanent API error (4xx other than 429) — fail the job, do not retry."""


class TranscriptionError(RuntimeError):
    """Any failure transcribing one recording (network, API, parsing, ...)."""


@dataclass
class DiarizedTranscription:
    """Parsed diarized transcription of one chunk (app segment shape)."""

    segments: list[dict[str, Any]] = field(default_factory=list)
    language: str | None = None


class OpenAITranscriber:
    """Async client for OpenAI diarized transcription (one WAV chunk per call)."""

    def __init__(self, settings: ServiceSettings) -> None:
        self._settings = settings

    def _endpoint(self) -> str:
        return f"{self._settings.openai_api_base.rstrip('/')}/audio/transcriptions"

    async def _post_once(
        self,
        client: httpx.AsyncClient,
        wav_path: str,
        filename: str,
        language: str | None,
    ) -> httpx.Response:
        data: dict[str, str] = {
            "model": self._settings.openai_transcription_model,
            "response_format": self._settings.openai_response_format,
            "chunking_strategy": self._settings.openai_chunking_strategy,
        }
        if language:
            data["language"] = language
        with open(wav_path, "rb") as fh:  # noqa: ASYNC230
            return await client.post(
                self._endpoint(),
                headers={"Authorization": f"Bearer {self._settings.openai_api_key}"},
                data=data,
                files={"file": (filename, fh, "audio/wav")},
            )

    async def transcribe(
        self,
        wav_path: str,
        language: str | None = None,
    ) -> DiarizedTranscription:
        """Transcribe one WAV chunk; returns normalized segments.

        ``language`` is an optional ISO-639-1 hint (config or detected from an
        earlier chunk); timestamps in the result are relative to the chunk.
        """
        settings = self._settings
        if not settings.openai_api_key:
            raise TranscriptionError(
                "OPENAI_API_KEY is not configured — set OPENAI_API_KEY in .env and "
                "start the stack with the OpenAI transcription variant: "
                "TRANSCRIPTION_PROVIDER=openai docker compose -f docker-compose.yml "
                "-f docker-compose.transcription.yml up -d --build "
                "(or the OpenAI-only docker-compose.api.yml; a container started "
                "without either override never receives the key, even if .env has it)"
            )

        filename = Path(wav_path).name
        timeout = settings.openai_request_timeout_sec
        attempts = max(1, settings.openai_max_retries)
        async with httpx.AsyncClient(timeout=timeout) as client:
            for attempt in range(1, attempts + 1):
                try:
                    resp = await self._post_once(client, wav_path, filename, language)
                except httpx.HTTPError as exc:
                    if attempt >= attempts:
                        raise TranscriptionError(
                            f"OpenAI request failed after {attempts} attempt(s): {exc}"
                        ) from exc
                    delay = min(2 ** (attempt - 1), 30)
                    logger.warning(
                        "OpenAI request error (attempt %d/%d): %s; retrying in %.1fs",
                        attempt, attempts, exc, delay,
                    )
                    await asyncio.sleep(delay)
                    continue

                if resp.status_code == 429 or resp.status_code >= 500:
                    if attempt >= attempts:
                        raise TranscriptionError(
                            f"OpenAI returned {resp.status_code} after {attempts} "
                            f"attempt(s): {resp.text[:500]}"
                        )
                    retry_after = resp.headers.get("retry-after", "")
                    delay = float(retry_after) if retry_after.isdigit() else min(2 ** (attempt - 1), 30)
                    logger.warning(
                        "OpenAI %s (attempt %d/%d); retrying in %.1fs",
                        resp.status_code, attempt, attempts, delay,
                    )
                    await asyncio.sleep(delay)
                    continue

                if resp.status_code >= 400:
                    raise OpenAIAPIError(
                        f"OpenAI returned {resp.status_code}: {resp.text[:500]}"
                    )
                return self._parse(resp)

        raise TranscriptionError("unreachable")  # pragma: no cover

    def _parse(self, resp: httpx.Response) -> DiarizedTranscription:
        try:
            payload = resp.json()
        except Exception as exc:  # non-JSON body
            raise TranscriptionError(
                f"OpenAI returned a non-JSON response: {resp.text[:300]}"
            ) from exc
        segments = normalize_segments(payload.get("segments") or [])
        language = payload.get("language") or self._settings.openai_transcription_language.strip() or None
        return DiarizedTranscription(segments=segments, language=language)