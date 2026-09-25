import os
import assemblyai as aai
import httpx


NETWORK_BLOCKED_PREFIX = "NETWORK_BLOCKED:"


def _network_blocked_message() -> str:
    return (
        f"{NETWORK_BLOCKED_PREFIX} Unable to establish the HTTPS connection to AssemblyAI "
        "before authentication. The API key was not rejected. Check that local Windows "
        "firewall or endpoint security allows outbound TCP 443 to api.assemblyai.com, "
        "and that any required VPN or HTTPS proxy is configured."
    )

def get_api_key():
    return os.environ.get("ASSEMBLYAI_API_KEY")

def is_mock_mode():
    return not bool(get_api_key())

def transcribe_audio(file_path: str) -> str:
    api_key = get_api_key()
    if not api_key:
        return mock_transcribe_audio()
    
    aai.settings.api_key = api_key
    try:
        transcriber = aai.Transcriber()
        transcript = transcriber.transcribe(file_path)
    except (httpx.ConnectError, httpx.ProxyError):
        return _network_blocked_message()
    
    if transcript.error:
        return f"Error: {transcript.error}"
    
    return transcript.text

def mock_transcribe_audio() -> str:
    return "This is a mock transcription of a critical grid incident. The Substation Alpha is experiencing a severe thermal overload and we need to deploy emergency crews immediately to prevent cascading failures."
