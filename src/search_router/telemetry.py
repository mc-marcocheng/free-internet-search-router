from opentelemetry import metrics, trace


class Telemetry:
    def __init__(self):
        self.tracer = trace.get_tracer("search_router")
        meter = metrics.get_meter("search_router")
        self.requests = meter.create_counter("router.logical_requests")
        self.attempts = meter.create_counter("router.provider_attempts")
        self.decisions = meter.create_counter("router.decisions")
        self.cache = meter.create_counter("router.cache_hits")
        self.latency = meter.create_histogram("router.attempt_latency_ms")
        self.quorum = meter.create_histogram("router.contributing_engines")
        self.credits = meter.create_counter("router.provider_credits")
        self.estimated_cost = meter.create_counter("router.estimated_cost_usd")


def configure(endpoint: str | None):
    if not endpoint:
        return None
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create({"service.name": "internet-search-router"})
    traces = TracerProvider(resource=resource)
    traces.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint + "/v1/traces"))
    )
    meters = MeterProvider(
        resource=resource,
        metric_readers=[
            PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=endpoint + "/v1/metrics"))
        ],
    )
    trace.set_tracer_provider(traces)
    metrics.set_meter_provider(meters)
    return traces, meters
