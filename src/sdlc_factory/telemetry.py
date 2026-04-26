import logging
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.resources import Resource
from openinference.instrumentation.google_genai import GoogleGenAIInstrumentor
from sdlc_factory.utils import global_logger

_TELEMETRY_INITIALIZED = False

def setup_telemetry(config_data: dict) -> None:
    """Initializes OpenTelemetry tracing based on config."""
    global _TELEMETRY_INITIALIZED
    if _TELEMETRY_INITIALIZED:
        return
    _TELEMETRY_INITIALIZED = True
        
    tracing_enabled = config_data.get("tracing_enabled", False)
    
    if not tracing_enabled:
        global_logger.info("📡 Tracing is disabled in configuration.")
        return

    tracing_endpoint = config_data.get("tracing_endpoint", "http://127.0.0.1:4317")
    
    global_logger.info(f"📡 Initializing OpenTelemetry tracing using endpoint: {tracing_endpoint}")

    try:
        import os
        os.environ["PHOENIX_PROJECT_NAME"] = "sdlc-factory"
        
        resource = Resource(attributes={
            "service.name": "sdlc-factory",
            "project.name": "sdlc-factory",
            "openinference.project.name": "sdlc-factory"
        })
        tracer_provider = TracerProvider(resource=resource)
        
        # We use a try-except around adding the processor so we fallback gracefully if it fails to connect/resolve
        # Note: OpenTelemetry OTLP Exporter usually doesn't fail on init if unreachable, it fails on send or warning,
        # but wrapping it provides a fail-safe.
        processor = SimpleSpanProcessor(OTLPSpanExporter(endpoint=tracing_endpoint, insecure=True))
        tracer_provider.add_span_processor(processor)
        
        GoogleGenAIInstrumentor().instrument(tracer_provider=tracer_provider)
        global_logger.info("📡 Google GenAI instrumentation applied successfully.")
        
    except Exception as e:
        global_logger.error(f"⚠️ Failed to initialize tracing: {e}. Falling back to un-instrumented execution.")
