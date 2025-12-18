#!/usr/bin/env python3
import json
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple, Dict, List

LOG_FILE = Path("server_uptime.log")
SESSIONS_FILE = Path("server_sessions.json")
STATS_FILE = Path("server_stats.json")

VALID_START_REASONS = {
    "manual_start_confirmed",
    "auto_detected",
}
VALID_STOP_REASONS = {
    "manual_stop",
    "auto_detected",
    "idle_timeout",
}

DUPLICATE_START_REASON = "manual_start_ignored_duplicate"


def parse_log_line(line: str) -> Optional[Tuple[datetime, str, str]]:
    """Parse a log line of the form: ISO-TS - EVENT - REASON"""
    parts = line.strip().split(" - ")
    if len(parts) < 2:
        return None
    try:
        ts = datetime.fromisoformat(parts[0].strip())
    except Exception:
        return None
    event = parts[1].strip()
    reason = parts[2].strip() if len(parts) > 2 else ""
    return ts, event, reason


def is_start_event(event: str, reason: str) -> bool:
    if event == "SERVER_START_CONFIRMED" and reason in VALID_START_REASONS:
        return True
    if event == "SERVER_START" and reason in VALID_START_REASONS:
        return True
    return False


def is_stop_event(event: str, reason: str) -> bool:
    if event == "SERVER_STOP" and reason in VALID_STOP_REASONS:
        return True
    return False


def repair_from_log():
    if not LOG_FILE.exists():
        print("server_uptime.log not found; cannot repair history.")
        sys.exit(1)

    # Accumulators
    sessions: List[Dict] = []
    current_start_ts: Optional[datetime] = None
    current_start_reason: Optional[str] = None

    daily_starts: Dict[str, int] = {}
    total_starts = 0
    last_start_iso: Optional[str] = None
    last_stop_iso: Optional[str] = None

    # Read all lines
    with LOG_FILE.open("r", encoding="utf-8") as f:
        lines = [ln.rstrip("\n") for ln in f]

    # anomaly counters
    duplicates_ignored = 0
    out_of_order_stops_skipped = 0
    stops_without_session = 0

    for ln in lines:
        parsed = parse_log_line(ln)
        if not parsed:
            continue
        ts, event, reason = parsed

        # START handling
        if is_start_event(event, reason):
            if reason == DUPLICATE_START_REASON:
                continue
            # If a session is already open, treat as duplicate and skip
            if current_start_ts is None:
                current_start_ts = ts
                current_start_reason = reason
                # Stats
                date_key = ts.strftime("%Y-%m-%d")
                daily_starts[date_key] = daily_starts.get(date_key, 0) + 1
                total_starts += 1
                last_start_iso = ts.isoformat()
            else:
                duplicates_ignored += 1
            continue

        # STOP handling
        if is_stop_event(event, reason):
            last_stop_iso = ts.isoformat()
            if current_start_ts is not None:
                if ts < current_start_ts:
                    out_of_order_stops_skipped += 1
                    continue
                duration_hours = (ts - current_start_ts).total_seconds() / 3600.0
                if duration_hours >= 0:
                    sessions.append(
                        {
                            "start": current_start_ts.isoformat(),
                            "end": ts.isoformat(),
                            "duration_hours": round(duration_hours, 2),
                            "start_reason": current_start_reason or "unknown",
                            "stop_reason": reason or "unknown",
                        }
                    )
                # Close the current session regardless
                current_start_ts = None
                current_start_reason = None
            else:
                stops_without_session += 1
            continue

        # other events ignored (health issues, start failures, etc.)

    # Build stats JSON
    stats = {
        "total_starts": total_starts,
        "daily": daily_starts,
        "last_start": last_start_iso,
        "last_stop": last_stop_iso,
    }

    # Persist (keep last 100 sessions, like the manager)
    try:
        with SESSIONS_FILE.open("w", encoding="utf-8") as sf:
            json.dump(sessions[-100:], sf, indent=2)
        with STATS_FILE.open("w", encoding="utf-8") as stf:
            json.dump(stats, stf, indent=2)
    except Exception as e:
        print(f"Error writing repaired files: {e}")
        sys.exit(2)

    print(f"Repair complete: {len(sessions)} sessions, {total_starts} total starts.")
    print(
        f"Details: duplicates_ignored={duplicates_ignored}, out_of_order_stops={out_of_order_stops_skipped}, stops_without_session={stops_without_session}."
    )


if __name__ == "__main__":
    repair_from_log()
