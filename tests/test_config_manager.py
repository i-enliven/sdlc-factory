import pytest
from sdlc_factory.utils import get_config

def test_get_config_returns_dict(mock_config):
    config = get_config()
    assert isinstance(config, dict)
    assert config["workspace_root"].endswith("test_workspace")
