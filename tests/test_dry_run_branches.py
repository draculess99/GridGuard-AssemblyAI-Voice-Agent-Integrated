import pytest
from backend.call_e_integration import build_dry_run_result

def test_dry_run_canonical_branches():
    # Canonical labels
    assert build_dry_run_result("Not authorized / wrong person")["final_status"] == "WRONG_RECIPIENT"
    assert build_dry_run_result("Not authorized / wrong person")["create_escalation_package"] is False
    
    assert build_dry_run_result("Authorized, not reviewed")["final_status"] == "PENDING_REVIEW"
    assert build_dry_run_result("Authorized, not reviewed")["create_escalation_package"] is False
    
    assert build_dry_run_result("Authorized, reviewed, hold")["final_status"] == "REVIEWED_HOLD"
    assert build_dry_run_result("Authorized, reviewed, hold")["create_escalation_package"] is False
    
    assert build_dry_run_result("Authorized, reviewed, reject")["final_status"] == "REVIEWED_NOT_APPROVED"
    assert build_dry_run_result("Authorized, reviewed, reject")["create_escalation_package"] is False
    
    assert build_dry_run_result("Authorized, reviewed, approve")["final_status"] == "ESCALATION_APPROVED"
    assert build_dry_run_result("Authorized, reviewed, approve")["create_escalation_package"] is True
    
    assert build_dry_run_result("Unclear response")["final_status"] == "NEEDS_MANUAL_FOLLOW_UP"
    assert build_dry_run_result("Unclear response")["create_escalation_package"] is False

def test_dry_run_legacy_labels():
    # Old legacy labels that still need to normalize correctly
    assert build_dry_run_result("Wrong person")["final_status"] == "WRONG_RECIPIENT"
    assert build_dry_run_result("Wrong person")["create_escalation_package"] is False
    
    assert build_dry_run_result("Authorized but not reviewed")["final_status"] == "PENDING_REVIEW"
    assert build_dry_run_result("Authorized but not reviewed")["create_escalation_package"] is False
    
    assert build_dry_run_result("Reviewed but hold")["final_status"] == "REVIEWED_HOLD"
    assert build_dry_run_result("Reviewed but hold")["create_escalation_package"] is False
    
    assert build_dry_run_result("Reviewed but reject")["final_status"] == "REVIEWED_NOT_APPROVED"
    assert build_dry_run_result("Reviewed but reject")["create_escalation_package"] is False
    
    assert build_dry_run_result("Reviewed and approve")["final_status"] == "ESCALATION_APPROVED"
    assert build_dry_run_result("Reviewed and approve")["create_escalation_package"] is True
