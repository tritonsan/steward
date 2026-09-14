from fastapi.testclient import TestClient
from test_workflow_jobs import opened

from steward.api import create_app


def test_factory_uses_cloud_simulation_setting_without_bypassing_sign_in(tmp_path, monkeypatch):
    runtime, _, _ = opened(tmp_path)
    monkeypatch.setenv("STEWARD_SIMULATION", "true")
    tokens = {"manager": {"actor_id": "Simon O.", "role": "manager"}}
    try:
        client = TestClient(create_app(runtime, tokens=tokens))
        assert client.get("/api/me").status_code == 401
        response = client.get("/api/me", headers={"Authorization": "Bearer manager"})
        assert response.status_code == 200
        assert response.json()["simulation"] is True
        explicit = TestClient(create_app(runtime, tokens=tokens, simulation=False))
        assert explicit.get("/api/me", headers={"Authorization": "Bearer manager"}).json()[
            "simulation"
        ] is False
    finally:
        runtime.close()
