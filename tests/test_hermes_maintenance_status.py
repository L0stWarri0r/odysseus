from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.hermes_routes import setup_hermes_routes
from src.hermes_control.maintenance import build_maintenance_status


def _fake_runner(outputs):
    def run(args, cwd=None):
        key = tuple(args)
        if key not in outputs:
            raise AssertionError(f"unexpected command: {args!r} cwd={cwd!r}")
        value = outputs[key]
        if isinstance(value, Exception):
            raise value
        return value
    return run


class FakeAuthManager:
    is_configured = True

    def is_admin(self, username):
        return username == "admin"


def _client_with_user(username=None):
    app = FastAPI()
    app.state.auth_manager = FakeAuthManager()

    @app.middleware("http")
    async def _stamp_user(request, call_next):
        if username is not None:
            request.state.current_user = username
        return await call_next(request)

    app.include_router(setup_hermes_routes())
    return TestClient(app)


def test_build_maintenance_status_reports_git_and_pwa_metadata(tmp_path):
    repo = tmp_path / "odysseus"
    static_dir = repo / "static"
    static_dir.mkdir(parents=True)
    (static_dir / "sw.js").write_text("const CACHE_NAME = 'odysseus-cache-v7';", encoding="utf-8")
    (static_dir / "sw-reset.html").write_text("reset", encoding="utf-8")
    script = tmp_path / "hermes" / "scripts" / "odysseus_daily_main_update.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")

    status = build_maintenance_status(
        repo_path=repo,
        intake_script=script,
        runner=_fake_runner(
            {
                ("git", "rev-parse", "--show-toplevel"): str(repo),
                ("git", "branch", "--show-current"): "lost/personal-core",
                ("git", "status", "--porcelain"): "",
                ("git", "status", "--short", "--branch"): "## lost/personal-core...origin/lost/personal-core [ahead 1]",
                ("git", "rev-parse", "--short", "HEAD"): "5db0651",
                ("git", "log", "-1", "--pretty=%s"): "fix: stabilize modal drag release",
                ("git", "rev-parse", "--short", "upstream/main"): "7c7ac10",
            }
        ),
    )

    assert status["content_returned"] is False
    assert status["repo"]["exists"] is True
    assert status["repo"]["branch"] == "lost/personal-core"
    assert status["repo"]["dirty"] is False
    assert status["repo"]["ahead"] == 1
    assert status["repo"]["behind"] == 0
    assert status["repo"]["head"] == "5db0651"
    assert status["repo"]["upstream_main"] == "7c7ac10"
    assert status["pwa"]["service_worker_exists"] is True
    assert status["pwa"]["reset_url"] == "/static/sw-reset.html"
    assert status["pwa"]["cache_prefix"] == "odysseus-"
    assert status["automation"]["daily_intake_script_exists"] is True
    assert "daily_intake_script" not in status["automation"]
    assert str(script) not in str(status)
    lowered = str(status).lower()
    assert "password" not in lowered
    assert "secret" not in lowered


def test_build_maintenance_status_handles_missing_repo(tmp_path):
    status = build_maintenance_status(repo_path=tmp_path / "missing")

    assert status["repo"]["exists"] is False
    assert status["repo"]["branch"] is None
    assert status["status"] == "missing_repo"


def test_hermes_maintenance_status_route_requires_admin(tmp_path, monkeypatch):
    repo = tmp_path / "missing-odysseus"
    monkeypatch.setenv("ODYSSEUS_REPO", str(repo))

    assert _client_with_user(None).get("/api/hermes/maintenance/status").status_code == 403
    assert _client_with_user("nonadmin").get("/api/hermes/maintenance/status").status_code == 403

    response = _client_with_user("admin").get("/api/hermes/maintenance/status")

    assert response.status_code == 200
    data = response.json()
    assert data["repo"]["path"] == str(repo)
    assert data["repo"]["exists"] is False
    assert data["content_returned"] is False
