import pytest
from unittest.mock import patch, MagicMock
from backend.call_e_integration import dispatch_escalation, retrieve_escalation
import os

def test_dry_run_mode_wrong_person():
    res = dispatch_escalation(advisory={}, dry_run=True, scenario="Wrong person")
    assert res["status"] == "escalation_completed"
    assert res["final_status"] == "WRONG_RECIPIENT"

def test_dry_run_mode_authorized_but_not_reviewed():
    res = dispatch_escalation(advisory={}, dry_run=True, scenario="Authorized but not reviewed")
    assert res["status"] == "escalation_completed"
    assert res["final_status"] == "PENDING_REVIEW"

def test_dry_run_mode_reviewed_but_hold():
    res = dispatch_escalation(advisory={}, dry_run=True, scenario="Reviewed but hold")
    assert res["status"] == "escalation_completed"
    assert res["final_status"] == "REVIEWED_HOLD"

def test_dry_run_mode_reviewed_but_reject():
    res = dispatch_escalation(advisory={}, dry_run=True, scenario="Reviewed but reject")
    assert res["status"] == "escalation_completed"
    assert res["final_status"] == "REVIEWED_NOT_APPROVED"

def test_dry_run_mode_reviewed_and_approve():
    res = dispatch_escalation(advisory={}, dry_run=True, scenario="Reviewed and approve")
    assert res["status"] == "escalation_completed"
    assert res["final_status"] == "ESCALATION_APPROVED"

def test_dry_run_mode_unclear():
    res = dispatch_escalation(advisory={}, dry_run=True, scenario="Unclear response")
    assert res["status"] == "escalation_completed"
    assert res["final_status"] == "NEEDS_MANUAL_FOLLOW_UP"

@patch.dict("os.environ", clear=True)
def test_live_mode_missing_api_key():
    with pytest.raises(ValueError, match="Missing CALLE_API_KEY"):
        dispatch_escalation(advisory={}, dry_run=False)

@patch.dict("os.environ", {"CALLE_API_KEY": "fake_key"})
def test_live_mode_missing_test_number():
    with pytest.raises(ValueError, match="Missing CALLE_AUTHORIZED_TEST_NUMBER"):
        dispatch_escalation(advisory={}, dry_run=False)

@patch.dict("os.environ", {"CALLE_API_KEY": "fake_key", "CALLE_AUTHORIZED_TEST_NUMBER": "12345"})
def test_live_mode_invalid_test_number():
    with pytest.raises(ValueError, match="Invalid E.164 phone number configured"):
        dispatch_escalation(advisory={}, dry_run=False)

@patch("time.sleep")
@patch("backend.call_e_integration.requests")
@patch("backend.call_e_integration.CalleClient")
@patch.dict("os.environ", {"CALLE_API_KEY": "fake_key", "CALLE_AUTHORIZED_TEST_NUMBER": "+15550199999"})
def test_live_mode_successful_call_approve(mock_calle_client, mock_requests, mock_sleep):
    mock_client = MagicMock()
    mock_calle_client.return_value = mock_client
    mock_client.calls.create.return_value = {"id": "call_123"}
    
    mock_response_1 = MagicMock()
    mock_response_1.json.return_value = {"status": "in_progress"}
    
    mock_response_2 = MagicMock()
    mock_response_2.json.return_value = {
        "status": "completed",
        "attempts": [
            {
                "transcript_summary": "Bot: Are you the authorized Grid Operations Shift Supervisor or duty operations manager? User: Yep, I am. Bot: Have you reviewed the GridGuard advisory and supporting dashboard evidence? User: I have. Bot: Do you approve escalation to the operations response workflow? User: I do.",
                "structured_result": {}
            }
        ]
    }
    mock_requests.get.side_effect = [mock_response_1, mock_response_2]
    
    res = dispatch_escalation(advisory={}, dry_run=False)
    
    assert res["status"] == "escalation_completed"
    assert res["final_status"] == "ESCALATION_APPROVED"
    assert res["polls"] == 2
    mock_client.calls.create.assert_called_once()

@patch("time.sleep")
@patch("backend.call_e_integration.requests")
@patch("backend.call_e_integration.CalleClient")
@patch.dict("os.environ", {"CALLE_API_KEY": "fake_key", "CALLE_AUTHORIZED_TEST_NUMBER": "+15550199999"})
def test_live_mode_completed_no_auth(mock_calle_client, mock_requests, mock_sleep):
    mock_client = MagicMock()
    mock_calle_client.return_value = mock_client
    mock_client.calls.create.return_value = {"id": "call_456"}
    
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "status": "completed",
        "attempts": [
            {
                "transcript_summary": "Not authorized.",
                "structured_result": {
                    "auth_confirmed": False,
                    "response_text": "Not authorized"
                }
            }
        ]
    }
    mock_requests.get.return_value = mock_response
    
    res = dispatch_escalation(advisory={}, dry_run=False)
    
    assert res["status"] == "escalation_completed"
    assert res["final_status"] == "WRONG_RECIPIENT"

@patch("time.sleep")
@patch("backend.call_e_integration.requests")
@patch("backend.call_e_integration.CalleClient")
@patch.dict("os.environ", {"CALLE_API_KEY": "fake_key", "CALLE_AUTHORIZED_TEST_NUMBER": "+15550199999"})
def test_live_mode_completed_unclear(mock_calle_client, mock_requests, mock_sleep):
    mock_client = MagicMock()
    mock_calle_client.return_value = mock_client
    mock_client.calls.create.return_value = {"id": "call_789"}
    
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "status": "completed",
        "attempts": [
            {
                "transcript_summary": "What is going on?",
                "structured_result": {}
            }
        ]
    }
    mock_requests.get.return_value = mock_response
    
    res = dispatch_escalation(advisory={}, dry_run=False)
    
    assert res["status"] == "escalation_completed"
    assert res["final_status"] == "NEEDS_MANUAL_FOLLOW_UP"

@patch("time.sleep")
@patch("backend.call_e_integration.requests")
@patch("backend.call_e_integration.CalleClient")
@patch.dict("os.environ", {"CALLE_API_KEY": "fake_key"})
def test_retrieve_existing_call(mock_calle_client, mock_requests, mock_sleep):
    mock_client = MagicMock()
    mock_calle_client.return_value = mock_client
    
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "status": "completed",
        "attempts": [
            {
                "transcript_summary": "I am available via retrieve.",
                "structured_result": {
                    "auth_confirmed": True,
                    "review_confirmed": True,
                    "approval_decision": "approve"
                }
            }
        ]
    }
    mock_requests.get.return_value = mock_response
    
    res = retrieve_escalation(call_id="call_789")
    
    assert res["status"] == "escalation_completed"
    assert res["call_id"] == "call_789"
    assert res["final_status"] == "ESCALATION_APPROVED"
    mock_client.calls.create.assert_not_called()

@patch("time.sleep")
@patch("backend.call_e_integration.requests")
@patch("backend.call_e_integration.CalleClient")
@patch.dict("os.environ", {"CALLE_API_KEY": "fake_key", "CALLE_AUTHORIZED_TEST_NUMBER": "+15550199999"})
def test_live_mode_nested_transcript_approve(mock_calle_client, mock_requests, mock_sleep):
    mock_client = MagicMock()
    mock_calle_client.return_value = mock_client
    mock_client.calls.create.return_value = {"id": "call_123"}
    
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "status": "completed",
        "recipients": [
            {
                "attempts": [
                    {
                        "transcript_turns": [
                            {"speaker": "bot", "text": "Are you the authorized shift supervisor?"},
                            {"speaker": "user", "text": "yes i am"},
                            {"speaker": "bot", "text": "Have you reviewed the evidence?"},
                            {"speaker": "user", "text": "yes"},
                            {"speaker": "bot", "text": "Do you approve escalation?"},
                            {"speaker": "user", "text": "yes i approve"}
                        ]
                    }
                ]
            }
        ]
    }
    mock_requests.get.return_value = mock_response
    
    res = dispatch_escalation(advisory={}, dry_run=False)
    assert res["status"] == "escalation_completed"
    assert res["final_status"] == "ESCALATION_APPROVED"

@patch("time.sleep")
@patch("backend.call_e_integration.requests")
@patch("backend.call_e_integration.CalleClient")
@patch.dict("os.environ", {"CALLE_API_KEY": "fake_key", "CALLE_AUTHORIZED_TEST_NUMBER": "+15550199999"})
def test_live_mode_evidence_transcript_reject(mock_calle_client, mock_requests, mock_sleep):
    mock_client = MagicMock()
    mock_calle_client.return_value = mock_client
    mock_client.calls.create.return_value = {"id": "call_123"}
    
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "status": "completed",
        "evidence": ["operator explicitly said reject"],
        "attempts": [
            {
                "structured_result": {
                    "auth_confirmed": True,
                    "review_confirmed": True,
                    "approval_decision": "reject"
                }
            }
        ]
    }
    mock_requests.get.return_value = mock_response
    
    res = dispatch_escalation(advisory={}, dry_run=False)
    assert res["status"] == "escalation_completed"
    assert res["final_status"] == "REVIEWED_NOT_APPROVED"

@patch("time.sleep")
@patch("backend.call_e_integration.requests")
@patch("backend.call_e_integration.CalleClient")
@patch.dict("os.environ", {"CALLE_API_KEY": "fake_key", "CALLE_AUTHORIZED_TEST_NUMBER": "+15550199999"})
def test_raw_response_redaction(mock_calle_client, mock_requests, mock_sleep):
    mock_client = MagicMock()
    mock_calle_client.return_value = mock_client
    mock_client.calls.create.return_value = {"id": "call_123"}
    
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "status": "completed",
        "phones": ["+15551234567"],
        "recipient": {"phone_number": "1234567890"}
    }
    mock_requests.get.return_value = mock_response
    
    res = dispatch_escalation(advisory={}, dry_run=False)
    assert res["status"] == "escalation_completed"
    assert "+*******4567" in res["raw_response_redacted"]["phones"]
    assert "******7890" in res["raw_response_redacted"]["recipient"]["phone_number"]

@patch("backend.call_e_integration.requests")
@patch("backend.call_e_integration.CalleClient")
@patch.dict("os.environ", {"CALLE_API_KEY": "fake_key"})
def test_reject_dashboard_id(mock_calle_client, mock_requests):
    res = retrieve_escalation(call_id="12345-dashboard-id")
    
    assert res["status"] == "failed"
    assert res["failure_reason"] == "Invalid ID Format"
    assert "not a GridGuard-captured CALL-E API call ID" in res["message"]
    mock_requests.get.assert_not_called()

@patch("time.sleep")
@patch("backend.call_e_integration.requests")
@patch("backend.call_e_integration.CalleClient")
@patch.dict("os.environ", {"CALLE_API_KEY": "fake_key", "CALLE_AUTHORIZED_TEST_NUMBER": "+15550199999"})
def test_idempotency_keys_are_unique_per_call(mock_calle_client, mock_requests, mock_sleep):
    mock_client = MagicMock()
    mock_calle_client.return_value = mock_client
    mock_client.calls.create.return_value = {"id": "call_123"}
    mock_response = MagicMock()
    mock_response.json.return_value = {"status": "completed"}
    mock_requests.get.return_value = mock_response
    
    dispatch_escalation(advisory={}, dry_run=False)
    key1 = mock_client.calls.create.call_args_list[0].kwargs["idempotency_key"]
    dispatch_escalation(advisory={}, dry_run=False)
    key2 = mock_client.calls.create.call_args_list[1].kwargs["idempotency_key"]
    assert key1 != key2

@patch("backend.call_e_integration.CalleClient")
@patch.dict("os.environ", {"CALLE_API_KEY": "fake_key", "CALLE_AUTHORIZED_TEST_NUMBER": "+15550199999"})
def test_live_mode_task_creation_failure(mock_calle_client):
    mock_client = MagicMock()
    mock_calle_client.return_value = mock_client
    mock_client.calls.create.side_effect = Exception("API rate limit exceeded")
    
    res = dispatch_escalation(advisory={}, dry_run=False)
    assert res["status"] == "failed"
    assert "fake_key" not in str(res)
    assert "+15550199999" not in str(res)

@patch("time.sleep")
@patch("backend.call_e_integration.requests")
@patch("backend.call_e_integration.CalleClient")
@patch.dict("os.environ", {"CALLE_API_KEY": "fake_key", "CALLE_AUTHORIZED_TEST_NUMBER": "+15550199999"})
def test_live_mode_provider_terminal_failure(mock_calle_client, mock_requests, mock_sleep):
    mock_client = MagicMock()
    mock_calle_client.return_value = mock_client
    mock_client.calls.create.return_value = {"id": "call_123"}
    
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "status": "failed",
        "error": "Recipient blocked",
        "error_code": 403,
        "message": "Call blocked by recipient network."
    }
    mock_requests.get.return_value = mock_response
    
    res = dispatch_escalation(advisory={}, dry_run=False)
    assert res["status"] == "failed"
