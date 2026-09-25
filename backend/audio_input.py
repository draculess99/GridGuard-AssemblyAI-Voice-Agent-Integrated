"""Shared handler for audio arriving from any input channel (upload or microphone)."""

import os
import tempfile
from dataclasses import dataclass
from typing import Optional

from backend.assemblyai_integration import (
    NETWORK_BLOCKED_PREFIX,
    is_mock_mode,
    mock_transcribe_audio,
    transcribe_audio,
)

AUDIO_CHANNELS = {"upload", "microphone"}
ALLOWED_SUFFIXES = {"wav", "mp3", "m4a"}

STATUS_OK = "ok"
STATUS_NETWORK_BLOCKED = "network_blocked"
STATUS_ERROR = "error"


@dataclass(frozen=True)
class AudioInputResult:
    status: str
    input_channel: str
    source: Optional[str] = None
    transcript: Optional[str] = None
    detail: Optional[str] = None


def handle_audio_input(audio_bytes: bytes, suffix: str, input_channel: str) -> AudioInputResult:
    """Turn submitted audio into a transcript plus provenance; never records a decision.

    In Mock Mode the audio is neither written to disk nor sent anywhere: the
    deterministic sample transcript is returned. In Live Mode the audio is written
    to a temp file, sent to AssemblyAI, and the temp file is always removed.
    """
    if input_channel not in AUDIO_CHANNELS:
        raise ValueError(f"Unsupported audio input channel: {input_channel}")
    if not audio_bytes:
        return AudioInputResult(STATUS_ERROR, input_channel, detail="No audio data was received.")

    if is_mock_mode():
        return AudioInputResult(
            STATUS_OK, input_channel, source="mock", transcript=mock_transcribe_audio()
        )

    clean_suffix = suffix.lower().lstrip(".")
    if clean_suffix not in ALLOWED_SUFFIXES:
        clean_suffix = "wav"

    with tempfile.NamedTemporaryFile(delete=False, suffix=f".{clean_suffix}") as tmp_file:
        tmp_file.write(audio_bytes)
        tmp_path = tmp_file.name
    try:
        transcript = transcribe_audio(tmp_path)
    finally:
        os.remove(tmp_path)

    if transcript.startswith(NETWORK_BLOCKED_PREFIX):
        return AudioInputResult(STATUS_NETWORK_BLOCKED, input_channel, detail=transcript)
    if transcript.startswith("Error:"):
        return AudioInputResult(STATUS_ERROR, input_channel, detail=transcript)
    return AudioInputResult(
        STATUS_OK, input_channel, source="assemblyai_live", transcript=transcript
    )
