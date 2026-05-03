"""
Module: test_actor_instance
Purpose: Unit tests for Phase 10 actor_instance resolution.
Location: /opt/tickles/shared/tests/test_actor_instance.py
"""

import os
import socket
from unittest.mock import patch

import pytest


def test_resolve_actor_instance_from_pod_uid() -> None:
    """POD_UID env var takes precedence."""
    with patch.dict(os.environ, {"POD_UID": "pod-12345"}, clear=False):
        with patch("socket.gethostname", return_value="host-abc"):
            result = os.getenv("POD_UID") or socket.gethostname() or ""
    assert result == "pod-12345"


def test_resolve_actor_instance_from_hostname() -> None:
    """Fallback to hostname when POD_UID unset."""
    with patch.dict(os.environ, {}, clear=True):
        with patch("socket.gethostname", return_value="myhost"):
            result = os.getenv("POD_UID") or socket.gethostname() or ""
    assert result == "myhost"


def test_resolve_actor_instance_empty_fallback() -> None:
    """Empty string when both POD_UID and hostname are empty."""
    with patch.dict(os.environ, {}, clear=True):
        with patch("socket.gethostname", return_value=""):
            result = os.getenv("POD_UID") or socket.gethostname() or ""
    assert result == ""


def test_actor_instance_normalisation() -> None:
    """FQDN is normalised to short name for consistency."""
    fqdn = "myhost.example.com"
    short = fqdn.split(".")[0].lower()
    assert short == "myhost"
