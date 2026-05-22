"""Local storage helpers for ``proxyctl traffic`` sampling.

Mihomo exposes byte counters only for live connections. These helpers store
counter deltas as NDJSON events and keep a small JSON state file with the last
seen counters per connection.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

TRAFFIC_STATE_FILE = "traffic_state.json"
TRAFFIC_EVENTS_FILE = "traffic_events.ndjson"


def store_paths(config: dict[str, Any], store_dir: str | None) -> dict[str, str]:
    """Return traffic cache paths.

    Args:
        config: proxyctl runtime config.
        store_dir: Optional user-provided cache directory.

    Returns:
        Paths for the traffic state file and event log.
    """
    base = (
        store_dir
        or config.get("traffic_store_dir")
        or os.path.join(os.path.expanduser("~"), ".cache", "proxyctl")
    )
    base = os.path.expanduser(str(base))
    return {
        "dir": base,
        "state_path": os.path.join(base, TRAFFIC_STATE_FILE),
        "events_path": os.path.join(base, TRAFFIC_EVENTS_FILE),
    }


def now() -> datetime:
    """Return the current UTC timestamp."""
    return datetime.now(timezone.utc)


def load_state(path: str) -> dict[str, Any]:
    """Load sampler state; corrupt or missing state starts fresh.

    Args:
        path: JSON state file path.

    Returns:
        State dictionary with a ``connections`` mapping.
    """
    try:
        with open(path, encoding="utf-8") as file_obj:
            data = json.load(file_obj)
    except (OSError, json.JSONDecodeError):
        return {"schema": 1, "connections": {}}
    if not isinstance(data.get("connections"), dict):
        return {"schema": 1, "connections": {}}
    return data


def save_state(path: str, state: dict[str, Any], timestamp: datetime) -> None:
    """Persist sampler state atomically.

    Args:
        path: JSON state file path.
        state: State dictionary to write.
        timestamp: Last update time.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    state["schema"] = 1
    state["updated_at"] = timestamp_text(timestamp)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as file_obj:
        json.dump(state, file_obj, ensure_ascii=False, indent=2)
        file_obj.write("\n")
    os.replace(tmp_path, path)


def append_events(path: str, events: list[dict[str, Any]]) -> None:
    """Append delta events as NDJSON.

    Args:
        path: NDJSON event file path.
        events: Events to append.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as file_obj:
        for event in events:
            file_obj.write(json.dumps(event, ensure_ascii=False) + "\n")


def read_events(path: str) -> list[dict[str, Any]]:
    """Read stored traffic delta events.

    Args:
        path: NDJSON event file path.

    Returns:
        Parsed event dictionaries. Bad lines are ignored.
    """
    rows: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as file_obj:
            for line in file_obj:
                _append_event_line(rows, line)
    except OSError:
        return []
    return rows


def _append_event_line(rows: list[dict[str, Any]], line: str) -> None:
    """Append one parsed event line when it is valid."""
    if not line.strip():
        return
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return
    if isinstance(event, dict):
        rows.append(event)


def delta_events(rows: list[dict[str, Any]], state: dict[str, Any],
                 timestamp: datetime) -> tuple[list[dict[str, Any]], int]:
    """Return delta events and update sampler state in place.

    Args:
        rows: Current active-connection snapshot rows.
        state: Mutable sampler state.
        timestamp: Sample timestamp.

    Returns:
        Tuple of recorded delta events and first-seen baseline count.
    """
    previous = state.get("connections") or {}
    current: dict[str, dict[str, int]] = {}
    events: list[dict[str, Any]] = []
    baseline_count = 0
    for row in rows:
        baseline_count += _append_row_delta(row, previous, current,
                                            events, timestamp)
    state["connections"] = current
    return events, baseline_count


def _append_row_delta(row: dict[str, Any], previous: dict[str, Any],
                      current: dict[str, dict[str, int]],
                      events: list[dict[str, Any]],
                      timestamp: datetime) -> int:
    """Append one row delta, returning 1 when the row is only a baseline."""
    key = state_key(row)
    upload = int(row.get("upload") or 0)
    download = int(row.get("download") or 0)
    current[key] = {"upload": upload, "download": download}
    old = previous.get(key)
    if old is None:
        return 1
    delta_upload = max(0, upload - int(old.get("upload") or 0))
    delta_download = max(0, download - int(old.get("download") or 0))
    if delta_upload or delta_download:
        events.append(event_from_row(row, key, delta_upload,
                                     delta_download, timestamp))
    return 0


def event_from_row(row: dict[str, Any], key: str, upload: int,
                   download: int, timestamp: datetime) -> dict[str, Any]:
    """Build one persisted delta event from a snapshot row.

    Args:
        row: Normalized active-connection row.
        key: Sampler state key.
        upload: Upload byte delta.
        download: Download byte delta.
        timestamp: Sample timestamp.

    Returns:
        Event row ready for NDJSON storage.
    """
    event = dict(row)
    event["sample_ts"] = timestamp_text(timestamp)
    event["state_key"] = key
    event["counter_upload"] = row.get("upload") or 0
    event["counter_download"] = row.get("download") or 0
    event["upload"] = upload
    event["download"] = download
    event["total"] = upload + download
    return event


def state_key(row: dict[str, Any]) -> str:
    """Return a stable key for one active connection counter.

    Args:
        row: Normalized active-connection row.

    Returns:
        Stable key used between samples.
    """
    if row.get("id"):
        return str(row["id"])
    parts = [
        str(row.get("start") or ""),
        str(row.get("local_source_port") or ""),
        str(row.get("host") or row.get("destination_ip") or ""),
        str(row.get("chain") or ""),
    ]
    return "|".join(parts)


def timestamp_text(value: datetime) -> str:
    """Format a UTC timestamp for JSON contracts.

    Args:
        value: Timestamp to format.

    Returns:
        ISO-8601 UTC timestamp with ``Z`` suffix.
    """
    return value.astimezone(timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def parse_timestamp(value: Any) -> datetime:
    """Parse a stored timestamp, returning epoch on bad data.

    Args:
        value: Stored timestamp value.

    Returns:
        UTC datetime.
    """
    if not isinstance(value, str):
        return datetime.fromtimestamp(0, timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.fromtimestamp(0, timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def merge_totals(target: dict[str, int], source: dict[str, int]) -> None:
    """Merge total counters into ``target``.

    Args:
        target: Mutable destination counters.
        source: Source counters.
    """
    for key in ("connection_count", "upload", "download", "total"):
        target[key] += int(source.get(key) or 0)
