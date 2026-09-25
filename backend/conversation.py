from typing import List, Dict

def generate_conversation_timeline(transcript: str, details: Dict[str, str], is_mock: bool) -> List[Dict[str, str]]:
    timeline = []
    
    if is_mock:
        caller_part1 = "This is Jordan Lee, Operations Supervisor for Substation Alpha. We have a critical thermal overload."
        caller_part2 = "My operator identifier is OPS-4721. I am requesting emergency crew dispatch."
    else:
        caller_part1 = transcript
        caller_part2 = "No caller identity or authorization claim has been verified from this audio."
        
    timeline.append({"speaker": "Caller", "text": caller_part1})
    
    timeline.append({"speaker": "GridGuard Voice Agent", "text": "Incident recorded as synthetic decision support. Audio does not establish caller identity, authorization, or approval authority."})
    
    timeline.append({"speaker": "Caller", "text": caller_part2})
    
    timeline.append({"speaker": "GridGuard Authorization Service", "text": "Synthetic demo authorization context only: Unconfirmed. No identity, authorization, or approval authority is inferred from the transcript.\n\n*This is not biometric, voice, or directory authentication.*"})
    
    action = details.get('requested_action', 'Unknown').lower()
    agent_summary = f"I have extracted: {details.get('severity', 'Unknown')} severity, {details.get('location', 'Unknown')}, {details.get('affected_asset', 'Unknown').lower()}, and a request to {action}. GridGuard provides decision support only and will not dispatch anyone automatically."
    timeline.append({"speaker": "GridGuard Voice Agent", "text": agent_summary})
    
    timeline.append({"speaker": "Caller", "text": "I have reviewed the recommendation and request approval to proceed with the documented dry-run decision."})
    
    timeline.append({"speaker": "GridGuard Voice Agent", "text": "Verbal intent recorded. A human reviewer must still explicitly select and execute the final decision below."})
    
    return timeline
