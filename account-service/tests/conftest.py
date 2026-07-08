"""Test fixtures: give each test a fresh, isolated in-memory database."""
import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import otel
from app.database import Base, get_db
from app.main import app

# Capture OTel spans in memory (no Collector/Jaeger needed for tests). Attached
# once to the provider set up at import; the `spans` fixture clears per test.
_memory_exporter = InMemorySpanExporter()
otel.provider.add_span_processor(SimpleSpanProcessor(_memory_exporter))


@pytest.fixture
def spans() -> InMemorySpanExporter:
    _memory_exporter.clear()
    return _memory_exporter


@pytest.fixture
def client():
    # In-memory SQLite shared across the connection pool (StaticPool) so the
    # schema persists for the duration of the test, isolated from ./account.db.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
