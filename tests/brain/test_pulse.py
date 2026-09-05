"""Tests for app/brain/pulse.py — the health/pulse read core (OR.N1 task 3).

Seeds `brain_documents` + `brain_edges` against the Docker-gated
`pgvector_engine` fixture (`tests/brain/conftest.py`) and exercises: row
counts + staleness watermark reporting, the `edges_empty_but_related_exists`
load-bearing flag in both the unhealthy (edges empty, related exists) and
healthy (edges present, or no related) states, and a guarded embedding-probe
failure that is captured in the report rather than raised.
"""

from datetime import datetime

from brain.pulse import PulseReport, pulse
from database.brain_document import BrainDocument
from database.brain_edge import BrainEdge


def _make_doc(doc_id: str, **overrides) -> BrainDocument:
    defaults = dict(
        file_path=f"docs/decisions/{doc_id}-example.md",
        doc_type="decision",
        content=f"Content for {doc_id}.",
        title=f"{doc_id} — Example Decision",
        section="",
        doc_id=doc_id,
        indexed_at=datetime(2026, 1, 1),
        authored_at=datetime(2026, 1, 1),
    )
    defaults.update(overrides)
    return BrainDocument(**defaults)


def _make_edge(source_doc_id: str, target_doc_id: str) -> BrainEdge:
    return BrainEdge(
        source_node_id=f"brain:{source_doc_id}",
        source_doc_id=source_doc_id,
        to_ref=target_doc_id,
        target_node_id=f"brain:{target_doc_id}",
        target_doc_id=target_doc_id,
    )


class _FakeEmbeddingService:
    """Stub embedding service — always succeeds."""

    def embed_text(self, text: str) -> list[float]:  # noqa: ARG002
        return [0.0] * 1024


class _FailingEmbeddingService:
    """Stub embedding service — always raises, simulating an unreachable backend."""

    def embed_text(self, text: str):  # noqa: ARG002
        raise ConnectionError("embedding backend unreachable")


class TestPulseRowCountsAndWatermark:
    """`pulse()` reports live row counts and a staleness watermark."""

    def test_reports_counts_and_watermark(self, pgvector_session):
        pgvector_session.add_all(
            [
                _make_doc("D36", indexed_at=datetime(2026, 1, 1), authored_at=datetime(2026, 1, 1)),
                _make_doc("D41", indexed_at=datetime(2026, 3, 1), authored_at=datetime(2026, 2, 1)),
                _make_edge("D36", "D41"),
            ]
        )
        pgvector_session.flush()

        report = pulse(session=pgvector_session, embedding_service=_FakeEmbeddingService())

        assert isinstance(report, PulseReport)
        assert report.pgvector_reachable is True
        assert report.brain_documents_count == 2
        assert report.brain_edges_count == 1
        assert report.max_indexed_at == datetime(2026, 3, 1)
        assert report.max_authored_at == datetime(2026, 2, 1)


class TestPulseEdgesEmptyButRelatedExists:
    """The load-bearing signal: brain_edges empty while related: frontmatter exists."""

    def test_flags_unhealthy_when_edges_empty_but_related_exists(self, pgvector_session):
        pgvector_session.add(_make_doc("D36", related=["D41", "D50"]))
        pgvector_session.flush()

        report = pulse(session=pgvector_session, embedding_service=_FakeEmbeddingService())

        assert report.brain_edges_count == 0
        assert report.edges_empty_but_related_exists is True
        assert report.healthy is False

    def test_healthy_corpus_reports_flag_false(self, pgvector_session):
        pgvector_session.add_all(
            [
                _make_doc("D36", related=["D41"]),
                _make_doc("D41"),
                _make_edge("D36", "D41"),
            ]
        )
        pgvector_session.flush()

        report = pulse(session=pgvector_session, embedding_service=_FakeEmbeddingService())

        assert report.edges_empty_but_related_exists is False
        assert report.healthy is True

    def test_no_related_and_no_edges_is_not_flagged(self, pgvector_session):
        pgvector_session.add(_make_doc("D36"))
        pgvector_session.flush()

        report = pulse(session=pgvector_session, embedding_service=_FakeEmbeddingService())

        assert report.brain_edges_count == 0
        assert report.edges_empty_but_related_exists is False
        assert report.healthy is True


class TestPulseEmbeddingProbe:
    """An unreachable embedding backend degrades the report; it never raises."""

    def test_unreachable_embedding_backend_is_captured_not_raised(self, pgvector_session):
        pgvector_session.add(_make_doc("D36"))
        pgvector_session.flush()

        report = pulse(session=pgvector_session, embedding_service=_FailingEmbeddingService())

        assert report.embedding_reachable is False
        assert report.embedding_error is not None
        assert report.healthy is False
        assert any("embedding backend unreachable" in err for err in report.errors)


class TestPulseRecoversAbortedTransaction:
    """A pgvector probe failure must roll back the session before returning.

    Regression for the sandbox `jynxpoc` incident: a transient DB error mid-probe
    (e.g. "server closed the connection unexpectedly") aborted the session's
    transaction; `_probe_pgvector` caught the exception but never rolled back, so
    `database.session.db_session`'s own cleanup `session.commit()` raised
    `PendingRollbackError` — turning every subsequent `/pulse` call into a 500
    until the process was restarted, contradicting this module's own contract
    that pulse() never raises for a degraded backend. A real Postgres session's
    exact "aborted transaction" error shape is awkward to reproduce cheaply and
    portably in a unit test, so this asserts the fix directly: a failed probe
    must call `session.rollback()`.
    """

    def test_probe_failure_rolls_back_the_session(self):
        from unittest.mock import MagicMock

        from brain.pulse import _probe_pgvector

        session = MagicMock()
        session.query.side_effect = RuntimeError("server closed the connection unexpectedly")

        result = _probe_pgvector(session)

        assert result["reachable"] is False
        assert "server closed the connection unexpectedly" in result["error"]
        session.rollback.assert_called_once()


class TestPulseReportSerialization:
    """`to_dict()` produces a JSON-serializable payload (datetimes as ISO strings)."""

    def test_to_dict_serializes_datetimes(self, pgvector_session):
        pgvector_session.add(_make_doc("D36"))
        pgvector_session.flush()

        report = pulse(session=pgvector_session, embedding_service=_FakeEmbeddingService())
        payload = report.to_dict()

        assert isinstance(payload["max_indexed_at"], str)
        assert isinstance(payload["max_authored_at"], str)
