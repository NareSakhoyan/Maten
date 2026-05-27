from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from conftest import PRIMARY_USER_ID


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def test_job_stream_requires_auth(client: TestClient) -> None:
    job_id = uuid4()
    response = client.get(f"/api/v1/jobs/{job_id}/stream")
    assert response.status_code == 401
