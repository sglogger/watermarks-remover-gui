from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import contract  # noqa: E402
from app.config import Settings, reset_settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.upstream import UpstreamClient  # noqa: E402
from tests.fake_engine import make_transport  # noqa: E402


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("GUI_", "WR_", "WATERMARKS_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GUI_UPDATE_CHECK", "0")
    reset_settings()
    yield
    reset_settings()


def build_client(settings: Settings | None = None, **transport_kwargs) -> TestClient:
    """A TestClient whose engine is the fake transport, with the contract read."""
    app = create_app(settings or Settings())
    client = TestClient(app)
    client.__enter__()

    transport = make_transport(**transport_kwargs)
    app.state.client = UpstreamClient(
        "http://engine.test",
        client=httpx.AsyncClient(
            base_url="http://engine.test", transport=transport, timeout=10.0
        ),
    )
    spec = httpx.Client(transport=transport, base_url="http://engine.test").get(
        "/openapi.json"
    )
    app.state.contract = contract.check_contract(spec.json())
    return client


@pytest.fixture
def client():
    test_client = build_client()
    yield test_client
    test_client.__exit__(None, None, None)
