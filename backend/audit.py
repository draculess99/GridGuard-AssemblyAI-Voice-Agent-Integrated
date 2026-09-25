import json
import os
from datetime import datetime, timezone
from typing import Dict, Optional

AUDIT_DIR = os.path.join("data", "runtime", "assembly_voice_audits")

def save_audit_packet(
    incident_record: Dict[str, object],
    decision: str,
    outcome_state: str,
    escalation_result: Optional[Dict] = None,
    audit_dir: str = AUDIT_DIR
) -> str:
    """
    Persist a reviewer decision for a synthetic, dry-run incident record.
    Optionally includes a Call-E escalation result in the same packet.
    """
    if not os.path.exists(audit_dir):
        os.makedirs(audit_dir)

    now_utc = datetime.now(timezone.utc)
    timestamp = now_utc.isoformat().replace("+00:00", "Z")
    packet = {
        "timestamp": timestamp,
        "structured_incident_record": incident_record,
        "provenance": {
            "source": incident_record["source"],
            "input_channel": incident_record.get("input_channel", "unspecified"),
        },
        "reviewer_decision": decision,
        "outcome_state": outcome_state,
        "dry_run": True,
    }

    # Optionally include Call-E escalation result (safe for dry-run)
    if escalation_result:
        packet["escalation_result"] = escalation_result

    filename = f"audit_{now_utc.strftime('%Y%m%d_%H%M%S')}.json"
    filepath = os.path.join(audit_dir, filename)

    with open(filepath, 'w') as f:
        json.dump(packet, f, indent=2)

    return filepath
