"""Local audio decode + chunking for the AssemblyAI transcription worker.

AssemblyAI accepts files up to 5 GB / 10 hours, but this worker mirrors the
on-prem WhisperX worker and the other cloud variants: the recording is decoded
locally and sliced into fixed-length chunks, each chunk WAV is uploaded and
transcribed separately, and the responses are spliced back into the session
timeline by the worker. Bounded per-job payloads keep latency low, respect the
account's parallel-job rate limits and produce `transcription.progress`
events after every chunk.

Only ffmpeg is involved (no torch, no ML): decoding is a container/codec
concern, and the resulting representation is the same 16 kHz mono PCM the
WhisperX pipeline uses.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

from app.clients.assemblyai_transcriber import TranscriptionError

logger = logging.getLogger(__name__)

# Same representation whisperx.load_audio produces: 16 kHz mono.
SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2  # pcm_s16le
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH


@dataclass
class AudioChunk:
    """One fixed-length slice of the recording (timestamps are absolute)."""

    start_sec: float
    end_sec: float
    path: str


def _ffmpeg_available() -> bool:
    """Whether the ffmpeg binary is on PATH (decode is required at runtime)."""
    return shutil.which("ffmpeg") is not None


def decode_to_wav(src: str, dst: str) -> None:
    """Decode any container to a 16 kHz mono PCM WAV.

    Lossy source codecs stay lossy; the PCM stage is what makes exact
    byte-level chunk slicing possible (WAV frames map 1:1 to samples).
    """
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", src,
        "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE),
        "-c:a", "pcm_s16le", "-f", "wav", dst,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise TranscriptionError(
            f"could not decode audio (ffmpeg exit {proc.returncode}): {proc.stderr[-500:]}"
        )


def split_wav(
    wav_path: str,
    chunk_seconds: float,
    max_bytes: int,
    out_dir: str,
) -> list[AudioChunk]:
    """Slice a PCM WAV into fixed-length chunk WAVs with absolute timestamps.

    ``chunk_seconds`` is validated against the WAV byte rate: if a chunk would
    exceed ``max_bytes`` (the per-upload cap), a clear config error is raised
    instead of uploading an oversized file.
    """
    frames_per_chunk = max(1, int(chunk_seconds * SAMPLE_RATE))
    bytes_per_chunk = frames_per_chunk * CHANNELS * SAMPLE_WIDTH
    if bytes_per_chunk > max_bytes:
        raise TranscriptionError(
            f"ASSEMBLYAI_CHUNK_SECONDS={chunk_seconds:g} produces ~"
            f"{bytes_per_chunk / (1024 * 1024):.1f} MiB WAV chunks, above "
            f"ASSEMBLYAI_MAX_UPLOAD_MB={max_bytes / (1024 * 1024):.0f}. "
            f"Lower ASSEMBLYAI_CHUNK_SECONDS."
        )

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    chunks: list[AudioChunk] = []
    with wave.open(wav_path, "rb") as reader:
        params = reader.getparams()
        if (
            params.nchannels != CHANNELS
            or params.sampwidth != SAMPLE_WIDTH
            or params.framerate != SAMPLE_RATE
        ):
            raise TranscriptionError(
                f"decoded WAV is not {SAMPLE_RATE} Hz mono PCM: "
                f"{params.nchannels}ch/{params.sampwidth}B/{params.framerate}Hz"
            )
        total_frames = reader.getnframes()
        if total_frames <= 0:
            raise TranscriptionError("audio decoded to 0 samples")

        start_frame = 0
        index = 0
        while start_frame < total_frames:
            end_frame = min(start_frame + frames_per_chunk, total_frames)
            reader.setpos(start_frame)
            data = reader.readframes(end_frame - start_frame)

            chunk_path = out / f"chunk_{index:04d}.wav"
            with wave.open(str(chunk_path), "wb") as writer:
                writer.setnchannels(params.nchannels)
                writer.setsampwidth(params.sampwidth)
                writer.setframerate(params.framerate)
                writer.writeframes(data)

            chunks.append(
                AudioChunk(
                    start_sec=round(start_frame / SAMPLE_RATE, 3),
                    end_sec=round(end_frame / SAMPLE_RATE, 3),
                    path=str(chunk_path),
                )
            )
            start_frame = end_frame
            index += 1
    return chunks
