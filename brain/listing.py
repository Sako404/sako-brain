"""Complete, deterministic note listing by live metadata.

Synthetic example: a fictional vault has six proposed decisions. A ranked
text search for "status: proposed" returns only four, including a decided
record whose body quotes the phrase. Metadata filtering avoids that mismatch.

Discovery scans canonical Markdown, not the rebuildable search index.
Filters use live frontmatter; ACL checks run before metadata validation or
materialization. Known hidden records affect neither entries nor counters.
Unverifiable visibility and filesystem traversal failures prevent a claim of
source completeness without exposing their paths, identities or counts.

`source_complete` describes source verification, `page_complete` describes
whether entries contain every match, and `complete` is their conjunction.
Ordering is updated descending, then id ascending. Reads are not a snapshot:
concurrent changes can require restarting pagination.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import frontmatter
from . import visibility
from .paths import Config

# The order every caller gets, named once so the CLI, the MCP tools and the
# tests all describe it the same way.
SORT_DESCRIPTION = "updated descending, then id ascending"

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000


class ListingError(ValueError):
    """A listing request that cannot be answered as asked — an unknown
    filter value is refused rather than silently returning nothing."""


@dataclass
class ListedNote:
    id: str
    type: str
    status: str
    title: str
    path: str
    updated: str
    projects: list = field(default_factory=list)
    tags: list = field(default_factory=list)


@dataclass
class ListResult:
    """`entries` is one page; the three completeness fields describe how much
    of the truth it represents.

    - `source_complete` — the scan accounted for every candidate file this
      caller may see. False when such a file carried a frontmatter block
      that would not parse, could not be decoded, or parsed without an
      `id`; `unreadable` counts those. Raw files without metadata also make
      the source incomplete and are counted in `unstructured_skipped`.
      Like `total_visible`, both counters
      are scoped to the caller: a file belonging to another principal never
      moves them, so a count can never be used to infer what someone else
      holds. Traversal failures and files whose visibility cannot be
      established also make the source incomplete, without adding paths or
      counts to the response. A parseable ACL that denies access is a known
      refusal, not an unknown-visibility fault.
    - `page_complete` — `entries` holds every match, not a window into them.
      False for any `offset > 0` and for any `limit` that cut the result,
      INCLUDING the final page and an offset past the last match: a page is
      not the whole answer even when there is nothing after it.
    - `complete` — both of the above. The only field that means "this is the
      full list"; never true merely because the request succeeded.
    """
    entries: list = field(default_factory=list)  # list[ListedNote]
    total_visible: int = 0
    returned: int = 0
    offset: int = 0
    limit: int = DEFAULT_LIMIT
    complete: bool = True
    page_complete: bool = True
    source_complete: bool = True
    unreadable: int = 0
    unstructured_skipped: int = 0
    next_offset: int | None = None
    filters: dict = field(default_factory=dict)
    sort: str = SORT_DESCRIPTION
    scope: str = ""


def _validate_status(config: Config, type_: str | None, status: str | None) -> None:
    """Only ever checks against the vault's OWN configured vocabulary — this
    module must not invent status words, and must not let a caller invent
    them either."""
    if not status:
        return
    if not type_:
        known = {s for _, statuses in config.vocabulary.status_vocabularies for s in statuses}
        if known and status not in known:
            raise ListingError(
                f"unknown status '{status}' — this vault's configured statuses are "
                f"{sorted(known)}; pass --type to narrow it to one type's vocabulary"
            )
        return
    allowed = config.vocabulary.statuses_for(type_)
    if allowed and status not in allowed:
        raise ListingError(
            f"unknown status '{status}' for type '{type_}' — allowed: {list(allowed)}"
        )


def _validate_type(config: Config, type_: str | None) -> None:
    if not type_:
        return
    if type_ not in config.vocabulary.note_types:
        raise ListingError(
            f"unknown type '{type_}' — this vault's configured types are "
            f"{sorted(config.vocabulary.note_types)}"
        )


def _has_frontmatter_block(path: Path) -> bool:
    """True when this file at least *claims* to be a structured note.

    A raw capture with no frontmatter at all is the normal shape of an
    untriaged inbox file, and `00_INBOX` is part of `content_dirs` — so
    they are counted separately as `unstructured_skipped`, rather than as
    malformed notes. They still prevent a claim of source completeness:
    there is no metadata with which to evaluate the requested filters.

    Decoded with the same `utf-8-sig` as `frontmatter.parse_file`, and with
    a leading BOM stripped either way: a BOM made a real note look like a
    raw capture here, which is how a perfectly good note disappeared from a
    listing that still called itself complete. A file this cannot decode at
    all is NOT a raw capture — the honest answer is "claims to be a note and
    cannot be read as one", so it lands in the `unreadable` bucket.
    """
    try:
        with path.open(encoding="utf-8-sig") as handle:
            first = handle.readline()
    except (OSError, UnicodeDecodeError):
        return True  # cannot be read at all — the caller's `unreadable` bucket
    return first.lstrip(frontmatter.BOM).strip() == frontmatter.FRONTMATTER_DELIM


def _listing_fields(note: frontmatter.Note) -> tuple[list[str], list[str]]:
    """Validate only the metadata this listing consumes, after authorization.

    Do not coerce mappings/numbers into lists or stringify arbitrary YAML:
    doing so can invent filter matches and make malformed data look valid.
    YAML dates are supported, as are scalar strings for list fields.
    """
    if not isinstance(note.id, str) or not note.id.strip():
        raise ValueError("invalid id")
    for key in ("type", "status"):
        value = note.meta.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError("invalid scalar metadata")
    updated = note.meta.get("updated")
    if updated is not None and not isinstance(updated, (str, date)):
        raise ValueError("invalid updated metadata")
    lists = []
    for key in ("projects", "tags"):
        value = note.meta.get(key)
        if value is None:
            value = []
        elif isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError("invalid list metadata")
        lists.append(value)
    return lists[0], lists[1]


def list_notes(config: Config, *, type: str | None = None, status: str | None = None,
                project: str | None = None, tag: str | None = None,
                limit: int = DEFAULT_LIMIT, offset: int = 0,
                principal_id: str | None = None) -> ListResult:
    """Every note matching the given metadata filters that `principal_id`
    may see, in a deterministic order, with an honest count.

    Reads only canonical Markdown: both discovery and filtering come from
    the files themselves, so a note written a second ago is listed now and
    a note deleted a second ago is not, with no reindex in between.
    """
    if principal_id is None:
        principal_id = config.acting_principal
    if limit < 0:
        raise ListingError("limit must be >= 0")
    if offset < 0:
        raise ListingError("offset must be >= 0")
    limit = min(limit, MAX_LIMIT)

    _validate_type(config, type)
    _validate_status(config, type, status)

    filters = {"type": type or None, "status": status or None,
               "project": project or None, "tag": tag or None}
    scope = f"visible-to:{principal_id}"

    matched: list[ListedNote] = []
    unreadable = 0
    unstructured = 0
    unverified = False

    def scan_error(_error):
        nonlocal unverified
        # No paths, counts or raw OS diagnostics cross the principal boundary.
        unverified = True

    def visible_metadata(meta):
        nonlocal unverified
        try:
            visibility.owner_and_audience(meta)
        except visibility.VisibilityError:
            # A malformed ACL is unknown visibility, not a verified denial.
            unverified = True
            return False
        return visibility.can_view(config, principal_id, meta)

    # Counters use the same ACL as entries. On a parse failure, recover only
    # diagnostic header metadata: a bad body must not erase a hidden owner.
    # An untrustworthy ACL contributes no principal-visible information.
    for path in frontmatter.iter_markdown_files(
            config.brain_root, config.content_dirs, on_error=scan_error):
        try:
            note = frontmatter.parse_file(path)
        except (frontmatter.FrontmatterError, OSError):
            meta = frontmatter.diagnostic_metadata(path)
            if meta is None:
                unverified = True
                continue
            if not visible_metadata(meta):
                continue
            if _has_frontmatter_block(path):
                # Claims to be a note and isn't readable as one: a real hole
                # in this listing, reported rather than quietly dropped.
                unreadable += 1
            else:
                unstructured += 1
            continue
        if not visible_metadata(note.meta):
            continue
        try:
            note_projects, note_tags = _listing_fields(note)
        except ValueError:
            unreadable += 1
            continue
        if type and (note.type or "") != type:
            continue
        if status and (note.status or "") != status:
            continue
        if project and project not in note_projects:
            continue
        if tag and tag not in note_tags:
            continue
        matched.append(ListedNote(
            id=note.id, type=note.type or "", status=note.status or "",
            title=note.title, path=str(path.relative_to(config.brain_root)),
            updated=str(note.meta.get("updated") or ""),
            projects=note_projects, tags=note_tags,
        ))

    # Deterministic, and deterministic for ties too: `updated` is a date, so
    # several notes routinely share one. Sorting on (updated desc, id asc)
    # leaves nothing to chance.
    matched.sort(key=lambda n: n.id)
    matched.sort(key=lambda n: n.updated, reverse=True)

    total = len(matched)
    page = matched[offset:offset + limit] if limit else []
    # A page is the whole answer only when it starts at the beginning AND
    # reaches the end. Anything else — a middle page, a final page, an offset
    # past the last match — is a window, and says so.
    page_complete = (offset == 0 and len(page) == total)
    source_complete = unreadable == 0 and unstructured == 0 and not unverified
    consumed = offset + len(page)
    return ListResult(
        entries=page, total_visible=total, returned=len(page), offset=offset,
        limit=limit,
        complete=page_complete and source_complete,
        page_complete=page_complete,
        source_complete=source_complete,
        unreadable=unreadable,
        unstructured_skipped=unstructured,
        # limit=0 is a count-only call: its page never advances, so offering
        # a next_offset would send a paging client round the same offset forever.
        next_offset=consumed if limit and consumed < total else None,
        filters=filters, scope=scope,
    )


def result_to_dict(result: ListResult) -> dict:
    """The one JSON shape `brain list --json`, the MCP tool and the bridge
    all return — so a client never has to care which transport it reached."""
    return {
        "entries": [
            {"id": e.id, "type": e.type, "status": e.status, "title": e.title,
             "path": e.path, "updated": e.updated, "projects": e.projects, "tags": e.tags}
            for e in result.entries
        ],
        "total_visible": result.total_visible,
        "returned": result.returned,
        "offset": result.offset,
        "limit": result.limit,
        "complete": result.complete,
        "page_complete": result.page_complete,
        "source_complete": result.source_complete,
        "unreadable": result.unreadable,
        "unstructured_skipped": result.unstructured_skipped,
        "next_offset": result.next_offset,
        "filters": result.filters,
        "sort": result.sort,
        "scope": result.scope,
    }
