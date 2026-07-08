"""OpenTelemetry tracing for the Event Gateway (bonus).

Sets up a TracerProvider and auto-instruments FastAPI (SERVER span per request).
The Gateway is the trace origin: its outbound Account Service call is turned into
a CLIENT span, and the W3C `traceparent` header is injected automatically so the
Account Service continues the same trace.

httpx is instrumented per-client (`instrument_httpx_client`) rather than
globally — this way propagation works even through the test MockTransport, and
we avoid instrumenting unrelated httpx clients (e.g. the test client).

Export is opt-in via `OTEL_EXPORTER_OTLP_ENDPOINT` (async BatchSpanProcessor);
tests capture spans with an in-memory exporter instead.
"""
import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

SERVICE_NAME = "event-gateway"

provider: TracerProvider | None = None


def _build_provider(endpoint: str | None) -> TracerProvider:
    prov = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
    if endpoint:
        prov.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    return prov


def setup_tracing(app) -> TracerProvider:
    global provider
    provider = _build_provider(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app)
    return provider


def instrument_httpx_client(client):
    """Instrument a specific httpx client so its calls create CLIENT spans and
    carry the traceparent header downstream."""
    HTTPXClientInstrumentor().instrument_client(client)
    return client
