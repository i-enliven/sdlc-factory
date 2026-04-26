import pytest
from sdlc_factory.main import main

def test_main(mocker):
    mock_app = mocker.patch("sdlc_factory.main.app")
    main()
    mock_app.assert_called_once()
