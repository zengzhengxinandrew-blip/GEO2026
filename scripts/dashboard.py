"""可观测看板：GEO 是周期性工作，关键信息是「这一期相对上一期变了什么」。

  python3 scripts/geo.py ui            # 起服务并打开浏览器

服务本身只用标准库 http.server，但顶层 import geolib 需要第三方依赖
（requests / beautifulsoup4 / lxml），缺失时会给出安装提示。
前端是 scripts/ui.html 单页应用，数据走 /api，
工单状态可以直接在界面上改（写回 tasks.json）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import threading
import time
import webbrowser
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

try:
    import geolib as G
except ModuleNotFoundError as e:
    raise SystemExit(f"缺少依赖：{e.name}。请先 pip3 install requests beautifulsoup4 lxml") from e
import jobs as J
import tasks as T

UI = Path(__file__).resolve().parent / "ui.html"


# ---------------------------------------------------------------- 数据聚合

def list_projects() -> list[dict]:
    out = []
    if not G.WORK.exists():
        return out
    for d in sorted(G.WORK.iterdir()):
        cfg_path = d / "geo.json"
        if not cfg_path.exists():
            continue
        cfg = G.read_json(cfg_path, {})
        audit = G.read_json(d / "audit.json", {})
        td = G.read_json(d / "tasks.json", {})
        s = td.get("summary", {})
        out.append({
            "slug": d.name,
            "name": cfg.get("brand", {}).get("name", d.name),
            "site": cfg.get("brand", {}).get("site", ""),
            "market": cfg.get("market", "cn"),
            "avg_score": audit.get("avg_score"),
            "pages": audit.get("page_count"),
            "tasks_total": s.get("total", 0),
            "tasks_done": s.get("by_status", {}).get("done", 0),
            "p0_open": sum(1 for t in td.get("tasks", [])
                           if t["priority"] == "P0" and t["status"] != "done"),
        })
    return out


def project(slug: str) -> dict:
    pdir = G.project_dir(slug)
    cfg = G.load_config(slug)
    audit = G.read_json(pdir / "audit.json", {})
    td = G.read_json(pdir / "tasks.json", {"tasks": [], "summary": {}})

    verify_hist = []
    vdir = pdir / "verify"
    import verify as V
    for f in sorted(vdir.glob("*.json"), key=V.report_key) if vdir.exists() else []:
        v = G.read_json(f, {})
        rs = v.get("results", [])
        verify_hist.append({
            "date": (v.get("verified_at") or f.stem)[:10],
            "pass": sum(1 for r in rs if r["verdict"] == "通过"),
            "fail": sum(1 for r in rs if r["verdict"] == "未达标"),
            "manual": sum(1 for r in rs if r["verdict"] == "待人工"),
            "avg_score": v.get("audit_avg_score"),
        })

    deliveries = sorted((d.name for d in (pdir / "delivery").iterdir() if d.is_dir()),
                        reverse=True) if (pdir / "delivery").exists() else []

    lint = G.read_json(pdir / "assets" / "drafts" / "_lint.json", None)

    # 成稿发布状态：content/ 里的每篇成稿 ↔ publish.json 的成功记录。
    # 行动计划页的「成稿发布」卡和问题库的「已发布」标记都吃这份数据。
    content_pub = []
    cdir = pdir / "content"
    if cdir.exists():
        import re as _re
        pub_by_path: dict[str, list] = {}
        for r in G.read_json(pdir / "publish.json", []) or []:
            if r.get("ok"):
                pub_by_path.setdefault(r.get("path", ""), []).append(
                    {"platform": r.get("platform"), "platform_name": r.get("platform_name"),
                     "url": r.get("url", ""), "at": r.get("at", "")})
        for f in sorted(cdir.glob("*.md")):
            if f.name == "facts.md":
                continue
            head = f.read_text("utf-8", "replace")[:800]
            m = _re.search(r"(?m)^#\s*(.+)$", head)
            content_pub.append({
                "path": f.name,
                "title": (m.group(1).strip() if m else f.name)[:80],
                "qids": _re.findall(r"\bq\d{3}\b", head),
                "published": pub_by_path.get(f"content/{f.name}", []),
            })

    return {
        "slug": slug,
        "brand": cfg.get("brand", {}),
        "market": cfg.get("market", "cn"),
        "audit": {"avg_score": audit.get("avg_score"), "page_count": audit.get("page_count"),
                  "grade_distribution": audit.get("grade_distribution", {}),
                  "language_coverage": audit.get("language_coverage", {}),
                  "site": audit.get("site", {}), "site_issues": audit.get("site_issues", []),
                  "layers": audit.get("layers", []),
                  "block_gap": audit.get("block_gap", []),
                  "pages": sorted(audit.get("pages", []), key=lambda p: p["score"])[:40]},
        "tasks": td.get("tasks", []),
        "verify_history": verify_hist,
        "deliveries": deliveries,
        "lint": {"total": (lint or {}).get("total_issues", 0), "high": (lint or {}).get("high", 0)},
        "content_pub": content_pub,
        "blueprint": G.read_json(pdir / "blueprint.json", None),
        "distribution": G.read_json(pdir / "distribution.json", {}),
        "question_count": len(cfg.get("questions", [])),
        "deliverables_files": sorted(f.name for f in (pdir / "deliverables").glob("*.html"))
                              if (pdir / "deliverables").exists() else [],
        "analytics": _analytics(slug),
        "facts_struct": _facts_struct(slug),
    }


def _facts_struct(slug: str):
    try:
        import generate
        f = generate.parse_facts(slug)
        f.pop("raw", None)
        return f
    except Exception:  # noqa: BLE001
        return {}


def workbench(slug: str, qid: str) -> dict:
    """内容工作台：定位某个问题现有的内容/草稿/大纲文件。"""
    pdir = G.project_dir(slug)
    cfg = G.load_config(slug)
    q = next((x for x in cfg.get("questions", []) if x.get("id") == qid), None)
    sources = []
    cdir = pdir / "content"
    if cdir.exists():
        for f in sorted(cdir.glob("*.md")):
            if qid and qid in f.read_text("utf-8", "replace")[:800]:
                sources.append({"kind": "content", "path": f.name})
    for kind, sub in (("draft", "drafts"), ("outline", "outlines")):
        f = pdir / "assets" / sub / f"{qid}.md"
        if f.exists():
            sources.append({"kind": kind, "path": f"{sub}/{qid}.md"})
    return {"question": q, "sources": sources}


def _analytics(slug: str):
    try:
        import analytics
        return analytics.build(slug)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------- HTTP

def asset_tree(slug: str) -> list[dict]:
    """资产目录，供界面预览。只列文本类文件。"""
    adir = G.project_dir(slug) / "assets"
    out = []
    if not adir.exists():
        return out
    for f in sorted(adir.rglob("*")):
        if f.is_file() and f.suffix in (".txt", ".json", ".html", ".md"):
            rel = f.relative_to(adir).as_posix()
            out.append({"path": rel, "size": f.stat().st_size,
                        "group": rel.split("/")[0] if "/" in rel else "根目录"})
    return out


def read_asset(slug: str, rel: str) -> dict:
    base = (G.project_dir(slug) / "assets").resolve()
    target = (base / rel).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise PermissionError(rel) from None
    if not target.is_file():
        raise FileNotFoundError(rel)
    return {"path": rel, "text": target.read_text("utf-8", "replace")}


def write_env(updates: dict[str, str]):
    """更新项目根目录 .env：值为空表示删除该行。同步进当前进程环境，让界面立即生效；
    任务子进程每次启动都重读 .env，天然生效。"""
    path = G.ENV_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text("utf-8").splitlines() if path.exists() else []
    for k, v in updates.items():
        pat = re.compile(rf"\s*(export\s+)?{re.escape(k)}\s*=")
        lines = [ln for ln in lines if not pat.match(ln)]
        if v:
            lines.append(f"{k}={v}")
            os.environ[k] = v
        else:
            os.environ.pop(k, None)
    path.write_text("\n".join(lines) + ("\n" if lines else ""), "utf-8")
    try:
        path.chmod(0o600)  # 密钥文件不给同机其他用户读
    except OSError:
        pass


def create_project(url: str, name: str, slug: str, market: str, max_pages: int) -> dict:
    import geo as CLI

    class A:  # 复用 CLI 的 init 逻辑，避免两份实现漂移
        pass
    a = A()
    a.url, a.name, a.slug, a.market, a.max_pages = url, name or None, slug or None, market, max_pages
    a.force = False          # 界面永不覆盖已有项目
    return CLI.cmd_init(a)


# ---------------------------------------------------------------- 用户认证
# 浏览器使用用户名/密码 + HttpOnly 会话 cookie。GEOLOOK_TOKEN 仅保留给
# 脚本、浏览器扩展和旧链接，不作为浏览器的日常登录方式。

AUTH_COOKIE = "glk_auth"          # 兼容旧 token cookie
SESSION_COOKIE = "glk_session"
LOGIN_PATH = "/__login"
SETUP_PATH = "/__setup"
LOGOUT_PATH = "/__logout"
SESSION_TTL = 12 * 60 * 60
_AUTH_LOCK = threading.RLock()
_SESSIONS: dict[str, dict] = {}
_LOGIN_ATTEMPTS: dict[str, list[float]] = {}


def _users_path() -> Path:
    return Path(os.environ.get("GEOLOOK_USERS_FILE", G.WORK / ".auth" / "users.json")).expanduser()


def _load_users() -> list[dict]:
    data = G.read_json(_users_path(), {"users": []}) or {"users": []}
    return data.get("users", []) if isinstance(data.get("users"), list) else []


def _save_users(users: list[dict]) -> None:
    path = _users_path()
    G.write_json(path, {"version": 1, "users": users})
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _public_user(user: dict) -> dict:
    return {k: user.get(k) for k in
            ("username", "role", "active", "created_at", "updated_at")}


def _validate_username(username: str) -> str:
    username = (username or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
        raise ValueError("用户名须为 3-32 位，只能包含字母、数字、点、下划线或连字符")
    return username


def _validate_password(password: str) -> str:
    if len(password or "") < 8:
        raise ValueError("密码至少需要 8 个字符")
    if len(password) > 256:
        raise ValueError("密码最多 256 个字符")
    return password


def _hash_password(password: str, *, salt: bytes | None = None) -> str:
    """使用标准库 scrypt，避免密码或可逆结果落盘。"""
    password = _validate_password(password)
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                            n=16384, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"


def _password_ok(password: str, encoded: str) -> bool:
    try:
        algo, n, r, p, salt, expected = encoded.split("$", 5)
        if algo != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt),
                                n=int(n), r=int(r), p=int(p), dklen=len(bytes.fromhex(expected)))
        return hmac.compare_digest(actual.hex(), expected)
    except (TypeError, ValueError):
        return False


def users_public() -> list[dict]:
    with _AUTH_LOCK:
        return [_public_user(u) for u in _load_users()]


def create_user(username: str, password: str, role: str = "user") -> dict:
    username = _validate_username(username)
    if role not in ("admin", "user"):
        raise ValueError("角色必须是 admin 或 user")
    password_hash = _hash_password(password)
    with _AUTH_LOCK:
        users = _load_users()
        if any(u.get("username", "").casefold() == username.casefold() for u in users):
            raise ValueError("用户名已存在")
        now = G.now_iso()
        user = {"username": username, "password_hash": password_hash, "role": role,
                "active": True, "created_at": now, "updated_at": now}
        users.append(user)
        _save_users(users)
        return _public_user(user)


def authenticate_user(username: str, password: str) -> dict | None:
    with _AUTH_LOCK:
        for user in _load_users():
            if (user.get("username", "").casefold() == (username or "").strip().casefold()
                    and user.get("active", True)
                    and _password_ok(password or "", user.get("password_hash", ""))):
                return _public_user(user)
    return None


def update_user(username: str, *, password: str | None = None,
                active: bool | None = None) -> dict:
    password_hash = _hash_password(password) if password is not None else None
    with _AUTH_LOCK:
        users = _load_users()
        user = next((u for u in users if u.get("username", "").casefold()
                     == (username or "").casefold()), None)
        if not user:
            raise ValueError("用户不存在")
        if active is False and user.get("role") == "admin":
            active_admins = [u for u in users if u.get("role") == "admin" and u.get("active", True)]
            if len(active_admins) <= 1:
                raise ValueError("不能停用最后一个管理员")
        if password_hash is not None:
            user["password_hash"] = password_hash
        if active is not None:
            user["active"] = bool(active)
        user["updated_at"] = G.now_iso()
        _save_users(users)
        return _public_user(user)


def delete_user(username: str, actor: str) -> None:
    if username.casefold() == actor.casefold():
        raise ValueError("不能删除当前登录用户")
    with _AUTH_LOCK:
        users = _load_users()
        target = next((u for u in users if u.get("username", "").casefold()
                       == username.casefold()), None)
        if not target:
            raise ValueError("用户不存在")
        if target.get("role") == "admin" and sum(
                1 for u in users if u.get("role") == "admin" and u.get("active", True)) <= 1:
            raise ValueError("不能删除最后一个管理员")
        _save_users([u for u in users if u is not target])


def _new_session(user: dict) -> str:
    sid = secrets.token_urlsafe(32)
    with _AUTH_LOCK:
        _SESSIONS[sid] = {"user": user, "expires": time.time() + SESSION_TTL}
    return sid


def _cookie(cookie_header: str | None, name: str) -> str:
    for part in (cookie_header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return ""


def _session_user(cookie_header: str | None) -> dict | None:
    sid = _cookie(cookie_header, SESSION_COOKIE)
    if not sid:
        return None
    with _AUTH_LOCK:
        session = _SESSIONS.get(sid)
        if not session or session["expires"] <= time.time():
            _SESSIONS.pop(sid, None)
            return None
        # 用户被停用后，已签发会话也立即失效。
        fresh = next((u for u in _load_users()
                      if u.get("username", "").casefold()
                      == session["user"]["username"].casefold()
                      and u.get("active", True)), None)
        if not fresh:
            _SESSIONS.pop(sid, None)
            return None
        session["expires"] = time.time() + SESSION_TTL
        session["user"] = _public_user(fresh)
        return session["user"]


def ensure_bootstrap_user(token: str | None = None) -> None:
    """服务器部署首次启动时自动创建管理员。

    优先用 GEOLOOK_ADMIN_PASSWORD；未配时沿用 GEOLOOK_TOKEN 作为初始密码，
    这样升级现有部署不会被锁在门外。
    """
    if _load_users():
        return
    password = os.environ.get("GEOLOOK_ADMIN_PASSWORD") or token
    if password:
        create_user(os.environ.get("GEOLOOK_ADMIN_USER", "admin"), password, "admin")


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def auth_ok(token: str | None, cookie_header: str | None,
            query_token: str | None = None, header_token: str | None = None) -> bool:
    """纯函数便于测试：任一凭证匹配即放行；未设 token 时全部放行。"""
    if not token:
        return True
    for cand in (query_token, header_token):
        if cand and hmac.compare_digest(cand, token):
            return True
    digest = _token_digest(token)
    for part in (cookie_header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == AUTH_COOKIE and v and hmac.compare_digest(v, digest):
            return True
    return False


def _login_html(wrong: bool = False, setup: bool = False, message: str = "") -> str:
    action = SETUP_PATH if setup else LOGIN_PATH
    title = "首次设置" if setup else "登录到工作台"
    button = "创建第一个用户名和密码" if setup else "登录"
    confirm = ('<label>确认密码</label><input name="confirm" type="password" '
               'autocomplete="new-password" required>') if setup else ""
    alert = message or ("用户名或密码不正确" if wrong else "")
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Get Found By AI</title><style>
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;
background:#FFF9F4;color:#1F2937;font:14px/1.5 system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}}
.wrap{{width:min(380px,100%)}}.brand{{font-size:24px;font-weight:600;margin-bottom:3px}}.brand span{{color:#EF6C1A}}
.sub{{color:#6B7280;font-size:13px;margin-bottom:28px}}form{{background:#fff;border:1px solid #E7E5E4;
padding:24px;border-radius:8px;box-shadow:0 10px 30px rgba(31,41,55,.08)}}h1{{font-size:18px;margin:0 0 18px}}
label{{display:block;font-size:12px;color:#6B7280;margin:12px 0 5px}}input{{width:100%;height:40px;border:1px solid #D1D5DB;
border-radius:7px;padding:0 11px;font:inherit}}input:focus{{outline:2px solid #FF8A3D;outline-offset:1px;border-color:#FF8A3D}}
button{{width:100%;height:40px;margin-top:20px;border:1px solid #EF6C1A;border-radius:7px;background:#FF8A3D;
color:#6B2C08;font-weight:600;cursor:pointer}}.err{{margin-top:12px;color:#DC2626;font-size:12px}}
.hint{{margin-top:12px;color:#6B7280;font-size:12px}}</style></head><body><div class="wrap">
<div class="brand">Get Found By <span>AI</span></div><div class="sub">AI 品牌可见度工作台</div>
<form method="post" action="{action}"><h1>{title}</h1>
<label>用户名</label><input name="username" autocomplete="username" minlength="3" maxlength="32" required autofocus>
<label>密码</label><input name="password" type="password" autocomplete="{'new-password' if setup else 'current-password'}" minlength="8" required>
{confirm}<button type="submit">{button}</button>{f'<div class="err">{alert}</div>' if alert else ''}
{('<div class="hint">首次使用：此账号将拥有用户管理权限。</div>' if setup else '')}</form></div></body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    TOKEN: str | None = None  # run() 注入；None = 不启用认证
    COOKIE_SECURE = False
    user: dict | None = None

    def log_message(self, *a):  # 静音访问日志
        pass

    def _auth(self) -> bool:
        """True=放行；False=已自行响应（401、302 或登录结果）。

        令牌只走两条不进访问日志的路：POST /__login 的请求体，或 X-Geolook-Token 头。
        ?token= 只为兼容旧链接保留，命中后立刻跳回干净地址。
        """
        u = urlparse(self.path)
        if self.command == "POST" and u.path == LOGIN_PATH:
            return self._do_login()
        if self.command == "POST" and u.path == SETUP_PATH:
            return self._do_setup()
        if self.command == "POST" and u.path == LOGOUT_PATH:
            return self._do_logout()
        user = _session_user(self.headers.get("Cookie"))
        if user:
            self.user = user
            return True
        qt = (parse_qs(u.query).get("token") or [None])[0]
        if Handler.TOKEN and qt and hmac.compare_digest(qt, Handler.TOKEN):
            # 令牌换 cookie 后跳回干净地址，别让令牌留在地址栏和访问日志里。
            # 只对 GET 跳：POST 被 302 会退化成 GET，脚本调用会莫名失败。
            if self.command == "GET":
                self._redirect_token(u.path or "/")
                return False
            self.user = {"username": "api-token", "role": "admin", "active": True}
            return True
        if Handler.TOKEN and auth_ok(Handler.TOKEN, self.headers.get("Cookie"),
                                     header_token=self.headers.get("X-Geolook-Token")):
            self.user = {"username": "api-token", "role": "admin", "active": True}
            return True
        if self.command == "GET":
            setup = not bool(_load_users())
            self._send(401, _login_html(setup=setup).encode("utf-8"),
                       "text/html; charset=utf-8")
        else:
            self._json({"error": "未授权：请先登录"}, 401)
        return False

    def _do_login(self) -> bool:
        """POST /__login：验用户名密码、种会话 cookie、跳首页。"""
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        form = parse_qs(raw)
        username = (form.get("username") or [""])[0]
        password = (form.get("password") or [""])[0]
        peer = self.client_address[0] if self.client_address else "unknown"
        now = time.time()
        attempts = [t for t in _LOGIN_ATTEMPTS.get(peer, []) if now - t < 300]
        if len(attempts) >= 10:
            self._send(429, _login_html(message="尝试次数过多，请 5 分钟后再试").encode("utf-8"),
                       "text/html; charset=utf-8")
            return False
        user = authenticate_user(username, password)
        if not user:
            attempts.append(now)
            _LOGIN_ATTEMPTS[peer] = attempts
            self._send(401, _login_html(wrong=True).encode("utf-8"),
                       "text/html; charset=utf-8")
            return False
        _LOGIN_ATTEMPTS.pop(peer, None)
        self._redirect_session("/", _new_session(user))
        return False

    def _do_setup(self) -> bool:
        """仅在用户库为空时允许创建首个管理员。"""
        if _load_users():
            self._send(409, _login_html(message="管理员已存在，请直接登录").encode("utf-8"),
                       "text/html; charset=utf-8")
            return False
        n = int(self.headers.get("Content-Length", 0))
        form = parse_qs(self.rfile.read(n).decode("utf-8", "replace") if n else "")
        username = (form.get("username") or [""])[0]
        password = (form.get("password") or [""])[0]
        confirm = (form.get("confirm") or [""])[0]
        try:
            if password != confirm:
                raise ValueError("两次输入的密码不一致")
            user = create_user(username, password, "admin")
        except ValueError as e:
            self._send(400, _login_html(setup=True, message=str(e)).encode("utf-8"),
                       "text/html; charset=utf-8")
            return False
        self._redirect_session("/", _new_session(user))
        return False

    def _do_logout(self) -> bool:
        sid = _cookie(self.headers.get("Cookie"), SESSION_COOKIE)
        with _AUTH_LOCK:
            _SESSIONS.pop(sid, None)
        secure = self._secure_cookie()
        self.send_response(302)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", f"{SESSION_COOKIE}=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/{secure}")
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _secure_cookie(self) -> str:
        return "; Secure" if (Handler.COOKIE_SECURE
                               or self.headers.get("X-Forwarded-Proto") == "https") else ""

    def _redirect_session(self, location: str, sid: str) -> None:
        secure = self._secure_cookie()
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Set-Cookie", f"{SESSION_COOKIE}={sid}; Max-Age={SESSION_TTL}; "
                                       f"HttpOnly; SameSite=Strict; Path=/{secure}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _redirect_token(self, location: str) -> None:
        """旧 token 链接换兼容 cookie，然后跳到不含令牌的地址。"""
        secure = self._secure_cookie()
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Set-Cookie", f"{AUTH_COOKIE}={_token_digest(Handler.TOKEN)}; "
                                       f"HttpOnly; SameSite=Strict; Path=/{secure}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _admin(self) -> bool:
        if self.user and self.user.get("role") == "admin":
            return True
        self._json({"error": "仅管理员可执行此操作"}, 403)
        return False

    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    # ------------------------------------------------------------ GET
    def do_GET(self):
        u = urlparse(self.path)
        p, q = unquote(u.path), parse_qs(u.query)
        if p == "/healthz":
            return self._json({"ok": True, "service": "geolook"})
        if not self._auth():
            return
        try:
            if p in ("/", "/index.html"):
                return self._send(200, UI.read_bytes(), "text/html; charset=utf-8")
            if p == "/api/auth/me":
                return self._json(self.user or {})
            if p == "/api/users":
                if not self._admin():
                    return
                return self._json(users_public())
            if p == "/api/projects":
                return self._json(list_projects())
            if p == "/api/actions":
                return self._json(J.ACTIONS)
            if p.startswith("/api/p/"):
                return self._json(project(p[len("/api/p/"):]))
            if p.startswith("/api/config/"):
                slug = p[len("/api/config/"):]
                return self._json(G.read_json(G.project_dir(slug) / "geo.json", {}))
            if p.startswith("/api/facts/"):
                slug = p[len("/api/facts/"):]
                f = G.project_dir(slug) / "content" / "facts.md"
                return self._json({"exists": f.exists(),
                                   "text": f.read_text("utf-8") if f.exists() else ""})
            if p.startswith("/api/assets/"):
                return self._json(asset_tree(p[len("/api/assets/"):]))
            if p.startswith("/api/asset/"):
                slug = p[len("/api/asset/"):]
                return self._json(read_asset(slug, q.get("path", [""])[0]))
            if p.startswith("/api/workbench/"):
                slug = p[len("/api/workbench/"):]
                return self._json(workbench(slug, q.get("qid", [""])[0]))
            if p.startswith("/api/samples/"):
                import sample as S
                slug = p[len("/api/samples/"):]
                try:
                    limit = max(1, min(2000, int(q.get("limit", ["300"])[0])))
                except ValueError:
                    limit = 300
                return self._json(S.list_samples(
                    slug, date=q.get("date", [""])[0], platform=q.get("platform", [""])[0],
                    qid=q.get("qid", [""])[0], flag=q.get("flag", [""])[0], limit=limit))
            if p.startswith("/api/sample/"):
                import sample as S
                slug = p[len("/api/sample/"):]
                r = S.get_sample(slug, q.get("key", [""])[0])
                return self._json(r or {"error": "找不到该样本"}, 200 if r else 404)
            if p.startswith("/api/collect/queue/"):
                # 浏览器插件的采样队列：按意图分组挑题 + 需人工采的平台
                import sample as S
                slug = p[len("/api/collect/queue/"):]
                cfg = G.load_config(slug)
                try:
                    limit = max(1, min(200, int(q.get("limit", ["20"])[0])))
                except ValueError:
                    limit = 20
                intent = q.get("intent", [""])[0]
                picked = [g for g in (q.get("groups", [""])[0] or "").split(",") if g.strip()]
                if not picked and intent == "buyer":
                    picked = sorted(S.BUYER_GROUPS)
                allq = cfg.get("questions", [])
                qs = [x for x in allq if not picked or x.get("group") in picked][:limit]
                counts: dict[str, int] = {}
                for x in allq:
                    g2 = x.get("group") or "未分组"
                    counts[g2] = counts.get(g2, 0) + 1
                groups = [{"name": g2, "count": c,
                           "buyer": g2 in S.BUYER_GROUPS} for g2, c in
                          sorted(counts.items(), key=lambda kv: -kv[1])]
                plats = [{"code": c, "label": lb, "market": mk}
                         for c, (lb, mk) in S.MANUAL_ONLY.items()]
                plats += [{"code": c, "label": s2["name"], "market": s2["market"]}
                          for c, s2 in S.PROVIDERS.items() if not S.available(c)]
                return self._json({"slug": slug, "brand": cfg.get("brand", {}).get("name", ""),
                                   "questions": qs, "platforms": plats,
                                   "groups": groups, "selected": picked})
            if p == "/api/keys":
                import sample as S
                rows = []
                for code, spec in S.PROVIDERS.items():
                    key = os.environ.get(spec["key_env"], "")
                    menv = spec.get("model_env")
                    rows.append({"code": code, "label": spec["name"], "market": spec["market"],
                                 "search": spec.get("search", False), "env": spec["key_env"],
                                 "ok": S.available(code),
                                 "key_tail": key[-4:] if len(key) >= 8 else "",
                                 "model": os.environ.get(menv) or spec.get("model", "") if menv else spec.get("model", ""),
                                 "model_env": menv,
                                 "model_set": bool(menv and os.environ.get(menv)),
                                 "note": spec.get("note", "")})
                for code, (label, mk) in S.MANUAL_ONLY.items():
                    rows.append({"code": code, "label": label, "market": mk,
                                 "search": True, "env": None, "ok": None})
                return self._json(rows)
            if p.startswith("/api/factcheck/"):
                slug = p[len("/api/factcheck/"):]
                return self._json(G.read_json(G.project_dir(slug) / "factcheck.json", []) or [])
            if p.startswith("/api/expand/"):
                slug = p[len("/api/expand/"):]
                return self._json(G.read_json(G.project_dir(slug) / "expand.json", {}) or {})
            if p.startswith("/api/publish/"):
                import publish as P
                slug = p[len("/api/publish/"):]
                pubs = []
                for code, spec in P.PUBLISHERS.items():
                    cfg = P._cfg(slug, code)
                    pubs.append({"code": code, "name": spec["name"], "note": spec["note"],
                                 "market": spec.get("market", "general"),
                                 "guide": spec.get("guide") or {},
                                 "env": spec["env"], "missing": P.missing_env(code),
                                 "cfg": [{"key": k, "hint": h, "value": cfg.get(k, "")}
                                         for k, h in spec["cfg"]]})
                return self._json({"publishers": pubs, "records": P.records(slug)})
            if p.startswith("/api/content/"):
                slug = p[len("/api/content/"):]
                base = (G.project_dir(slug) / "content").resolve()
                rel = q.get("path", [""])[0]
                if rel:
                    target = (base / rel).resolve()
                    try:
                        target.relative_to(base)
                    except ValueError:
                        return self._json({"error": "非法路径"}, 403)
                    if not target.is_file():
                        return self._json({"error": "文件不存在"}, 404)
                    return self._json({"path": rel, "text": target.read_text("utf-8", "replace")})
                files = sorted(f.name for f in base.glob("*.md")) if base.exists() else []
                return self._json({"files": files})
            if p == "/api/jobs":
                slug = q.get("slug", [None])[0]
                return self._json({"jobs": J.recent(slug),
                                   "running": J.running_for(slug) if slug else None})
            if p.startswith("/api/job/"):
                jid = p[len("/api/job/"):]
                job = J.get(jid)
                if not job:
                    return self._json({"error": "job not found"}, 404)
                try:
                    off = int(q.get("offset", ["0"])[0])
                except ValueError:
                    return self._json({"error": "offset 必须是整数"}, 400)
                text, new_off = J.tail(jid, off)
                return self._json({"job": job, "log": text, "offset": new_off})
            if p.startswith("/api/files/"):
                slug = p[len("/api/files/"):]
                pdir = G.project_dir(slug)
                def ls(sub, pat="*"):
                    d = pdir / sub
                    return sorted((x.name for x in d.glob(pat)), reverse=True) if d.exists() else []
                dv = pdir / "deliverables"
                return self._json({
                    "reports": [d for d in ls("reports") if d.startswith("2")],
                    "deliveries": [d for d in ls("delivery") if d.startswith("2")],
                    "samples": ls("samples", "*.md"),
                    "deliverables": sorted(f.name for f in dv.glob("*.html")) if dv.exists() else [],
                    "content": sorted(f.name for f in (pdir / "content").glob("*.md"))
                               if (pdir / "content").exists() else [],
                })
            if p.startswith("/files/"):
                rel = p[len("/files/"):]
                target = (G.WORK / rel).resolve()
                try:
                    target.relative_to(G.WORK.resolve())
                except ValueError:
                    return self._send(403, b"forbidden", "text/plain")
                if not target.is_file():
                    return self._send(404, b"not found", "text/plain")
                ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
                if ctype.startswith("text/") or ctype in ("application/json",):
                    ctype += "; charset=utf-8"
                return self._send(200, target.read_bytes(), ctype)
            return self._send(404, b"not found", "text/plain")
        except FileNotFoundError:
            return self._json({"error": "文件不存在"}, 404)
        except PermissionError:
            return self._json({"error": "非法路径"}, 403)
        except SystemExit:
            return self._json({"error": "项目不存在"}, 404)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    # ------------------------------------------------------------ POST
    def do_POST(self):
        if not self._auth():
            return
        p = unquote(urlparse(self.path).path)
        try:
            body = self._body()

            if p == "/api/users":
                if not self._admin():
                    return
                action = body.get("action") or "create"
                username = str(body.get("username") or "").strip()
                if action == "create":
                    user = create_user(username, str(body.get("password") or ""),
                                       str(body.get("role") or "user"))
                    return self._json({"ok": True, "user": user})
                if action == "reset-password":
                    user = update_user(username, password=str(body.get("password") or ""))
                    return self._json({"ok": True, "user": user})
                if action == "set-active":
                    if "active" not in body:
                        return self._json({"ok": False, "error": "缺少 active"}, 400)
                    if (self.user or {}).get("username", "").casefold() == username.casefold() \
                            and not bool(body["active"]):
                        return self._json({"ok": False, "error": "不能停用当前登录用户"}, 400)
                    user = update_user(username, active=bool(body["active"]))
                    return self._json({"ok": True, "user": user})
                if action == "delete":
                    delete_user(username, (self.user or {}).get("username", ""))
                    return self._json({"ok": True})
                return self._json({"ok": False, "error": f"未知操作：{action}"}, 400)

            if p == "/api/task":
                missing = [k for k in ("slug", "id", "status") if k not in body]
                if missing:
                    return self._json({"error": f"缺参数：{', '.join(missing)}"}, 400)
                valid = ("todo", "doing", "done", "blocked", "wontfix")  # 与 tasks.py 汇总口径一致
                if body["status"] not in valid:
                    return self._json({"ok": False, "error": f"非法状态：{body['status']}",
                                       "valid": list(valid)}, 400)
                try:
                    t = T.set_status(body["slug"], body["id"], body["status"], body.get("note", ""))
                except KeyError as e:
                    return self._json({"error": e.args[0] if e.args else str(e)}, 404)
                return self._json({"ok": True, "task": t})

            if p == "/api/init":
                url = (body.get("url") or "").strip()
                if not url:
                    return self._json({"ok": False, "error": "请填写官网地址"}, 400)
                cfg = create_project(url, body.get("name", ""), body.get("slug", ""),
                                     body.get("market", "cn"), int(body.get("max_pages", 25)))
                return self._json({"ok": True, "slug": cfg["slug"]})

            if p == "/api/run":
                job = J.start(body["slug"], body["action"], body.get("params") or {})
                return self._json({"ok": True, "job": job})

            if p.startswith("/api/sample/"):
                import sample as S
                slug = p[len("/api/sample/"):]
                key = body.get("key") or ""
                if not key:
                    return self._json({"ok": False, "error": "缺少 key"}, 400)
                res = S.patch_sample(slug, key, body.get("patch") or {})
                return self._json(res, 200 if res.get("ok") else 400)

            if p.startswith("/api/collect/"):
                # 浏览器插件回传样本。服务只绑 127.0.0.1，来源即本机用户。
                import sample as S
                slug = p[len("/api/collect/"):]
                records = body.get("records")
                if not isinstance(records, list) or not records:
                    return self._json({"ok": False, "error": "records 必须是非空数组"}, 400)
                if len(records) > 200:
                    return self._json({"ok": False, "error": "单次最多 200 条"}, 400)
                # 采样/导入类任务运行中会写同一份当日样本文件，先挡回避免并发写丢行
                jid = J.running_for(slug)
                job = J.get(jid) if jid else None
                if job and job.get("action") in ("sample", "sample-import", "serve", "cycle", "autopilot"):
                    return self._json({"ok": False,
                                       "error": f"任务「{job.get('label') or job.get('action')}」正在运行，"
                                                "会写同一份样本文件——等它结束后再上传"}, 409)
                with G.project_lock(slug):
                    res = S.collect_import(slug, records)
                return self._json(res, 200 if res.get("ok") else 400)

            if p.startswith("/api/job/") and p.endswith("/stop"):
                jid = p[len("/api/job/"):-len("/stop")]
                return self._json({"ok": J.stop(jid)})

            if p.startswith("/api/config/"):
                slug = p[len("/api/config/"):]
                cur = G.read_json(G.project_dir(slug) / "geo.json", {})
                cur.update(body)          # 整体覆盖字段，前端传完整对象
                G.save_config(slug, cur)
                return self._json({"ok": True})

            if p.startswith("/api/facts/"):
                slug = p[len("/api/facts/"):]
                f = G.project_dir(slug) / "content" / "facts.md"
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(body.get("text", ""), "utf-8")
                return self._json({"ok": True})

            if p.startswith("/api/asset/"):
                slug = p[len("/api/asset/"):]
                base = (G.project_dir(slug) / "assets").resolve()
                target = (base / body["path"]).resolve()
                try:
                    target.relative_to(base)
                except ValueError:
                    return self._json({"ok": False, "error": "非法路径"}, 403)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(body.get("text", ""), "utf-8")
                return self._json({"ok": True})

            if p == "/api/precheck":
                import analytics
                return self._json(analytics.precheck(body.get("text", "")))

            if p.startswith("/api/factcheck/"):
                slug = p[len("/api/factcheck/"):]
                items = body.get("items")
                if not isinstance(items, list):
                    return self._json({"ok": False, "error": "items 必须是数组"}, 400)
                G.write_json(G.project_dir(slug) / "factcheck.json", items)
                return self._json({"ok": True, "count": len(items)})

            if p.startswith("/api/content/"):
                slug = p[len("/api/content/"):]
                base = (G.project_dir(slug) / "content").resolve()
                rel = (body.get("path") or "").strip()
                # 文件名允许中文（现有成稿即中文名），只挡路径分隔符和隐藏文件；
                # 问题归属靠文件头的 qid 注释识别，不靠文件名
                if ("/" in rel or "\\" in rel or ".." in rel or rel.startswith(".")
                        or not rel.endswith(".md") or len(rel) <= 3):
                    return self._json({"ok": False, "error": "文件名须是 .md，不能包含路径"}, 400)
                base.mkdir(parents=True, exist_ok=True)
                (base / rel).write_text(body.get("text", ""), "utf-8")
                return self._json({"ok": True})

            if p == "/api/keys":
                import publish as P
                import sample as S
                allowed = set()
                for spec in S.PROVIDERS.values():
                    allowed.add(spec["key_env"])
                    if spec.get("model_env"):
                        allowed.add(spec["model_env"])
                for spec in P.PUBLISHERS.values():
                    allowed.update(spec["env"])
                updates = body.get("updates")
                if not isinstance(updates, dict) or not updates:
                    return self._json({"ok": False, "error": "updates 必须是非空对象"}, 400)
                bad = [k for k in updates if k not in allowed]
                if bad:
                    return self._json({"ok": False,
                                       "error": f"不允许的变量：{', '.join(bad)}"}, 400)
                clean = {k: str(v or "").strip() for k, v in updates.items()}
                if any("\n" in v or "\r" in v for v in clean.values()):
                    return self._json({"ok": False, "error": "值不能包含换行"}, 400)
                write_env(clean)
                return self._json({"ok": True})

            if p.startswith("/api/publishcfg/"):
                import publish as P
                slug = p[len("/api/publishcfg/"):]
                code = body.get("platform")
                if code not in P.PUBLISHERS:
                    return self._json({"ok": False, "error": f"未知渠道 {code}"}, 400)
                keys = {k for k, _ in P.PUBLISHERS[code]["cfg"]}
                cfg = G.read_json(G.project_dir(slug) / "geo.json", {})
                pub = cfg.setdefault("publishing", {})
                pub[code] = {k: str(v or "").strip() for k, v in (body.get("cfg") or {}).items()
                             if k in keys}
                G.save_config(slug, cfg)
                return self._json({"ok": True})

            if p.startswith("/api/publish/"):
                # 发布 = 外发动作：只响应界面上用户的明确点击，服务端绝不自行调用
                import publish as P
                slug = p[len("/api/publish/"):]
                r = P.publish(slug, body.get("platform", ""), body.get("path", ""),
                              body.get("title", ""))
                return self._json(r, 200 if r.get("ok") else 400)

            if p.startswith("/api/distribution/"):
                # 分发打勾：记录某问题的内容已铺到某阵地（人工确认口径，非自动判定）
                slug = p[len("/api/distribution/"):]
                qid, ch = (body.get("qid") or "").strip(), (body.get("channel") or "").strip()
                if not qid or not ch:
                    return self._json({"ok": False, "error": "缺 qid / channel"}, 400)
                path = G.project_dir(slug) / "distribution.json"
                dist = G.read_json(path, {})
                if body.get("on"):
                    dist.setdefault(qid, {})[ch] = G.now_iso()
                else:
                    dist.get(qid, {}).pop(ch, None)
                    if not dist.get(qid):
                        dist.pop(qid, None)
                G.write_json(path, dist)
                return self._json({"ok": True, "distribution": dist})

            if p == "/api/questions-add":
                slug = body.get("slug") or ""
                items = body.get("items")
                if not slug or not isinstance(items, list) or not items:
                    return self._json({"ok": False, "error": "缺 slug / items"}, 400)
                cfg = G.read_json(G.project_dir(slug) / "geo.json", {})
                qs = cfg.setdefault("questions", [])
                existing = {q.get("text", "").strip() for q in qs}
                series = {"cn": 1, "global": 101, "both": 901}
                used = {int(m.group(1)) for q in qs
                        if (m := re.match(r"q(\d+)$", str(q.get("id", ""))))}
                added = []
                for it in items:
                    text = str(it.get("text") or "").strip()
                    mk = it.get("market") if it.get("market") in series else "cn"
                    grp = str(it.get("group") or "场景").strip() or "场景"
                    if not text or text in existing:
                        continue
                    n = series[mk]
                    while n in used:
                        n += 1
                    used.add(n)
                    q = {"id": f"q{n:03d}", "group": grp, "market": mk, "text": text,
                         "source": "expand"}
                    qs.append(q)
                    existing.add(text)
                    added.append(q)
                if added:
                    G.save_config(slug, cfg)
                return self._json({"ok": True, "added": len(added),
                                   "ids": [q["id"] for q in added]})

            if p == "/api/sample-import":
                import sample as S
                path = G.project_dir(body["slug"]) / "samples" / body["file"]
                if body.get("text") is not None:
                    path.write_text(body["text"], "utf-8")
                S.sample_import(body["slug"], str(path))
                return self._json({"ok": True})

            return self._send(404, b"not found", "text/plain")
        except SystemExit:  # G.die 会 sys.exit
            return self._json({"ok": False, "error": "操作失败（常见原因：项目标识已被占用）"}, 400)
        except (ValueError, RuntimeError) as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)


def _monitor_tick():
    """周期复跑：geo.json 的 monitor.next_run 到期就自动跑完整一期。

    GEO 是周期性工作——只在看板服务运行时触发（单机自托管，没有独立守护进程），
    服务停着的那几天不补跑，到期后下次启动时跑一次。"""
    for d in (G.WORK.iterdir() if G.WORK.exists() else []):
        cfg_path = d / "geo.json"
        if not cfg_path.exists():
            continue
        cfg = G.read_json(cfg_path, {})
        mon = cfg.get("monitor") or {}
        every = mon.get("every_days")
        if not every or (mon.get("next_run") or "") > G.today():
            continue
        if J.running_for(d.name):
            continue  # 有任务在跑，下个 tick 再看
        try:
            J.start(d.name, "serve", {})
            mon["next_run"] = (date.today() + timedelta(days=int(every))).isoformat()
            cfg["monitor"] = mon
            G.save_config(d.name, cfg)
            G.info(f"周期复跑触发：{d.name}，下次 {mon['next_run']}")
        except (ValueError, RuntimeError) as e:
            G.info(f"周期复跑跳过 {d.name}：{e}")


def _monitor_loop():
    while True:
        try:
            _monitor_tick()
        except Exception as e:  # noqa: BLE001  调度线程绝不能死
            G.info(f"周期复跑检查出错：{type(e).__name__}: {e}")
        time.sleep(1800)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def run(port: int = 8765, open_browser: bool = True,
        host: str | None = None, token: str | None = None):
    host = host or os.environ.get("GEOLOOK_HOST") or "127.0.0.1"
    token = token or os.environ.get("GEOLOOK_TOKEN") or None
    if host not in ("127.0.0.1", "localhost") and not token:
        G.die(f"绑定到 {host} 会把看板暴露给网络上的所有人。"
              "先设置访问令牌再启动：export GEOLOOK_TOKEN=$(openssl rand -hex 16)")
    ensure_bootstrap_user(token)
    Handler.TOKEN = token
    Handler.COOKIE_SECURE = _env_bool("GEOLOOK_COOKIE_SECURE", host not in ("127.0.0.1", "localhost"))
    J.reap_orphans()  # 回收上次服务留下的 running 僵尸记录，恢复并发保护
    threading.Thread(target=_monitor_loop, daemon=True).start()
    srv = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}/"
    G.info(f"看板已启动：{url}（Ctrl+C 退出），访问需用户名和密码")
    if host not in ("127.0.0.1", "localhost"):
        G.info("经网络暴露：请用 Nginx 等反代做 HTTPS 终止并转发 X-Forwarded-Proto；"
               "令牌走登录表单或 X-Geolook-Token 头，不要放在 URL 里。")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        G.info("看板已停止")
    finally:
        srv.server_close()
