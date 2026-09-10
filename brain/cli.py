"""`brain` — CLI for the Sako Brain personal knowledge system."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import assistant, backup, capture, context as context_mod, discover, gitops, handoff, indexer, integrity, memoryqueue, projectsync, search, systemdstatus, timeline, validate
from . import paths
from . import __version__
from . import init as init_mod
from .gateway_client import BrainGatewayError
from .frontmatter import parse_file
from . import paths as paths_mod
from .paths import Config, default_config
from .registry import load_registry


def cmd_index(config: Config, args) -> int:
    stats = indexer.rebuild(config)
    print(f"Indexed {stats['indexed']} notes.")
    if stats["errors"]:
        print(f"{len(stats['errors'])} problem(s) while indexing:")
        for e in stats["errors"]:
            print(f"  - {e}")
    return 0


def cmd_search(config: Config, args) -> int:
    results = search.search(config, args.query, limit=args.limit)
    if not results:
        print("No matches.")
        return 0
    for r in results:
        print(f"{r.id}  [{r.type}/{r.status}]  {r.title}")
        print(f"    {r.path}")
        if r.snippet:
            print(f"    {r.snippet}")
    return 0


def cmd_context(config: Config, args) -> int:
    """Search + rank + concise structured context for one question.

    Returns provenance-carrying summaries (id/title/snippet, current vs
    historical), never full note bodies — a caller that needs one specific
    note's text asks for it with `brain get`. This is the shape an AI agent
    wants, and `--json` is how any agent in any language consumes it through
    a plain shell call.
    """
    if not config.db_path.exists():
        print("Search index not built yet. Run 'brain index' first.", file=sys.stderr)
        return 1

    result = context_mod.get_context(
        config, args.query, limit=args.limit,
        include_restricted=args.restricted,
    )

    if args.json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
        return 0

    payload = result.to_dict()
    if not payload["notes"] and not payload["projects"] and not payload["timeline"]:
        print("No context found.")
        return 0
    for note in payload["notes"]:
        print(f"{note.get('id', '')}  {note.get('title', '')}")
        if note.get("snippet"):
            print(f"    {note['snippet']}")
    for project in payload["projects"]:
        print(f"[project] {project.get('id', '')}  {project.get('status', '')}")
    for event in payload["timeline"]:
        print(f"[timeline] {event.get('date', '')}  {event.get('title', '')}")
    if payload["restricted_omitted"]:
        print(f"({payload['restricted_omitted']} restricted note(s) omitted — pass --restricted to include)")
    return 0


def cmd_get(config: Config, args) -> int:
    row = search.get_note_row(config, args.id)
    if not row:
        print(f"No note with id '{args.id}' in index. Try 'brain index' first.", file=sys.stderr)
        return 1
    full_path = config.brain_root / row["path"]
    print(full_path.read_text(encoding="utf-8"))
    return 0


def cmd_remember(config: Config, args) -> int:
    try:
        dest = capture.capture(
            config, type_=args.type, title=args.title, text=args.text or "",
            tags=args.tags, people=args.people, projects=args.projects,
            sensitivity=args.sensitivity, confidence=args.confidence, source=args.source or "",
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(f"Captured to {dest.relative_to(config.brain_root)}")
    print("Run 'brain index' to make it searchable, and review 00_INBOX for triage.")
    return 0


def cmd_projects(config: Config, args) -> int:
    entries = load_registry(config)
    if not entries:
        print(f"No projects registered yet. See {config.registry_path.relative_to(config.brain_root)}, "
              "or run 'brain project discover'.")
        return 0
    for e in entries:
        print(f"{e.id}  [{e.status}]  {e.name}")
        print(f"    {e.path}")
    return 0


def cmd_project_show(config: Config, project_id: str) -> int:
    entries = {e.id: e for e in load_registry(config)}
    e = entries.get(project_id)
    if not e:
        print(f"No registered project with id '{project_id}'.", file=sys.stderr)
        return 1
    print(f"id: {e.id}\nname: {e.name}\nstatus: {e.status}\npath: {e.path}\ncategory: {e.category}")
    p = Path(e.path)
    print(f"path exists: {p.exists()}")
    return 0


def cmd_project_discover(config: Config, args) -> int:
    if not config.projects_roots:
        print("No projects root configured — set `projects_root:` (a path, or a "
              "list of paths) in the vault's 90_SYSTEM/config.yaml.", file=sys.stderr)
        return 1
    candidates = discover.discover(config)
    roots = ", ".join(str(r) for r in config.projects_roots)
    if not candidates:
        print("No candidate projects found (or all already registered).")
        return 0
    print(f"Found {len(candidates)} candidate(s) under {roots} (proposal only — nothing registered):\n")
    for c in candidates:
        flags = []
        if c.has_git:
            flags.append("git")
        if c.has_readme:
            flags.append("readme")
        if c.has_package_manifest:
            flags.append("manifest")
        status = "already registered" if c.already_registered else "NOT registered"
        print(f"- {c.name}  [{', '.join(flags) or 'no signals'}]  ({status})")
        print(f"    {c.path}")
    return 0


def cmd_project_sync(config: Config, project_id: str) -> int:
    entries = {e.id: e for e in load_registry(config)}
    e = entries.get(project_id)
    if not e:
        print(f"No registered project with id '{project_id}'.", file=sys.stderr)
        return 1
    facts = projectsync.gather(e.path)
    print(json.dumps(facts.__dict__, indent=2))
    return 0


def cmd_timeline(config: Config, args) -> int:
    entries = timeline.list_timeline(config)
    if not entries:
        print("No timeline entries yet.")
        return 0
    for entry in entries[: args.limit]:
        print(f"{entry.date}  {entry.id}  {entry.title}")
    return 0


def cmd_status(config: Config, args) -> int:
    from .indexer import connect
    print(f"Brain root:     {config.brain_root}")
    roots = ", ".join(str(r) for r in config.projects_roots) or "(not configured)"
    print(f"Projects roots: {roots}")
    print(f"Backup target:  {config.backup_target or '(not configured)'}")

    if not config.db_path.exists():
        print("\nIndex: not built yet. Run 'brain index'.")
    else:
        conn = connect(config)
        counts = conn.execute("SELECT type, COUNT(*) c FROM notes GROUP BY type ORDER BY type").fetchall()
        total = conn.execute("SELECT COUNT(*) c FROM notes").fetchone()["c"]
        conn.close()
        print(f"\nIndexed notes: {total}")
        for row in counts:
            print(f"  {row['type'] or '(none)'}: {row['c']}")

    inbox = config.inbox_dir
    pending = list(inbox.glob("*.md")) if inbox.exists() else []
    print(f"\nInbox pending triage: {len(pending)}")

    entries = load_registry(config)
    by_status: dict[str, int] = {}
    for e in entries:
        by_status[e.status or "unknown"] = by_status.get(e.status or "unknown", 0) + 1
    print(f"\nRegistered projects: {len(entries)}")
    for status_, count in sorted(by_status.items()):
        print(f"  {status_}: {count}")
    return 0


def cmd_doctor(config: Config, args) -> int:
    problems = validate.run_all(config)
    if not problems:
        print("Brain doctor: no problems found.")
        return 0
    print(f"Brain doctor: {len(problems)} problem(s) found:\n")
    by_check: dict[str, list] = {}
    for p in problems:
        by_check.setdefault(p.check, []).append(p)
    for check, items in by_check.items():
        print(f"== {check} ({len(items)}) ==")
        for item in items:
            print(f"  - {item.message}")
        print()
    return 1


def cmd_git_init(config: Config, args) -> int:
    if gitops.is_initialized(config):
        print(f"Already initialized at {config.git_dir}.")
        return 0
    gitops.init_repo(config)
    print(f"Initialized git history at {config.git_dir}")
    print(f"(working tree: {config.brain_root}, a small .git pointer file was added there)")
    print("Nothing has been committed yet — run 'brain git snapshot' when ready.")
    return 0


def cmd_git_status(config: Config, args) -> int:
    if not gitops.is_initialized(config):
        print("Git not initialized. Run 'brain git init' first.", file=sys.stderr)
        return 1
    print(gitops.status(config))
    return 0


def cmd_git_log(config: Config, args) -> int:
    if not gitops.is_initialized(config):
        print("Git not initialized. Run 'brain git init' first.", file=sys.stderr)
        return 1
    out = gitops.log(config, limit=args.limit)
    print(out if out else "No commits yet.")
    return 0


def cmd_git_snapshot(config: Config, args) -> int:
    if not gitops.is_initialized(config):
        print("Git not initialized. Run 'brain git init' first.", file=sys.stderr)
        return 1

    result = gitops.snapshot(config, message=args.message)

    if not result.committed:
        if result.reason == "blocking_doctor_problems":
            print("Snapshot REFUSED — brain doctor found blocking problems:\n")
            for p in result.blocking:
                print(f"  [{p.check}] {p.message}")
            print("\nFix these first (see 'brain doctor'), then try again.")
            return 1
        if result.reason == "nothing_to_commit":
            print("Nothing to snapshot — working tree matches the last commit.")
            if result.warnings:
                print(f"\n({len(result.warnings)} non-blocking doctor warning(s) — see 'brain doctor'.)")
            return 0

    print(f"Snapshot committed: {result.commit_hash}")
    print(f"\n{result.message}\n")
    print(result.stat.strip())
    if result.warnings:
        print(f"\n{len(result.warnings)} non-blocking doctor warning(s) — see 'brain doctor' for detail.")
    return 0


def cmd_backup_init(config: Config, args) -> int:
    settings = backup.default_settings(config)
    if not backup.credential_ready(settings):
        print(f"No backup password file at {settings.password_file}.", file=sys.stderr)
        print("Create it first (600 permissions, contents = repository password only).", file=sys.stderr)
        return 1
    initialized, detail = backup.is_initialized(settings)
    if initialized:
        print(f"Backup repository already initialized at {settings.repository()}.")
        return 0
    print(f"About to initialize an encrypted restic repository at:\n  {settings.repository()}")
    result = backup.init_repo(settings)
    backup.log_result(config, "init", result)
    if result.returncode != 0:
        print("Init FAILED:", file=sys.stderr)
        print(result.stderr.strip(), file=sys.stderr)
        return 1
    print("Repository initialized.")
    print(result.stdout.strip())
    return 0


def cmd_backup_run(config: Config, args) -> int:
    settings = backup.default_settings(config)
    if not backup.credential_ready(settings):
        print(f"No backup password file at {settings.password_file}.", file=sys.stderr)
        return 1

    problems = validate.run_all(config)
    blocking = [p for p in problems if p.check in gitops.BLOCKING_CHECKS]
    if blocking:
        print("Backup REFUSED — brain doctor found blocking problems:\n", file=sys.stderr)
        for p in blocking:
            print(f"  [{p.check}] {p.message}", file=sys.stderr)
        print("\nFix these first (see 'brain doctor'), then try again.", file=sys.stderr)
        return 1

    initialized, detail = backup.is_initialized(settings)
    if not initialized:
        print(f"Repository not initialized at {settings.repository()}. Run 'brain backup init' first.", file=sys.stderr)
        return 1

    print(f"Backing up {', '.join(backup.backup_paths(config))}")
    print(f"  -> {settings.repository()}")
    result = backup.run_backup(config, settings)
    backup.log_result(config, "run", result)
    if result.returncode != 0:
        print("Backup FAILED:", file=sys.stderr)
        print(result.stderr.strip(), file=sys.stderr)
        return 1
    print(result.stdout.strip())

    print("\nVerifying repository integrity (metadata check)...")
    check_result = backup.check_repo(settings, read_data=False)
    backup.log_result(config, "check", check_result)
    if check_result.returncode != 0:
        print("Integrity check FAILED after backup:", file=sys.stderr)
        print(check_result.stderr.strip(), file=sys.stderr)
        return 1
    print("Repository integrity OK.")
    return 0


def cmd_backup_check(config: Config, args) -> int:
    settings = backup.default_settings(config)
    if not backup.credential_ready(settings):
        print(f"No backup password file at {settings.password_file}.", file=sys.stderr)
        return 1
    result = backup.check_repo(settings, read_data=args.read_data)
    backup.log_result(config, "check", result)
    print(result.stdout.strip())
    if result.returncode != 0:
        print(result.stderr.strip(), file=sys.stderr)
        return 1
    return 0


def cmd_backup_snapshots(config: Config, args) -> int:
    settings = backup.default_settings(config)
    if not backup.credential_ready(settings):
        print(f"No backup password file at {settings.password_file}.", file=sys.stderr)
        return 1
    try:
        snaps = backup.list_snapshots(settings)
    except backup.BackupError as exc:
        print(f"Could not list snapshots: {exc}", file=sys.stderr)
        return 1
    if not snaps:
        print("No snapshots yet.")
        return 0
    for s in snaps:
        print(f"{s.short_id}  {s.time[:19]}  tags={','.join(s.tags) or '-'}")
        for p in s.paths:
            print(f"    {p}")
    return 0


def cmd_backup_restore(config: Config, args) -> int:
    settings = backup.default_settings(config)
    if not backup.credential_ready(settings):
        print(f"No backup password file at {settings.password_file}.", file=sys.stderr)
        return 1
    target = Path(args.target)
    if target == config.brain_root or str(target).startswith(str(config.brain_root)):
        print("Refusing to restore over the live Brain — pass a separate --target directory "
              f"(e.g. /tmp/{paths.APP_DIRNAME}-restore-test/) and copy files back manually after inspecting them.",
              file=sys.stderr)
        return 1
    try:
        result = backup.restore_snapshot(settings, args.snapshot_id, target, include=args.include)
    except backup.BackupError as exc:
        print(f"Restore refused: {exc}", file=sys.stderr)
        return 1
    backup.log_result(config, "restore", result)
    print(result.stdout.strip())
    if result.returncode != 0:
        print(result.stderr.strip(), file=sys.stderr)
        return 1
    print(f"\nRestored into {target} — inspect it there before touching the live Brain.")
    return 0


def cmd_backup_retention(config: Config, args) -> int:
    settings = backup.default_settings(config)
    if not backup.credential_ready(settings):
        print(f"No backup password file at {settings.password_file}.", file=sys.stderr)
        return 1
    policy = backup.RETENTION_POLICY
    print(f"Applying retention: keep-daily={policy['daily']} keep-weekly={policy['weekly']} "
          f"keep-monthly={policy['monthly']}" + (" (dry run)" if args.dry_run else ""))
    result = backup.forget_and_prune(settings, dry_run=args.dry_run)
    backup.log_result(config, "retention", result)
    print(result.stdout.strip())
    if result.returncode != 0:
        print(result.stderr.strip(), file=sys.stderr)
        return 1
    return 0


def cmd_backup_last(config: Config, args) -> int:
    settings = backup.default_settings(config)
    if not backup.credential_ready(settings):
        print(f"No backup password file at {settings.password_file}.", file=sys.stderr)
        return 1
    try:
        snap = backup.latest_snapshot(settings)
    except backup.BackupError as exc:
        print(f"Could not reach the backup repository: {exc}", file=sys.stderr)
        return 1
    if not snap:
        print("No snapshots yet.")
        return 0
    print(f"Latest snapshot: {snap.short_id}")
    print(f"Time:            {snap.time[:19]}")
    print(f"Tags:            {','.join(snap.tags) or '-'}")
    print("Paths:")
    for p in snap.paths:
        print(f"  {p}")
    return 0


def cmd_backup_status(config: Config, args) -> int:
    settings = backup.default_settings(config)
    print(f"Repository:      {settings.repository()}")
    print(f"Credential file: {settings.password_file}  "
          f"({'present' if backup.credential_ready(settings) else 'MISSING'})")

    if backup.credential_ready(settings):
        try:
            reachable, detail = backup.is_initialized(settings)
        except Exception as exc:  # network/tooling issues shouldn't crash status
            reachable, detail = False, str(exc)
        print(f"Reachable:       {reachable} ({detail})")
        if reachable:
            try:
                snap = backup.latest_snapshot(settings)
            except backup.BackupError as exc:
                snap = None
                print(f"Latest snapshot: could not list ({exc})")
            if snap:
                print(f"Latest snapshot: {snap.short_id}  ({snap.time[:19]})")

    policy = backup.RETENTION_POLICY
    print(f"\nRetention policy: keep-daily={policy['daily']} keep-weekly={policy['weekly']} "
          f"keep-monthly={policy['monthly']}")

    print("\nScheduled timers (systemd --user):")
    for unit in systemdstatus.TIMER_UNITS:
        st = systemdstatus.timer_status(unit)
        print(f"  {st.unit}: enabled={st.enabled} active={st.active}")

    return 0


def cmd_backup_schedule(config: Config, args) -> int:
    policy = backup.RETENTION_POLICY
    print("Configured schedule (see 90_SYSTEM/systemd/):")
    print(f"  {systemdstatus.BACKUP_TIMER}:      daily backup, Persistent=true")
    print(f"  {systemdstatus.MAINTENANCE_TIMER}: weekly retention (forget/prune) + repository check, Persistent=true")
    print(f"\nRetention policy: keep-daily={policy['daily']} keep-weekly={policy['weekly']} "
          f"keep-monthly={policy['monthly']}")
    print("\nLive systemd --user state:")
    print(systemdstatus.list_timers_raw())
    return 0


def cmd_init(args) -> int:
    """First run. Takes no Config: the vault does not exist yet."""
    from . import init as init_mod

    if args.demo:
        result = init_mod.initialise_demo(args.path)
    else:
        result = init_mod.initialise(
            args.path, with_git=args.git, force=args.force)

    if result.already_initialised and not result.created:
        print(f"Already initialised: {result.vault}")
    else:
        print(f"{'Completed' if result.already_initialised else 'Initialised'} "
              f"vault: {result.vault}\n")
        for item in result.created:
            print(f"  created  {item}")
    for item in result.skipped:
        print(f"  kept     {item}")
    for item in result.locations:
        print(f"  uses     {item}")

    config = default_config(result.vault)
    problems = validate.run_all(config)
    print()
    if problems:
        print(f"brain doctor found {len(problems)} problem(s) in the new vault:")
        for p in problems:
            print(f"  {p}")
        return 1
    print("brain doctor: no problems found.")

    if args.demo:
        print(f"\nNext: `brain --vault {result.vault} index`, then "
              f"`brain --vault {result.vault} search observatory`.")
    else:
        print("\nNext: `brain remember --type fact --title \"...\"` to capture "
              "something, then `brain index`.")
    return 0


def cmd_agents_doc(config: Config, args) -> int:
    from . import agentsdoc

    rendered = agentsdoc.render(config)
    if not args.write:
        print(rendered, end="")
        return 0

    dest = agentsdoc.output_path(config)
    if dest.exists() and not args.force:
        print(f"{dest} already exists — pass --force to overwrite.", file=sys.stderr)
        return 1
    dest.write_text(rendered, encoding="utf-8")
    print(f"Wrote {dest}")
    return 0


def cmd_integrity(config: Config, args) -> int:
    report = integrity.run(config, check_backup=not args.skip_backup_check)

    print(f"Generated: {report.generated_at}")
    print(f"\nTotal notes: {report.total_notes}")
    for t, c in sorted(report.note_count_by_type.items()):
        print(f"  {t}: {c}")

    print(f"\nManifest: {len(report.manifest)} file(s) hashed (sha256)")
    print(f"Doctor problems: {len(report.problems)}  "
          f"(duplicate_ids={report.duplicate_ids}, broken_links={report.broken_links}, "
          f"registry_errors={report.registry_errors})")

    print(f"\nGit history: {'initialized' if report.git_initialized else 'NOT initialized'}")
    print(f"Last git snapshot: {report.last_git_snapshot or 'never'}")

    if report.backup_checked:
        if report.backup_repo_reachable is None:
            print(f"\nBackup repository: could not check ({report.backup_repo_detail})")
        elif report.backup_repo_reachable:
            print(f"\nBackup repository: reachable ({report.backup_repo_detail})")
            print(f"Last successful backup: {report.last_backup_date or 'no snapshots yet'}")
        else:
            print(f"\nBackup repository: NOT reachable/initialized ({report.backup_repo_detail})")
    else:
        print("\nBackup repository: skipped (--skip-backup-check)")

    if args.save:
        path = integrity.save_manifest(config, report)
        print(f"\nManifest saved to {path.relative_to(config.brain_root)}")

    return 1 if report.problems else 0


def cmd_memory_add(config: Config, args) -> int:
    try:
        entry = memoryqueue.add(
            config, candidate_fact=args.fact, entities=args.entities,
            source=args.source or "", source_date=args.source_date or "",
            proposed_destination=args.destination or "", proposed_type=args.type,
            sensitivity=args.sensitivity, confidence=args.confidence, reason=args.reason or "",
        )
    except memoryqueue.MemoryQueueError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 1
    print(f"Queued as {entry.id} (status: pending). Review with 'brain memory pending'.")
    return 0


def cmd_memory_pending(config: Config, args) -> int:
    entries = memoryqueue.list_pending(config)
    if not entries:
        print("No pending memory items.")
        return 0
    for e in entries:
        print(f"{e.id}  [{e.sensitivity}/{e.confidence}]  {e.candidate_fact[:80]}")
    return 0


def cmd_memory_review(config: Config, args) -> int:
    entries = memoryqueue.list_all(config)
    if not entries:
        print("Pending memory queue is empty.")
        return 0
    for e in entries:
        print(f"id:                    {e.id}")
        print(f"status:                {e.status}")
        print(f"candidate_fact:        {e.candidate_fact}")
        print(f"entities:              {', '.join(e.entities) or '-'}")
        print(f"source:                {e.source or '-'}")
        print(f"source_date:           {e.source_date or '-'}")
        print(f"proposed_destination:  {e.proposed_destination or '-'}")
        print(f"proposed_type:         {e.proposed_type}")
        print(f"sensitivity:           {e.sensitivity}")
        print(f"confidence:            {e.confidence}")
        print(f"reason:                {e.reason or '-'}")
        if e.status != "pending":
            print(f"resolved_at:           {e.resolved_at}")
            print(f"written_to:            {e.written_to or '-'}")
        print()
    return 0


def cmd_memory_accept(config: Config, args) -> int:
    try:
        entry = memoryqueue.accept(config, args.id, title=args.title)
    except memoryqueue.MemoryQueueError as exc:
        print(f"Could not accept: {exc}", file=sys.stderr)
        return 1
    print(f"Accepted {entry.id} -> wrote {entry.written_to}")
    print("Run 'brain index' to make it searchable.")
    return 0


def cmd_memory_reject(config: Config, args) -> int:
    try:
        entry = memoryqueue.reject(config, args.id, note=args.note or "")
    except memoryqueue.MemoryQueueError as exc:
        print(f"Could not reject: {exc}", file=sys.stderr)
        return 1
    print(f"Rejected {entry.id}.")
    return 0


def cmd_handoff_write(config: Config, args) -> int:
    payload = json.load(sys.stdin)
    sections = handoff.HandoffSections(
        attempted=payload.get("attempted", ""),
        changed=payload.get("changed", ""),
        working_state=payload.get("working_state", ""),
        unresolved=payload.get("unresolved", ""),
        next_action=payload.get("next_action", ""),
        files_changed=payload.get("files_changed", []),
        decisions=payload.get("decisions", []),
    )
    try:
        path = handoff.write(config, args.project, sections)
    except handoff.HandoffError as exc:
        print(f"Could not write handoff: {exc}", file=sys.stderr)
        return 1
    print(f"Handoff written: {path.relative_to(config.brain_root)}")
    return 0


def cmd_handoff_show(config: Config, args) -> int:
    latest = handoff.read_latest(config, args.project)
    if latest is None:
        print(f"No handoff exists yet for '{args.project}'.")
        return 0
    print(latest)
    return 0


def cmd_handoff_list(config: Config, args) -> int:
    ids = handoff.list_projects_with_handoffs(config)
    if not ids:
        print("No project has a handoff yet.")
        return 0
    for pid in ids:
        print(pid)
    return 0


def _warn_if_index_stale(config: Config) -> None:
    """`brain ask` relies on the FTS5 index — warn (don't block) if a
    Markdown file is newer than the index, since that's a real, easy-to-hit
    failure mode ("Brain index stale" per the Phase 5C spec)."""
    if not config.db_path.exists():
        print("Note: no search index found yet — run 'brain index' first for useful results.", file=sys.stderr)
        return
    db_mtime = config.db_path.stat().st_mtime
    newest_md = 0.0
    for path in frontmatter_iter_safe(config):
        try:
            newest_md = max(newest_md, path.stat().st_mtime)
        except OSError:
            continue
    if newest_md > db_mtime:
        print("Note: the Brain search index looks stale (a note changed after the last 'brain index'). "
              "Results may miss recent edits — consider running 'brain index'.", file=sys.stderr)


def frontmatter_iter_safe(config: Config):
    from . import frontmatter as frontmatter_mod
    return frontmatter_mod.iter_markdown_files(config.brain_root, config.content_dirs)


def cmd_ask(config: Config, args) -> int:
    _warn_if_index_stale(config)

    try:
        result = assistant.ask(
            args.question, model=args.model, limit=args.limit,
            include_restricted=args.restricted, brain_root=config.brain_root,
        )
    except assistant.ModelNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except assistant.OllamaError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except BrainGatewayError as exc:
        print(f"Error talking to the Brain gateway: {exc}", file=sys.stderr)
        return 1

    if args.show_context:
        print("--- Retrieved context ---")
        for n in result.sources:
            currency = "current" if n.get("is_current") else "historical"
            print(f"  [{currency}] {n['id']}: {n['title']}")
        if result.context.get("projects"):
            print("  Projects:", ", ".join(p["id"] for p in result.context["projects"]))
        if result.context.get("timeline"):
            print("  Timeline:", ", ".join(t["id"] for t in result.context["timeline"]))
        print()

    print(result.answer)

    if not result.no_context:
        print()
        print(assistant.format_sources(result.sources))

    if result.restricted_used:
        print()
        print("(Restricted Brain context was included in this answer.)")

    if result.queued_memory:
        print()
        print(f"Queued pending-memory candidate: {result.queued_memory.get('queued_id')} "
              f"(review with 'brain memory review')")

    return 0


def cmd_chat(config: Config, args) -> int:
    assistant.run_chat(model=args.model, include_restricted=args.restricted, brain_root=config.brain_root)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="brain", description=f"{paths.APP_NAME} CLI")
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}",
        help="Print the version and exit.",
    )
    parser.add_argument(
        "--vault", metavar="PATH", default=None,
        help="Vault to operate on. Overrides BRAIN_ROOT, an enclosing vault, "
             "and the per-user default. Must come before the subcommand.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("index", help="Rebuild the SQLite FTS5 search index from Markdown")

    p_search = sub.add_parser("search", help="Full-text search across the vault")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=20)

    p_context = sub.add_parser(
        "context",
        help="Ranked, provenance-carrying context for a question (use --json for agents)",
    )
    p_context.add_argument("query")
    p_context.add_argument("--limit", type=int, default=10)
    p_context.add_argument("--json", action="store_true", help="Emit JSON on stdout")
    p_context.add_argument("--restricted", action="store_true",
                           help="Include sensitivity: restricted notes (excluded by default)")

    p_get = sub.add_parser("get", help="Print a note by id")
    p_get.add_argument("id")

    p_remember = sub.add_parser("remember", help="Raw capture into 00_INBOX (deterministic; the /remember skill decides dedupe/placement)")
    # No `choices=`: the parser is built before a vault is resolved, so it
    # cannot know a vault's configured note types (OSS-2/C4). Validated against
    # config.vocabulary in capture.capture(), which reports the real list.
    p_remember.add_argument("--type", required=True,
                            metavar="TYPE", help=f"note type (default vocabulary: {', '.join(sorted(capture.ALLOWED_TYPES))})")
    p_remember.add_argument("--title", required=True)
    p_remember.add_argument("--text", default="")
    p_remember.add_argument("--tags", nargs="*", default=[])
    p_remember.add_argument("--people", nargs="*", default=[])
    p_remember.add_argument("--projects", nargs="*", default=[])
    p_remember.add_argument("--sensitivity", default="normal", choices=["normal", "private", "restricted"])
    p_remember.add_argument("--confidence", default="fact", choices=["fact", "assumption", "opinion"])
    p_remember.add_argument("--source", default="")

    sub.add_parser("projects", help="List registered projects")

    p_project = sub.add_parser("project", help="Show / discover / sync projects")
    p_project.add_argument("target", help="A project id, or 'discover', or 'sync <id>'")
    p_project.add_argument("sync_id", nargs="?", help="Project id, only used with 'sync'")

    p_timeline = sub.add_parser("timeline", help="List timeline entries, newest first")
    p_timeline.add_argument("--limit", type=int, default=50)

    sub.add_parser("status", help="Vault overview: counts, inbox, projects")
    sub.add_parser("doctor", help="Run health checks")

    p_git = sub.add_parser("git", help="Local git version history (init / status / snapshot / log)")
    git_sub = p_git.add_subparsers(dest="git_command", required=True)
    git_sub.add_parser("init", help="Initialize git history (separate git-dir outside Nextcloud)")
    git_sub.add_parser("status", help="git status")
    p_git_snapshot = git_sub.add_parser("snapshot", help="Doctor-gated commit of current changes")
    p_git_snapshot.add_argument("--message", "-m", default=None, help="Custom commit message (default: auto-generated)")
    p_git_log = git_sub.add_parser("log", help="Show recent snapshots")
    p_git_log.add_argument("--limit", type=int, default=20)

    p_backup = sub.add_parser("backup", help="Encrypted versioned backup (restic) — init / run / check / snapshots / restore")
    backup_sub = p_backup.add_subparsers(dest="backup_command", required=True)
    backup_sub.add_parser("init", help="Initialize the encrypted repository (does not back up data)")
    backup_sub.add_parser("run", help="Doctor-gated encrypted backup + post-backup integrity check")
    p_backup_check = backup_sub.add_parser("check", help="Verify repository integrity")
    p_backup_check.add_argument("--read-data", action="store_true", help="Deep check: verify actual pack contents, not just metadata (slow)")
    backup_sub.add_parser("snapshots", help="List backup snapshots")
    p_backup_restore = backup_sub.add_parser("restore", help="Restore a snapshot into a separate target directory")
    p_backup_restore.add_argument("snapshot_id")
    p_backup_restore.add_argument("--target", required=True, help="Destination directory (must NOT be the live Brain)")
    p_backup_restore.add_argument("--include", default=None, help="Restrict restore to files matching this path pattern")
    p_backup_retention = backup_sub.add_parser("retention", help="Apply keep-daily/weekly/monthly retention and prune (run weekly, not per-backup)")
    p_backup_retention.add_argument("--dry-run", action="store_true", help="Show what would be removed without actually removing it")
    backup_sub.add_parser("last", help="Show the most recent snapshot")
    backup_sub.add_parser("status", help="Backup dashboard: reachability, latest snapshot, retention policy, timer status")
    backup_sub.add_parser("schedule", help="Show the configured backup/maintenance schedule and live systemd timer state")

    p_init = sub.add_parser(
        "init", help="Create a new vault (first run), or complete a partial one")
    p_init.add_argument("path", help="Where the vault should live. Never inferred.")
    p_init.add_argument("--demo", action="store_true",
                        help="Create a synthetic example vault with sample content "
                             "instead of an empty one. Fictional; safe to delete.")
    # Deprecated since 0.2.0: AGENTS.md is no longer generated by default, so
    # this flag has nothing left to switch off. Still accepted, and deliberately
    # kept, so scripts written against 0.1.0 keep working unchanged.
    p_init.add_argument("--no-agents", action="store_true",
                        help="Deprecated and ignored: AGENTS.md is no longer "
                             "generated by default. Accepted for compatibility "
                             "with 0.1.0 scripts. Use `brain agents-doc --write` "
                             "to create one.")
    p_init.add_argument("--git", action="store_true",
                        help="Also initialise git version history (optional; the "
                             "vault works without it)")
    p_init.add_argument("--force", action="store_true",
                        help="Fill in anything missing from an existing vault. "
                             "Never overwrites existing files.")

    p_agents = sub.add_parser("agents-doc", help="Render this vault's AGENTS.md from the shipped template")
    p_agents.add_argument("--write", action="store_true", help=f"Write it to the vault root instead of printing")
    p_agents.add_argument("--force", action="store_true", help="With --write, overwrite an existing file")

    p_integrity = sub.add_parser("integrity", help="Point-in-time health/manifest report (not authoritative over Markdown)")
    p_integrity.add_argument("--skip-backup-check", action="store_true", help="Don't contact the backup repository (faster, offline-friendly)")
    p_integrity.add_argument("--save", action="store_true", help="Also save a JSON manifest under the state directory's integrity/")

    p_memory = sub.add_parser("memory", help="Pending-memory queue (Level 2 write-policy staging) — add / pending / review / accept / reject")
    memory_sub = p_memory.add_subparsers(dest="memory_command", required=True)
    p_memory_add = memory_sub.add_parser("add", help="Queue a candidate fact for later review (does not write it into the Brain yet)")
    p_memory_add.add_argument("--fact", required=True, help="The candidate fact, in plain language")
    p_memory_add.add_argument("--entities", nargs="*", default=[], help="person-*/project-* ids this fact relates to")
    p_memory_add.add_argument("--source", default="", help="Where this came from, e.g. 'user, 2026-07-27 conversation'")
    p_memory_add.add_argument("--source-date", default="", help="Date the source evidence is dated (defaults to today)")
    p_memory_add.add_argument("--destination", default="", help="Human-readable hint for where this should live if accepted")
    p_memory_add.add_argument("--type", default="fact", metavar="TYPE",
                              help=f"Brain note type to use if accepted (default vocabulary: {', '.join(sorted(capture.ALLOWED_TYPES))})")
    p_memory_add.add_argument("--sensitivity", default="normal", choices=["normal", "private", "restricted"])
    p_memory_add.add_argument("--confidence", default="assumption", choices=["fact", "assumption", "opinion"])
    p_memory_add.add_argument("--reason", default="", help="Why this might be worth remembering")
    memory_sub.add_parser("pending", help="List open (pending) items")
    memory_sub.add_parser("review", help="Show full detail for every item, including resolved ones")
    p_memory_accept = memory_sub.add_parser("accept", help="Write a pending item into the Brain")
    p_memory_accept.add_argument("id")
    p_memory_accept.add_argument("--title", default=None, help="Override the auto-derived note title")
    p_memory_reject = memory_sub.add_parser("reject", help="Discard a pending item (kept in the queue for audit history)")
    p_memory_reject.add_argument("id")
    p_memory_reject.add_argument("--note", default="", help="Why it was rejected")

    p_handoff = sub.add_parser("handoff", help="Project session handoff — write / show / list")
    handoff_sub = p_handoff.add_subparsers(dest="handoff_command", required=True)
    p_handoff_write = handoff_sub.add_parser("write", help="Prepend a new session section (reads a JSON payload from stdin)")
    p_handoff_write.add_argument("--project", required=True, help="Registered project id")
    p_handoff_show = handoff_sub.add_parser("show", help="Print the most recent session's handoff section")
    p_handoff_show.add_argument("project")
    handoff_sub.add_parser("list", help="List projects that have a handoff document")

    p_ask = sub.add_parser("ask", help="Ask a question grounded in Brain context, answered by a local Ollama model")
    p_ask.add_argument("question")
    p_ask.add_argument("--model", default=assistant.DEFAULT_MODEL, help=f"Ollama model to use (default: {assistant.DEFAULT_MODEL})")
    p_ask.add_argument("--restricted", action="store_true", help="Include sensitivity:restricted notes in retrieval (off by default)")
    p_ask.add_argument("--show-context", action="store_true", help="Print the retrieved context before the answer (diagnostics)")
    p_ask.add_argument("--limit", type=int, default=assistant.DEFAULT_CONTEXT_LIMIT, help="Max notes to retrieve (context budget)")

    p_chat = sub.add_parser("chat", help="Interactive Brain-grounded chat session (in-memory only, nothing persisted)")
    p_chat.add_argument("--model", default=assistant.DEFAULT_MODEL)
    p_chat.add_argument("--restricted", action="store_true", help="Start with restricted-context retrieval enabled")

    return parser


# Domain errors that carry a message written for a human. Raised anywhere in a
# command's call tree, they should reach the user as that message and a
# non-zero exit — never as a traceback (OSS-3).
#
# Found by running the CLI against a synthetic first-run vault: on this
# machine a backup destination and a git identity always exist, so three
# `brain backup` commands and `brain git snapshot` printed a stack trace on
# top of a perfectly good error message for anyone who had not configured them
# yet. `backup snapshots` and `brain integrity` already handled their own case,
# which is the pattern generalised here.
USER_FACING_ERRORS = (
    paths_mod.VaultNotFoundError,
    init_mod.InitError,
    paths_mod.TaxonomyError,
    paths_mod.VocabularyError,
    backup.BackupError,
    gitops.GitError,
    handoff.HandoffError,
)


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # `init` is the one command that must run without a resolvable vault —
    # it is what creates one.
    if args.command == "init":
        try:
            return cmd_init(args)
        except USER_FACING_ERRORS as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    try:
        config = default_config(args.vault)
    except USER_FACING_ERRORS as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    try:
        return _dispatch(config, args, parser)
    except USER_FACING_ERRORS as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def _dispatch(config: Config, args, parser) -> int:
    if args.command == "index":
        return cmd_index(config, args)
    if args.command == "search":
        return cmd_search(config, args)
    if args.command == "context":
        return cmd_context(config, args)
    if args.command == "get":
        return cmd_get(config, args)
    if args.command == "remember":
        return cmd_remember(config, args)
    if args.command == "projects":
        return cmd_projects(config, args)
    if args.command == "project":
        if args.target == "discover":
            return cmd_project_discover(config, args)
        if args.target == "sync":
            if not args.sync_id:
                print("usage: brain project sync <id>", file=sys.stderr)
                return 2
            return cmd_project_sync(config, args.sync_id)
        return cmd_project_show(config, args.target)
    if args.command == "timeline":
        return cmd_timeline(config, args)
    if args.command == "status":
        return cmd_status(config, args)
    if args.command == "doctor":
        return cmd_doctor(config, args)
    if args.command == "git":
        if args.git_command == "init":
            return cmd_git_init(config, args)
        if args.git_command == "status":
            return cmd_git_status(config, args)
        if args.git_command == "snapshot":
            return cmd_git_snapshot(config, args)
        if args.git_command == "log":
            return cmd_git_log(config, args)
    if args.command == "backup":
        if args.backup_command == "init":
            return cmd_backup_init(config, args)
        if args.backup_command == "run":
            return cmd_backup_run(config, args)
        if args.backup_command == "check":
            return cmd_backup_check(config, args)
        if args.backup_command == "snapshots":
            return cmd_backup_snapshots(config, args)
        if args.backup_command == "restore":
            return cmd_backup_restore(config, args)
        if args.backup_command == "retention":
            return cmd_backup_retention(config, args)
        if args.backup_command == "last":
            return cmd_backup_last(config, args)
        if args.backup_command == "status":
            return cmd_backup_status(config, args)
        if args.backup_command == "schedule":
            return cmd_backup_schedule(config, args)
    if args.command == "agents-doc":
        return cmd_agents_doc(config, args)
    if args.command == "integrity":
        return cmd_integrity(config, args)
    if args.command == "memory":
        if args.memory_command == "add":
            return cmd_memory_add(config, args)
        if args.memory_command == "pending":
            return cmd_memory_pending(config, args)
        if args.memory_command == "review":
            return cmd_memory_review(config, args)
        if args.memory_command == "accept":
            return cmd_memory_accept(config, args)
        if args.memory_command == "reject":
            return cmd_memory_reject(config, args)
    if args.command == "handoff":
        if args.handoff_command == "write":
            return cmd_handoff_write(config, args)
        if args.handoff_command == "show":
            return cmd_handoff_show(config, args)
        if args.handoff_command == "list":
            return cmd_handoff_list(config, args)
    if args.command == "ask":
        return cmd_ask(config, args)
    if args.command == "chat":
        return cmd_chat(config, args)

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
