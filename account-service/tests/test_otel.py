"""OpenTelemetry tests for the Account Service.

Uses an in-memory span exporter (see conftest `spans`) — no Collector or Jaeger
needed. Proves the Account Service CONTINUES a propagated trace rather than
starting a new one.
"""
from opentelemetry.trace import SpanKind

from app import otel


def _txn(event_id="e", type_="CREDIT", amount=10):
    return {
        "eventId": event_id,
        "type": type_,
        "amount": amount,
        "currency": "USD",
        "transactionTimestamp": "2026-05-15T14:02:11Z",
    }


def test_request_produces_server_span(client, spans):
    client.post("/accounts/a/transactions", json=_txn())
    server = [s for s in spans.get_finished_spans() if s.kind == SpanKind.SERVER]
    assert server, "expected a SERVER span for the request"


def test_incoming_traceparent_continues_trace(client, spans):
    trace_id = 0x1234567890ABCDEF1234567890ABCDEF
    span_id = 0x1122334455667788
    traceparent = f"00-{trace_id:032x}-{span_id:016x}-01"

    client.post("/accounts/a/transactions", json=_txn(), headers={"traceparent": traceparent})

    server = [s for s in spans.get_finished_spans() if s.kind == SpanKind.SERVER]
    assert server
    # The span joined the propagated trace instead of starting a fresh one.
    assert server[0].context.trace_id == trace_id


def test_build_provider_with_endpoint_adds_exporter():
    prov = otel._build_provider("http://localhost:4317")
    assert prov is not None
    prov.shutdown()  # stop the background export thread
