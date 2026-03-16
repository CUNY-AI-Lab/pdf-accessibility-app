from fastapi.testclient import TestClient

from app import main


class _DummyJobManager:
    async def shutdown(self):
        return None


async def _noop_async():
    return None


def test_create_app_serves_built_frontend(tmp_path, monkeypatch):
    dist_dir = tmp_path / "frontend" / "dist"
    assets_dir = dist_dir / "assets"
    assets_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text(
        "<!doctype html><html><body><div id='root'></div></body></html>",
        encoding="utf-8",
    )
    (dist_dir / "vite.svg").write_text("<svg></svg>", encoding="utf-8")
    (assets_dir / "app.js").write_text("console.log('ok');", encoding="utf-8")

    monkeypatch.setattr(main, "ensure_dirs", lambda: None)
    monkeypatch.setattr(main, "init_db", _noop_async)
    monkeypatch.setattr(main, "get_job_manager", lambda: _DummyJobManager())

    app = main.create_app(frontend_dist_dir=dist_dir)

    with TestClient(app) as client:
        root_response = client.get("/")
        assert root_response.status_code == 200
        assert "div id='root'" in root_response.text

        root_head_response = client.head("/")
        assert root_head_response.status_code == 200

        asset_response = client.get("/assets/app.js")
        assert asset_response.status_code == 200
        assert asset_response.text == "console.log('ok');"

        missing_asset_response = client.get("/assets/missing.js")
        assert missing_asset_response.status_code == 404

        spa_response = client.get("/review/123")
        assert spa_response.status_code == 200
        assert "div id='root'" in spa_response.text

        health_response = client.get("/health")
        assert health_response.status_code == 200
