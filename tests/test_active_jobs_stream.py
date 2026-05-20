from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def test_active_jobs_stream_requires_auth(client: TestClient) -> None:
    response = client.get("/api/v1/me/active-jobs/stream")
    assert response.status_code == 401
