"""OpenTelemetry tests for the Event Gateway.

Uses an in-memory span exporter (see conftest `spans`) — no Collector or Jaeger
needed. Proves the Gateway emits a SERVER span, turns the Account Service call
into a CLIENT span in the same trace, and propagates `traceparent` downstream.
"""
from opentelemetry.trace import SpanKind

from app import otel


def _event(event_id="e", account_id="a", type_="CREDIT", amount=10):
    return {
        "eventId": event_id,
        "accountId": account_id,
        "type": type_,
        "amount": amount,
        "currency": "USD",
        "eventTimestamp": "2026-05-15T14:02:11Z",
    }


def test_emits_server_and_client_spans_in_one_trace(client, fake_account, spans):
    r = client.post("/events", json=_event())
    assert r.status_code == 201

    finished = spans.get_finished_spans()
    server = [s for s in finished if s.kind == SpanKind.SERVER]
    assert server, "expected a SERVER span for the gateway request"
    trace_id = server[0].context.trace_id

    # The outbound Account Service call is a CLIENT span in the SAME trace.
    client_spans = [s for s in finished if s.kind == SpanKind.CLIENT and s.context.trace_id == trace_id]
    assert client_spans, "expected the account call as a CLIENT span in the same trace"


def test_traceparent_propagated_to_account(client, fake_account):
    client.post("/events", json=_event())
    # httpx instrumentation injected a W3C traceparent into the downstream call.
    assert fake_account.last_traceparent is not None
    assert fake_account.last_traceparent.startswith("00-")


def test_build_provider_with_endpoint_adds_exporter():
    prov = otel._build_provider("http://localhost:4317")
    assert prov is not None
    prov.shutdown()
