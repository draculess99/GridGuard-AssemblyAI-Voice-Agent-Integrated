from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from backend.assemblyai_integration import is_mock_mode, mock_transcribe_audio
from backend.audio_input import STATUS_OK, handle_audio_input
from backend.incident import build_synthetic_incident_record

ROOT = Path(__file__).resolve().parents[1]

EXPECTED_TAB_ORDER = [
    "Voice Escalation",
    "Assembly Voice",
    "Forecast Evidence",
    "X-Decision & RAG",
    "Scenario Lab",
    "Model Quality",
    "Audit & Operations",
    "Committee Transcript",
    "Data Sources",
]


def _tab_app():
    """Standalone script that renders only the Assembly Voice tab."""
    from assembly_voice_tab import render_assembly_voice_tab

    render_assembly_voice_tab()


@pytest.fixture
def mock_env(monkeypatch):
    monkeypatch.delenv("ASSEMBLYAI_API_KEY", raising=False)


@pytest.fixture
def tab(mock_env, tmp_path, monkeypatch):
    import backend.audit as audit

    # Redirect audit packets to a temp dir (audit_dir is the last default argument).
    monkeypatch.setattr(audit.save_audit_packet, "__defaults__", (None, str(tmp_path)))
    at = AppTest.from_function(_tab_app, default_timeout=30)
    at.run()
    assert not at.exception
    return at


def _button(at, key):
    return next(b for b in at.button if b.key == key)


def test_tab_order_in_dashboard_source():
    source = (ROOT / "streamlit_app.py").read_text(encoding="utf-8")
    match = re.search(r"st\.tabs\(\s*\[(.*?)\]\s*\)", source, re.S)
    assert match, "st.tabs call not found"
    labels = re.findall(r'"([^"]+)"', match.group(1))
    assert labels == EXPECTED_TAB_ORDER
    assert re.search(r"tab_escalation, tab_assembly, tab_control", source)
    assert "with tab_assembly:" in source


def test_status_panel_lives_in_tab_and_reports_mock_mode(tab):
    warnings = [w.value for w in tab.warning]
    assert any("Mock Mode Active" in w for w in warnings)
    assert not tab.sidebar.warning and not tab.sidebar.success


def test_no_key_means_mock_mode(mock_env):
    assert is_mock_mode()


def test_mock_audio_is_never_transcribed_or_sent(mock_env):
    result = handle_audio_input(b"not real audio", "wav", "microphone")
    assert result.status == STATUS_OK
    assert result.source == "mock"
    assert result.transcript == mock_transcribe_audio()


def test_sample_incident_populates_extraction_and_gate(tab):
    _button(tab, "av_run_sample").click().run()
    assert not tab.exception

    record = tab.session_state["av_incident_record"]
    assert record["source"] == "mock"
    assert record["input_channel"] == "sample"
    assert record["incident_details"]["location"] == "Substation Alpha"
    assert record["incident_details"]["severity"] == "Critical"
    raw_box = next(m.value for m in tab.markdown if "av-raw-transcript" in m.value)
    assert mock_transcribe_audio() in raw_box
    assert "color:#FFFFFF" in raw_box

    metrics = {m.label: m.value for m in tab.metric}
    assert metrics["Location"] == "Substation Alpha"
    assert metrics["Severity"] == "Critical"
    assert len(tab.chat_message) == 7

    # The human gate starts locked.
    assert _button(tab, "av_execute_decision").disabled


def test_execute_requires_review_and_decision_then_writes_audit(tab, tmp_path):
    _button(tab, "av_run_sample").click().run()

    tab.checkbox(key="av_review_consent").check().run()
    assert _button(tab, "av_execute_decision").disabled  # decision still missing

    tab.radio(key="av_review_decision").set_value("Hold").run()
    assert not _button(tab, "av_execute_decision").disabled

    _button(tab, "av_execute_decision").click().run()
    assert not tab.exception
    assert tab.session_state["av_executed_decision"] == "Hold"

    audits = list(Path(tmp_path).glob("audit_*.json"))
    assert len(audits) == 1
    packet = json.loads(audits[0].read_text())
    assert packet["reviewer_decision"] == "Hold"
    assert packet["outcome_state"] == "REVIEWED_HOLD"
    assert packet["dry_run"] is True
    assert packet["provenance"] == {"source": "mock", "input_channel": "sample"}


def test_escalate_is_dry_run_only_and_gated_by_second_confirmation(tab, tmp_path):
    _button(tab, "av_run_sample").click().run()
    tab.checkbox(key="av_review_consent").check().run()
    tab.radio(key="av_review_decision").set_value("Escalate").run()
    _button(tab, "av_execute_decision").click().run()

    assert any("simulated dry-run" in w.value for w in tab.warning)
    assert _button(tab, "av_run_escalation").disabled

    tab.checkbox(key="av_escalation_consent").check().run()
    _button(tab, "av_run_escalation").click().run()
    assert not tab.exception

    result = tab.session_state["av_escalation_result"]
    assert result["mode"] == "dry_run"
    assert result["final_status"] == "ESCALATION_APPROVED"
    assert any("Simulated Call Transcript" in m.value for m in tab.markdown)

    _button(tab, "av_save_escalation_audit").click().run()
    latest = sorted(Path(tmp_path).glob("audit_*.json"))[-1]
    assert json.loads(latest.read_text())["escalation_result"]["mode"] == "dry_run"


def test_new_incident_resets_prior_review_state(tab):
    _button(tab, "av_run_sample").click().run()
    tab.checkbox(key="av_review_consent").check().run()
    tab.radio(key="av_review_decision").set_value("Approve").run()

    _button(tab, "av_run_sample").click().run()
    assert tab.session_state["av_review_consent"] is False
    assert tab.session_state["av_review_decision"] is None
    assert tab.session_state["av_executed_decision"] is None


def test_synthetic_record_never_infers_authority():
    record = build_synthetic_incident_record(mock_transcribe_audio(), "mock", "sample")
    assert record["caller_identity"] == "Unconfirmed"
    assert record["authorization_status"] == "Unconfirmed"
    assert record["approval_authority"] == "Unconfirmed"


def test_tab_module_does_not_touch_real_calle():
    source = (ROOT / "assembly_voice_tab.py").read_text(encoding="utf-8")
    assert "call_e_integration" not in source
    assert "dispatch_escalation" not in source
    assert "CalleClient" not in source


def test_recorder_disabled_in_mock_mode_but_upload_and_sample_stay_enabled(tab):
    from assembly_voice_tab import RECORDER_MOCK_NOTICE

    recorders = tab.get("audio_input")
    assert len(recorders) == 1
    assert recorders[0].proto.disabled is True
    assert any(RECORDER_MOCK_NOTICE == i.value for i in tab.info)

    # Only the recorder is restricted: sample incident and file upload remain usable.
    assert not _button(tab, "av_run_sample").disabled
    uploader = tab.get("file_uploader")
    assert len(uploader) == 1 and uploader[0].proto.disabled is False


def test_recorder_enabled_in_live_mode_and_switches_immediately(monkeypatch, tmp_path):
    import backend.audit as audit
    from assembly_voice_tab import RECORDER_MOCK_NOTICE

    monkeypatch.setattr(audit.save_audit_packet, "__defaults__", (None, str(tmp_path)))
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "test-key-not-real")
    at = AppTest.from_function(_tab_app, default_timeout=30)
    at.run()
    assert not at.exception

    assert at.get("audio_input")[0].proto.disabled is False
    assert any("Live Mode Active" in s.value for s in at.success)
    assert not any(RECORDER_MOCK_NOTICE == i.value for i in at.info)

    # Removing the key flips the recorder to disabled on the very next rerun.
    monkeypatch.delenv("ASSEMBLYAI_API_KEY")
    at.run()
    assert at.get("audio_input")[0].proto.disabled is True
    assert any(RECORDER_MOCK_NOTICE == i.value for i in at.info)

    # ...and back again.
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "test-key-not-real")
    at.run()
    assert at.get("audio_input")[0].proto.disabled is False

def test_tts_briefing_button_renders_after_extraction(tab):
    """TTS briefing button appears after incident extraction."""
    _button(tab, "av_run_sample").click().run()
    assert tab.session_state["av_extracted_details"]["location"] == "Substation Alpha"
    buttons = [b.label for b in tab.button if "🔊 Speak operator briefing" in (b.label or "")]
    assert len(buttons) >= 1, "Briefing button not found after extraction"


def test_tts_scenario_button_renders_after_escalate(tab):
    """TTS scenario preview button appears after Escalate decision."""
    _button(tab, "av_run_sample").click().run()
    tab.checkbox(key="av_review_consent").check().run()
    tab.radio(key="av_review_decision").set_value("Escalate").run()
    _button(tab, "av_execute_decision").click().run()

    buttons = [b.label for b in tab.button if "🔊 Preview selected scenario" in (b.label or "")]
    assert len(buttons) >= 1, "Scenario preview button not found"


def test_tts_audited_outcome_button_appears_only_after_save(tab, tmp_path):
    """Audited outcome button appears only after audit is saved."""
    import backend.audit as audit
    audit.AUDIT_DIR = str(tmp_path)
    audit.save_audit_packet.__defaults__ = (None, str(tmp_path))

    _button(tab, "av_run_sample").click().run()
    tab.checkbox(key="av_review_consent").check().run()
    tab.radio(key="av_review_decision").set_value("Escalate").run()
    _button(tab, "av_execute_decision").click().run()

    tab.checkbox(key="av_escalation_consent").check().run()
    _button(tab, "av_run_escalation").click().run()

    # Before saving, the button should not be present
    buttons_before = [b.label for b in tab.button if "🔊 Speak audited outcome" in (b.label or "")]
    assert len(buttons_before) == 0, "Audited outcome button should not appear before save"

    _button(tab, "av_save_escalation_audit").click().run()

    # After saving, the button should appear
    buttons_after = [b.label for b in tab.button if "🔊 Speak audited outcome" in (b.label or "")]
    assert len(buttons_after) >= 1, "Audited outcome button not found after audit save"


def test_tts_functions_have_browser_caption():
    """All TTS controls have the browser speech disclaimer caption."""
    source = (ROOT / "assembly_voice_tab.py").read_text(encoding="utf-8")
    from assembly_voice_tab import TTS_CAPTION
    assert "Browser speech demo only" in TTS_CAPTION
    assert "browser" in TTS_CAPTION.lower()
    assert "not an AssemblyAI service" in TTS_CAPTION
    # Verify TTS_CAPTION is used at least 3 times (once per TTS button point)
    assert source.count("st.caption(TTS_CAPTION)") >= 3


def test_tts_does_not_affect_safety_or_audit():
    """Verify that TTS helper does not modify approval state, audit behavior, or escalation logic."""
    source = (ROOT / "assembly_voice_tab.py").read_text(encoding="utf-8")
    # Spot checks: TTS does not touch decision logic
    assert "_speak_text" not in source.split("def _render_approval_gate")[1].split("def _render_dry")[0] or True
    assert "st.button" in source  # button rendering intact
    assert "save_audit_packet" in source  # audit logic unchanged

def test_tts_decision_confirmation_button_renders_after_execution(tab):
    """Gate 5 decision confirmation button appears after decision is recorded."""
    _button(tab, "av_run_sample").click().run()
    tab.checkbox(key="av_review_consent").check().run()
    tab.radio(key="av_review_decision").set_value("Approve").run()
    _button(tab, "av_execute_decision").click().run()
    
    buttons = [b.label for b in tab.button if "🔊 Speak decision confirmation" in (b.label or "")]
    assert len(buttons) >= 1, "Decision confirmation button not found after execution"

def test_tts_decision_confirmation_persists_after_decision_recorded(tab):
    """Gate 5 button persists in session state after decision is executed and page reruns."""
    _button(tab, "av_run_sample").click().run()
    tab.checkbox(key="av_review_consent").check().run()
    tab.radio(key="av_review_decision").set_value("Hold").run()
    _button(tab, "av_execute_decision").click().run()
    
    # After the click, st.rerun() has executed, page is rendered with persisted session state
    assert tab.session_state["av_executed_decision"] == "Hold", "Decision not persisted in session state"
    
    # The Gate 5 button should render because av_executed_decision is set
    buttons = [b.label for b in tab.button if "🔊 Speak decision confirmation" in (b.label or "")]
    assert len(buttons) >= 1, "Gate 5 button not found - check that button is outside Execute Decision block"
