#!/usr/bin/env python3
"""Supervisor Event Listener for Process State Changes.

WBS-LOG1.10: Event listener for restart logging (AC-LOG1.5)

This script listens to Supervisor events and logs process state changes
to help with debugging and monitoring service restarts.

Events monitored:
- PROCESS_STATE_EXITED: Process has exited
- PROCESS_STATE_STOPPED: Process has been stopped
- PROCESS_STATE_STARTING: Process is starting

Usage:
    This script is configured as an eventlistener in supervisord.conf.
    It runs automatically when Supervisor starts.

Log output:
    ~/Library/Logs/ai-platform/supervisor/event_listener.log
"""

import json
import sys
from datetime import datetime, timezone


def write_stdout(message: str) -> None:
    """Write a message to stdout and flush.
    
    Supervisor event listeners communicate via stdout.
    """
    sys.stdout.write(message)
    sys.stdout.flush()


def write_stderr(message: str) -> None:
    """Write a message to stderr for logging."""
    sys.stderr.write(f"{message}\n")
    sys.stderr.flush()


def log_event(event_type: str, process_name: str, details: dict) -> None:
    """Log an event in JSON format.
    
    Args:
        event_type: Type of supervisor event
        process_name: Name of the affected process
        details: Additional event details
    """
    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": "INFO",
        "service": "supervisor-event-listener",
        "event_type": event_type,
        "process_name": process_name,
        "message": f"Process {process_name}: {event_type}",
        **details
    }
    write_stderr(json.dumps(log_entry))


def parse_event_data(data: str) -> dict:
    """Parse supervisor event data into a dictionary.
    
    Args:
        data: Space-separated key:value pairs
        
    Returns:
        Dictionary of parsed values
    """
    result = {}
    for item in data.split():
        if ":" in item:
            key, value = item.split(":", 1)
            result[key] = value
    return result


def main() -> None:
    """Main event listener loop.
    
    Supervisor event listeners follow a specific protocol:
    1. Write "READY\n" to indicate readiness
    2. Read header line with event metadata
    3. Read payload with event data
    4. Process the event
    5. Write "RESULT 2\nOK" or "RESULT 4\nFAIL"
    6. Repeat
    """
    while True:
        # Signal that we're ready for events
        write_stdout("READY\n")
        
        # Read header line
        header_line = sys.stdin.readline()
        if not header_line:
            break
            
        # Parse header
        headers = parse_event_data(header_line)
        event_name = headers.get("eventname", "UNKNOWN")
        payload_length = int(headers.get("len", 0))
        
        # Read payload
        payload = sys.stdin.read(payload_length) if payload_length > 0 else ""
        payload_data = parse_event_data(payload)
        
        # Extract process information
        process_name = payload_data.get("processname", "unknown")
        group_name = payload_data.get("groupname", "unknown")
        from_state = payload_data.get("from_state", "unknown")
        
        # Log based on event type
        if event_name == "PROCESS_STATE_EXITED":
            expected = payload_data.get("expected", "0")
            exit_code = payload_data.get("exitcode", "unknown")
            log_event(
                event_type="PROCESS_EXITED",
                process_name=process_name,
                details={
                    "group": group_name,
                    "from_state": from_state,
                    "exit_code": exit_code,
                    "expected": expected == "1",
                    "severity": "WARNING" if expected == "0" else "INFO"
                }
            )
            
        elif event_name == "PROCESS_STATE_STOPPED":
            log_event(
                event_type="PROCESS_STOPPED",
                process_name=process_name,
                details={
                    "group": group_name,
                    "from_state": from_state
                }
            )
            
        elif event_name == "PROCESS_STATE_STARTING":
            log_event(
                event_type="PROCESS_STARTING",
                process_name=process_name,
                details={
                    "group": group_name,
                    "from_state": from_state
                }
            )
            
        elif event_name == "PROCESS_STATE_RUNNING":
            log_event(
                event_type="PROCESS_RUNNING",
                process_name=process_name,
                details={
                    "group": group_name,
                    "from_state": from_state
                }
            )
            
        elif event_name == "PROCESS_STATE_FATAL":
            log_event(
                event_type="PROCESS_FATAL",
                process_name=process_name,
                details={
                    "group": group_name,
                    "from_state": from_state,
                    "severity": "ERROR"
                }
            )
        
        # Acknowledge the event
        write_stdout("RESULT 2\nOK")


if __name__ == "__main__":
    main()
