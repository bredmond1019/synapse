"""app/brain/pulse.py — the Brain's health/pulse read core.

Reports whether the corpus and its retrieval substrate are actually usable:
pgvector reachability, embedding-model reachability, `brain_documents` /
`brain_edges` row counts, and a staleness watermark. The load-bearing signal
is ``edges_empty_but_related_exists`` — the exact silent failure that has
bitten twice (structural expansion no-oping on every query because
`brain_edges` was never loaded, even though `related:` frontmatter exists
across the corpus). `pulse()` never raises for a degraded backend and never
calls `sys.exit`; the CLI maps the returned verdict to an exit code.
"""

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime


@dataclass
class PulseReport:  # pylint: disable=too-many-instance-attributes
    """Structured health snapshot of the Brain corpus + retrieval substrate."""

    pgvector_reachable: bool
    embedding_reachable: bool
    embedding_error: str | None
    brain_documents_count: int
    brain_edges_count: int
    max_indexed_at: datetime | None
    max_authored_at: datetime | None
    edges_empty_but_related_exists: bool
    healthy: bool
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Return a JSON-serializable dict (datetimes as ISO 8601 strings)."""
        payload = asdict(self)
        for key in ("max_indexed_at", "max_authored_at"):
            if payload[key] is not None:
                payload[key] = payload[key].isoformat()
        return payload


def _probe_pgvector(session) -> dict:
    """Probe pgvector reachability and gather row counts + watermarks.

    Returns a dict of raw probe results; never raises (a DB-unavailable
    failure is captured in `error` and every other field defaults to a
    conservative "unknown" value).
    """
    # local imports: app/ only on sys.path at call time
    from database.brain_document import BrainDocument  # pylint: disable=import-outside-toplevel
    from database.brain_edge import BrainEdge  # pylint: disable=import-outside-toplevel
    from sqlalchemy import func  # pylint: disable=import-outside-toplevel

    result = {
        "reachable": True,
        "error": None,
        "brain_documents_count": 0,
        "brain_edges_count": 0,
        "max_indexed_at": None,
        "max_authored_at": None,
        "edges_empty_but_related_exists": False,
    }

    try:
        result["brain_documents_count"] = (
            session.query(func.count(BrainDocument.id)).scalar() or 0  # pylint: disable=not-callable
        )
        result["brain_edges_count"] = (
            session.query(func.count(BrainEdge.id)).scalar() or 0  # pylint: disable=not-callable
        )
        result["max_indexed_at"] = session.query(
            func.max(BrainDocument.indexed_at)  # pylint: disable=not-callable
        ).scalar()
        result["max_authored_at"] = session.query(
            func.max(BrainDocument.authored_at)  # pylint: disable=not-callable
        ).scalar()

        if result["brain_edges_count"] == 0:
            has_related = (
                session.query(BrainDocument)
                .filter(BrainDocument.related.isnot(None))
                .filter(func.cardinality(BrainDocument.related) > 0)  # pylint: disable=not-callable
                .first()
                is not None
            )
            result["edges_empty_but_related_exists"] = has_related
    except Exception as exc:  # pylint: disable=broad-exception-caught
        result["reachable"] = False
        result["error"] = f"pgvector unreachable: {exc}"
        logging.warning("pulse: pgvector probe failed: %s", exc)
        # A failed query leaves the session's transaction aborted; without this
        # rollback, db_session()'s own session.commit() on generator cleanup
        # raises PendingRollbackError, turning a reported "unreachable" into an
        # actual 500 — the exact thing this module's docstring says never happens.
        session.rollback()

    return result


def _probe_embedding(embedding_service) -> tuple[bool, str | None]:
    """Probe embedding-backend reachability. Never raises.

    Returns `(reachable, error)`; `error` is None when reachable.
    """
    try:
        if embedding_service is None:
            from services.embedding_service import (  # pylint: disable=import-outside-toplevel
                EmbeddingService,
            )

            embedding_service = EmbeddingService()
        embedding_service.embed_text("pulse")
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logging.warning("pulse: embedding probe failed: %s", exc)
        return False, f"embedding backend unreachable: {exc}"

    return True, None


def pulse(*, session=None, embedding_service=None) -> PulseReport:
    """Probe the Brain corpus + retrieval substrate and report its health.

    Args:
        session: An open SQLAlchemy session (injected; opened via
            `database.session.db_session` when omitted).
        embedding_service: An `EmbeddingService` instance (injected;
            constructed with defaults when omitted).

    Returns:
        A `PulseReport`. `healthy` is False when pgvector is unreachable,
        the embedding backend is unreachable, or `brain_edges` is empty
        while at least one `brain_documents` row has a non-empty `related`
        array (`edges_empty_but_related_exists`).
    """
    if session is None:
        from database.session import (  # pylint: disable=import-outside-toplevel
            db_session as _db_session,
        )

        session = next(_db_session())

    db_probe = _probe_pgvector(session)
    embedding_reachable, embedding_error = _probe_embedding(embedding_service)

    errors: list[str] = []
    if db_probe["error"]:
        errors.append(db_probe["error"])
    if embedding_error:
        errors.append(embedding_error)

    healthy = (
        db_probe["reachable"]
        and embedding_reachable
        and not db_probe["edges_empty_but_related_exists"]
    )

    return PulseReport(
        pgvector_reachable=db_probe["reachable"],
        embedding_reachable=embedding_reachable,
        embedding_error=embedding_error,
        brain_documents_count=db_probe["brain_documents_count"],
        brain_edges_count=db_probe["brain_edges_count"],
        max_indexed_at=db_probe["max_indexed_at"],
        max_authored_at=db_probe["max_authored_at"],
        edges_empty_but_related_exists=db_probe["edges_empty_but_related_exists"],
        healthy=healthy,
        errors=errors,
    )
