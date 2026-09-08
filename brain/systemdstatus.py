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


def timer_status(unit: str) -> TimerStatus:
    enabled_result = _systemctl("is-enabled", unit)
    enabled = enabled_result.stdout.strip() if enabled_result is not None else "unknown"
    if not enabled:
        enabled = (enabled_result.stderr.strip() if enabled_result is not None else "unknown") or "unknown"

    active_result = _systemctl("is-active", unit)
    active = active_result.stdout.strip() if active_result is not None else "unknown"
    if not active:
        active = "unknown"

    return TimerStatus(unit=unit, enabled=enabled, active=active)


def list_timers_raw() -> str:
    """Human-readable `systemctl --user list-timers` output for this tool's two
    timers, including next-elapse times. Returns a message (not an exception)
    if systemctl isn't usable in this environment."""
    result = _systemctl("list-timers", "--all")
    if result is None:
        return "systemctl not available in this environment."
    lines = [result.stdout.splitlines()[0]] if result.stdout.splitlines() else []
    lines += [line for line in result.stdout.splitlines() if APP_DIRNAME in line]
    return ("\n".join(lines) if len(lines) > 1
            else f"No {APP_DIRNAME} timers found (not installed yet?).")
