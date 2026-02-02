"""Pytest configuration and fixtures for xnatctl tests."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Generator

import pytest


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def sample_config_yaml() -> str:
    """Sample config YAML content."""
    return """
default_profile: test
output_format: table

profiles:
  test:
    url: https://xnat-test.example.org
    verify_ssl: false
    timeout: 30
    default_project: TESTPROJ

  production:
    url: https://xnat.example.org
    verify_ssl: true
    timeout: 60
"""


@pytest.fixture
def sample_config_with_credentials_yaml() -> str:
    """Sample config YAML with credentials."""
    return """
default_profile: test
output_format: table

profiles:
  test:
    url: https://xnat-test.example.org
    username: testuser
    password: testpass
    verify_ssl: false
    timeout: 30
    default_project: TESTPROJ

  production:
    url: https://xnat.example.org
    username: produser
    password: prodpass
    verify_ssl: true
    timeout: 60
"""


@pytest.fixture
def sample_patterns_json() -> str:
    """Sample patterns JSON for label fixes."""
    return """
{
  "description": "Test patterns",
  "patterns": [
    {
      "project": "TEST01",
      "match": "^(SUB\\\\d{3})$",
      "to": "{project}_{1}",
      "description": "SUBNNN -> TEST01_SUBNNN"
    }
  ]
}
"""
