"""Assembly Voice tab: AssemblyAI voice intake, incident extraction and human approval.

Ported from the standalone GridGuard AssemblyAI Voice Agent. Everything here is
decision support only and dry-run: the tab never places a real Call-E call and
never executes a grid action. All session-state and widget keys use the ``av_``
prefix so they cannot collide with the rest of the GridGuard dashboard.
"""
from __future__ import annotations

import html

import streamlit as st

from backend.assembly_call_e_dry_run import DRY_RUN_SCENARIOS, build_advisory, build_dry_run_escalation
from backend.assemblyai_integration import is_mock_mode, mock_transcribe_audio
from backend.audio_input import STATUS_ERROR, STATUS_NETWORK_BLOCKED, handle_audio_input
from backend.audit import save_audit_packet
from backend.conversation import generate_conversation_timeline
from backend.incident import build_synthetic_incident_record

ROOT_KEY = "av_root"

RECORDER_MOCK_NOTICE = (
    "Recording is available only in Live Mode; Mock Mode uses deterministic sample text "
    "and never sends audio externally."
)

OUTCOME_MAPPING = {
    "Approve": "ESCALATION_APPROVED",
    "Hold": "REVIEWED_HOLD",
    "Reject": "REVIEWED_NOT_APPROVED",
    "Escalate": "ESCALATION_APPROVED",
}

TTS_CAPTION = "Browser speech demo only. Spoken playback is generated locally by the browser; it is not an AssemblyAI service."


def _speak_text(text: str) -> None:
    """Play text using browser Web Speech API (window.speechSynthesis)."""
    st.components.v1.html(
        f"""
        <script>
        (function() {{
            if ('speechSynthesis' in window) {{
                const text = {repr(text)};
                const utterance = new SpeechSynthesisUtterance(text);
                utterance.rate = 0.95;
                window.speechSynthesis.cancel();
                window.speechSynthesis.speak(utterance);
            }}
        }})();
        </script>
        """,
        height=0,
    )

# Scoped to this tab's container so the dashboard's other tabs keep their styling.
_STYLE = f"""
<style>
.st-key-{ROOT_KEY} .incident-source {{
    font-size: 1.05rem;
    line-height: 1.5;
    margin: 0.25rem 0 0.6rem;
}}
.st-key-{ROOT_KEY} .incident-source-badge {{
    background: #164e63;
    border: 1px solid #38bdf8;
    border-radius: 0.35rem;
    color: #F1F5F9;
    display: inline-block;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.95rem;
    font-weight: 700;
    padding: 0.1rem 0.4rem;
}}
</style>
"""


def _init_state() -> None:
    for key, default in {
        "av_transcript": None,
        "av_extracted_details": None,
        "av_incident_record": None,
        "av_executed_decision": None,
        "av_audit_filepath": None,
        "av_escalation_result": None,
        "av_escalation_scenario_executed": None,
        "av_escalation_audit_saved": False,
    }.items():
        st.session_state.setdefault(key, default)


def store_incident(transcript: str, source: str, input_channel: str) -> None:
    record = build_synthetic_incident_record(transcript, source, input_channel)
    st.session_state.av_transcript = transcript
    st.session_state.av_incident_record = record
    st.session_state.av_extracted_details = record["incident_details"]
    st.session_state.av_executed_decision = None
    st.session_state.av_audit_filepath = None
    # A new incident must be reviewed afresh: clear the previous acknowledgement and choice.
    st.session_state.av_review_consent = False
    st.session_state.av_review_decision = None
    # Clear any previous dry-run escalation state.
    st.session_state.av_escalation_result = None
    st.session_state.av_escalation_scenario_executed = None
    st.session_state.av_escalation_consent = False
    st.session_state.av_escalation_audit_saved = False


def _ingest_audio(audio_bytes: bytes, suffix: str, input_channel: str, mock_mode: bool) -> None:
    """Shared path for uploaded and microphone audio; ends at the same approval gate."""
    with st.spinner("Preparing mock transcript..." if mock_mode else "Transcribing..."):
        result = handle_audio_input(audio_bytes, suffix, input_channel)

    if result.status == STATUS_NETWORK_BLOCKED:
        st.error(
            "AssemblyAI could not be reached over HTTPS. "
            "The API key was not rejected; the outbound connection was blocked "
            "before AssemblyAI authentication."
        )
        st.info(
            "Required network action: allow outbound TCP 443 to "
            "`api.assemblyai.com` in Windows Firewall or endpoint security, "
            "or configure the approved HTTPS proxy/VPN for this process."
        )
    elif result.status == STATUS_ERROR:
        st.error(f"AssemblyAI transcription failed: {result.detail}")
    else:
        store_incident(result.transcript, result.source, result.input_channel)


def _render_status_panel(mock_mode: bool) -> None:
    with st.container(border=True):
        st.markdown("#### AssemblyAI Configuration")
        if mock_mode:
            st.warning(
                "⚠️ **Mock Mode Active** — no `ASSEMBLYAI_API_KEY` detected. "
                "The tab uses a deterministic mock transcript for safe offline testing."
            )
        else:
            st.success(
                "✅ **Live Mode Active** — `ASSEMBLYAI_API_KEY` configured. "
                "Audio will be transcribed using AssemblyAI."
            )
        st.markdown(
            """
- **Live Mode**: upload or record real audio for AssemblyAI transcription.
- **Mock Mode**: upload, record, or run a sample incident. Audio is never transcribed or sent externally; a deterministic sample transcript is used.
- **Dry-Run Default**: this tab never automatically executes grid actions or places real calls.
"""
        )


def _render_audio_ingestion(mock_mode: bool) -> None:
    st.subheader("1. Audio Ingestion")
    col_upload, col_sample = st.columns([2, 1])

    with col_upload:
        uploaded_file = st.file_uploader(
            "Upload incident report audio", type=["wav", "mp3", "m4a"], key="av_upload"
        )
        if uploaded_file is not None and st.button("Transcribe Audio", key="av_transcribe_upload"):
            _ingest_audio(uploaded_file.getvalue(), uploaded_file.name.split(".")[-1], "upload", mock_mode)

        st.markdown("**Or record from your microphone:**")
        if mock_mode:
            st.info(RECORDER_MOCK_NOTICE)
        else:
            st.caption(
                "Live Mode: the recording is sent to AssemblyAI for transcription only after you "
                "click **Transcribe Recording**."
            )
        # Recording is Live Mode only. Uploads and the sample incident stay available in Mock Mode.
        recorded_audio = st.audio_input("Record incident report", key="av_record", disabled=mock_mode)
        if (
            not mock_mode
            and recorded_audio is not None
            and st.button("Transcribe Recording", key="av_transcribe_record")
        ):
            _ingest_audio(recorded_audio.getvalue(), "wav", "microphone", mock_mode)

    with col_sample:
        st.write("Or run a test scenario:")
        if st.button("Run Sample Incident", key="av_run_sample"):
            with st.spinner("Generating mock transcript..."):
                store_incident(mock_transcribe_audio(), "mock", "sample")


def _render_extraction(record: dict) -> dict:
    st.markdown("---")
    st.subheader("2. Transcription & Extraction")
    st.markdown(
        f'<div class="incident-source">Synthetic incident source: '
        f'<span class="incident-source-badge">{record["source"]}</span>'
        f' &nbsp;Input channel: '
        f'<span class="incident-source-badge">{record["input_channel"]}</span></div>',
        unsafe_allow_html=True,
    )
    if record["source"] == "mock" and record["input_channel"] in ("upload", "microphone"):
        st.warning(
            f"Mock Mode: the submitted {record['input_channel']} audio was not transcribed and was "
            "not sent to any external service. The transcript below is the deterministic sample."
        )
    st.markdown("Exact raw transcript")
    # Inline styles (not a CSS selector) so the text is always white on the dark box.
    st.markdown(
        '<div data-testid="av-raw-transcript" style="background:#0f172a;border:1px solid #334155;'
        "border-radius:0.45rem;color:#FFFFFF;font-size:1.1rem;line-height:1.6;min-height:6rem;"
        'padding:0.8rem 0.9rem;white-space:pre-wrap;">'
        f'{html.escape(record["raw_transcript"])}</div>',
        unsafe_allow_html=True,
    )
    st.caption(record["safety_note"])

    st.markdown("### Extracted Grid Parameters")
    details = st.session_state.av_extracted_details
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Location", details["location"])
    col2.metric("Severity", details["severity"])
    col3.metric("Affected Asset", details["affected_asset"])
    col4.metric("Requested Action", details["requested_action"])

    st.markdown("---")
    col_speak, col_spacer = st.columns([1, 4])
    with col_speak:
        if st.button("🔊 Speak operator briefing", key="av_speak_briefing"):
            briefing = (
                f"Incident location: {details['location']}. "
                f"Severity: {details['severity']}. "
                f"Affected asset: {details['affected_asset']}. "
                f"Requested action: {details['requested_action']}. "
                f"Please review the extracted details before selecting a decision."
            )
            _speak_text(briefing)
    st.caption(TTS_CAPTION)

    return details


def _render_conversation(record: dict, details: dict) -> None:
    st.markdown("---")
    st.subheader("3. Conversation Review Panel")
    timeline = generate_conversation_timeline(st.session_state.av_transcript, details, record["source"] == "mock")
    for msg in timeline:
        speaker_icon = "👤" if msg["speaker"] == "Caller" else "🤖"
        with st.chat_message(msg["speaker"], avatar=speaker_icon):
            st.markdown(f"**{msg['speaker']}**")
            st.write(msg["text"])


def _render_approval_state(details: dict) -> None:
    st.markdown("---")
    st.subheader("4. Approval State")
    executed = st.session_state.get("av_executed_decision")
    final_authorization = "Recorded human reviewer decision" if executed else "Pending explicit human decision"
    lines = [
        "* **Caller identity:** Unconfirmed — never inferred from audio",
        "* **Authorization status:** Unconfirmed — synthetic demo context only",
        f"* **Requested action:** {details.get('requested_action')}",
        "* **Verbal intent:** Recorded",
        f"* **Final authorization:** {final_authorization}",
    ]
    if executed:
        lines += [
            f"* **Decision outcome:** {executed}",
            "* **Execution mode:** Dry-run recorded — no grid action executed",
            "* **Audit status:** Recorded successfully",
        ]
    else:
        lines.append("* **Execution mode:** Dry-run only")
    st.markdown("\n".join(lines))


def _render_approval_gate() -> None:
    st.markdown("---")
    st.subheader("5. Human Approval Gate")
    st.markdown("GridGuard acts as **decision support only**. Review the incident and explicitly approve or reject.")

    consent = st.checkbox("I have reviewed the transcription and extraction.", key="av_review_consent")
    decision = st.radio(
        "Decision Outcome:",
        ["Approve", "Hold", "Reject", "Escalate"],
        index=None,
        horizontal=True,
        key="av_review_decision",
    )

    if st.button("Execute Decision", disabled=not (consent and decision is not None), key="av_execute_decision"):
        with st.spinner("Saving audit packet..."):
            filepath = save_audit_packet(
                st.session_state.av_incident_record,
                decision,
                OUTCOME_MAPPING.get(decision, "UNKNOWN"),
            )
            st.session_state.av_executed_decision = decision
            st.session_state.av_audit_filepath = filepath
            st.rerun()

    if not st.session_state.get("av_executed_decision"):
        return

    st.success(
        f"Decision '{st.session_state.av_executed_decision}' recorded successfully! "
        f"Audit saved to `{st.session_state.get('av_audit_filepath')}`."
    )
    st.info("Note: GridGuard never executes grid actions or contacts anyone automatically.")

    col_speak, col_spacer = st.columns([1, 4])
    with col_speak:
        if st.button("🔊 Speak decision confirmation", key="av_speak_decision_confirmation"):
            confirmation = (
                f"Decision recorded: {st.session_state.av_executed_decision}. "
                f"This workflow remains a simulated dry run. "
                f"No real grid action, telephone call, or external escalation has occurred."
            )
            _speak_text(confirmation)
    st.caption(TTS_CAPTION)

    if st.session_state.av_executed_decision == "Escalate":
        _render_dry_run_escalation()


def _render_dry_run_escalation() -> None:
    st.markdown("---")
    st.subheader("6. Simulated Call‑E Supervisor Escalation")

    with st.container(border=True):
        st.markdown("#### 🔔 Call‑E Simulation Disclosure")
        st.warning(
            "⚠️ **This is a simulated dry-run.** No real call will be placed. "
            "Call‑E is an AI voice agent. Supervisor identity and approval are simulated outcomes only."
        )
        st.info(
            "This simulation demonstrates how GridGuard would escalate to an authorized supervisor "
            "for review and approval. The supervisor's authorization and decision are mocked for demonstration."
        )
        st.markdown("#### Choose a simulated outcome:")
        selected_scenario = st.selectbox(
            "Supervisor response scenario (dry-run only):", DRY_RUN_SCENARIOS, index=0, key="av_escalation_scenario"
        )

        col_speak, col_spacer = st.columns([1, 4])
        with col_speak:
            if st.button("🔊 Preview selected scenario", key="av_speak_scenario_preview"):
                scenario_msgs = {
                    "Authorized, reviewed, approve": "Simulated supervisor scenario: authorized, reviewed, and approving escalation. This is a dry-run preview only. No real call, grid action, or escalation has occurred.",
                    "Authorized, reviewed, reject": "Simulated supervisor scenario: authorized, reviewed, and rejecting escalation. This is a dry-run preview only. No real call, grid action, or escalation has occurred.",
                    "Not authorized / wrong person": "Simulated supervisor scenario: not authorized or wrong person. This is a dry-run preview only. No real call, grid action, or escalation has occurred.",
                    "Authorized, not reviewed": "Simulated supervisor scenario: authorized but not yet reviewed. This is a dry-run preview only. No real call, grid action, or escalation has occurred.",
                    "Call-E execution failure": "Simulated supervisor scenario: Call-E execution failure. This is a dry-run preview only. No real call, grid action, or escalation has occurred.",
                }
                preview_msg = scenario_msgs.get(selected_scenario, "Unknown scenario. This is a dry-run preview only.")
                _speak_text(preview_msg)
        st.caption(TTS_CAPTION)

        st.markdown("#### Final confirmation:")
        escalation_consent = st.checkbox(
            "I understand this is a dry-run simulation with no real call or grid action.",
            key="av_escalation_consent",
        )
        if st.button(
            "Run Simulated Supervisor Escalation",
            disabled=not escalation_consent,
            type="primary",
            key="av_run_escalation",
        ):
            with st.spinner("Running Call‑E dry-run simulation..."):
                advisory = build_advisory(st.session_state.av_incident_record)
                st.session_state.av_escalation_result = build_dry_run_escalation(advisory, selected_scenario)
                st.session_state.av_escalation_scenario_executed = selected_scenario
            st.rerun()

    if st.session_state.get("av_escalation_result"):
        _render_escalation_result(st.session_state.av_escalation_result)


def _render_escalation_result(result: dict) -> None:
    st.markdown("---")
    st.subheader("7. Supervisor Escalation Result")
    supervisor_outcome = result.get("supervisor_outcome", "unknown").lower()

    if supervisor_outcome == "approved":
        st.success("APPROVED: Supervisor authorized and approved escalation")
        st.markdown(f"**Workflow result:** {result.get('workflow_result_text', 'Approved')}")
        st.info("Escalation package would be created with the incident details and supervisor approval.")
    elif supervisor_outcome == "denied":
        st.warning("DENIED: Supervisor authorized but denied escalation")
        st.markdown(f"**Workflow result:** {result.get('workflow_result_text', 'Denied')}")
    elif supervisor_outcome == "unavailable":
        st.error("UNAVAILABLE: Supervisor not available or not authorized")
        st.markdown(f"**Workflow result:** {result.get('workflow_result_text', 'Not available')}")
    elif supervisor_outcome == "failed":
        st.error("FAILED: Call-E execution failed")
        st.markdown(f"**Error:** {result.get('error_message', 'Call could not be completed')}")
        st.markdown(f"**Workflow result:** {result.get('workflow_result_text', 'Execution failed')}")
        st.warning("No supervisor response received. Manual escalation may be required.")
    else:
        st.warning("UNCLEAR: Unable to interpret supervisor response")
        st.markdown(f"**Workflow result:** {result.get('workflow_result_text', 'Manual follow-up needed')}")

    def _yes_no(value):
        return "Yes" if value else ("No" if value is False else "Unknown")

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Auth Confirmed", _yes_no(result.get("auth_confirmed")))
    col2.metric("Review Confirmed", _yes_no(result.get("review_confirmed")))
    col3.metric("Supervisor Outcome", supervisor_outcome.capitalize())
    col4.metric("Mode", "Dry-run")

    st.markdown("#### Simulated Call Transcript:")
    st.text(result.get("transcript_summary", "No transcript"))

    with st.container(border=True):
        st.markdown("#### Audit Update")
        if st.button("Save escalation result to audit packet", key="av_save_escalation_audit"):
            audit_filepath = save_audit_packet(
                st.session_state.av_incident_record,
                st.session_state.av_executed_decision,
                OUTCOME_MAPPING.get(st.session_state.av_executed_decision, "UNKNOWN"),
                escalation_result=result,
            )
            st.session_state.av_escalation_audit_saved = True
            st.success(f"Escalation result recorded in audit: `{audit_filepath}`")
            st.info("The audit packet now includes both the operator decision and the simulated supervisor response.")

        if st.session_state.get("av_escalation_audit_saved"):
            st.markdown("---")
            col_speak, col_spacer = st.columns([1, 4])
            with col_speak:
                if st.button("🔊 Speak audited outcome", key="av_speak_audited_outcome"):
                    supervisor_outcome = result.get("supervisor_outcome", "unknown").lower()
                    outcome_text = {
                        "approved": "Supervisor approved escalation",
                        "denied": "Supervisor denied escalation",
                        "unavailable": "Supervisor unavailable or not authorized",
                        "failed": "Escalation failed",
                    }.get(supervisor_outcome, "Unknown outcome")
                    audited_summary = (
                        f"Operator decision: {st.session_state.av_executed_decision}. "
                        f"Simulated supervisor outcome: {outcome_text}. "
                        f"Audit has been saved. This remains a dry-run. "
                        f"No real call, grid action, or external escalation has occurred."
                    )
                    _speak_text(audited_summary)
            st.caption(TTS_CAPTION)


def render_assembly_voice_tab() -> None:
    """Render the Assembly Voice tab; call inside the ``with tab:`` block."""
    _init_state()
    mock_mode = is_mock_mode()

    with st.container(key=ROOT_KEY):
        st.markdown(_STYLE, unsafe_allow_html=True)
        st.subheader("Assembly Voice Intake")
        st.markdown("**Approval-gated, inbound AssemblyAI voice transcription for grid incident reports.**")
        _render_status_panel(mock_mode)

        st.markdown("---")
        st.subheader("Operational Status")
        col_a, col_b, col_c, col_d = st.columns(4)
        col_a.metric("Grid Risk", "Standby")
        col_b.metric("Affected Asset", "None")
        col_c.metric("Incident Severity", "Nominal")
        col_d.metric("Human Approval", "Required")

        st.markdown("---")
        _render_audio_ingestion(mock_mode)

        if st.session_state.av_transcript:
            record = st.session_state.av_incident_record
            details = _render_extraction(record)
            _render_conversation(record, details)
            _render_approval_state(details)
            _render_approval_gate()
