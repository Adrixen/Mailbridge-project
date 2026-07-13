"""
MailBridge — state persistence.

Manages a JSON file tracking per-account/per-folder UIDVALIDITY and last
processed UID. All writes are atomic (temp file + os.replace). Thread-safe via
a global lock.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def cleanup_stale_tmp_files(state_path: str, max_age: float = 0) -> int:
    """
    Remove stale tmp*.json files left from previous crashes.

    Only removes files older than *max_age* seconds (default 0 = all).
    Returns the number of removed files.
    """
    dirname = os.path.dirname(state_path) or "."
    now = time.time()
    removed = 0
    try:
        for f in os.listdir(dirname):
            if f.startswith("tmp") and f.endswith(".json"):
                fpath = os.path.join(dirname, f)
                try:
                    age = now - os.path.getmtime(fpath)
                    if age >= max_age:
                        os.unlink(fpath)
                        removed += 1
                except OSError:
                    pass
    except OSError:
        pass
    return removed


def load_state(path: str) -> Dict[str, Any]:
    """Load state from JSON file. Returns empty dict if missing or corrupt."""
    # Clean up stale tmp files on startup
    cleanup_stale_tmp_files(path)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        # Corrupt state → start fresh; log warning
        import logging

        logging.getLogger("state").warning(
            "Failed to load state from %s (%s); starting fresh.", path, exc
        )
        return {}


def save_state(path: str, state: Dict[str, Any]) -> None:
    """
    Atomically write *state* to *path*.

    The state dict is assumed to be a reference shared across workers; copy
    under the lock to get a consistent snapshot.  Stale tmp files from previous
    crashes are cleaned up before each write.
    """
    with _state_lock:
        snapshot = json.loads(json.dumps(state))  # deep-copy
        dirname = os.path.dirname(path) or "."
        # Remove any leftover tmp files before creating a new one
        cleanup_stale_tmp_files(path)
        fd, tmp_path = tempfile.mkstemp(dir=dirname, suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, indent=2)
            os.replace(tmp_path, path)
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise


# ---------------------------------------------------------------------------
# Per-account helpers
# ---------------------------------------------------------------------------

def get_account_state(
    state: Dict[str, Any],
    account_id: str,
    folder: str,
) -> Dict[str, Any]:
    """
    Return the per-folder state dict for *account_id*/*folder*.
    Creates the nested structure if missing.
    """
    with _state_lock:
        acc = state.setdefault(account_id, {})
        return acc.setdefault(folder, {})


def get_last_uid(
    state: Dict[str, Any],
    account_id: str,
    folder: str,
) -> int:
    """Return the last processed UID for an account/folder, or 0."""
    folder_state = get_account_state(state, account_id, folder)
    return folder_state.get("last_uid", 0)


def get_uidvalidity(
    state: Dict[str, Any],
    account_id: str,
    folder: str,
) -> Optional[int]:
    """Return stored UIDVALIDITY or None if not yet recorded."""
    folder_state = get_account_state(state, account_id, folder)
    return folder_state.get("uidvalidity")


def set_last_uid(
    state: Dict[str, Any],
    account_id: str,
    folder: str,
    uid: int,
) -> None:
    """Set the last processed UID (caller must persist with save_state)."""
    with _state_lock:
        state.setdefault(account_id, {}).setdefault(folder, {})["last_uid"] = uid


def set_uidvalidity(
    state: Dict[str, Any],
    account_id: str,
    folder: str,
    uidvalidity: int,
) -> None:
    """Set UIDVALIDITY for an account/folder."""
    with _state_lock:
        state.setdefault(account_id, {}).setdefault(folder, {})[
            "uidvalidity"
        ] = uidvalidity


# ---------------------------------------------------------------------------
# Cumulative stats (persisted across restarts)
# ---------------------------------------------------------------------------

_STATS_KEY = "_stats"


def get_cumulative_stats(state: Dict[str, Any]) -> Dict[str, Dict[str, int]]:
    """Return the cumulative stats dict, creating it if missing."""
    with _state_lock:
        return state.setdefault(_STATS_KEY, {})


def get_account_cumulative_stats(
    state: Dict[str, Any], account_id: str
) -> Dict[str, int]:
    """Return cumulative stats for one account, defaulting to zeros."""
    all_stats = get_cumulative_stats(state)
    with _state_lock:
        return all_stats.setdefault(account_id, {"copied": 0, "errors": 0})


def add_cumulative_stats(
    state: Dict[str, Any], account_id: str, copied: int, errors: int
) -> None:
    """Add to cumulative counters for an account (caller must persist)."""
    with _state_lock:
        stats = state.setdefault(_STATS_KEY, {}).setdefault(
            account_id, {"copied": 0, "errors": 0}
        )
        stats["copied"] = stats.get("copied", 0) + copied
        stats["errors"] = stats.get("errors", 0) + errors


def set_cumulative_stats(
    state: Dict[str, Any], account_id: str, copied: int, errors: int
) -> None:
    """Overwrite cumulative counters for an account."""
    with _state_lock:
        state.setdefault(_STATS_KEY, {})[account_id] = {
            "copied": copied,
            "errors": errors,
        }
