"""transcription-service — FastAPI entrypoint (health + model/device info).

Runs in the same container as the queue worker; exposes only lightweight
status endpoints (the heavy WhisperX pipeline lives in app.pipeline).
"""

from fastapi import FastAPI

from app.core.config import get_settings

settings = get_settings()
app = FastAPI(title="transcription-service", version="0.1.0")


@app.get("/health")
async def health():
    return {"status": "ok", "service": settings.service_name}


@app.get("/api/transcription/models")
async def model_info():
    """Configured models (device is detected lazily; may be None pre-warm)."""
    return {
        "asr_model": settings.whisper_model,
        "diarization_model": settings.diarization_model,
        "compute_type": settings.whisper_compute_type,
    }


@app.get("/api/transcription/status")
async def pipeline_status():
    """Runtime status: detected device and whether the models are loaded."""
    from app.pipeline import _models, get_device

    device = get_device(settings)
    asr_loaded = any(kind == "asr" for kind, _, _ in _models)
    diarizer_loaded = any(kind == "diarize" for kind, _, _ in _models)
    return {
        "device": device,
        "asr_loaded": asr_loaded,
        "diarizer_loaded": diarizer_loaded,
    }