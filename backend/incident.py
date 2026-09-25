"""Create deterministic, explicitly synthetic incident records."""

from hashlib import sha256
from typing import Dict

from backend.extraction import extract_incident_details


VALID_SOURCES = {"mock", "assemblyai_live"}

# How the incident reached the app, independent of which transcriber produced the text.
VALID_INPUT_CHANNELS = {"upload", "microphone", "sample"}
UNSPECIFIED_INPUT_CHANNEL = "unspecified"


def build_synthetic_incident_record(
    transcript: str, source: str, input_channel: str = UNSPECIFIED_INPUT_CHANNEL
) -> Dict[str, object]:
    """Build a deterministic record from transcript text without inferring authority."""
    if source not in VALID_SOURCES:
        raise ValueError(f"Unsupported incident source: {source}")
    if input_channel != UNSPECIFIED_INPUT_CHANNEL and input_channel not in VALID_INPUT_CHANNELS:
        raise ValueError(f"Unsupported input channel: {input_channel}")

    # The ID intentionally covers source + transcript only, so it stays stable across channels.
    digest = sha256(f"{source}:{transcript}".encode("utf-8")).hexdigest()[:12].upper()
    return {
        "record_type": "synthetic_incident_record",
        "incident_id": f"SYN-{digest}",
        "source": source,
        "input_channel": input_channel,
        "raw_transcript": transcript,
        "incident_details": extract_incident_details(transcript),
        "caller_identity": "Unconfirmed",
        "authorization_status": "Unconfirmed",
        "approval_authority": "Unconfirmed",
        "safety_note": (
            "Synthetic decision-support record only. Caller identity, authorization, "
            "and approval authority are not inferred from the transcript."
        ),
    }
