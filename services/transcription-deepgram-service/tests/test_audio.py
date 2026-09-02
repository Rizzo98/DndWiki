"""Tests for local decode + chunk splitting (ffmpeg only where noted)."""

import shutil
import wave

import pytest

from app.audio import SAMPLE_RATE, decode_to_wav, split_wav
from app.clients.deepgram_transcriber import TranscriptionError


def _make_wav(path, seconds=2.0, rate=SAMPLE_RATE, channels=1, sampwidth=2) -> str:
    """Write a silent PCM WAV (stdlib wave)."""
    frames = int(seconds * rate)
    data = b"\x00" * (frames * channels * sampwidth)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(sampwidth)
        w.setframerate(rate)
        w.writeframes(data)
    return str(path)


def _read_params(path):
    with wave.open(str(path), "rb") as w:
        return w.getparams()


def test_split_wav_two_chunks(tmp_path):
    wav = _make_wav(tmp_path / "full.wav", seconds=2.0)
    chunks = split_wav(wav, chunk_seconds=1.0, max_bytes=24 * 1024 * 1024, out_dir=str(tmp_path / "out"))

    assert [(c.start_sec, c.end_sec) for c in chunks] == [(0.0, 1.0), (1.0, 2.0)]
    for c in chunks:
        params = _read_params(c.path)
        assert params.framerate == SAMPLE_RATE
        assert params.nchannels == 1
        assert params.sampwidth == 2


def test_split_wav_chunk_longer_than_audio(tmp_path):
    wav = _make_wav(tmp_path / "full.wav", seconds=2.0)
    chunks = split_wav(wav, chunk_seconds=10.0, max_bytes=24 * 1024 * 1024, out_dir=str(tmp_path / "out"))

    assert len(chunks) == 1
    assert (chunks[0].start_sec, chunks[0].end_sec) == (0.0, 2.0)


def test_split_wav_preserves_frames(tmp_path):
    wav = _make_wav(tmp_path / "full.wav", seconds=2.0)
    chunks = split_wav(wav, chunk_seconds=0.7, max_bytes=24 * 1024 * 1024, out_dir=str(tmp_path / "out"))
    total = sum(_read_params(c.path).nframes for c in chunks)
    assert total == 2.0 * SAMPLE_RATE


def test_split_wav_oversized_chunk_raises(tmp_path):
    """chunk_seconds that would exceed the per-upload cap -> clear error."""
    wav = _make_wav(tmp_path / "full.wav", seconds=2.0)
    with pytest.raises(TranscriptionError, match="DEEPGRAM_CHUNK_SECONDS"):
        split_wav(wav, chunk_seconds=1000.0, max_bytes=1 * 1024 * 1024, out_dir=str(tmp_path / "out"))


def test_split_wav_rejects_wrong_params(tmp_path):
    """A non-16k/mono/stereo WAV is refused (decode always normalizes)."""
    wav = _make_wav(tmp_path / "stereo.wav", seconds=1.0, channels=2)
    with pytest.raises(TranscriptionError, match="not 16000 Hz mono PCM"):
        split_wav(wav, chunk_seconds=1.0, max_bytes=24 * 1024 * 1024, out_dir=str(tmp_path / "out"))


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_decode_to_wav_normalizes(tmp_path):
    """Decoding a 44.1 kHz stereo wav yields 16 kHz mono PCM."""
    src = _make_wav(tmp_path / "src.wav", seconds=1.0, rate=44100, channels=2)
    dst = str(tmp_path / "out.wav")
    decode_to_wav(src, dst)
    params = _read_params(dst)
    assert params.framerate == SAMPLE_RATE
    assert params.nchannels == 1
    assert params.sampwidth == 2
    # duration preserved (~1 s)
    assert abs(params.nframes / SAMPLE_RATE - 1.0) < 0.05
