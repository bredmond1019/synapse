"""Tests for the zombie-row prune path — SY.ticket.prune-retired-repo-rows-from-the-brain-corpus.

The prune primitive (`brain.ops.prune_paths`) and the deleted-source sweep
(`brain.reconcile.deep_stale`'s `deleted_but_embedded` axis, dispatched by
`brain.ops.repair_deep_stale`) already exist — this file introduces no new
module. It proves that existing path end to end against a real pgvector
session (`tests/database/conftest.py`'s Docker-gated `pgvector_session`
fixture) and a real on-disk corpus root (`tmp_path`), following the pattern
established in `tests/brain/test_reconcile.py` (the `_make_doc` seeding
helper) and `tests/brain/test_ops.py::TestRepairDeepStale
.test_orphaned_chunks_deleted_via_repository` (patching
`database.session.db_session` so every DB session opened deep inside
`prune_paths` -> `index_brain.main(["--prune-paths", ...])` and the
post-repair `deep_stale()` re-check resolves to the same session/transaction
the test seeded and asserts against).

Two rows are seeded: one whose source file exists on disk (kept — the
positive control HQ standing rule 11 requires) and one whose source file is
absent (pruned). A row surviving is only meaningful evidence once a sibling
row is proven prunable by the identical instrument.
"""

from datetime import datetime
from unittest.mock import patch

from brain.ops import repair_deep_stale
from brain.reconcile import deep_stale
from database.brain_document import BrainDocument

_STAMP = "ollama:mxbai-embed-large"


class _FakeEmbeddingService:
    """Stub embedding service — fixed stamp, never calls a real backend."""

    stamp = _STAMP


def _make_doc(file_path: str, **overrides) -> BrainDocument:
    defaults = dict(
        file_path=file_path,
        doc_type="decision",
        section="",
        content="content",
        embedding_model=_STAMP,
        indexed_at=datetime(2026, 1, 1),
        authored_at=datetime(2026, 1, 1),
    )
    defaults.update(overrides)
    return BrainDocument(**defaults)


def _write_brain_toml(root):
    """`--prune-paths` routes through `index_brain._resolve_brain_path`, which
    requires a `brain.toml` at the root — a bare `tmp_path` fails validation."""
    (root / "brain.toml").write_text(
        "[vocab]\n"
        'layer = ["brain"]\n'
        'status = ["active"]\n'
        "[crawl]\n"
        "skip_dirs = []\n",
        encoding="utf-8",
    )


def _fake_db_session(session):
    """A `database.session.db_session`-shaped generator yielding `session`
    itself, so every lazy `next(db_session())` deep inside `prune_paths` and
    `deep_stale`'s own session resolution lands on the same transaction the
    test seeded and asserts against."""

    def _generator():
        yield session

    return _generator


class TestZombieRowPrune:
    """`deep_stale` names an absent-source row; `repair_deep_stale` prunes it
    via the existing `prune_paths` primitive; a present-source row is kept."""

    def test_absent_source_pruned_present_source_kept(self, pgvector_session, tmp_path):
        _write_brain_toml(tmp_path)
        (tmp_path / "still-here.md").write_text("body\n", encoding="utf-8")

        pgvector_session.add_all(
            [
                _make_doc("still-here.md", doc_id="D1"),
                _make_doc("gone.md", doc_id="D2"),
            ]
        )
        pgvector_session.flush()

        report = deep_stale(
            brain_path=str(tmp_path),
            session=pgvector_session,
            embedding_service=_FakeEmbeddingService(),
        )

        # Positive control (HQ standing rule 11): the still-existing row must
        # be named as NOT reported, proving the sweep actually distinguishes
        # present from absent rather than reporting nothing for any reason.
        assert report.deleted_but_embedded == ["gone.md"]
        assert report.drift is True

        with patch(
            "database.session.db_session",
            side_effect=_fake_db_session(pgvector_session),
        ):
            result = repair_deep_stale(report, brain_path=str(tmp_path))

        assert result["actions"] == [
            {"axis": "deleted_but_embedded", "action": "prune_paths", "count": 1}
        ]
        assert result["after"]["deleted_but_embedded"] == []

        remaining_paths = sorted(
            file_path
            for (file_path,) in pgvector_session.query(BrainDocument.file_path).all()
        )
        assert remaining_paths == ["still-here.md"]

    def test_no_absent_sources_is_a_noop(self, pgvector_session, tmp_path):
        """Positive control for the repair dispatch itself: a healthy corpus
        reports no prune action and deletes nothing — distinguishing a true
        clean result from a sweep that would report nothing regardless."""
        _write_brain_toml(tmp_path)
        (tmp_path / "still-here.md").write_text("body\n", encoding="utf-8")
        pgvector_session.add(_make_doc("still-here.md", doc_id="D1"))
        pgvector_session.flush()

        report = deep_stale(
            brain_path=str(tmp_path),
            session=pgvector_session,
            embedding_service=_FakeEmbeddingService(),
        )
        assert report.deleted_but_embedded == []

        with patch(
            "database.session.db_session",
            side_effect=_fake_db_session(pgvector_session),
        ):
            result = repair_deep_stale(report, brain_path=str(tmp_path))

        assert result["actions"] == []

        remaining_paths = sorted(
            file_path
            for (file_path,) in pgvector_session.query(BrainDocument.file_path).all()
        )
        assert remaining_paths == ["still-here.md"]
