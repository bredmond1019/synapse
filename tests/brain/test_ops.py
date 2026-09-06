"""Tests for app/brain/ops.py — the Brain write/ops core (OR.N2 task 2; repair dispatch task 3).

Mocks `index_brain.main`, the `mev` subprocess, and `load_brain_edges.load_edges`
rather than requiring a live/Docker-gated DB — `stale()`'s DB read is exercised
against a MagicMock session built the same way `tests/test_index_brain.py`'s
incremental-skip tests do. Covers: `refresh()` step ordering + `--dry-run` skip,
`refresh_edges()` subprocess parsing + `MevUnavailableError`, `stale()`'s
content/structure axes (clean corpus -> zero drift; a changed file -> named;
pulse's `edges_empty_but_related_exists` -> `edges_stale`; `ingested/%` rows
structurally cannot appear), `run_routine` dispatch (including the deep-check
`"reconcile"` routine) + `UnknownRoutineError`, `embed_paths`/`ingest_dir`/
`prune_paths` argument forwarding (including `ingested/%` paths), and
`repair_deep_stale`'s per-axis dispatch to existing primitives only
(pgvector-gated for the orphaned-chunks delete, which needs a real session).
"""

import json
import logging
import uuid
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from brain.ops import (
    DEFAULT_QUERY_KEEP_DAYS,
    MevUnavailableError,
    UnknownRoutineError,
    _prune_deleted_but_embedded,
    _resolve_keep_days,
    embed_paths,
    ingest_dir,
    prune_paths,
    prune_queries,
    refresh,
    refresh_edges,
    repair_deep_stale,
    run_routine,
    stale,
)

FAKE_PAYLOAD = {
    "version": "2",
    "nodes": [{"id": "orchestrator:D1", "doc_id": "D1", "scope": "orchestrator"}],
    "edges": [],
}

_TEST_BRAIN_TOML = """\
[vocab]
layer = ["brain", "engine", "factory", "console", "surface", "infra", "business", "content", "meta"]
status = ["active", "draft", "deprecated", "superseded", "archived"]

[crawl]
skip_dirs = ["target", "node_modules", ".git", ".claude", ".agent", "planning/archive", "venv", ".venv"]

[[repos]]
slug = "brain"
tier = "_root"
repo_path = "."
status_file = "planning/status.md"
cache_doc = "README.md"
heading = "Company Brain"
"""


@pytest.fixture(autouse=True)
def _auto_brain_toml(tmp_path):
    """Make every test's `tmp_path` a valid brain root for `stale()`'s `_load_brain_config`."""
    (tmp_path / "brain.toml").write_text(_TEST_BRAIN_TOML, encoding="utf-8")


class TestEmbedPaths:
    """`embed_paths` forwards to `index_brain.main` with `--only-paths` (+ `--force`)."""

    @patch("index_brain.main")
    def test_forwards_only_paths(self, mock_main):
        mock_main.return_value = 0

        result = embed_paths(["docs/a.md"])

        mock_main.assert_called_once_with(["--only-paths", "docs/a.md"])
        assert result == {
            "embedded": ["docs/a.md"],
            "forced": False,
            "exit_code": 0,
            "success": True,
            "errors": [],
        }

    @patch("index_brain.main")
    def test_forwards_force_and_brain_path(self, mock_main):
        mock_main.return_value = 0

        result = embed_paths(["docs/a.md"], force=True, brain_path="/tmp/brain")

        mock_main.assert_called_once_with(
            ["--only-paths", "docs/a.md", "--force", "--brain-path", "/tmp/brain"]
        )
        assert result["forced"] is True

    @patch("index_brain.main")
    def test_nonzero_exit_surfaces_as_failure(self, mock_main):
        """OR.2.C task 3: `index_brain.main()`'s non-zero result must reach the
        payload, not be silently discarded like it was before this task."""

        def fake_main(_argv):
            logging.getLogger("index_brain").error("Failed to parse %s: %s", "docs/bad.md", "boom")
            return 1

        mock_main.side_effect = fake_main

        result = embed_paths(["docs/bad.md"])

        assert result["exit_code"] == 1
        assert result["success"] is False
        assert result["errors"] == ["Failed to parse docs/bad.md: boom"]

    @patch("index_brain.main")
    def test_error_capture_handler_is_removed_after_the_call(self, mock_main):
        """The capturing handler must not leak onto `index_brain`'s logger across calls
        — a leaked handler would accumulate every subsequent run's log records."""
        index_logger = logging.getLogger("index_brain")
        before = list(index_logger.handlers)
        mock_main.return_value = 0

        embed_paths(["docs/a.md"])

        assert index_logger.handlers == before


class TestIngestDir:
    """`ingest_dir` expands a directory to its *.md files and reuses `embed_paths`."""

    @patch("brain.ops.embed_paths")
    def test_forwards_collected_files(self, mock_embed_paths, tmp_path):
        (tmp_path / "a.md").write_text("A", encoding="utf-8")
        (tmp_path / "b.md").write_text("B", encoding="utf-8")
        (tmp_path / "c.txt").write_text("C", encoding="utf-8")
        mock_embed_paths.return_value = {
            "embedded": [],
            "forced": True,
            "exit_code": 0,
            "success": True,
            "errors": [],
        }

        result = ingest_dir(str(tmp_path), force=True)

        called_files = mock_embed_paths.call_args[0][0]
        assert {p.split("/")[-1] for p in called_files} == {"a.md", "b.md"}
        assert mock_embed_paths.call_args.kwargs["force"] is True
        assert set(result["ingested"]) == set(called_files)
        assert result["success"] is True

    def test_non_directory_raises(self, tmp_path):
        missing = tmp_path / "nope"

        with pytest.raises(NotADirectoryError):
            ingest_dir(str(missing))

    @patch("brain.ops.embed_paths")
    def test_empty_directory_is_a_noop(self, mock_embed_paths, tmp_path):
        result = ingest_dir(str(tmp_path))

        mock_embed_paths.assert_not_called()
        assert result == {
            "ingested": [],
            "forced": False,
            "exit_code": 0,
            "success": True,
            "errors": [],
        }

    @patch("brain.ops.embed_paths")
    def test_forwards_embed_paths_failure(self, mock_embed_paths, tmp_path):
        """`ingest_dir` has no direct `index_brain.main()` call site of its own —
        it must forward `embed_paths`'s exit_code/success/errors verbatim."""
        (tmp_path / "a.md").write_text("A", encoding="utf-8")
        mock_embed_paths.return_value = {
            "embedded": ["a.md"],
            "forced": False,
            "exit_code": 1,
            "success": False,
            "errors": ["a.md: parse error"],
        }

        result = ingest_dir(str(tmp_path))

        assert result["exit_code"] == 1
        assert result["success"] is False
        assert result["errors"] == ["a.md: parse error"]


class TestPrunePaths:
    """`prune_paths` forwards to `index_brain.main` with `--prune-paths`."""

    @patch("index_brain.main")
    def test_forwards_paths(self, mock_main):
        mock_main.return_value = 0

        result = prune_paths(["docs/old.md", "docs/gone.md"])

        mock_main.assert_called_once_with(["--prune-paths", "docs/old.md", "docs/gone.md"])
        assert result == {
            "pruned": ["docs/old.md", "docs/gone.md"],
            "dry_run": False,
            "exit_code": 0,
            "success": True,
            "errors": [],
        }

    @patch("index_brain.main")
    def test_forwards_dry_run_and_brain_path(self, mock_main):
        mock_main.return_value = 0

        result = prune_paths(["docs/old.md"], dry_run=True, brain_path="/tmp/brain")

        mock_main.assert_called_once_with(
            ["--prune-paths", "docs/old.md", "--dry-run", "--brain-path", "/tmp/brain"]
        )
        assert result["dry_run"] is True

    @patch("index_brain.main")
    def test_accepts_ingested_lane_synthetic_paths(self, mock_main):
        """`ingested/%` paths are exact-match deletes like any other path (OR.ticket task 3)."""
        mock_main.return_value = 0

        result = prune_paths(["ingested/proposal/abc123.md"])

        mock_main.assert_called_once_with(
            ["--prune-paths", "ingested/proposal/abc123.md"]
        )
        assert result == {
            "pruned": ["ingested/proposal/abc123.md"],
            "dry_run": False,
            "exit_code": 0,
            "success": True,
            "errors": [],
        }


class TestRefreshEdges:
    """`refresh_edges` shells out to `mev emit-graph`, parses, delegates to `load_edges`."""

    @patch("load_brain_edges.load_edges")
    @patch("database.session.db_session")
    @patch("brain.ops.subprocess.run")
    def test_parses_mev_output_and_delegates_to_load_edges(
        self, mock_run, mock_db_session, mock_load_edges
    ):
        mock_run.return_value = MagicMock(stdout=json.dumps(FAKE_PAYLOAD))
        fake_session = MagicMock()
        fake_session.__enter__ = MagicMock(return_value=fake_session)
        fake_session.__exit__ = MagicMock(return_value=False)

        def fake_db_session():
            yield fake_session

        mock_db_session.side_effect = fake_db_session
        mock_load_edges.return_value = 3

        count = refresh_edges("/tmp/some-brain")

        mock_run.assert_called_once_with(
            ["mev", "emit-graph", "--json", "/tmp/some-brain"],
            capture_output=True,
            text=True,
            check=True,
        )
        mock_load_edges.assert_called_once_with(FAKE_PAYLOAD, fake_session)
        assert count == 3

    @patch("brain.ops.subprocess.run", side_effect=FileNotFoundError("no mev"))
    def test_missing_mev_binary_raises_typed_error(self, _mock_run):
        with pytest.raises(MevUnavailableError):
            refresh_edges("/tmp/some-brain")


class TestRefresh:
    """`refresh()` runs the content step then the edge step, in order; --dry-run skips edges.

    Every non-dry-run case here patches `brain.ops._prune_deleted_but_embedded`
    directly (its own behaviour — success, clean-corpus zero, and failure — is
    covered in isolation by `TestPruneDeletedButEmbedded` and the pgvector-gated
    `TestRefreshPrunesZombieRows` below) so this class stays focused on step
    ordering and the `documents`/`edges` payload shape.
    """

    @patch("brain.ops._prune_deleted_but_embedded")
    @patch("brain.ops.refresh_edges")
    @patch("index_brain.main")
    def test_default_run_calls_both_steps_in_order(
        self, mock_index_main, mock_refresh_edges, mock_prune
    ):
        mock_index_main.return_value = 0
        parent = MagicMock()
        parent.attach_mock(mock_index_main, "index_main")
        parent.attach_mock(mock_refresh_edges, "refresh_edges")
        mock_refresh_edges.return_value = 5
        mock_prune.return_value = {"deleted_but_embedded": 0, "paths": []}

        result = refresh()

        assert [c[0] for c in parent.mock_calls] == ["index_main", "refresh_edges"]
        mock_index_main.assert_called_once_with([])
        mock_prune.assert_called_once_with(None)
        assert result == {
            "documents": {"dry_run": False, "exit_code": 0, "success": True, "errors": []},
            "edges": {"loaded": 5},
            "pruned": {"deleted_but_embedded": 0, "paths": []},
        }

    @patch("brain.ops._prune_deleted_but_embedded")
    @patch("brain.ops.refresh_edges")
    @patch("index_brain.main")
    def test_dry_run_skips_edge_step(self, mock_index_main, mock_refresh_edges, mock_prune):
        mock_index_main.return_value = 0

        result = refresh(dry_run=True)

        mock_index_main.assert_called_once_with(["--dry-run"])
        mock_refresh_edges.assert_not_called()
        mock_prune.assert_not_called()
        assert result == {
            "documents": {"dry_run": True, "exit_code": 0, "success": True, "errors": []},
            "edges": {"skipped": True},
        }

    @patch("brain.ops._prune_deleted_but_embedded")
    @patch("brain.ops.refresh_edges")
    @patch("index_brain.main")
    def test_forwards_rebuild_and_brain_path(
        self, mock_index_main, mock_refresh_edges, mock_prune
    ):
        mock_index_main.return_value = 0
        mock_refresh_edges.return_value = 0
        mock_prune.return_value = {"deleted_but_embedded": 0, "paths": []}

        refresh(rebuild=True, brain_path="/tmp/some-brain")

        mock_index_main.assert_called_once_with(["--brain-path", "/tmp/some-brain", "--rebuild"])
        called_path = mock_refresh_edges.call_args[0][0]
        assert str(called_path) == "/tmp/some-brain"
        mock_prune.assert_called_once_with("/tmp/some-brain")

    @patch("brain.ops._prune_deleted_but_embedded")
    @patch("brain.ops.refresh_edges")
    @patch("index_brain.main")
    def test_parse_failure_surfaces_in_documents_payload(
        self, mock_index_main, mock_refresh_edges, mock_prune
    ):
        """OR.2.C task 3: `syn refresh` (and `syn routine refresh`) must be able to
        see a parse/embed/DB failure `index_brain.main()` reported, not just log it."""

        def fake_main(_argv):
            logging.getLogger("index_brain").error("docs/bad.md: db error — boom")
            return 1

        mock_index_main.side_effect = fake_main
        mock_refresh_edges.return_value = 2
        mock_prune.return_value = {"deleted_but_embedded": 0, "paths": []}

        result = refresh()

        assert result["documents"]["exit_code"] == 1
        assert result["documents"]["success"] is False


class TestPruneDeletedButEmbedded:
    """`_prune_deleted_but_embedded` — the helper `refresh()` calls to sweep and
    prune the deleted-but-embedded axis. Mocked `reconcile.deep_stale`/`prune_paths`
    so this covers dispatch and the swallow-don't-raise contract in isolation;
    `TestRefreshPrunesZombieRows` proves the same path end to end against a real
    pgvector session."""

    @patch("brain.ops.prune_paths")
    @patch("brain.reconcile.deep_stale")
    def test_prunes_named_paths_and_reports_count(self, mock_deep_stale, mock_prune_paths):
        mock_deep_stale.return_value = MagicMock(deleted_but_embedded=["side/amistad/gone.md"])

        result = _prune_deleted_but_embedded("/tmp/some-brain")

        mock_deep_stale.assert_called_once_with(brain_path="/tmp/some-brain")
        mock_prune_paths.assert_called_once_with(
            ["side/amistad/gone.md"], brain_path="/tmp/some-brain"
        )
        assert result == {"deleted_but_embedded": 1, "paths": ["side/amistad/gone.md"]}

    @patch("brain.ops.prune_paths")
    @patch("brain.reconcile.deep_stale")
    def test_clean_corpus_prunes_nothing_and_reports_zero(
        self, mock_deep_stale, mock_prune_paths
    ):
        """Positive control: a clean sweep must be distinguishable from a sweep
        that silently never ran — both would otherwise report `{"deleted_but_embedded": 0}`."""
        mock_deep_stale.return_value = MagicMock(deleted_but_embedded=[])

        result = _prune_deleted_but_embedded(None)

        mock_deep_stale.assert_called_once_with(brain_path=None)
        mock_prune_paths.assert_not_called()
        assert result == {"deleted_but_embedded": 0, "paths": []}

    @patch("brain.reconcile.deep_stale", side_effect=RuntimeError("db unreachable"))
    def test_sweep_failure_is_reported_not_raised(self, _mock_deep_stale):
        """A prune failure must not raise out of `refresh()` — the routine stays
        cron-safe and reports the failure in the payload instead."""
        result = _prune_deleted_but_embedded(None)

        assert result == {"deleted_but_embedded": 0, "paths": [], "error": "db unreachable"}

    @patch("brain.ops.refresh_edges")
    @patch("index_brain.main")
    def test_dry_run_parse_failure_surfaces_without_edge_step(
        self, mock_index_main, mock_refresh_edges
    ):
        def fake_main(_argv):
            logging.getLogger("index_brain").error("docs/bad.md: frontmatter parse error")
            return 1

        mock_index_main.side_effect = fake_main

        result = refresh(dry_run=True)

        mock_refresh_edges.assert_not_called()
        assert result["documents"]["success"] is False
        assert result["edges"] == {"skipped": True}


class _FakePulseReport:
    def __init__(self, edges_empty_but_related_exists: bool) -> None:
        self.edges_empty_but_related_exists = edges_empty_but_related_exists


class TestStale:
    """`stale()`: content axis (mtime vs indexed_at, read-only) + structure axis (pulse reuse)."""

    def _make_mock_session(self, existing_indexed_at):
        mock_session = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        if existing_indexed_at is None:
            mock_query.first.return_value = None
        else:
            mock_doc = MagicMock()
            mock_doc.indexed_at = existing_indexed_at
            mock_query.first.return_value = mock_doc
        mock_session.query.return_value = mock_query
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        return mock_session

    @patch("brain.pulse.pulse")
    @patch("database.session.db_session")
    def test_untouched_corpus_reports_zero_drift(self, mock_db_session, mock_pulse, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "career.md").write_text("## Section\nContent.", encoding="utf-8")
        (tmp_path / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")

        future_indexed = datetime.now() + timedelta(hours=1)
        fake_session = self._make_mock_session(future_indexed)

        def fake_db_session():
            yield fake_session

        mock_db_session.side_effect = fake_db_session
        mock_pulse.return_value = _FakePulseReport(edges_empty_but_related_exists=False)

        result = stale(brain_path=str(tmp_path))

        assert result == {"changed_files": [], "edges_stale": False, "drift": False}

    @patch("brain.pulse.pulse")
    @patch("database.session.db_session")
    def test_changed_file_is_named(self, mock_db_session, mock_pulse, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "career.md").write_text("## Section\nContent.", encoding="utf-8")
        (tmp_path / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")

        old_indexed = datetime.now() - timedelta(hours=24)
        fake_session = self._make_mock_session(old_indexed)

        def fake_db_session():
            yield fake_session

        mock_db_session.side_effect = fake_db_session
        mock_pulse.return_value = _FakePulseReport(edges_empty_but_related_exists=False)

        result = stale(brain_path=str(tmp_path))

        assert result["changed_files"] == ["docs/career.md"]
        assert result["drift"] is True

    @patch("brain.pulse.pulse")
    @patch("database.session.db_session")
    def test_edges_stale_reflects_pulse_flag(self, mock_db_session, mock_pulse, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "career.md").write_text("## Section\nContent.", encoding="utf-8")
        (tmp_path / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")

        future_indexed = datetime.now() + timedelta(hours=1)
        fake_session = self._make_mock_session(future_indexed)

        def fake_db_session():
            yield fake_session

        mock_db_session.side_effect = fake_db_session
        mock_pulse.return_value = _FakePulseReport(edges_empty_but_related_exists=True)

        result = stale(brain_path=str(tmp_path))

        assert result["changed_files"] == []
        assert result["edges_stale"] is True
        assert result["drift"] is True

    @patch("index_brain._collect_files", return_value=[])
    @patch("brain.pulse.pulse")
    @patch("database.session.db_session")
    def test_ingested_lane_rows_never_appear_in_changed_files(
        self, mock_db_session, mock_pulse, _mock_collect_files, tmp_path
    ):
        """`_collect_files` walks disk only, so `ingested/%` DB rows structurally
        cannot surface here regardless of how many exist — pinning the exemption
        `stale()`'s docstring now states explicitly (OR.ticket task 3)."""
        fake_session = MagicMock()
        fake_session.__enter__ = MagicMock(return_value=fake_session)
        fake_session.__exit__ = MagicMock(return_value=False)

        def fake_db_session():
            yield fake_session

        mock_db_session.side_effect = fake_db_session
        mock_pulse.return_value = _FakePulseReport(edges_empty_but_related_exists=False)

        result = stale(brain_path=str(tmp_path))

        assert result["changed_files"] == []
        assert result["drift"] is False


class TestRunRoutine:
    """`run_routine` dispatches over the `ROUTINES` registry; unknown names raise."""

    @patch("brain.ops.refresh")
    def test_dispatches_refresh(self, mock_refresh):
        mock_refresh.return_value = {"documents": {}, "edges": {}}

        result = run_routine("refresh")

        mock_refresh.assert_called_once_with()
        assert result == {"documents": {}, "edges": {}}

    @patch("brain.ops.stale")
    def test_dispatches_stale(self, mock_stale):
        mock_stale.return_value = {"changed_files": [], "edges_stale": False, "drift": False}

        result = run_routine("stale")

        mock_stale.assert_called_once_with()
        assert result["drift"] is False

    @patch("brain.reconcile.deep_stale")
    def test_dispatches_reconcile_report_only(self, mock_deep_stale):
        """`"reconcile"` runs the deep check and serializes it — report-only, no repair
        dispatch (a routine must be safe to cron)."""
        fake_report = _make_report()
        mock_deep_stale.return_value = fake_report

        result = run_routine("reconcile")

        mock_deep_stale.assert_called_once_with()
        assert result == fake_report.to_dict()

    @patch("brain.ops.prune_queries")
    def test_dispatches_queries_prune(self, mock_prune):
        """`"queries_prune"` is the one destructive routine — bounded, idempotent, and
        registered lambda-style so it stays patchable like every other entry."""
        mock_prune.return_value = {"deleted": 3, "kept": 7, "cutoff": "2026-05-03T00:00:00"}

        result = run_routine("queries_prune")

        mock_prune.assert_called_once_with()
        assert result["deleted"] == 3

    def test_unknown_name_raises(self):
        with pytest.raises(UnknownRoutineError):
            run_routine("nope")


class TestResolveKeepDays:
    """Retention window resolution: explicit argument > env var > 90, never a crash."""

    def test_defaults_to_ninety_when_unset(self, monkeypatch):
        monkeypatch.delenv("BRAIN_QUERY_LOG_KEEP_DAYS", raising=False)

        assert _resolve_keep_days(None) == DEFAULT_QUERY_KEEP_DAYS == 90

    def test_env_override_is_read_at_call_time(self, monkeypatch):
        monkeypatch.setenv("BRAIN_QUERY_LOG_KEEP_DAYS", "30")

        assert _resolve_keep_days(None) == 30

    def test_explicit_argument_beats_env(self, monkeypatch):
        monkeypatch.setenv("BRAIN_QUERY_LOG_KEEP_DAYS", "30")

        assert _resolve_keep_days(7) == 7

    def test_garbage_env_falls_back_with_a_warning(self, monkeypatch, caplog):
        monkeypatch.setenv("BRAIN_QUERY_LOG_KEEP_DAYS", "ninety")

        with caplog.at_level(logging.WARNING):
            assert _resolve_keep_days(None) == DEFAULT_QUERY_KEEP_DAYS
        assert "BRAIN_QUERY_LOG_KEEP_DAYS" in caplog.text

    @pytest.mark.parametrize("raw", ["0", "-5"])
    def test_non_positive_env_falls_back(self, monkeypatch, caplog, raw):
        monkeypatch.setenv("BRAIN_QUERY_LOG_KEEP_DAYS", raw)

        with caplog.at_level(logging.WARNING):
            assert _resolve_keep_days(None) == DEFAULT_QUERY_KEEP_DAYS
        assert "non-positive" in caplog.text

    def test_empty_env_falls_back_silently(self, monkeypatch):
        monkeypatch.setenv("BRAIN_QUERY_LOG_KEEP_DAYS", "   ")

        assert _resolve_keep_days(None) == DEFAULT_QUERY_KEEP_DAYS

    def test_non_positive_explicit_argument_falls_back(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert _resolve_keep_days(0) == DEFAULT_QUERY_KEEP_DAYS
        assert "non-positive" in caplog.text


_FROZEN_NOW = datetime(2026, 8, 1, 12, 0, 0)


def _retrieval_query_db():
    """In-memory SQLite `retrieval_queries` engine/session-factory pair mirroring
    `database.session.db_session`'s commit/rollback/close shape."""
    from database.retrieval_query import RetrievalQuery
    from database.session import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine, tables=[RetrievalQuery.__table__])
    session_factory = sessionmaker(bind=engine)

    def _db_session():
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return engine, session_factory, _db_session


def _seed_query(session_factory, *, query: str, created_at: datetime) -> None:
    from database.retrieval_query import RetrievalQuery

    session = session_factory()
    session.add(
        RetrievalQuery(
            id=uuid.uuid4(),
            query=query,
            surface="cli",
            workspace_id=None,
            hybrid=True,
            via_mix={"semantic": 1},
            result_count=1,
            top_score=0.9,
            retrieval_confidence=0.8,
            abstained=False,
            top_doc_ids=["doc-1"],
            latency_ms=5,
            created_at=created_at,
        )
    )
    session.commit()
    session.close()


def _remaining_queries(session_factory) -> list[str]:
    from database.retrieval_query import RetrievalQuery

    session = session_factory()
    rows = sorted(row.query for row in session.query(RetrievalQuery).all())
    session.close()
    return rows


class TestPruneQueries:
    """`prune_queries` — bounded, idempotent retention for the OR.K1 query log.

    Deletion only (the D51 guard: no rollup, nothing persisted at prune time),
    with `deleted`/`kept` derived from real `count()` reads around the delete
    rather than from what the caller asked for.
    """

    @pytest.fixture
    def queries_db(self):
        engine, session_factory, fake_db_session = _retrieval_query_db()
        with patch("database.session.db_session", fake_db_session):
            yield session_factory
        engine.dispose()

    @pytest.fixture
    def frozen_now(self):
        with patch("brain.ops.datetime") as mock_datetime:
            mock_datetime.now.return_value = _FROZEN_NOW
            yield _FROZEN_NOW

    def test_deletes_only_rows_older_than_the_window(self, queries_db, frozen_now):
        _seed_query(queries_db, query="recent", created_at=frozen_now - timedelta(days=1))
        _seed_query(queries_db, query="old", created_at=frozen_now - timedelta(days=120))

        result = prune_queries(90)

        assert result["deleted"] == 1
        assert result["kept"] == 1
        assert result["keep_days"] == 90
        assert result["dry_run"] is False
        assert result["cutoff"] == (frozen_now - timedelta(days=90)).isoformat()
        assert _remaining_queries(queries_db) == ["recent"]

    def test_row_exactly_at_the_cutoff_is_kept(self, queries_db, frozen_now):
        """Strictly older-than: the boundary row survives."""
        _seed_query(queries_db, query="boundary", created_at=frozen_now - timedelta(days=90))
        _seed_query(
            queries_db,
            query="one-second-older",
            created_at=frozen_now - timedelta(days=90, seconds=1),
        )

        result = prune_queries(90)

        assert result["deleted"] == 1
        assert result["kept"] == 1
        assert _remaining_queries(queries_db) == ["boundary"]

    def test_dry_run_deletes_nothing_but_reports_the_count(self, queries_db, frozen_now):
        _seed_query(queries_db, query="recent", created_at=frozen_now - timedelta(days=1))
        _seed_query(queries_db, query="old", created_at=frozen_now - timedelta(days=200))

        result = prune_queries(90, dry_run=True)

        assert result["deleted"] == 1
        assert result["kept"] == 1
        assert result["dry_run"] is True
        assert _remaining_queries(queries_db) == ["old", "recent"]

    def test_is_idempotent_and_zero_deletion_is_not_an_error(self, queries_db, frozen_now):
        _seed_query(queries_db, query="recent", created_at=frozen_now - timedelta(days=2))
        _seed_query(queries_db, query="old", created_at=frozen_now - timedelta(days=400))

        first = prune_queries(90)
        second = prune_queries(90)

        assert first["deleted"] == 1
        assert second["deleted"] == 0
        assert second["kept"] == 1
        assert _remaining_queries(queries_db) == ["recent"]

    def test_empty_table_is_a_clean_no_op(self, queries_db, frozen_now):
        result = prune_queries()

        assert result == {
            "deleted": 0,
            "kept": 0,
            "cutoff": (frozen_now - timedelta(days=90)).isoformat(),
            "keep_days": 90,
            "dry_run": False,
        }

    def test_env_var_narrows_the_window(self, queries_db, frozen_now, monkeypatch):
        monkeypatch.setenv("BRAIN_QUERY_LOG_KEEP_DAYS", "7")
        _seed_query(queries_db, query="recent", created_at=frozen_now - timedelta(days=3))
        _seed_query(queries_db, query="middling", created_at=frozen_now - timedelta(days=30))

        result = prune_queries()

        assert result["keep_days"] == 7
        assert result["deleted"] == 1
        assert _remaining_queries(queries_db) == ["recent"]

    def test_garbage_env_falls_back_to_ninety_days(self, queries_db, frozen_now, monkeypatch):
        monkeypatch.setenv("BRAIN_QUERY_LOG_KEEP_DAYS", "not-a-number")
        _seed_query(queries_db, query="middling", created_at=frozen_now - timedelta(days=30))

        result = prune_queries()

        assert result["keep_days"] == 90
        assert result["deleted"] == 0
        assert _remaining_queries(queries_db) == ["middling"]


def _make_report(**overrides):
    """Build a `ReconcileReport` with axis-empty defaults, overridable per test."""
    from brain.reconcile import ReconcileReport

    defaults = dict(
        deleted_but_embedded=[],
        section_orphans=[],
        orphaned_chunks=[],
        dangling_edges=[],
        model_mismatch=[],
        unstamped_count=0,
        ingested_count=0,
        ingested_min_authored_at=None,
        ingested_max_authored_at=None,
        drift=False,
    )
    defaults.update(overrides)
    return ReconcileReport(**defaults)


class TestRepairDeepStale:
    """`repair_deep_stale` dispatches per-axis using only pre-existing ops primitives,
    re-runs detection, and reports the delta. Never touches `client_slug` rows — every
    `deep_stale` axis already excludes them, so there is nothing extra to guard here."""

    @patch("brain.reconcile.deep_stale")
    @patch("brain.ops.prune_paths")
    def test_deleted_but_embedded_dispatches_prune_paths(self, mock_prune, mock_deep_stale):
        report = _make_report(deleted_but_embedded=["gone.md"], drift=True)
        mock_deep_stale.return_value = _make_report()

        result = repair_deep_stale(report, brain_path="/tmp/brain")

        mock_prune.assert_called_once_with(["gone.md"], brain_path="/tmp/brain")
        assert result["actions"] == [
            {"axis": "deleted_but_embedded", "action": "prune_paths", "count": 1}
        ]
        assert result["before"] == report.to_dict()
        assert result["after"]["drift"] is False

    @patch("brain.reconcile.deep_stale")
    @patch("brain.ops.refresh_edges")
    def test_dangling_edges_dispatches_refresh_edges(self, mock_refresh_edges, mock_deep_stale):
        report = _make_report(
            dangling_edges=[{"source_doc_id": "D1", "to_ref": "X", "target_doc_id": None}],
            drift=True,
        )
        mock_refresh_edges.return_value = 3
        mock_deep_stale.return_value = _make_report()

        result = repair_deep_stale(report, brain_path="/tmp/brain")

        called_path = mock_refresh_edges.call_args[0][0]
        assert str(called_path) == "/tmp/brain"
        assert result["actions"] == [
            {"axis": "dangling_edges", "action": "refresh_edges", "loaded": 3}
        ]

    @patch("brain.reconcile.deep_stale")
    @patch("brain.ops.prune_paths")
    @patch("brain.ops.refresh_edges")
    def test_section_orphans_and_model_mismatch_are_manual_only(
        self, mock_refresh_edges, mock_prune, mock_deep_stale
    ):
        report = _make_report(
            section_orphans=[("doc.md", "## Old")],
            model_mismatch=[{"embedding_model": "voyage:voyage-2"}],
            drift=True,
        )
        mock_deep_stale.return_value = report

        result = repair_deep_stale(report)

        mock_prune.assert_not_called()
        mock_refresh_edges.assert_not_called()
        actions_by_axis = {action["axis"]: action for action in result["actions"]}
        assert actions_by_axis["section_orphans"]["action"] == "manual --rebuild"
        assert actions_by_axis["model_mismatch"]["action"] == "manual --rebuild"

    @patch("brain.reconcile.deep_stale")
    def test_healthy_report_is_a_noop(self, mock_deep_stale):
        report = _make_report()
        mock_deep_stale.return_value = report

        result = repair_deep_stale(report)

        assert result["actions"] == []
        assert result["after"]["drift"] is False

    def test_orphaned_chunks_deleted_via_repository(self, pgvector_session):
        """End-to-end delete against a real session — the targeted, provably-orphaned
        delete this axis is explicitly allowed (mirrors `_prune_paths`'s style)."""
        from database.content_chunk import ContentChunk

        orphan = ContentChunk(doc_id=uuid.uuid4(), position=1, content="orphan chunk")
        pgvector_session.add(orphan)
        pgvector_session.flush()
        chunk_id = str(orphan.id)

        report = _make_report(orphaned_chunks=[chunk_id], drift=True)

        def fake_db_session():
            yield pgvector_session

        with patch("database.session.db_session", side_effect=fake_db_session), patch(
            "brain.reconcile.deep_stale", return_value=_make_report()
        ):
            result = repair_deep_stale(report)

        assert result["actions"] == [
            {"axis": "orphaned_chunks", "action": "delete_orphaned_chunks", "count": 1}
        ]
        # `chunk_id` (captured before the delete) rather than `orphan.id` — the bulk
        # delete + commit expires `orphan`, and it is no longer bound to a session.
        remaining = (
            pgvector_session.query(ContentChunk)
            .filter(ContentChunk.id == uuid.UUID(chunk_id))
            .first()
        )
        assert remaining is None
