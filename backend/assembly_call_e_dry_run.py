"""Dry-run Call-E escalation adapter for voice incident escalation simulation."""

from datetime import datetime, timezone
from typing import Dict, Optional


# Dry-run outcome scenarios adapted from the frozen Call-E repo's test matrix.
# Includes normal outcomes and a simulated Call-E execution failure.
DRY_RUN_SCENARIOS = [
    "Authorized, reviewed, approve",
    "Authorized, reviewed, reject",
    "Not authorized / wrong person",
    "Authorized, not reviewed",
    "Call-E execution failure",
]

# Canonical supervisor outcomes for audit and display consistency.
SUPERVISOR_OUTCOMES = {
    "approved": "Supervisor approved the escalation.",
    "denied": "Supervisor denied the escalation.",
    "unavailable": "Supervisor not available or not authorized.",
    "failed": "Call-E simulation execution failed.",
}


def build_advisory(incident_record: Dict) -> Dict:
    """
    Map this project's incident record to a Call-E advisory shape.
    Does not mutate the incident record.
    """
    details = incident_record.get("incident_details", {})
    return {
        "advisory_id": incident_record.get("incident_id", "SYN-UNKNOWN"),
        "asset_id": details.get("location", "Unknown Location"),
        "risk_type": f"{details.get('severity', 'Unknown')} Risk",
        "severity": details.get("severity", "Unknown"),
        "location": details.get("location", "Unknown"),
        "affected_asset": details.get("affected_asset", "Unknown"),
        "requested_action": details.get("requested_action", "Unknown"),
        "evidence_summary": incident_record.get("raw_transcript", "No transcript"),
    }


def build_dry_run_escalation(advisory: Dict, scenario: str) -> Dict:
    """
    Simulate a Call-E escalation for the given scenario.
    Returns a structured result with supervisor authorization, approval status,
    and a canonical supervisor_outcome field for audit consistency.
    Includes AI agent self-identification in all simulated transcripts.
    """
    scenario_clean = scenario.lower().replace(",", "").replace("  ", " ").strip()

    base_response = {
        "status": "escalation_completed",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dry_run_scenario": scenario,
        "advisory_id": advisory.get("advisory_id", "SYN-UNKNOWN"),
        "mode": "dry_run",
    }

    # Canonical AI agent introduction text for all call transcripts
    AI_INTRO = (
        "Hello, I'm Call-E, an AI agent calling on behalf of GridGuard's "
        "incident-escalation workflow. This simulated interaction will be logged in "
        "the audit packet."
    )

    if scenario_clean in ["authorized reviewed approve", "reviewed and approve", "approved", "approve"]:
        base_response.update({
            "final_status": "ESCALATION_APPROVED",
            "auth_confirmed": True,
            "review_confirmed": True,
            "approval_decision": "approve",
            "supervisor_outcome": "approved",
            "supervisor_response": "I have reviewed it and approve escalation.",
            "response_understood": "Authorized supervisor approved escalation",
            "create_escalation_package": True,
            "workflow_result_text": "Authorized supervisor approved escalation. Escalation package created.",
            "transcript_summary": (
                f"Call-E Agent: {AI_INTRO}\n"
                "Call-E Agent: Are you the authorized Grid Operations Shift Supervisor or duty operations manager?\n"
                "Supervisor: I am the authorized grid operations shift supervisor.\n"
                "Call-E Agent: Have you reviewed the incident evidence?\n"
                "Supervisor: I have.\n"
                "Call-E Agent: Do you approve escalation?\n"
                "Supervisor: I have reviewed it and approve escalation."
            ),
        })
    elif scenario_clean in ["authorized reviewed reject", "reviewed and reject"]:
        base_response.update({
            "final_status": "REVIEWED_NOT_APPROVED",
            "auth_confirmed": True,
            "review_confirmed": True,
            "approval_decision": "reject",
            "supervisor_outcome": "denied",
            "supervisor_response": "I reviewed it and do not approve escalation.",
            "response_understood": "Authorized supervisor rejected escalation",
            "create_escalation_package": False,
            "workflow_result_text": "Authorized supervisor reviewed and rejects escalation. Escalation blocked.",
            "transcript_summary": (
                f"Call-E Agent: {AI_INTRO}\n"
                "Call-E Agent: Are you the authorized Grid Operations Shift Supervisor?\n"
                "Supervisor: Yes.\n"
                "Call-E Agent: Have you reviewed the evidence?\n"
                "Supervisor: Yes.\n"
                "Call-E Agent: Do you approve escalation?\n"
                "Supervisor: I reviewed it and do not approve escalation."
            ),
        })
    elif scenario_clean in ["not authorized / wrong person", "wrong person"]:
        base_response.update({
            "final_status": "WRONG_RECIPIENT",
            "auth_confirmed": False,
            "review_confirmed": None,
            "approval_decision": None,
            "supervisor_outcome": "unavailable",
            "supervisor_response": "I am not the shift supervisor.",
            "response_understood": "Recipient was not authorized",
            "create_escalation_package": False,
            "workflow_result_text": "Recipient is not authorized. Escalation blocked.",
            "transcript_summary": (
                f"Call-E Agent: {AI_INTRO}\n"
                "Call-E Agent: Are you the authorized Grid Operations Shift Supervisor or duty operations manager?\n"
                "Recipient: I am not the shift supervisor."
            ),
        })
    elif scenario_clean in ["authorized not reviewed", "authorized but not reviewed"]:
        base_response.update({
            "final_status": "PENDING_REVIEW",
            "auth_confirmed": True,
            "review_confirmed": False,
            "approval_decision": None,
            "supervisor_outcome": "unavailable",
            "supervisor_response": "I am authorized, but I have not reviewed it yet.",
            "response_understood": "Authorized supervisor has not reviewed the evidence",
            "create_escalation_package": False,
            "workflow_result_text": "Right person, but evidence has not been reviewed. Escalation blocked.",
            "transcript_summary": (
                f"Call-E Agent: {AI_INTRO}\n"
                "Call-E Agent: Are you the authorized Grid Operations Shift Supervisor?\n"
                "Supervisor: Yes, I am.\n"
                "Call-E Agent: Have you reviewed the evidence?\n"
                "Supervisor: I am authorized, but I have not reviewed it yet."
            ),
        })
    elif scenario_clean in ["call-e execution failure", "execution failure", "call execution failure"]:
        base_response.update({
            "status": "failed",
            "final_status": "CALL_EXECUTION_FAILED",
            "auth_confirmed": None,
            "review_confirmed": None,
            "approval_decision": None,
            "supervisor_outcome": "failed",
            "supervisor_response": None,
            "response_understood": "Call-E simulation execution failed. No supervisor contact was established.",
            "create_escalation_package": False,
            "workflow_result_text": "Call-E execution failed. No supervisor response received. Manual escalation required.",
            "transcript_summary": (
                "Call-E Agent: Attempting to establish connection...\n"
                "[Connection failed - network unavailable or recipient unreachable]"
            ),
            "error_message": "Simulated Call-E provider error: unable to complete call.",
        })
    else:
        # Default: unclear response or unknown scenario
        base_response.update({
            "final_status": "NEEDS_MANUAL_FOLLOW_UP",
            "auth_confirmed": None,
            "review_confirmed": None,
            "approval_decision": "unclear",
            "supervisor_outcome": "unavailable",
            "supervisor_response": "Unclear or no response.",
            "response_understood": "Response cannot be safely interpreted. Manual follow-up needed.",
            "create_escalation_package": False,
            "workflow_result_text": "Response unclear. Escalation blocked.",
            "transcript_summary": (
                f"Call-E Agent: {AI_INTRO}\n"
                "Call-E Agent: Are you available to discuss this incident?"
            ),
        })

    return base_response
