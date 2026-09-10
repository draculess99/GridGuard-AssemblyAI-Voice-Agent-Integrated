import os
import re
import hashlib
from datetime import datetime, timezone
import requests
from calle import CalleClient

def get_calle_call_result(call_id: str) -> dict:
    api_key = os.environ.get("CALLE_API_KEY")
    if not api_key:
        raise ValueError("Missing CALLE_API_KEY")
    headers = {"Authorization": f"Bearer {api_key}"}
    response = requests.get(f"https://api.heycall-e.com/v1/calls/{call_id}", headers=headers, timeout=10)
    response.raise_for_status()
    return response.json()

def get_calle_client() -> CalleClient | None:
    api_key = os.environ.get("CALLE_API_KEY")
    if not api_key:
        return None
    return CalleClient(api_key=api_key)

def _poll_and_parse_result(call_id: str) -> dict:
    if not call_id or not str(call_id).startswith("call_"):
        return {
            "status": "failed",
            "call_id": call_id,
            "failure_reason": "Invalid ID Format",
            "message": "This is not a GridGuard-captured CALL-E API call ID. Dashboard record IDs cannot be retrieved through the Developer API.",
            "creation_succeeded": True
        }
        
    import time
    max_polls = 20
    polls = 0
    terminal_statuses = {"completed", "failed", "cancelled"}
    
    last_poll = None
    while polls < max_polls:
        polls += 1
        time.sleep(5)
        last_poll = get_calle_call_result(call_id)
        if last_poll.get("status") in terminal_statuses:
            break
            
    if not last_poll or last_poll.get("status") not in terminal_statuses:
        return {
            "status": "failed",
            "call_id": call_id,
            "failure_reason": "Timeout",
            "message": f"Result retrieval timed out after {polls * 5} seconds; no additional call was placed.",
            "polls": polls,
            "creation_succeeded": True
        }
        
    if last_poll.get("status") != "completed":
        error_msg = last_poll.get("message", last_poll.get("error", "Unknown terminal failure"))
        return {
            "status": "failed",
            "call_id": call_id,
            "failure_reason": last_poll.get("error", "Provider terminal failure"),
            "error_code": last_poll.get("error_code"),
            "message": f"CALL-E reported a terminal failure: {error_msg}.",
            "polls": polls,
            "creation_succeeded": True
        }

    def extract_all_text(obj) -> str:
        texts = []
        if isinstance(obj, dict):
            for v in obj.values():
                texts.append(extract_all_text(v))
        elif isinstance(obj, list):
            for item in obj:
                texts.append(extract_all_text(item))
        elif isinstance(obj, str):
            texts.append(obj)
        return " ".join(filter(None, texts))

    def mask_secrets(obj):
        import re
        if isinstance(obj, dict):
            new_dict = {}
            for k, v in obj.items():
                if ("phone" in k.lower() or "recipient" in k.lower()) and isinstance(v, str):
                    new_dict[k] = re.sub(r'\d(?=\d{4})', '*', v)
                elif ("phone" in k.lower() or "recipient" in k.lower()) and isinstance(v, list) and all(isinstance(x, str) for x in v):
                    new_dict[k] = [re.sub(r'\d(?=\d{4})', '*', x) for x in v]
                else:
                    new_dict[k] = mask_secrets(v)
            return new_dict
        elif isinstance(obj, list):
            return [mask_secrets(item) for item in obj]
        return obj

    raw_response_redacted = mask_secrets(last_poll)
    
    all_normalized_text = extract_all_text(last_poll).lower()
    
    # Best-effort targeted extraction for UI
    structured = {}
    transcript = ""
    duration = last_poll.get("duration")

    # Merge structured results and transcript from call-level attempts
    for attempt in last_poll.get("attempts", []):
        if attempt.get("structured_result"):
            structured.update(attempt.get("structured_result"))
        if attempt.get("transcript_summary"):
            transcript = attempt.get("transcript_summary")
        elif not transcript:
            transcript = str(attempt.get("transcript", "")) or str(attempt.get("transcript_turns", ""))
        if not duration and attempt.get("duration"):
            duration = attempt.get("duration")

    # Merge structured results and transcript from recipient-level attempts
    for recipient in last_poll.get("recipients", []):
        for attempt in recipient.get("attempts", []):
            if attempt.get("structured_result"):
                structured.update(attempt.get("structured_result"))
            if attempt.get("transcript_summary"):
                transcript = attempt.get("transcript_summary")
            elif not transcript:
                transcript = str(attempt.get("transcript", "")) or str(attempt.get("transcript_turns", ""))
            if not duration and attempt.get("duration"):
                duration = attempt.get("duration")

    # If transcript is still empty, grab something from the raw text
    if not transcript:
        # Get the first 200 chars from evidence or similar if available
        ev = last_poll.get("evidence", "")
        if isinstance(ev, list):
            ev = " ".join(str(e) for e in ev)
        transcript = str(ev)[:200] if ev else "No direct transcript returned."

    field_inspection = {
        "status present": "status" in last_poll,
        "evidence count": len(last_poll.get("evidence", [])) if isinstance(last_poll.get("evidence"), list) else (1 if last_poll.get("evidence") else 0),
        "structured_result present": "structured_result" in last_poll or bool(structured),
        "recipients count": len(last_poll.get("recipients", [])),
        "attempts count": len(last_poll.get("attempts", [])),
        "transcript_turns count": len(last_poll.get("transcript_turns", [])) if isinstance(last_poll.get("transcript_turns"), list) else (1 if last_poll.get("transcript_turns") else 0),
    }
    
    # Detect final_status
    def extract_turns():
        turns = []
        # First try structured transcript turns
        for attempt in last_poll.get("attempts", []):
            if attempt.get("transcript_turns"):
                return attempt.get("transcript_turns")
        for recipient in last_poll.get("recipients", []):
            for attempt in recipient.get("attempts", []):
                if attempt.get("transcript_turns"):
                    return attempt.get("transcript_turns")
                    
        # If empty, parse the compact transcript field
        if not transcript:
            return turns
            
        import json, ast, re
        parsed = None
        if isinstance(transcript, list):
            parsed = transcript
        elif isinstance(transcript, str):
            try:
                parsed = json.loads(transcript)
            except:
                try:
                    parsed = ast.literal_eval(transcript)
                except:
                    pass
                    
        if isinstance(parsed, list):
            return parsed
            
        # Plain text fallback
        text_turns = re.split(r'(Bot:|User:|Agent:|Operator:|Recipient:)', transcript, flags=re.IGNORECASE)
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

    turns = extract_turns()
    
    auth_resp = ""
    review_resp = ""
    approval_resp = ""
    
    current_question = None
    for turn in turns:
        if not isinstance(turn, dict): continue
        speaker = str(turn.get("speaker", "bot")).lower()
        text = str(turn.get("text", "")).lower()
        
        if speaker in ["bot", "agent"]:
            if "authorized" in text or "shift supervisor" in text or "duty operations manager" in text:
                current_question = "auth"
            elif "reviewed" in text or "supporting dashboard" in text:
                current_question = "review"
            elif "approve escalation" in text or "approve, hold, or reject" in text:
                current_question = "approval"
        elif speaker in ["user", "recipient", "operator"]:
            if current_question == "auth" and not auth_resp:
                auth_resp = text
            elif current_question == "review" and not review_resp:
                review_resp = text
            elif current_question == "approval" and not approval_resp:
                approval_resp = text

    def check_positive(resp, valid_words):
        if not resp: return False
        resp_clean = resp.replace(",", "").replace(".", "").strip()
        words = resp_clean.split()
        for w in valid_words:
            if w in resp_clean or w in words:
                return True
        return False
        
    auth_valid = ["yes", "yep", "yeah", "correct", "i am", "yep i am", "yes i am", "that's me", "speaking", "authorized", "duty manager", "supervisor"]
    review_valid = ["yes", "yep", "i have", "yes i have", "reviewed", "i reviewed it", "already reviewed"]
    approve_valid = ["approve", "approved", "i approve", "yes", "yep", "i do", "authorize", "authorized", "proceed", "escalate", "go ahead"]
    
    auth_confirmed = structured.get("auth_confirmed")
    if auth_confirmed is None and auth_resp:
        auth_confirmed = check_positive(auth_resp, auth_valid)
        
    review_confirmed = structured.get("review_confirmed")
    if review_confirmed is None and review_resp:
        review_confirmed = check_positive(review_resp, review_valid)
        
    approval_decision = structured.get("approval_decision")
    if approval_decision is None and approval_resp:
        if check_positive(approval_resp, approve_valid):
            approval_decision = "approved"
        elif "hold" in approval_resp:
            approval_decision = "hold"
        elif "reject" in approval_resp or "no" in approval_resp.split():
            approval_decision = "reject"
            
    final_status = "NEEDS_MANUAL_FOLLOW_UP"
    if auth_confirmed and review_confirmed and approval_decision in ["approve", "approved"]:
        final_status = "ESCALATION_APPROVED"
        approval_decision = "approved"
    elif auth_confirmed is False:
        final_status = "WRONG_RECIPIENT"
    elif auth_confirmed and not review_confirmed:
        final_status = "PENDING_REVIEW"
    elif auth_confirmed and review_confirmed and approval_decision == "hold":
        final_status = "REVIEWED_HOLD"
    elif auth_confirmed and review_confirmed and approval_decision == "reject":
        final_status = "REVIEWED_NOT_APPROVED"

    if final_status == "ESCALATION_APPROVED":
        auth_confirmed = True
        review_confirmed = True
        approval_decision = "approved"
        structured["response_text"] = "approved"

    return {
        "status": "escalation_completed",
        "call_id": call_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "final_status": final_status,
        "auth_confirmed": auth_confirmed,
        "review_confirmed": review_confirmed,
        "approval_decision": approval_decision,
        "recipient_response": structured.get("response_text", "Unknown"),
        "transcript_summary": transcript,
        "transcript_turns": turns,
        "evidence": str(last_poll.get("evidence", "")),
        "task_completed": last_poll.get("task_completed"),
        "completion_confidence": last_poll.get("completion_confidence"),
        "duration": duration,
        "polls": polls,
        "raw_response_redacted": raw_response_redacted,
        "field_inspection": field_inspection
    }

def retrieve_escalation(call_id: str) -> dict:
    try:
        return _poll_and_parse_result(call_id)
    except Exception as e:
        diag = {
            "status": "failed",
            "creation_succeeded": True,
            "call_id": call_id,
            "failure_reason": type(e).__name__,
            "message": str(e),
            "exception_class": type(e).__name__
        }
        if hasattr(e, 'response') and e.response:
            diag["http_status"] = getattr(e.response, 'status_code', None)
        return diag

DRY_RUN_BRANCHES = [
    "Not authorized / wrong person",
    "Authorized, not reviewed",
    "Authorized, reviewed, hold",
    "Authorized, reviewed, reject",
    "Authorized, reviewed, approve",
    "Unclear response"
]

def build_dry_run_result(selected_branch: str) -> dict:
    scenario_clean = selected_branch.lower().replace(",", "").replace("  ", " ").strip()
    
    base_response = {
        "status": "escalation_completed",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dry_run_scenario": selected_branch,
    }
    
    if scenario_clean in ["not authorized / wrong person", "wrong person"]:
        base_response.update({
            "final_status": "WRONG_RECIPIENT",
            "auth_confirmed": False,
            "review_confirmed": None,
            "approval_decision": None,
            "recipient_response": "I am not the shift supervisor.",
            "response_understood": "Recipient was not authorized",
            "create_escalation_package": False,
            "workflow_result_text": "Recipient is not authorized. Escalation blocked.",
            "transcript_summary": "Bot: Are you the authorized Grid Operations Shift Supervisor or duty operations manager?\\nUser: I am not the shift supervisor.",
            "transcript_turns": [
                {"speaker": "bot", "text": "Are you the authorized Grid Operations Shift Supervisor or duty operations manager?"},
                {"speaker": "user", "text": "I am not the shift supervisor."}
            ]
        })
    elif scenario_clean in ["authorized not reviewed", "authorized but not reviewed"]:
        base_response.update({
            "final_status": "PENDING_REVIEW",
            "auth_confirmed": True,
            "review_confirmed": False,
            "approval_decision": None,
            "recipient_response": "I am authorized, but I have not reviewed it yet.",
            "response_understood": "Authorized recipient has not reviewed the evidence",
            "create_escalation_package": False,
            "workflow_result_text": "Right person, but evidence has not been reviewed. Escalation blocked.",
            "transcript_summary": "Bot: Are you the authorized Grid Operations Shift Supervisor or duty operations manager?\\nUser: Yes, I am.\\nBot: Have you had a chance to review the GridGuard advisory and the supporting dashboard evidence?\\nUser: I am authorized, but I have not reviewed it yet.",
            "transcript_turns": [
                {"speaker": "bot", "text": "Are you the authorized Grid Operations Shift Supervisor or duty operations manager?"},
                {"speaker": "user", "text": "Yes, I am."},
                {"speaker": "bot", "text": "Have you had a chance to review the GridGuard advisory and the supporting dashboard evidence?"},
                {"speaker": "user", "text": "I am authorized, but I have not reviewed it yet."}
            ]
        })
    elif scenario_clean in ["authorized reviewed hold", "reviewed but hold"]:
        base_response.update({
            "final_status": "REVIEWED_HOLD",
            "auth_confirmed": True,
            "review_confirmed": True,
            "approval_decision": "hold",
            "recipient_response": "I have reviewed it, but hold escalation for now.",
            "response_understood": "Authorized reviewer placed escalation on hold",
            "create_escalation_package": False,
            "workflow_result_text": "Authorized reviewer reviewed the advisory but wants to wait. Escalation blocked/pending.",
            "transcript_summary": "Bot: Are you the authorized Grid Operations Shift Supervisor?\\nUser: Yes.\\nBot: Have you reviewed the evidence?\\nUser: Yes.\\nBot: Do you approve escalation?\\nUser: I have reviewed it, but hold escalation for now.",
            "transcript_turns": [
                {"speaker": "bot", "text": "Are you the authorized Grid Operations Shift Supervisor?"},
                {"speaker": "user", "text": "Yes."},
                {"speaker": "bot", "text": "Have you reviewed the evidence?"},
                {"speaker": "user", "text": "Yes."},
                {"speaker": "bot", "text": "Do you approve escalation?"},
                {"speaker": "user", "text": "I have reviewed it, but hold escalation for now."}
            ]
        })
    elif scenario_clean in ["authorized reviewed reject", "reviewed but reject"]:
        base_response.update({
            "final_status": "REVIEWED_NOT_APPROVED",
            "auth_confirmed": True,
            "review_confirmed": True,
            "approval_decision": "reject",
            "recipient_response": "I reviewed it and do not approve escalation.",
            "response_understood": "Authorized reviewer rejected escalation",
            "create_escalation_package": False,
            "workflow_result_text": "Authorized reviewer reviewed and rejects escalation. Escalation blocked.",
            "transcript_summary": "Bot: Are you the authorized Grid Operations Shift Supervisor?\\nUser: Yes.\\nBot: Have you reviewed the evidence?\\nUser: Yes.\\nBot: Do you approve escalation?\\nUser: I reviewed it and do not approve escalation.",
            "transcript_turns": [
                {"speaker": "bot", "text": "Are you the authorized Grid Operations Shift Supervisor?"},
                {"speaker": "user", "text": "Yes."},
                {"speaker": "bot", "text": "Have you reviewed the evidence?"},
                {"speaker": "user", "text": "Yes."},
                {"speaker": "bot", "text": "Do you approve escalation?"},
                {"speaker": "user", "text": "I reviewed it and do not approve escalation."}
            ]
        })
    elif scenario_clean in ["authorized reviewed approve", "reviewed and approve", "approved", "approve"]:
        base_response.update({
            "final_status": "ESCALATION_APPROVED",
            "auth_confirmed": True,
            "review_confirmed": True,
            "approval_decision": "approve",
            "recipient_response": "I have reviewed it and approve escalation.",
            "response_understood": "Authorized reviewer approved escalation",
            "create_escalation_package": True,
            "workflow_result_text": "Authorized reviewer approved escalation. Escalation package created.",
            "transcript_summary": "Bot: Are you the authorized Grid Operations Shift Supervisor or duty operations manager?\\nUser: I am the authorized grid operations shift supervisor.\\nBot: Have you had a chance to review the GridGuard advisory and the supporting dashboard evidence?\\nUser: I have.\\nBot: Do you approve escalation to the operations response workflow?\\nUser: I have reviewed it and approve escalation.",
            "transcript_turns": [
                {"speaker": "bot", "text": "Are you the authorized Grid Operations Shift Supervisor or duty operations manager?"},
                {"speaker": "user", "text": "I am the authorized grid operations shift supervisor."},
                {"speaker": "bot", "text": "Have you had a chance to review the GridGuard advisory and the supporting dashboard evidence?"},
                {"speaker": "user", "text": "I have."},
                {"speaker": "bot", "text": "Do you approve escalation to the operations response workflow?"},
                {"speaker": "user", "text": "I have reviewed it and approve escalation."}
            ]
        })
    else: # "Unclear response"
        base_response.update({
            "final_status": "NEEDS_MANUAL_FOLLOW_UP",
            "auth_confirmed": None,
            "review_confirmed": None,
            "approval_decision": "unclear",
            "recipient_response": "What is this about?",
            "response_understood": "Response unclear. Manual follow-up needed",
            "create_escalation_package": False,
            "workflow_result_text": "Response cannot be safely interpreted. Escalation blocked.",
            "transcript_summary": "Bot: Are you the authorized Grid Operations Shift Supervisor?\\nUser: What is this about?",
            "transcript_turns": [
                {"speaker": "bot", "text": "Are you the authorized Grid Operations Shift Supervisor?"},
                {"speaker": "user", "text": "What is this about?"}
            ]
        })
    return base_response

def dispatch_escalation(advisory: dict, dry_run: bool = True, idempotency_key: str | None = None, scenario: str = "Authorized, reviewed, approve") -> dict:
    """
    Dispatches a voice escalation call via the CALL-E Python SDK.
    """
    if dry_run:
        return build_dry_run_result(scenario)

    # Live Mode
    # 1. Validate API Key
    client = get_calle_client()
    if not client:
        raise ValueError("Missing CALLE_API_KEY for live mode.")

    # 2. Strict E.164 lock check
    test_number = os.environ.get("CALLE_AUTHORIZED_TEST_NUMBER")
    if not test_number:
        raise ValueError("Missing CALLE_AUTHORIZED_TEST_NUMBER for live mode.")
    
    if not re.match(r'^\+[1-9]\d{1,14}$', test_number):
        raise ValueError(f"Invalid E.164 phone number configured for CALLE_AUTHORIZED_TEST_NUMBER.")

    # 3. Use provided idempotency key or generate a fresh UUID v4
    import uuid
    if not idempotency_key:
        idempotency_key = str(uuid.uuid4())

    # 4. Use CalleClient and create
    created = None
    try:
        advisory_id = advisory.get("advisory_id", "Unknown")
        asset_or_feeder = advisory.get("asset_id", "Unknown Asset")
        
        spoken_asset = asset_or_feeder.replace("-", " ")
        live_goal = (
            "Hello, this is the GridGuard Voice Escalation Agent.\n\n"
            f"This call is being logged for operational audit purposes regarding a critical GridGuard advisory for {spoken_asset}.\n\n"
            "Are you the authorized Grid Operations Shift Supervisor or duty operations manager?\n\n"
            "Have you had a chance to review the GridGuard advisory and the supporting dashboard evidence?\n\n"
            "Do you approve escalation to the operations response workflow?"
        )
        
        schema = {
            "type": "object",
            "properties": {
                "auth_confirmed": {"type": "boolean"},
                "review_confirmed": {"type": "boolean"},
                "approval_decision": {"type": "string", "enum": ["approve", "hold", "reject", "unclear"]},
                "response_text": {"type": "string"}
            },
            "required": ["auth_confirmed", "review_confirmed", "approval_decision", "response_text"]
        }

        created = client.calls.create(
            task=live_goal,
            recipients=[
                {
                    "phones": [test_number],
                    "region": "US",
                    "locale": "en-US",
                }
            ],
            result_schema={"type": "object", "properties": {"overall_status": {"type": "string"}}},
            recipient_result_schema=schema,
            idempotency_key=idempotency_key,
        )
        
        call_id = created["id"]
        
        return _poll_and_parse_result(call_id)
    except Exception as e:
        diag = {
            "status": "failed",
            "creation_succeeded": created is not None,
            "failure_reason": type(e).__name__,
            "message": str(e),
            "exception_class": type(e).__name__
        }
        if hasattr(e, 'response') and e.response:
            diag["http_status"] = getattr(e.response, 'status_code', None)
        if created:
            diag["call_id"] = created.get("id")
            diag["created_at"] = created.get("created_at")
            diag["updated_at"] = created.get("updated_at")
        return diag
