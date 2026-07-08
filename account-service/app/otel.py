"""OpenTelemetry tracing for the Account Service (bonus).

Sets up a TracerProvider and auto-instruments FastAPI so every request becomes a
SERVER span. Incoming W3C `traceparent` headers are extracted automatically, so
a span here CONTINUES the trace the Gateway started (rather than beginning a new
one).

Export is opt-in: if `OTEL_EXPORTER_OTLP_ENDPOINT` is set (e.g. the Jaeger
all-in-one at http://jaeger:4317), spans are exported via OTLP; otherwise the
provider just records spans (which tests capture with an in-memory exporter).
Export is asynchronous (BatchSpanProcessor), so it never adds request latency.
"""
import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

SERVICE_NAME = "account-service"

# Set by setup_tracing(); tests attach an in-memory span exporter to it.
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
