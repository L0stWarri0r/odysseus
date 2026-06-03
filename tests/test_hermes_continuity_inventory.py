import sqlite3
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.hermes_routes import setup_hermes_routes
from src.hermes_control.continuity import build_continuity_inventory


def _create_hermes_state_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            user_id TEXT,
            model TEXT,
            started_at REAL NOT NULL,
            title TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            timestamp REAL NOT NULL
        )
        """
    )
    con.executemany(
        "INSERT INTO sessions (id, source, user_id, model, started_at, title) VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("s-telegram", "telegram", "lost", "cloud", 10.0, "Telegram chat"),
            ("s-cli", "cli", "lost", "local", 20.0, "CLI chat"),
        ],
    )
    con.executemany(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        [
            ("s-telegram", "user", "private taste detail that must not be returned", 11.0),
            ("s-telegram", "assistant", "assistant reply", 12.0),
            ("s-cli", "tool", "tool output", 21.0),
        ],
    )
    con.commit()
    con.close()


def _sample_hermes_home(tmp_path: Path) -> Path:
    home = tmp_path / "hermes"
    (home / "memories").mkdir(parents=True)
    (home / "user-profile").mkdir(parents=True)
    (home / "skills" / "software-development" / "sample-skill").mkdir(parents=True)
    (home / "profiles" / "secure-web").mkdir(parents=True)
    (home / "memories" / "MEMORY.md").write_text("secret-ish durable note", encoding="utf-8")
    (home / "memories" / "USER.md").write_text("Lost profile details", encoding="utf-8")
    (home / "user-profile" / "lost-warrior-preferences.md").write_text("preference detail", encoding="utf-8")
    (home / "skills" / "software-development" / "sample-skill" / "SKILL.md").write_text("---\nname: sample\n---\n", encoding="utf-8")
    _create_hermes_state_db(home / "state.db")
    return home


def test_build_continuity_inventory_returns_counts_without_private_content(tmp_path):
    hermes_home = _sample_hermes_home(tmp_path)

    inventory = build_continuity_inventory(hermes_home)

    assert inventory["exists"] is True
    assert inventory["state_db"]["exists"] is True
    assert inventory["state_db"]["session_count"] == 2
    assert inventory["state_db"]["message_count"] == 3
    assert inventory["state_db"]["source_counts"] == {"cli": 1, "telegram": 1}
    assert inventory["state_db"]["role_counts"] == {"assistant": 1, "tool": 1, "user": 1}
    assert {item["name"] for item in inventory["memory_files"]} == {"MEMORY.md", "USER.md"}
    assert inventory["profile_markdown_count"] == 1
    assert inventory["skill_count"] == 1
    assert inventory["profile_names"] == ["secure-web"]
    assert inventory["content_returned"] is False
    assert "private taste detail" not in str(inventory)
    assert "secret-ish durable note" not in str(inventory)


def test_build_continuity_inventory_handles_missing_home(tmp_path):
    inventory = build_continuity_inventory(tmp_path / "missing-hermes")

    assert inventory["exists"] is False
    assert inventory["state_db"]["exists"] is False
    assert inventory["state_db"]["session_count"] == 0
    assert inventory["privacy_warnings"]


def test_hermes_continuity_inventory_route_uses_env_home(tmp_path, monkeypatch):
    hermes_home = _sample_hermes_home(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    app = FastAPI()
    app.include_router(setup_hermes_routes())
    client = TestClient(app)

    response = client.get("/api/hermes/continuity/inventory")

    assert response.status_code == 200
    data = response.json()
    assert data["hermes_home"] == str(hermes_home)
    assert data["state_db"]["session_count"] == 2
    assert data["content_returned"] is False
