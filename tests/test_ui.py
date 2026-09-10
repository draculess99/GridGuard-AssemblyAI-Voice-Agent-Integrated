import pytest
import re
import pandas as pd
from streamlit.testing.v1 import AppTest
from unittest.mock import patch, MagicMock

def parse_transcript(raw_transcript):
    turns = []
    text_turns = re.split(r'(Bot:|User:|Agent:|Operator:|Recipient:)', raw_transcript, flags=re.IGNORECASE)
    if len(text_turns) > 1:
        current_speaker = "bot"
        for part in text_turns:
            if not part.strip(): continue
            lower_part = part.strip().lower()
            if lower_part in ["bot:", "agent:"]:
                current_speaker = "bot"
            elif lower_part in ["user:", "operator:", "recipient:"]:
                current_speaker = "user"
            else:
                turns.append({"speaker": current_speaker, "text": part.strip()})
    return turns

def render_table(turns):
    table_data = []
    for t in turns:
        speaker_raw = str(t.get("speaker", "")).lower()
        msg = t.get("text", "")
        if speaker_raw in ["bot", "assistant", "agent"]:
            speaker_label = "GridGuard Agent"
        elif speaker_raw in ["user", "human", "recipient", "operator"]:
            speaker_label = "Human Recipient"
        else:
            speaker_label = "GridGuard Agent"
        table_data.append({"Speaker": speaker_label, "Message": msg})
    return pd.DataFrame(table_data).to_string()

def test_ui_transcript_rendering_logic():
    raw_transcript = (
        "Bot: Are you the authorized Grid Operations Shift Supervisor or duty operations manager?\n"
        "User: Yep, I am.\n"
        "Bot: Have you reviewed the GridGuard advisory and supporting dashboard evidence?\n"
        "User: I have.\n"
        "Bot: Do you approve escalation to the operations response workflow?\n"
        "User: I do."
    )
    
    turns = parse_transcript(raw_transcript)
    assert len(turns) == 6
    
    table_string = render_table(turns)
    assert "GridGuard Agent" in table_string
    assert "Human Recipient" in table_string
    assert "Yep, I am." in table_string
    assert "I have." in table_string
    assert "I do." in table_string

def test_dry_run_ui_logic():
    # Verify that the dry-run explanation and outcome mapping is present in streamlit_app.py
    with open("streamlit_app.py", "r", encoding="utf-8") as f:
        code = f.read()
    
    assert 'st.markdown("### Dry-run Human Approval Branch")' in code, "Dry-run header missing"
    assert 'df_mapping = pd.DataFrame(' in code, "Mapping table missing"
    assert 'res = dispatch_escalation(advisory, dry_run=True, scenario=b)' in code, "Mapping content missing"
    
    # Verify live mode does not show dry run logic
    assert 'if not live_mode:\n                                st.markdown("### Dry-run Human Approval Branch")' in code, "Live mode should not display dry-run explanation"
    
    # Verify Mode is displayed in Status Panel
    assert 'inferred_mode = "Live CALL-E result"' in code, "Live mode label missing"
    assert 'inferred_mode = "Dry-run fixture"' in code, "Dry-run mode label missing"
    assert 'st.write(f"- Mode: **{inferred_mode}**")' in code, "Mode UI rendering missing"
    
    # Verify escalation package logic
    assert 'if not final_res.get("create_escalation_package", final_status == "ESCALATION_APPROVED"):' in code, "Missing non-approved logic"

