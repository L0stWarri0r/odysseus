from pathlib import Path


def test_version_constants_and_fastapi_metadata_share_one_value():
    from core.constants import APP_VERSION as core_version
    from src.constants import APP_VERSION as src_version

    assert core_version == src_version == "1.0.0"

    app_source = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
    assert "APP_VERSION, BASE_DIR" in app_source
    assert "version=APP_VERSION" in app_source
