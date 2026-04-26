import pytest
from sdlc_factory.telemetry import setup_telemetry

def test_setup_telemetry_disabled(mocker):
    # Ensure it doesn't initialize when disabled
    mocker.patch("sdlc_factory.telemetry._TELEMETRY_INITIALIZED", False)
    mock_instrument = mocker.patch("sdlc_factory.telemetry.GoogleGenAIInstrumentor.instrument")
    setup_telemetry({"tracing_enabled": False})
    mock_instrument.assert_not_called()

def test_setup_telemetry_enabled(mocker):
    mocker.patch("sdlc_factory.telemetry._TELEMETRY_INITIALIZED", False)
    mock_instrument = mocker.patch("sdlc_factory.telemetry.GoogleGenAIInstrumentor.instrument")
    mock_provider = mocker.patch("sdlc_factory.telemetry.TracerProvider")
    
    setup_telemetry({"tracing_enabled": True, "tracing_endpoint": "http://localhost:4317"})
    mock_instrument.assert_called_once()
    mock_provider.assert_called()

def test_setup_telemetry_already_init(mocker):
    mocker.patch("sdlc_factory.telemetry._TELEMETRY_INITIALIZED", True)
    mock_instrument = mocker.patch("sdlc_factory.telemetry.GoogleGenAIInstrumentor.instrument")
    setup_telemetry({"tracing_enabled": True})
    mock_instrument.assert_not_called()

def test_setup_telemetry_fails_gracefully(mocker):
    mocker.patch("sdlc_factory.telemetry._TELEMETRY_INITIALIZED", False)
    mock_instrument = mocker.patch("sdlc_factory.telemetry.GoogleGenAIInstrumentor.instrument", side_effect=Exception("Failed"))
    setup_telemetry({"tracing_enabled": True})
    mock_instrument.assert_called()
    # Should not raise exception
