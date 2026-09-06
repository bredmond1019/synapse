"""app/brain/ops.py — the Brain write/ops core (embed, ingest, prune, refresh, stale, routine).

Also home to `prune_queries`, the retention half of the OR.K1 query log —
deletion, never aggregation (the D51 guard).

Wraps `scripts/index_brain.py`'s incremental content-index path and the
`mev emit-graph | scripts/load_brain_edges.py::load_edges` structural-edge path
behind one set of typed functions, so `syn` (`app/brain/cli.py`) and the brain
repo's post-commit freshness hook share a single implementation. No second
chunk->embed->write path is introduced here (CLAUDE.md rule 10).

`OR.2.C` task 3: `embed_paths`/`ingest_dir`/`prune_paths`/`refresh` all run
`index_brain.main()` in-process and, before this task, discarded its return
value — a real parse/embed/DB failure never reached `syn ingest`/`syn
refresh`/`syn embed`'s exit code. `_run_index_brain` is now the single choke
point every in-process caller goes through; it returns `index_brain.main()`'s
real exit code plus its captured error summary, and every public function
here folds both into its own return payload as `exit_code`/`success`/
`errors`.
"""

import json
import logging
import os
import subprocess
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from brain import _bootstrap  # noqa: F401  pylint: disable=unused-import

logger = logging.getLogger(__name__)


class UnknownRoutineError(Exception):
    """Raised when `run_routine` is given a name absent from the registry."""

    def __init__(self, name: str) -> None:
        super().__init__(f"Unknown routine: {name!r}. Known: {sorted(ROUTINES)}")
        self.name = name


class MevUnavailableError(Exception):
    """Raised when the `mev` binary is not on PATH for an edge refresh."""


# Default retention window for `retrieval_queries`. 90 days is deliberate, not
# arbitrary: the retained window is the sample the retrieval golden set gets
# grown from (OR.K2), so tightening it below a quarter of real traffic shrinks
# that sample — do not lower it without recording the trade in the ledger.
DEFAULT_QUERY_KEEP_DAYS: int = 90

_KEEP_DAYS_ENV_VAR = "BRAIN_QUERY_LOG_KEEP_DAYS"

# The module name `index_brain.main()` logs under. `import index_brain` below
# resolves it as a top-level module (via the `scripts/` sys.path shim shared
# by every call site in this file), so `logging.getLogger(__name__)` inside
# `scripts/index_brain.py` always names this logger — pinned here rather than
# re-derived so a rename of that module would fail loudly (empty capture)
# instead of silently.
_INDEX_BRAIN_LOGGER_NAME = "index_brain"


class _IndexBrainErrorCapture(logging.Handler):
    """Captures `index_brain`'s ERROR-level log records for one `main()` call.

    `index_brain.main()` returns only an int exit code (task 1) — it has no
    second, structured error-reporting channel, and adding one would mean two
    places describing the same failure (out of scope for this task's file
    list). Every per-file failure it records is *also* `logger.error(...)`'d
    immediately before the matching `errors.append(...)` (parse, embed, DB,
    and generic-processing cases alike), so capturing ERROR-level records
    from its logger for the duration of one `main()` call reconstructs the
    same failed-path summary the log carries — without index_brain.py
    exposing anything new. Deliberately ERROR-only: the WARNING-level
    "Errors (N):" recap `main()` also logs would otherwise duplicate every
    entry captured here.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _run_index_brain(argv: list[str]) -> tuple[int, list[str]]:
    """Run `index_brain.main(argv)`, returning its exit code and error summary.

    The single choke point every in-process caller of `index_brain.main()` in
    this module goes through, so `embed_paths`, `prune_paths`, and `refresh`
    all surface the same shape rather than three call sites each discarding
    the result their own way (the bug this task exists to close — see the
    module docstring's task-3 note).

    Args:
        argv: Argument vector forwarded to `index_brain.main()`.

    Returns:
        `(exit_code, error_messages)` — `exit_code` is `index_brain.main()`'s
        real return value (0 clean, 1 on any parse/embed/DB failure);
        `error_messages` names each failure captured from its ERROR-level log
        records for this call only.
    """
    import index_brain  # pylint: disable=import-outside-toplevel,import-error

    index_logger = logging.getLogger(_INDEX_BRAIN_LOGGER_NAME)
    capture = _IndexBrainErrorCapture()
    index_logger.addHandler(capture)
    try:
        code = index_brain.main(argv)
    finally:
        index_logger.removeHandler(capture)
    return code, capture.messages


def embed_paths(paths: list[str], *, force: bool = False, brain_path: str | None = None) -> dict:
    """Re-embed exactly the named files via `index_brain`'s `--only-paths` path.

    No second chunk->embed->write implementation — this shells straight into
    `index_brain.main()`, which already carries the incremental-skip logic.

    Args:
        paths: File paths to restrict indexing to (forwarded to `--only-paths`).
        force: Disable the per-file incremental skip so the named paths fully
            re-embed, regardless of their existing `indexed_at`.
        brain_path: Optional brain root override (forwarded to `--brain-path`).

    Returns:
        A summary dict: `{"embedded": [...], "forced": bool, "exit_code": int,
        "success": bool, "errors": [...]}`. `success` is `exit_code == 0` —
        `syn embed` (`app/brain/cli.py::_run_embed`) exits non-zero on it
        rather than silently discarding what `index_brain.main()` reported.
    """
    argv: list[str] = ["--only-paths", *paths]
    if force:
        argv.append("--force")
    if brain_path:
        argv += ["--brain-path", brain_path]
    code, errors = _run_index_brain(argv)
    return {
        "embedded": list(paths),
        "forced": force,
        "exit_code": code,
        "success": code == 0,
        "errors": errors,
    }


def ingest_dir(directory: str, *, force: bool = False, brain_path: str | None = None) -> dict:
    """Index every on-disk markdown file under `directory` via the `index_brain` path.

    This is on-disk *file* indexing (frontmatter-parsing, `doc_type`
    classification, `authored_at` from mtime) — a different concern from
    `app/brain/ingest.py::ingest_artifact`'s arbitrary-content API path. Do
    not route this through the OR.Q ingest core.

    Args:
        directory: Root directory to collect `*.md` files under.
        force: Forwarded to `embed_paths` — disables the incremental skip.
        brain_path: Optional brain root override.

    Returns:
        A summary dict: `{"ingested": [...], "forced": bool, "exit_code": int,
        "success": bool, "errors": [...]}` — `exit_code`/`success`/`errors`
        are `embed_paths`'s (this function has no direct `index_brain.main()`
        call site of its own); an empty directory reports a clean `(0, True,
        [])` rather than skipping the embed step's own contract.
    """
    root = Path(directory)
    if not root.is_dir():
        raise NotADirectoryError(f"Not a directory: {directory}")

    files = [str(p) for p in sorted(root.rglob("*.md"))]
    if not files:
        return {"ingested": [], "forced": force, "exit_code": 0, "success": True, "errors": []}

    embed_result = embed_paths(files, force=force, brain_path=brain_path)
    return {
        "ingested": files,
        "forced": force,
        "exit_code": embed_result["exit_code"],
        "success": embed_result["success"],
        "errors": embed_result["errors"],
    }


def prune_paths(paths: list[str], *, dry_run: bool = False, brain_path: str | None = None) -> dict:
    """Delete `brain_documents` rows for deleted/renamed-away file paths.

    Surgical cleanup — shells into `index_brain`'s `--prune-paths` mode (no
    embedding, no API call). The single implementation shared by `syn prune`
    and the brain repo's post-commit delete/rename freshness hook, which
    previously called `scripts/index_brain.py --prune-paths` directly.

    Args:
        paths: File paths (brain-root-relative or absolute) to prune.
        dry_run: Report what would be deleted without writing.
        brain_path: Optional brain root override (forwarded to `--brain-path`).

    Returns:
        A summary dict: `{"pruned": [...], "dry_run": bool, "exit_code": int,
        "success": bool, "errors": [...]}`. `--prune-paths` exits before the
        corpus walk that can raise `DocumentParseError`, so `exit_code` is
        `0`/`success` is `True` today — captured anyway so this function's
        contract matches `embed_paths`/`refresh` rather than being the one
        exception a future caller has to special-case.
    """
    argv: list[str] = ["--prune-paths", *paths]
    if dry_run:
        argv.append("--dry-run")
    if brain_path:
        argv += ["--brain-path", brain_path]
    code, errors = _run_index_brain(argv)
    return {
        "pruned": list(paths),
        "dry_run": dry_run,
        "exit_code": code,
        "success": code == 0,
        "errors": errors,
    }


def _resolve_keep_days(keep_days: int | None) -> int:
    """Resolve the retention window: explicit argument > env var > 90.

    The env var (`BRAIN_QUERY_LOG_KEEP_DAYS`) is read at CALL time, not
    import time — mirroring `query_log._query_log_enabled`'s discipline, so a
    long-lived process (or a test using `monkeypatch.setenv`) sees changes.
    An unparsable or non-positive value never crashes a cron routine: it
    falls back to the default with a `logging.warning`.

    Args:
        keep_days: Explicit override, or None to consult the environment.

    Returns:
        A positive number of days to retain.
    """
    if keep_days is not None:
        if keep_days <= 0:
            logger.warning(
                "ignoring non-positive keep_days %s; falling back to %s",
                keep_days,
                DEFAULT_QUERY_KEEP_DAYS,
            )
            return DEFAULT_QUERY_KEEP_DAYS
        return keep_days

    raw = os.environ.get(_KEEP_DAYS_ENV_VAR)
    if raw is None or not raw.strip():
        return DEFAULT_QUERY_KEEP_DAYS

    try:
        parsed = int(raw.strip())
    except ValueError:
        logger.warning(
            "unparsable %s=%r; falling back to %s days",
            _KEEP_DAYS_ENV_VAR,
            raw,
            DEFAULT_QUERY_KEEP_DAYS,
        )
        return DEFAULT_QUERY_KEEP_DAYS

    if parsed <= 0:
        logger.warning(
            "non-positive %s=%r; falling back to %s days",
            _KEEP_DAYS_ENV_VAR,
            raw,
            DEFAULT_QUERY_KEEP_DAYS,
        )
        return DEFAULT_QUERY_KEEP_DAYS

    return parsed


def prune_queries(keep_days: int | None = None, *, dry_run: bool = False) -> dict:
    """Delete `retrieval_queries` rows older than the retention window.

    The OR.K1 query log is unbounded by design at ship time — one row per
    retrieval call, `BRAIN_QUERY_LOG_ENABLED` defaulting on. This is its
    retention half: a bounded, idempotent delete that is safe to run from
    cron (`ROUTINES["queries_prune"]`).

    Retention is **deletion, not aggregation** (the D51 guard): nothing is
    rolled up, summarized, or persisted at prune time. `syn queries` remains
    the only read surface and still computes every statistic over raw rows.

    Bounded-delete shape mirrors `_delete_orphaned_chunks` (one filtered
    delete, one commit, return the real count) rather than
    `GenericRepository.delete`, which is one row per commit. `deleted` and
    `kept` are derived from actual `GenericRepository.count()` reads around
    the delete — never from what the caller asked for.

    Args:
        keep_days: Retention window in days. `None` (the default) consults
            `BRAIN_QUERY_LOG_KEEP_DAYS`, then falls back to
            `DEFAULT_QUERY_KEEP_DAYS` (90).
        dry_run: Count what would be deleted and delete nothing. `deleted`
            then reports the would-delete count (mirroring `prune_paths`,
            whose `pruned` list is populated on a dry run too) and the
            `dry_run` flag in the return distinguishes the two cases.

    Returns:
        `{"deleted": n, "kept": m, "cutoff": iso, "keep_days": d,
        "dry_run": bool}` — rows with `created_at` exactly at the cutoff are
        KEPT (the comparison is strictly older-than).
    """
    from database.repository import GenericRepository  # pylint: disable=import-outside-toplevel
    from database.retrieval_query import RetrievalQuery  # pylint: disable=import-outside-toplevel
    from database.session import db_session  # pylint: disable=import-outside-toplevel

    resolved_days = _resolve_keep_days(keep_days)
    # `RetrievalQuery.created_at` defaults to a naive `datetime.now()`, so the
    # cutoff is computed naively too — comparing naive to aware would raise.
    cutoff = datetime.now() - timedelta(days=resolved_days)

    with next(db_session()) as session:  # type: ignore[arg-type]
        repository = GenericRepository(session=session, model=RetrievalQuery)
        stale_query = session.query(RetrievalQuery).filter(RetrievalQuery.created_at < cutoff)

        if dry_run:
            deleted = stale_query.count()
            kept = repository.count() - deleted
        else:
            before = repository.count()
            stale_query.delete(synchronize_session=False)
            session.commit()
            kept = repository.count()
            deleted = before - kept

    return {
        "deleted": deleted,
        "kept": kept,
        "cutoff": cutoff.isoformat(),
        "keep_days": resolved_days,
        "dry_run": dry_run,
    }


def refresh_edges(brain_path: Path) -> int:
    """Run `mev emit-graph --json <brain_path>` and load the payload into `brain_edges`.

    Moved here from `scripts/refresh_brain.py` (task 2) so `syn refresh`, the
    `refresh_brain.py` shim, and `syn routine refresh` share one edge-reload
    implementation.

    Args:
        brain_path: Path to the brain repo root to crawl.

    Returns:
        The number of edge rows loaded.

    Raises:
        MevUnavailableError: the `mev` binary is not on PATH.
    """
    from load_brain_edges import load_edges  # pylint: disable=import-outside-toplevel,import-error

    try:
        result = subprocess.run(
            ["mev", "emit-graph", "--json", str(brain_path)],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise MevUnavailableError("`mev` binary not found on PATH") from exc

    payload = json.loads(result.stdout)

    from database.session import db_session  # pylint: disable=import-outside-toplevel

    with next(db_session()) as session:  # type: ignore[arg-type]
        return load_edges(payload, session)


def refresh(*, rebuild: bool = False, dry_run: bool = False, brain_path: str | None = None) -> dict:
    """Run the content-index step then the edge-reload step, in that order.

    Supersedes `scripts/refresh_brain.py`'s `main()` sequencing — one
    invocation reproduces both freshness paths. `--dry-run` skips the edge
    step entirely (brain_edges has no dry-run equivalent), matching today's
    behavior.

    Args:
        rebuild: Forwarded to `index_brain.main` (`--rebuild`).
        dry_run: Forwarded to `index_brain.main` (`--dry-run`); also skips
            the edge-reload step.
        brain_path: Optional brain root override.

    Returns:
        `{"documents": {"dry_run": bool, "exit_code": int, "success": bool,
        "errors": [...]}, "edges": {"loaded": N} | {"skipped": True},
        "pruned": {"deleted_but_embedded": N, "paths": [...]}}` —
        `documents.success` is `False` whenever `index_brain.main()` reported
        a parse/embed/DB failure, so `syn refresh` (and `syn routine refresh`)
        surface it instead of always reporting a clean run. `pruned` is only
        present on a non-dry-run: a retirement commit otherwise leaves
        `brain_documents` rows for deleted sources, which `syn recall` can
        still score and answer from (the defect this key closes — see
        `docs/brain-rag.md`). A prune failure is reported inside `pruned`,
        never raised, so a cron `syn routine refresh` stays cron-safe.
    """
    import index_brain  # pylint: disable=import-outside-toplevel,import-error

    index_argv: list[str] = []
    if brain_path:
        index_argv += ["--brain-path", brain_path]
    if rebuild:
        index_argv.append("--rebuild")
    if dry_run:
        index_argv.append("--dry-run")

    code, errors = _run_index_brain(index_argv)
    documents_payload = {
        "dry_run": dry_run,
        "exit_code": code,
        "success": code == 0,
        "errors": errors,
    }

    if dry_run:
        return {"documents": documents_payload, "edges": {"skipped": True}}

    resolved = (
        Path(brain_path) if brain_path else index_brain._DEFAULT_BRAIN_PATH  # pylint: disable=protected-access
    )
    loaded = refresh_edges(resolved)
    pruned_payload = _prune_deleted_but_embedded(brain_path)
    return {
        "documents": documents_payload,
        "edges": {"loaded": loaded},
        "pruned": pruned_payload,
    }


def _prune_deleted_but_embedded(brain_path: str | None) -> dict:
    """Sweep `reconcile.deep_stale`'s deleted-but-embedded axis and prune it.

    Called from `refresh()` (never `dry_run`) so a retirement commit that
    deletes a repo's files cannot leave zombie `brain_documents` rows behind
    for the incremental indexer to silently never revisit — measured
    2026-09-06: 257 such rows in the live corpus, none of them ever pruned by
    the routine path (see `docs/brain-rag.md`). No second write path: this
    calls the existing `reconcile.deep_stale` detector and the existing
    `prune_paths` primitive, the same pair `ops.repair_deep_stale` already
    dispatches by hand for `syn stale --deep --repair`.

    Deliberately swallows any exception — a prune failure must not raise out
    of `refresh()` and break the cron-safe `syn routine refresh` path; it is
    reported in the returned payload instead, mirroring how
    `documents.errors` already surfaces `index_brain` failures without
    raising.

    Args:
        brain_path: Optional brain root override, forwarded to both
            `reconcile.deep_stale` and `prune_paths`.

    Returns:
        `{"deleted_but_embedded": N, "paths": [...]}` on success (N is 0 on a
        clean corpus — the positive control distinguishing "nothing to
        prune" from "the sweep didn't run"), or `{"deleted_but_embedded": 0,
        "paths": [], "error": "..."}` if the sweep or prune itself raised.
    """
    from brain.reconcile import deep_stale  # pylint: disable=import-outside-toplevel

    try:
        report = deep_stale(brain_path=brain_path)
        stale_paths = list(report.deleted_but_embedded)
        if stale_paths:
            prune_paths(stale_paths, brain_path=brain_path)
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("refresh: deleted-but-embedded prune failed: %s", exc)
        return {"deleted_but_embedded": 0, "paths": [], "error": str(exc)}

    return {"deleted_but_embedded": len(stale_paths), "paths": stale_paths}


def _changed_files(root: Path, files: list, session) -> list[str]:
    """Content axis: names files whose mtime is newer than their `indexed_at`."""
    from database.brain_document import BrainDocument  # pylint: disable=import-outside-toplevel

    changed: list[str] = []
    for file_path, _doc_type, project_override in files:
        rel = str(file_path.relative_to(root))
        authored_at = datetime.fromtimestamp(file_path.stat().st_mtime)
        query = session.query(BrainDocument).filter(BrainDocument.file_path == rel)
        if project_override is not None:
            query = query.filter(BrainDocument.project == project_override)
        existing = query.order_by(BrainDocument.indexed_at.desc()).first()
        if existing is None or existing.indexed_at is None or authored_at > existing.indexed_at:
            changed.append(rel)
    return changed


def stale(*, brain_path: str | None = None) -> dict:
    """Report content-axis and structure-axis drift. Read-only — no writes.

    Content axis: reuses `index_brain._collect_files` and compares each
    file's mtime to the newest matching `brain_documents.indexed_at` (the
    same comparison `index_brain`'s incremental skip makes, but read-only).
    Structure axis: reuses `brain.pulse.pulse()`'s
    `edges_empty_but_related_exists` flag rather than re-deriving it.

    `ingested/%` rows (synthetic paths written by `app/brain/ingest.py` — no
    on-disk file was ever expected) are exempted from this axis rather than
    silently never surfacing: `index_brain._collect_files` only walks the
    filesystem, so it never yields an `ingested/%` path in the first place —
    they simply cannot appear in `changed`. `syn stale --deep` is what covers
    them (the informational `ingested/` lane in `reconcile.deep_stale`).

    Args:
        brain_path: Optional brain root override.

    Returns:
        `{"changed_files": [...], "edges_stale": bool, "drift": bool}` —
        `drift` is False on an untouched, fully-loaded corpus.
    """
    import index_brain  # pylint: disable=import-outside-toplevel,import-error
    from database.session import db_session  # pylint: disable=import-outside-toplevel

    root = (
        Path(brain_path) if brain_path else index_brain._DEFAULT_BRAIN_PATH  # pylint: disable=protected-access
    )
    config = index_brain._load_brain_config(root)  # pylint: disable=protected-access
    files = index_brain._collect_files(root, config)  # pylint: disable=protected-access

    with next(db_session()) as session:  # type: ignore[arg-type]
        changed = _changed_files(root, files, session)

    from brain.pulse import pulse  # pylint: disable=import-outside-toplevel

    report = pulse()
    edges_stale = report.edges_empty_but_related_exists

    return {
        "changed_files": changed,
        "edges_stale": edges_stale,
        "drift": bool(changed) or edges_stale,
    }


def _delete_orphaned_chunks(chunk_ids: list[str], session) -> int:
    """Delete `content_chunks` rows by id — targeted, provably-orphaned rows only.

    Mirrors `_prune_paths`'s style (exact-match delete, one commit, return the
    count) rather than reusing `GenericRepository.delete`, which is one
    row/one commit at a time and would be needlessly chatty for a batch of
    orphaned chunk ids.

    Args:
        chunk_ids: `ContentChunk.id` values (as returned by
            `reconcile.deep_stale`'s `orphaned_chunks` axis) to delete.
        session: An open SQLAlchemy session.

    Returns:
        The number of rows deleted.
    """
    import uuid as _uuid  # pylint: disable=import-outside-toplevel

    from database.content_chunk import ContentChunk  # pylint: disable=import-outside-toplevel

    ids = [_uuid.UUID(chunk_id) for chunk_id in chunk_ids]
    deleted = session.query(ContentChunk).filter(ContentChunk.id.in_(ids)).delete(
        synchronize_session=False
    )
    session.commit()
    return deleted


def repair_deep_stale(report, *, brain_path: str | None = None) -> dict:
    """Repair the repairable `reconcile.deep_stale` axes using existing primitives only.

    Dispatch, per axis (never touches `client_slug` diagnostic rows — every
    `deep_stale` axis already excludes them):

    - **deleted-but-embedded** -> `prune_paths` (exact paths).
    - **section-orphans** -> no automatic action; a targeted delete of one
      `(file_path, section)` pair is not an existing primitive, so the report
      names the manual follow-up (`refresh(rebuild=True)` / `syn refresh
      --rebuild`) instead of inventing a second write path.
    - **dangling brain_edges** -> `refresh_edges` (reloads the structural
      graph wholesale from `mev emit-graph`).
    - **model mismatch** -> no automatic action; same manual `--rebuild`
      follow-up as section-orphans (repairing in place would mean a second
      embed path, which this module does not introduce).
    - **orphaned content_chunks** -> `_delete_orphaned_chunks` (a targeted
      delete of provably-orphaned rows, mirroring `prune_paths`'s style).

    Detection re-runs after repair so the caller sees the delta, not just the
    actions taken.

    Args:
        report: A `reconcile.ReconcileReport` (typically freshly produced by
            `reconcile.deep_stale`) naming what to repair.
        brain_path: Optional brain root override (forwarded to `prune_paths`,
            `refresh_edges`, and the post-repair `deep_stale` re-check).

    Returns:
        `{"actions": [...], "before": {...}, "after": {...}}` — `before`/
        `after` are `ReconcileReport.to_dict()` snapshots.
    """
    import index_brain  # pylint: disable=import-outside-toplevel,import-error
    from database.session import db_session  # pylint: disable=import-outside-toplevel

    from brain.reconcile import deep_stale  # pylint: disable=import-outside-toplevel

    actions: list[dict] = []

    if report.deleted_but_embedded:
        prune_paths(list(report.deleted_but_embedded), brain_path=brain_path)
        actions.append(
            {
                "axis": "deleted_but_embedded",
                "action": "prune_paths",
                "count": len(report.deleted_but_embedded),
            }
        )

    if report.section_orphans:
        actions.append(
            {
                "axis": "section_orphans",
                "action": "manual --rebuild",
                "count": len(report.section_orphans),
            }
        )

    if report.dangling_edges:
        root = Path(brain_path) if brain_path else index_brain._DEFAULT_BRAIN_PATH  # pylint: disable=protected-access
        loaded = refresh_edges(root)
        actions.append(
            {"axis": "dangling_edges", "action": "refresh_edges", "loaded": loaded}
        )

    if report.model_mismatch:
        actions.append(
            {
                "axis": "model_mismatch",
                "action": "manual --rebuild",
                "count": len(report.model_mismatch),
            }
        )

    if report.orphaned_chunks:
        with next(db_session()) as session:  # type: ignore[arg-type]
            deleted = _delete_orphaned_chunks(report.orphaned_chunks, session)
        actions.append(
            {"axis": "orphaned_chunks", "action": "delete_orphaned_chunks", "count": deleted}
        )

    after = deep_stale(brain_path=brain_path)
    return {"actions": actions, "before": report.to_dict(), "after": after.to_dict()}


def _reconcile_routine() -> dict:
    """`ROUTINES["reconcile"]` body — report-only (a routine must be cron-safe)."""
    from brain.reconcile import deep_stale  # pylint: disable=import-outside-toplevel

    return deep_stale().to_dict()


def _eval_routine() -> dict:
    """`ROUTINES["eval"]` body (OR.K2 task 3) — report-only, cron-safe.

    Scores the default golden set (`planning/retrieval-golden-set.yaml`)
    against the live corpus, writes a dated JSON report to
    `planning/retrieval-eval-runs/`, and returns that report as a dict (the
    routine's return contract, mirroring `_reconcile_routine`). No
    `--baseline` regression gate here — that is `syn eval --baseline`'s job,
    invoked deliberately, not from an unattended cron routine.
    """
    from brain.eval import (  # pylint: disable=import-outside-toplevel
        load_cases,
        run_eval,
        write_report,
    )

    cases = load_cases()
    report = run_eval(cases)
    write_report(report)
    return report.to_dict()


ROUTINES: dict[str, Callable[[], dict]] = {
    # Lambdas (not direct function refs) so tests can `patch("app.brain.ops.refresh", ...)`
    # / `patch("app.brain.ops.stale", ...)` and have the registry dispatch to the patch —
    # a direct `refresh`/`stale` reference here would bind the original function object
    # at import time, before any patch is applied.
    "refresh": lambda: refresh(),  # pylint: disable=unnecessary-lambda
    "stale": lambda: stale(),  # pylint: disable=unnecessary-lambda
    # Deep drift check, report-only — no `--repair` dispatch from a cron routine.
    "reconcile": lambda: _reconcile_routine(),  # pylint: disable=unnecessary-lambda
    "eval": lambda: _eval_routine(),  # pylint: disable=unnecessary-lambda
    # The one DESTRUCTIVE routine, and deliberately so. `reconcile`/`eval` are
    # report-only because they would otherwise *repair* or *write* — open-ended,
    # judgement-shaped work that must not run unattended. Retention is the
    # opposite: a bounded, idempotent delete of rows past a fixed window, which
    # is exactly what a cron routine is for. It is deletion, not aggregation
    # (the D51 guard), so it adds no rollup surface; running it twice in a row
    # is a no-op the second time; and the window it keeps is the golden-set
    # growth sample, so it can never delete recent traffic.
    "queries_prune": lambda: prune_queries(),  # pylint: disable=unnecessary-lambda
}


def run_routine(name: str) -> dict:
    """Run a named chore from the registry — the convention `OR.J`'s cron invokes.

    Args:
        name: A key in `ROUTINES` (e.g. `"refresh"`, `"stale"`).

    Returns:
        The result dict of the underlying routine function.

    Raises:
        UnknownRoutineError: `name` is not a registered routine.
    """
    if name not in ROUTINES:
        raise UnknownRoutineError(name)
    return ROUTINES[name]()
