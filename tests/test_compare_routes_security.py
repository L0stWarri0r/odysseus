import pytest
from fastapi import HTTPException

from routes.compare_routes import _endpoint_visible_to_user


class _State:
    current_user = "bob"


class _AppState:
    class _Mgr:
        @staticmethod
        def is_admin(_user):
            return False

    auth_manager = _Mgr()


class _App:
    state = _AppState()


class _Req:
    state = _State()
    app = _App()


class _DbShouldNotResolve:
    def query(self, *_args, **_kwargs):  # pragma: no cover - failure path
        raise AssertionError("unsafe URLs must be rejected before DB lookup")


def test_compare_endpoint_rejects_link_local_metadata_url():
    with pytest.raises(HTTPException) as exc:
        _endpoint_visible_to_user(
            _DbShouldNotResolve(),
            "http://169.254.169.254/latest/meta-data",
            _Req(),
            "bob",
        )

    assert exc.value.status_code == 400
    assert "Unsafe model endpoint" in exc.value.detail
