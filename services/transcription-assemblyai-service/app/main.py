"""transcription-assemblyai-service — FastAPI entrypoint (health + config info).

Runs in the same container as the queue worker; exposes the same status
endpoints as the on-prem WhisperX service (same field names) so the web
/system page keeps working. There is nothing heavy to report: the model runs
in the cloud, so the API is always "ready".
"""

from fastapi import FastAPI

from app.core.config import get_settings

settings = get_settings()
app = FastAPI(title="transcription-assemblyai-service", version="0.1.0")


@app.get("/health")
async def health():
    return {"status": "ok", "service": settings.service_name}


@app.get("/api/transcription/models")
async def model_info():
    """Configured engine (cloud API; no local models)."""
    return {
        "asr_model": f"assemblyai:{settings.assemblyai_transcription_model}",
        "diarization_model": "assemblyai (built-in diarization)",
        "compute_type": "cloud",
        "provider": "assemblyai",
    }


@app.get("/api/transcription/status")
async def pipeline_status():
    """Runtime status: cloud-backed, so nothing to preload."""
    return {
        "device": "cloud",
        "asr_loaded": True,
        "diarizer_loaded": True,
        "provider": "assemblyai",
        "api_key_configured": bool(settings.assemblyai_api_key),
    }
