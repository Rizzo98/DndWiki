"""AssemblyAI Speech-to-Text client (Universal-3.5 Pro, diarized, async jobs).

AssemblyAI's pre-recorded API is asynchronous: audio must be hosted at a URL
AssemblyAI can reach, so the worker first uploads each chunk WAV to
POST /v2/upload (raw bytes), then submits a transcript job with that
upload URL and polls GET /v2/transcript/{id} until it completes:

    POST /v2/upload        (raw WAV bytes)   -> {"upload_url": ...}
    POST /v2/transcript    (JSON)            -> {"id": ...}
    GET  /v2/transcript/{id}                 -> {status, utterances, ...}

Per-chunk request parameters (built from the campaign config by the worker):
- speech_models      ["universal-3-5-pro"] (ASR + speaker diarization in one
                     job; the setting may list several models, comma-separated,
                     for an automatic fallback chain)
- language_code      the campaign language (e.g. "it"); auto-detected when
                     unknown (language_detection=true)
- speakers_expected  the campaign member count (the people at the table)
- prompt             contextual prompt: plain-language description of what the
                     audio is (domain/scenario/details) — steers the model
                     toward the D&D-session domain (see app.prompts)
- keyterms_prompt    roster names (full + single words of multi-word names)
                     the model should recognize exactly

Chunk timestamps are in milliseconds and relative to the chunk; the worker
offsets them back into the session timeline (app.artifacts.offset_segments)
and merges the diarized segments in sequence. Speaker identification then
happens locally (speaker-service), unchanged.

Failure policy (per HTTP step, mirroring the other cloud variants):
- transient failures (429 honoring Retry-After, 5xx, network errors) retry
  client-side with exponential backoff up to ASSEMBLYAI_MAX_RETRIES;
- permanent 4xx (bad key, ...) fail fast: the worker then marks the session
  failed and the broker redelivery is a no-op (ConflictTransition), so a
  failed job is never re-run;
- a transcript job that reaches status "error" (or never completes within
  ASSEMBLYAI_POLL_TIMEOUT_SEC) raises TranscriptionError.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.artifacts import normalize_segments
from app.core.config import ServiceSettings

logger = logging.getLogger(__name__)


class AssemblyAIAPIError(RuntimeError):
    """Permanent API error (4xx other than 429) — fail the job, do not retry."""


class TranscriptionError(RuntimeError):
    """Any failure transcribing one recording (network, API, parsing, ...)."""


@dataclass
class DiarizedTranscription:
    """Parsed diarized transcription of one chunk (app segment shape)."""

    segments: list[dict[str, Any]] = field(default_factory=list)
    language: str | None = None


class AssemblyAITranscriber:
    """Async client for AssemblyAI diarized transcription (one WAV chunk per job)."""

    def __init__(self, settings: ServiceSettings) -> None:
        self._settings = settings

    @property
    def _base_url(self) -> str:
        return self._settings.assemblyai_api_base.rstrip("/")

    def _headers(self) -> dict[str, str]:
        # AssemblyAI authenticates with the raw API key in `authorization`
        # (no "Bearer" prefix).
        return {"authorization": self._settings.assemblyai_api_key}

    def _upload_endpoint(self) -> str:
        return f"{self._base_url}/v2/upload"

    def _transcript_endpoint(self, transcript_id: str | None = None) -> str:
        if transcript_id is None:
            return f"{self._base_url}/v2/transcript"
        return f"{self._base_url}/v2/transcript/{transcript_id}"

    # ------------------------------------------------------------------ retry

    async def _call_with_retries(
        self,
        operation: str,
        factory: Callable[[], Awaitable[httpx.Response]],
    ) -> httpx.Response:
        """Run one HTTP step with the shared retry policy (429/5xx/network)."""
        settings = self._settings
        attempts = max(1, settings.assemblyai_max_retries)
        for attempt in range(1, attempts + 1):
            try:
                resp = await factory()
            except httpx.HTTPError as exc:
                if attempt >= attempts:
                    raise TranscriptionError(
                        f"AssemblyAI {operation} failed after {attempts} attempt(s): {exc}"
                    ) from exc
                delay = min(2 ** (attempt - 1), 30)
                logger.warning(
                    "AssemblyAI %s request error (attempt %d/%d): %s; retrying in %.1fs",
                    operation, attempt, attempts, exc, delay,
                )
                await asyncio.sleep(delay)
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt >= attempts:
                    raise TranscriptionError(
                        f"AssemblyAI {operation} returned {resp.status_code} after "
                        f"{attempts} attempt(s): {resp.text[:500]}"
                    )
                retry_after = resp.headers.get("retry-after", "")
                delay = float(retry_after) if retry_after.isdigit() else min(2 ** (attempt - 1), 30)
                logger.warning(
                    "AssemblyAI %s %s (attempt %d/%d); retrying in %.1fs",
                    operation, resp.status_code, attempt, attempts, delay,
                )
                await asyncio.sleep(delay)
                continue

            if resp.status_code >= 400:
                raise AssemblyAIAPIError(
                    f"AssemblyAI {operation} returned {resp.status_code}: {resp.text[:500]}"
                )
            return resp

        raise TranscriptionError("unreachable")  # pragma: no cover

    @staticmethod
    def _json(resp: httpx.Response, operation: str) -> dict[str, Any]:
        try:
            payload = resp.json()
        except Exception as exc:  # non-JSON body
            raise TranscriptionError(
                f"AssemblyAI {operation} returned a non-JSON response: {resp.text[:300]}"
            ) from exc
        if not isinstance(payload, dict):
            raise TranscriptionError(
                f"AssemblyAI {operation} returned an unexpected body: {resp.text[:300]}"
            )
        return payload

    # ------------------------------------------------------------ API steps

    async def upload(self, wav_path: str) -> str:
        """Upload one WAV chunk; returns the hosted upload_url for the job."""
        with open(wav_path, "rb") as fh:  # noqa: ASYNC230
            data = fh.read()

        async def _post(client: httpx.AsyncClient) -> httpx.Response:
            return await client.post(
                self._upload_endpoint(),
                headers={**self._headers(), "Content-Type": "application/octet-stream"},
                content=data,
            )

        resp = await self._call_with_retries(
            "upload",
            lambda: self._run(_post),
        )
        payload = self._json(resp, "upload")
        upload_url = payload.get("upload_url")
        if not upload_url:
            raise TranscriptionError(
                f"AssemblyAI upload response missing upload_url: {resp.text[:300]}"
            )
        return str(upload_url)

    async def _run(
        self, fn: Callable[[httpx.AsyncClient], Awaitable[httpx.Response]]
    ) -> httpx.Response:
        """Run one request against a fresh client with the configured timeout."""
        async with httpx.AsyncClient(timeout=self._settings.assemblyai_request_timeout_sec) as client:
            return await fn(client)

    def _submit_body(
        self,
        audio_url: str,
        language: str | None,
        speakers_expected: int | None,
        prompt: str | None,
        keyterms: list[str] | None,
    ) -> dict[str, Any]:
        settings = self._settings
        body: dict[str, Any] = {
            "audio_url": audio_url,
            # Model selection is via the speech_models ARRAY (there is no
            # 'model' field in the /v2/transcript schema). The setting may list
            # several models comma-separated (e.g.
            # "universal-3-5-pro,universal-2") so AssemblyAI can fall back when
            # a language is not supported by the first model.
            "speech_models": [
                m.strip()
                for m in settings.assemblyai_transcription_model.split(",")
                if m.strip()
            ]
            or ["universal-3-5-pro"],
            # speaker_labels=true returns per-speaker utterances (diarization)
            "speaker_labels": True,
        }
        if language:
            body["language_code"] = language
        else:
            # Explicit auto-detection (the model detects the dominant language)
            body["language_detection"] = True
        if speakers_expected is not None:
            # Hard boundary on the number of speaker labels; the campaign's
            # member count is the number of people at the table.
            body["speakers_expected"] = speakers_expected
        if prompt:
            body["prompt"] = prompt
        if keyterms:
            body["keyterms_prompt"] = list(keyterms)
        return body

    async def submit(
        self,
        audio_url: str,
        *,
        language: str | None = None,
        speakers_expected: int | None = None,
        prompt: str | None = None,
        keyterms: list[str] | None = None,
    ) -> str:
        """Submit a transcript job for a hosted audio URL; returns its id."""
        body = self._submit_body(audio_url, language, speakers_expected, prompt, keyterms)

        async def _post(client: httpx.AsyncClient) -> httpx.Response:
            return await client.post(
                self._transcript_endpoint(),
                headers={**self._headers(), "Content-Type": "application/json"},
                json=body,
            )

        resp = await self._call_with_retries("submit", lambda: self._run(_post))
        payload = self._json(resp, "submit")
        transcript_id = payload.get("id")
        if not transcript_id:
            raise TranscriptionError(
                f"AssemblyAI submit response missing id: {resp.text[:300]}"
            )
        return str(transcript_id)

    async def get(self, transcript_id: str) -> dict[str, Any]:
        """One poll of a transcript job (raises on HTTP-level failures)."""

        async def _get(client: httpx.AsyncClient) -> httpx.Response:
            return await client.get(
                self._transcript_endpoint(transcript_id),
                headers=self._headers(),
            )

        resp = await self._call_with_retries("get", lambda: self._run(_get))
        return self._json(resp, "get")

    async def wait_for_completion(self, transcript_id: str) -> dict[str, Any]:
        """Poll a transcript job until completed/error (or the timeout expires)."""
        settings = self._settings
        deadline = time.monotonic() + settings.assemblyai_poll_timeout_sec
        while True:
            payload = await self.get(transcript_id)
            status = payload.get("status")
            if status == "completed":
                return payload
            if status == "error":
                raise TranscriptionError(
                    f"AssemblyAI transcription {transcript_id} failed: "
                    f"{payload.get('error') or 'unknown error'}"
                )
            if time.monotonic() >= deadline:
                raise TranscriptionError(
                    f"AssemblyAI transcription {transcript_id} did not complete within "
                    f"{settings.assemblyai_poll_timeout_sec:g}s (status {status!r})"
                )
            await asyncio.sleep(settings.assemblyai_poll_interval_sec)

    # ----------------------------------------------------------- orchestrate

    async def transcribe(
        self,
        wav_path: str,
        *,
        language: str | None = None,
        speakers_expected: int | None = None,
        prompt: str | None = None,
        keyterms: list[str] | None = None,
    ) -> DiarizedTranscription:
        """Transcribe one WAV chunk end-to-end: upload -> submit -> poll -> parse.

        language is the campaign's BCP-47-ish code (or None for auto-detect);
        speakers_expected is the campaign's member count (or None for auto
        diarization); timestamps in the result are relative to the chunk
        (milliseconds converted to seconds).
        """
        settings = self._settings
        if not settings.assemblyai_api_key:
            raise TranscriptionError(
                "ASSEMBLYAI_API_KEY is not configured — set ASSEMBLYAI_API_KEY in .env and "
                "start the stack with the AssemblyAI transcription variant: "
                "TRANSCRIPTION_PROVIDER=assemblyai docker compose -f docker-compose.yml "
                "-f docker-compose.transcription.yml up -d --build "
                "(a container started without that override never receives the key, "
                "even if .env has it)"
            )

        upload_url = await self.upload(wav_path)
        transcript_id = await self.submit(
            upload_url,
            language=language,
            speakers_expected=speakers_expected,
            prompt=prompt,
            keyterms=keyterms,
        )
        logger.info("AssemblyAI job %s submitted for %s", transcript_id, wav_path)
        payload = await self.wait_for_completion(transcript_id)
        return self._parse(payload)

    def _parse(self, payload: dict[str, Any]) -> DiarizedTranscription:
        segments = normalize_segments(payload.get("utterances") or [])
        language = payload.get("language_code") or None
        return DiarizedTranscription(segments=segments, language=language)
