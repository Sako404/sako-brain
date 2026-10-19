"""Read-only `systemctl --user` queries for `brain backup status/schedule`.

This module never installs, enables, or modifies any unit — that's a
manual, explicit step (see 90_SYSTEM/systemd/README.md). It only reports
what's already there, gracefully, even if systemd or the units aren't
present at all (e.g. before they've been installed).
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass

from .paths import APP_DIRNAME

BACKUP_TIMER = f"{APP_DIRNAME}-backup.timer"
MAINTENANCE_TIMER = f"{APP_DIRNAME}-maintenance.timer"
TIMER_UNITS = (BACKUP_TIMER, MAINTENANCE_TIMER)


def _systemctl(*args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


@dataclass
class TimerStatus:
    unit: str
    enabled: str  # "enabled" | "disabled" | "not-found" | "unknown"
    active: str   # "active" | "inactive" | "failed" | "unknown"
    next_elapse: str = ""
    diagnostic: str = ""  # fixed category; never raw stdout/stderr


def user_manager_available() -> bool:
    """True only when a `systemctl --user` query succeeds. A host without a
    user manager (a container, a server without lingering) cannot report
    timer state at all, so callers should say "not checked" rather than
    list every unit as unknown."""
    probe = _systemctl("show", "--property=Version", "--value")
    return probe is not None and probe.returncode == 0


def timer_status(unit: str) -> TimerStatus:
    """Normalize machine-readable states; stderr is never part of the API.

    Only a successful LoadState query can establish absence. Failure to
    contact the user manager is an unknown state, not a missing unit.
    Diagnostics are fixed categories, never systemctl's arbitrary output.
    """
    loaded = _systemctl("show", "--property=LoadState", "--value", unit)
    if loaded is None or loaded.returncode != 0:
        return TimerStatus(unit, "unknown", "unknown", diagnostic="manager-query-failed")
    load_state = loaded.stdout.strip()
    if load_state == "not-found":
        return TimerStatus(unit, "not-found", "unknown")
    if load_state != "loaded":
        return TimerStatus(unit, "unknown", "unknown", diagnostic="unit-load-state-unavailable")

    enabled_result = _systemctl("is-enabled", unit)
    active_result = _systemctl("is-active", unit)
    enabled_raw = enabled_result.stdout.strip() if enabled_result is not None else ""
    active_raw = active_result.stdout.strip() if active_result is not None else ""
    # is-enabled/is-active deliberately return nonzero for disabled/inactive.
    if enabled_result is not None and enabled_result.returncode in (0, 1):
        if enabled_raw in {"enabled", "enabled-runtime", "alias"}:
            enabled = "enabled"
        elif enabled_raw in {"disabled", "linked", "linked-runtime", "static", "indirect", "masked", "masked-runtime", "generated", "transient"}:
            enabled = "disabled"
        else:
            enabled = "unknown"
    else:
        enabled = "unknown"
    active = (active_raw if active_result is not None
              and active_result.returncode in (0, 3)
              and active_raw in {"active", "inactive", "failed"} else "unknown")
    diagnostic = "state-query-incomplete" if "unknown" in (enabled, active) else ""
    return TimerStatus(unit, enabled, active, diagnostic=diagnostic)


def list_timers_raw() -> str:
    """Human-readable `systemctl --user list-timers` output for this tool's two
    timers, including next-elapse times. Returns a message (not an exception)
    if systemctl isn't usable in this environment."""
    result = _systemctl("list-timers", "--all")
    if result is None or result.returncode != 0:
        return "systemctl user manager unavailable; timer state could not be checked."
    lines = [result.stdout.splitlines()[0]] if result.stdout.splitlines() else []
    lines += [line for line in result.stdout.splitlines() if APP_DIRNAME in line]
    return ("\n".join(lines) if len(lines) > 1
            else f"No {APP_DIRNAME} timers found (not installed yet?).")
