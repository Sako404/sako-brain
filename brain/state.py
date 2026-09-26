"""`brain state` — a deterministic, read-only operational-state aggregator.

Composes *existing* domain functions (registry, validate, timeline, handoff,
memoryqueue, systemdstatus, the integrity module's own manifest, and one new
minimal index query for decisions) into a single envelope. It never invents
business logic of its own: no new definition of "stale", "blocked" or
"important" is introduced here, and no section duplicates logic that already
lives in `cli.py` or elsewhere — see `count_by_type`/`count_inbox_pending` in
`indexer.py`, extracted for exactly this reason.

Design record: Brain note `project-sako-brain-sako-os-audit` (sections
16-18) and decision `decision-2026-09-26-sako-brain-brain-state-design-accepted`.

Hard rules this module must never break:
- Deterministic only. No AI call, no network call, no Matrix/TRON/n8n call.
- Read-only. Never writes to the vault, the index, or anything else.
- Never crashes as a whole: every section is collected independently, and a
  single failing section degrades to an empty/default value plus a `sources`
  entry recording why — it never takes the rest of the envelope down with it.
- `sensitivity: restricted` content is filtered the same way `context.py`
  already does (`include_restricted`, counted rather than silently dropped),
  wherever a canonical relation to a note's sensitivity exists. `decisions`
  and `memory_queue` carry their own `sensitivity` field directly.
  `timeline_recent` has no such field on `TimelineEntry` itself, but its `id`
  is the same id the index keys notes by (see `timeline.list_timeline`,
  which sets `TimelineEntry.id = note.id`) — so sensitivity is looked up via
  that canonical join, not guessed. An entry whose id cannot be found in the
  index (not yet indexed, or blank) is treated as unverifiable and omitted,
  never assumed safe.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from . import __version__
from . import gitops
from . import validate
from .handoff import list_projects_with_handoffs
from .indexer import connect
from .memoryqueue import PendingEntry, list_pending
from .paths import Config
from .registry import ProjectEntry, load_registry
from .systemdstatus import TIMER_UNITS, TimerStatus, timer_status
from .timeline import TimelineEntry, list_timeline
from .validate import Problem

SCHEMA_VERSION = 1


@dataclass
class SourceStatus:
    ok: bool
    error: str | None = None


@dataclass
class ProjectsSection:
    by_status: dict = field(default_factory=dict)
    entries: list = field(default_factory=list)  # list[ProjectEntry]


@dataclass
class DecisionsSection:
    open: list = field(default_factory=list)  # list[dict]: id/title/status/source/confidence
    counts_by_status: dict = field(default_factory=dict)


@dataclass
class MemoryQueueSection:
    pending_count: int = 0
    entries: list = field(default_factory=list)  # list[PendingEntry], sensitivity-filtered


@dataclass
class HandoffsSection:
    project_ids: list = field(default_factory=list)


@dataclass
class DoctorSection:
    problem_count: int = 0
    blocking_count: int = 0
    problems: list = field(default_factory=list)  # list[Problem]


@dataclass
class TimelineSection:
    window_days: int = 14
    entries: list = field(default_factory=list)  # list[TimelineEntry], sensitivity-filtered


@dataclass
class SystemdSection:
    timers: list = field(default_factory=list)  # list[TimerStatus]


@dataclass
class IntegrityRefSection:
    generated_at: str | None = None
    total_notes: int | None = None


@dataclass
class OperationalState:
    schema_version: int
    generated_at: str
    brain_version: str
    vault_root: str
    sources: dict  # dict[str, SourceStatus]
    projects: ProjectsSection
    decisions: DecisionsSection
    memory_queue: MemoryQueueSection
    handoffs: HandoffsSection
    doctor: DoctorSection
    timeline_recent: TimelineSection
    systemd: SystemdSection
    integrity_ref: IntegrityRefSection
    sensitivity_omitted: dict  # dict[str, int | None] — None = not verifiable this run


def _collect_projects(config: Config) -> ProjectsSection:
    entries = load_registry(config)
    by_status: dict[str, int] = {}
    for e in entries:
        by_status[e.status or "unknown"] = by_status.get(e.status or "unknown", 0) + 1
    return ProjectsSection(by_status=by_status, entries=entries)


def _collect_decisions(config: Config, include_restricted: bool) -> tuple[DecisionsSection, int]:
    """The one section with no existing function to reuse — established
    style (a small, direct query against the index, as `cmd_status` and
    `context.py` already do), filtering only on the canonical, documented
    status vocabulary (`proposed`/`decided`/`superseded`), never a new one."""
    conn = connect(config)
    try:
        rows = conn.execute(
            "SELECT id, title, status, sensitivity, source, confidence "
            "FROM notes WHERE type = 'decision' ORDER BY updated DESC"
        ).fetchall()
    finally:
        conn.close()

    section = DecisionsSection()
    omitted = 0
    for row in rows:
        status = row["status"] or ""
        section.counts_by_status[status] = section.counts_by_status.get(status, 0) + 1
        if status != "proposed":
            continue
        sensitivity = row["sensitivity"] or "normal"
        if sensitivity == "restricted" and not include_restricted:
            omitted += 1
            continue
        section.open.append({
            "id": row["id"], "title": row["title"], "status": status,
            "source": row["source"] or "", "confidence": row["confidence"] or "",
        })
    return section, omitted


def _collect_memory_queue(config: Config, include_restricted: bool) -> tuple[MemoryQueueSection, int]:
    all_pending = list_pending(config)
    section = MemoryQueueSection(pending_count=len(all_pending))
    omitted = 0
    for entry in all_pending:
        if entry.sensitivity == "restricted" and not include_restricted:
            omitted += 1
            continue
        section.entries.append(entry)
    return section, omitted


def _collect_handoffs(config: Config) -> HandoffsSection:
    """Deliberately bare: no age, no staleness, no priority. `handoff.py`
    exposes nothing beyond the project-id list today, and inventing that
    metadata here would be exactly the new business logic this module must
    not introduce."""
    return HandoffsSection(project_ids=list_projects_with_handoffs(config))


def _collect_doctor(config: Config) -> DoctorSection:
    problems = validate.run_all(config)
    blocking = [p for p in problems if p.check in gitops.BLOCKING_CHECKS]
    return DoctorSection(problem_count=len(problems), blocking_count=len(blocking), problems=problems)


def _collect_timeline(config: Config, window_days: int, include_restricted: bool) -> tuple[TimelineSection, int]:
    """Sensitivity is checked via the canonical relation `TimelineEntry.id ==
    notes.id` (the same id `timeline.list_timeline` reads off the parsed
    note) — not a new interpretation, the same join `context.py` already
    does to resolve a search hit's sensitivity. An id the index cannot find
    (not yet indexed, or blank) is unverifiable and is omitted, never
    assumed 'normal'."""
    entries = list_timeline(config)
    cutoff = date.today() - timedelta(days=window_days)
    windowed = []
    for e in entries:
        try:
            entry_date = date.fromisoformat(e.date)
        except ValueError:
            continue
        if entry_date >= cutoff:
            windowed.append(e)

    sensitivity_by_id: dict[str, str] = {}
    ids = [e.id for e in windowed if e.id]
    if ids:
        conn = connect(config)
        try:
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT id, sensitivity FROM notes WHERE id IN ({placeholders})", ids
            ).fetchall()
        finally:
            conn.close()
        sensitivity_by_id = {row["id"]: (row["sensitivity"] or "normal") for row in rows}

    visible = []
    omitted = 0
    for e in windowed:
        sensitivity = sensitivity_by_id.get(e.id)
        if sensitivity is None or (sensitivity == "restricted" and not include_restricted):
            omitted += 1
            continue
        visible.append(e)

    return TimelineSection(window_days=window_days, entries=visible), omitted


def _collect_systemd() -> SystemdSection:
    return SystemdSection(timers=[timer_status(unit) for unit in TIMER_UNITS])


def _collect_integrity_ref(config: Config) -> IntegrityRefSection:
    """Reads the newest saved `brain integrity` manifest, if any. Never
    recomputes hashes — that stays `brain integrity`'s job."""
    integrity_dir = config.integrity_dir
    if not integrity_dir.exists():
        return IntegrityRefSection()
    manifests = sorted(integrity_dir.glob("manifest-*.json"))
    if not manifests:
        return IntegrityRefSection()
    # Filenames are `manifest-YYYYMMDD-HHMMSS.json` — lexicographic sort is
    # chronological sort.
    payload = json.loads(manifests[-1].read_text(encoding="utf-8"))
    return IntegrityRefSection(
        generated_at=payload.get("generated_at"),
        total_notes=payload.get("total_notes"),
    )


def get_operational_state(config: Config, *, include_restricted: bool = False,
                           timeline_window_days: int = 14) -> OperationalState:
    """Deterministic snapshot of Brain's own current state. Composition only
    — see the module docstring for the hard rules this function must hold to.
    Never raises: a failing section degrades to its default value and is
    recorded in `sources`, so one bad collector cannot take down the rest.
    """
    sources: dict[str, SourceStatus] = {}
    sensitivity_omitted: dict[str, int | None] = {
        "decisions": None, "memory_queue": None, "timeline_recent": None,
    }

    try:
        projects = _collect_projects(config)
        sources["projects"] = SourceStatus(ok=True)
    except Exception as exc:  # noqa: BLE001 — isolate this section only
        projects = ProjectsSection()
        sources["projects"] = SourceStatus(ok=False, error=str(exc))

    try:
        decisions, sensitivity_omitted["decisions"] = _collect_decisions(config, include_restricted)
        sources["decisions"] = SourceStatus(ok=True)
    except Exception as exc:  # noqa: BLE001
        decisions = DecisionsSection()
        sources["decisions"] = SourceStatus(ok=False, error=str(exc))

    try:
        memory_queue, sensitivity_omitted["memory_queue"] = _collect_memory_queue(config, include_restricted)
        sources["memory_queue"] = SourceStatus(ok=True)
    except Exception as exc:  # noqa: BLE001
        memory_queue = MemoryQueueSection()
        sources["memory_queue"] = SourceStatus(ok=False, error=str(exc))

    try:
        handoffs = _collect_handoffs(config)
        sources["handoffs"] = SourceStatus(ok=True)
    except Exception as exc:  # noqa: BLE001
        handoffs = HandoffsSection()
        sources["handoffs"] = SourceStatus(ok=False, error=str(exc))

    try:
        doctor = _collect_doctor(config)
        sources["doctor"] = SourceStatus(ok=True)
    except Exception as exc:  # noqa: BLE001
        doctor = DoctorSection()
        sources["doctor"] = SourceStatus(ok=False, error=str(exc))

    try:
        timeline_recent, sensitivity_omitted["timeline_recent"] = _collect_timeline(
            config, timeline_window_days, include_restricted,
        )
        sources["timeline_recent"] = SourceStatus(ok=True)
    except Exception as exc:  # noqa: BLE001
        timeline_recent = TimelineSection(window_days=timeline_window_days)
        sources["timeline_recent"] = SourceStatus(ok=False, error=str(exc))

    try:
        systemd = _collect_systemd()
        sources["systemd"] = SourceStatus(ok=True)
    except Exception as exc:  # noqa: BLE001
        systemd = SystemdSection()
        sources["systemd"] = SourceStatus(ok=False, error=str(exc))

    try:
        integrity_ref = _collect_integrity_ref(config)
        sources["integrity_ref"] = SourceStatus(ok=True)
    except Exception as exc:  # noqa: BLE001
        integrity_ref = IntegrityRefSection()
        sources["integrity_ref"] = SourceStatus(ok=False, error=str(exc))

    return OperationalState(
        schema_version=SCHEMA_VERSION,
        generated_at=datetime.now().isoformat(timespec="seconds"),
        brain_version=__version__,
        vault_root=str(config.brain_root),
        sources=sources,
        projects=projects,
        decisions=decisions,
        memory_queue=memory_queue,
        handoffs=handoffs,
        doctor=doctor,
        timeline_recent=timeline_recent,
        systemd=systemd,
        integrity_ref=integrity_ref,
        sensitivity_omitted=sensitivity_omitted,
    )
