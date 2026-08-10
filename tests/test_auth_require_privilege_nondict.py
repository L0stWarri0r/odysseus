import types

import pytest
from fastapi import HTTPException

from src import auth_helpers
from src.auth_helpers import require_privilege


class _Mgr:
    def __init__(self, privs):
        self._privs = privs

    def get_privileges(self, user):
        return self._privs


def _request(mgr):
    state = types.SimpleNamespace(auth_manager=mgr)
    return types.SimpleNamespace(app=types.SimpleNamespace(state=state))


def test_require_privilege_fails_closed_on_non_dict_privileges(monkeypatch):
    # A corrupt auth.json can make get_privileges return a non-dict (e.g. a
    # list). Privilege checks must fail closed (403), not permit the action.
    monkeypatch.setattr(auth_helpers, "require_user", lambda request: "bob")
    req = _request(_Mgr(["do_x"]))
    with pytest.raises(HTTPException) as exc:
        require_privilege(req, "do_x")
    assert exc.value.status_code == 403


def test_require_privilege_still_blocks_disallowed(monkeypatch):
    monkeypatch.setattr(auth_helpers, "require_user", lambda request: "bob")
    req = _request(_Mgr({"do_x": False}))
    with pytest.raises(HTTPException):
        require_privilege(req, "do_x")
