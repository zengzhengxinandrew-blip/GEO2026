"""Project membership registry. Global admins retain access to every project.

Existing projects have no implicit non-admin members: an admin must grant access.
The registry lives outside individual projects so a project cannot grant itself access.
"""

from __future__ import annotations

import fcntl
import threading
from contextlib import contextmanager

import geolib as G

ROLES = {"viewer": 1, "editor": 2, "admin": 3}
_LOCK = threading.RLock()


def _path():
    return G.WORK / ".auth" / "project_members.json"


@contextmanager
def _locked():
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK, (p.parent / ".project_members.lock").open("a+") as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


def _read():
    data = G.read_json(_path(), {}) or {}
    return data if isinstance(data, dict) else {}


def role_for(user: dict | None, slug: str) -> str | None:
    if not G.SLUG_OK.fullmatch(slug or ""):
        return None
    if not user or not user.get("active", True):
        return None
    if user.get("role") == "admin":
        return "admin"
    return _read().get(slug, {}).get(str(user.get("username", "")).casefold())


def allowed(user: dict | None, slug: str, required: str = "viewer") -> bool:
    return ROLES.get(role_for(user, slug), 0) >= ROLES[required]


def members(slug: str) -> list[dict]:
    if not G.SLUG_OK.fullmatch(slug or ""):
        raise ValueError("无效的项目标识")
    return [{"username": name, "role": role} for name, role in
            sorted(_read().get(slug, {}).items())]


def set_member(slug: str, username: str, role: str | None) -> None:
    if not G.SLUG_OK.fullmatch(slug or ""):
        raise ValueError("无效的项目标识")
    name = username.casefold().strip()
    if not name or (role is not None and role not in ROLES):
        raise ValueError("无效的项目成员或角色")
    with _locked():
        data = _read()
        project = data.setdefault(slug, {})
        if role is None:
            project.pop(name, None)
        else:
            project[name] = role
        if not project:
            data.pop(slug, None)
        G.write_json(_path(), data)


def remove_user(username: str) -> None:
    name = username.casefold()
    with _locked():
        data = _read()
        for project in data.values():
            project.pop(name, None)
        G.write_json(_path(), data)
