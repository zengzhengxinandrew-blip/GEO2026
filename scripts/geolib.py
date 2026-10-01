"""GEO 工具箱共用模块：路径、配置、HTTP、正文抽取。

常规请求用 requests；受浏览器指纹拦截时可安全回退到 curl_cffi。两条路径都必须
校验 TLS，浏览器指纹回退不执行 JavaScript，也不处理登录或验证码。
"""

from __future__ import annotations

import fcntl
import ipaddress
import json
import os
import re
import socket
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as browser_requests
except ImportError:  # 本机轻量安装仍可运行；Docker 镜像会安装完整依赖
    browser_requests = None

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = Path(os.environ.get("GEOLOOK_ENV_FILE", ROOT / ".env")).expanduser()

# 仅页面可编辑项以持久化文件为准；部署参数仍由进程环境优先决定。
# 独立于 sample/publish 的导入，避免启动时循环依赖；测试校验与注册表一致。
UI_ENV_KEYS = frozenset({
    "ZHIPUAI_API_KEY", "GLM_MODEL", "ARK_API_KEY", "ARK_MODEL",
    "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "MOONSHOT_API_KEY", "MOONSHOT_MODEL",
    "MINIMAX_API_KEY", "MINIMAX_MODEL", "GEMINI_API_KEY", "GEMINI_MODEL",
    "OPENAI_API_KEY", "OPENAI_MODEL", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL",
    "XAI_API_KEY", "GROK_MODEL", "PERPLEXITY_API_KEY", "PERPLEXITY_MODEL",
    "GITHUB_TOKEN", "WP_USER", "WP_APP_PASSWORD", "PUBLISH_WEBHOOK_URL",
    "WECHAT_APPID", "WECHAT_APPSECRET", "X_API_KEY", "X_API_SECRET",
    "X_ACCESS_TOKEN", "X_ACCESS_SECRET", "REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET",
    "REDDIT_USERNAME", "REDDIT_PASSWORD",
})


def load_env(path: Path | None = None):
    """页面可编辑项：文件值（含显式空值）优先；其他项：非空环境变量优先。"""
    p = path or ENV_PATH
    if not p.exists():
        return
    for line in p.read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = re.sub(r"^export\s+", "", k.strip())
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
            continue
        v = v.strip().strip("'\"")
        # compose 注入的旧 Key 不能盖过页面保存的新值；空值是清除标记，
        # 不能删除该行，否则重启会重新启用 compose 的旧凭据。
        if k in UI_ENV_KEYS or not os.environ.get(k):
            os.environ[k] = v


load_env(ENV_PATH)

# Hosted deployments can keep mutable state outside the application directory
# so Docker images stay immutable and backups can target one mounted folder.
WORK = Path(os.environ.get("GEOLOOK_WORK_DIR", ROOT / "work")).expanduser().resolve()

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 GeoLookBot/1.0"
)
# 403/406 回退用：不带 GeoLookBot 标记的纯浏览器 UA。很多 WAF 规则只拦
# 「带工具标记的 UA」，回退能区分「拦工具」还是「拦 IP」，这本身是诊断信号。
UA_BROWSER = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------- 基础工具


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def slugify(text: str) -> str:
    text = re.sub(r"^https?://", "", (text or "").strip().lower())
    text = re.sub(r"[^a-z0-9一-鿿]+", "-", text).strip("-")
    return text[:48] or "project"


def die(msg: str, code: int = 1):
    print(f"[geo] 错误：{msg}", file=sys.stderr)
    sys.exit(code)


def info(msg: str):
    print(f"[geo] {msg}", file=sys.stderr)


# ---------------------------------------------------------------- 项目目录

SLUG_OK = re.compile(r"^[a-z0-9一-鿿][a-z0-9一-鿿-]{0,47}$")


def project_dir(slug: str) -> Path:
    if not SLUG_OK.match(slug or ""):
        die(f"非法项目标识：{slug!r}")
    return WORK / slug


@contextmanager
def project_lock(slug: str):
    """项目级跨进程锁：load-modify-write 操作必须走它。"""
    d = project_dir(slug)
    d.mkdir(parents=True, exist_ok=True)
    with (d / ".lock").open("w") as fd:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


def load_config(slug: str) -> dict:
    p = project_dir(slug) / "geo.json"
    if not p.exists():
        die(f"找不到项目配置 {p}，先运行：python3 scripts/geo.py init --url <网址>")
    return json.loads(p.read_text("utf-8"))


def has_site(cfg: dict) -> bool:
    """项目有没有自有网站。无站点项目（电商商品、线下品牌、小程序等）同样能做 GEO：
    抓取/体检/站内资产这几步不适用，但采样、竞品、阵地、内容、验收全都照常。
    判据只看 brand.site 是否为空——不引入第二个真相源。"""
    return bool((cfg.get("brand") or {}).get("site", "").strip())


def save_config(slug: str, cfg: dict):
    """写配置前先备份。geo.json 里是一期的人工投入（问题库、竞品、口径），
    被误覆盖的代价远大于留几个备份文件。"""
    p = project_dir(slug) / "geo.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        bak = p.parent / ".geo.bak"
        bak.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        (bak / f"geo-{stamp}.json").write_text(p.read_text("utf-8"), "utf-8")
        old = sorted(bak.glob("geo-*.json"))
        for f in old[:-10]:
            f.unlink()
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text("utf-8"))
    except json.JSONDecodeError:
        info(f"警告：{p} 已损坏，使用默认值")
        return default


def write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def read_jsonl(path: Path):
    p = Path(path)
    if not p.exists():
        return []
    out = []
    # 必须按 "\n" 切，不能用 splitlines()：后者还会在 U+2028/U+2029/U+0085/\v/\f
    # 处断行，而 json.dumps 不转义这些字符，抓到含 U+2028 的页面就会把一条记录
    # 劈成两半 → JSONDecodeError，整期体检中断。
    for line in p.read_text("utf-8").split("\n"):
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


# ---------------------------------------------------------------- HTTP

MAX_BYTES = 4_000_000  # 单页最多读 4MB，防止一头扎进安装包/大文件把管线拖死

# 这段地址常被代理软件用作 fake-IP 池。命中它不等于一定有问题，但若同时出现
# 自签名证书，几乎可以确定是代理/流量接管，而不是目标网站漏发中间证书。
FAKE_IP_NETS = (ipaddress.ip_network("198.18.0.0/15"),)

TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504}
WAF_CHALLENGE_MARKERS = (
    b"/cdn-cgi/challenge-platform/", b"window._cf_chl_opt",
    b"<title>just a moment", b"geetest_challenge", b"__cf_chl_",
    b"__jsl_clearance", b"acw_sc__v2", b"waf_verify", b"cf-chl-widget",
)
BROWSER_FALLBACK_STATUS = {403, 406}

# 一看就不是网页的路径，直接跳过（安装包、媒体、静态资源等）
SKIP_EXT = re.compile(
    r"\.(zip|gz|tgz|bz2|7z|rar|dmg|pkg|exe|msi|apk|ipa|deb|rpm|bin|iso"
    r"|mp4|mov|avi|mkv|mp3|wav|flac|png|jpe?g|gif|webp|svg|ico|bmp|tiff"
    r"|woff2?|ttf|eot|css|js|csv|xlsx?|docx?|pptx?|pdf)(\?|$)",
    re.I,
)
SKIP_PATH = re.compile(r"/(downloads?|dl|releases?|assets|static|cdn)/", re.I)


def is_fetchable(url: str) -> bool:
    if SKIP_EXT.search(url) or SKIP_PATH.search(url):
        return False
    tail = url.rstrip("/").rsplit("/", 1)[-1].lower()
    return tail not in {"download", "dl"}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"", "0", "false", "no", "off"}


def _tls_verify() -> bool | str:
    """每次请求时解析 CA，保证网页里更新 .env 后无需重启 Python 才生效。"""
    bundle = (os.environ.get("GEOLOOK_CA_BUNDLE")
              or os.environ.get("REQUESTS_CA_BUNDLE")
              or os.environ.get("CURL_CA_BUNDLE")
              or os.environ.get("SSL_CERT_FILE"))
    if not bundle:
        return True
    path = Path(bundle).expanduser()
    if not path.is_file():
        raise OSError(f"CA 证书文件不存在：{path}（检查 GEOLOOK_CA_BUNDLE 和 Docker 挂载路径）")
    return str(path)


def _empty_fetch(url: str, error: str = "") -> dict:
    return {"url": url, "final_url": url, "status": 0, "html": "", "content_type": "",
            "x_robots_tag": "", "elapsed": 0, "error": error,
            "ua_fallback": False, "browser_fallback": False,
            "fetch_transport": "requests"}


def _read_response_body(response) -> bytes:
    """兼容 requests 与 curl_cffi 响应；最多保留 MAX_BYTES。"""
    if hasattr(response, "iter_content"):
        chunks, size = [], 0
        for chunk in response.iter_content(65536):
            if not chunk:
                continue
            remaining = MAX_BYTES - size
            chunks.append(chunk[:remaining])
            size += min(len(chunk), remaining)
            if size >= MAX_BYTES:
                break
        return b"".join(chunks)
    raw = getattr(response, "content", b"") or b""
    return bytes(raw[:MAX_BYTES])


def _response_result(url: str, response, started: float, *, ua_fallback: bool = False,
                     browser_fallback: bool = False) -> dict:
    """把两种 HTTP 客户端统一成抓取器内部结构，避免两套解析逻辑逐渐漂移。"""
    status = int(response.status_code)
    ctype = response.headers.get("Content-Type", "")
    xrobots = response.headers.get("X-Robots-Tag", "")
    final_url = str(response.url)
    transport = "browser-fingerprint" if browser_fallback else "requests"
    if ctype and not any(k in ctype.lower() for k in ("html", "text/plain", "xml")):
        return {"url": url, "final_url": final_url, "status": status, "html": "",
                "content_type": ctype, "x_robots_tag": xrobots,
                "elapsed": round(time.time() - started, 2),
                "error": f"跳过非网页内容（{ctype.split(';')[0]}）",
                "ua_fallback": ua_fallback, "browser_fallback": browser_fallback,
                "fetch_transport": transport}

    raw = _read_response_body(response)
    low = raw[:60000].lower()
    challenge = any(marker in low for marker in WAF_CHALLENGE_MARKERS)
    if status == 200 and challenge:
        return {"url": url, "final_url": final_url, "status": 403, "html": "",
                "content_type": ctype, "x_robots_tag": xrobots,
                "elapsed": round(time.time() - started, 2),
                "error": "WAFChallenge: 返回的是浏览器验证页，不是官网正文",
                "ua_fallback": ua_fallback, "browser_fallback": browser_fallback,
                "fetch_transport": transport, "waf_challenge": True}

    enc = getattr(response, "encoding", None)
    if not enc or str(enc).lower() == "iso-8859-1":
        m = re.search(rb'charset=["\']?([\w\-]+)', raw[:4000], re.I)
        enc = m.group(1).decode("ascii", "ignore") if m else "utf-8"
    return {"url": url, "final_url": final_url, "status": status,
            "html": raw.decode(str(enc), "replace"), "content_type": ctype,
            "x_robots_tag": xrobots, "elapsed": round(time.time() - started, 2),
            "error": None, "ua_fallback": ua_fallback,
            "browser_fallback": browser_fallback, "fetch_transport": transport}



def _retry_delay(response, attempt: int) -> float:
    """优先尊重服务端 Retry-After，且给重试等待设上限，避免后台任务假死。"""
    raw = (response.headers.get("Retry-After", "") if response is not None else "").strip()
    try:
        return max(0.5, min(12.0, float(raw))) if raw else min(6.0, 1.2 * (2 ** attempt))
    except ValueError:
        return min(6.0, 1.2 * (2 ** attempt))


def _network_context(url: str) -> str:
    """给 TLS/连接错误补充解析结果；只做诊断，不绕过证书或代理。"""
    host = urlparse(url).hostname or ""
    if not host:
        return ""
    try:
        ips = sorted({x[4][0] for x in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})
    except OSError:
        return ""
    fake = []
    for value in ips:
        try:
            ip = ipaddress.ip_address(value)
        except ValueError:
            continue
        if any(ip in net for net in FAKE_IP_NETS):
            fake.append(value)
    if fake:
        return (f"；DNS 解析到代理常用 fake-IP {', '.join(fake)}。若浏览器能开而 Python 失败，"
                "请检查代理分流/HTTPS 解密，或用 GEOLOOK_CA_BUNDLE 配置受信根证书")
    return f"；DNS={', '.join(ips[:3])}" if ips else ""


def _requests_fetch(url: str, timeout: int, retries: int, ua: str | None,
                    referer: str | None) -> dict:
    """常规 requests 抓取；只有 403/406/WAF 验证页才切换纯浏览器 UA。"""
    last = _empty_fetch(url)
    ua_plan = [ua or UA] + ([UA_BROWSER] if ua is None else [])
    for ua_idx, cur_ua in enumerate(ua_plan):
        for attempt in range(retries + 1):
            response = None
            try:
                started = time.time()
                headers = {
                    "User-Agent": cur_ua,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Accept-Encoding": "gzip, deflate",
                    "Cache-Control": "no-cache",
                    "Upgrade-Insecure-Requests": "1",
                }
                if referer:
                    headers["Referer"] = referer
                response = requests.get(
                    url, timeout=timeout, headers=headers, allow_redirects=True,
                    stream=True, verify=_tls_verify(),
                )
                if response.status_code in TRANSIENT_STATUS and attempt < retries:
                    wait = _retry_delay(response, attempt)
                    response.close()
                    time.sleep(wait)
                    continue
                # 403/406 不在同一 UA 上空转重试；换一次浏览器 UA 才有诊断价值。
                if response.status_code in BROWSER_FALLBACK_STATUS and ua_idx + 1 < len(ua_plan):
                    response.close()
                    last = {**_empty_fetch(url, f"HTTP {response.status_code}（默认 UA 被拦）"),
                            "status": response.status_code, "ua_fallback": False}
                    break
                result = _response_result(
                    url, response, started, ua_fallback=ua_idx > 0,
                )
                response.close()
                if result.get("waf_challenge") and ua_idx + 1 < len(ua_plan):
                    last = result
                    break
                return result
            except Exception as e:  # noqa: BLE001
                if response is not None:
                    try:
                        response.close()
                    except Exception:  # noqa: BLE001
                        pass
                extra = _network_context(url) if isinstance(
                    e, (requests.exceptions.SSLError, requests.exceptions.ConnectionError, OSError)
                ) else ""
                last = _empty_fetch(url, f"{type(e).__name__}: {e}{extra}")
                if attempt < retries:
                    time.sleep(_retry_delay(None, attempt))
                    continue
                # UA 不会修复证书、DNS 或连接错误；交给下一层传输回退一次即可。
                return last
    return last


def _browser_fetch(url: str, timeout: int, referer: str | None) -> dict | None:
    """浏览器 TLS/HTTP2 指纹回退；不执行 JS、不解验证码，也从不关闭证书校验。"""
    if browser_requests is None or not _env_bool("GEOLOOK_BROWSER_FALLBACK", True):
        return None
    response = None
    started = time.time()
    try:
        kwargs = {
            "timeout": timeout,
            "allow_redirects": True,
            "impersonate": "chrome",
            "verify": _tls_verify(),
            "stream": True,
            "headers": {"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
        }
        if referer:
            kwargs["referer"] = referer
        doh_url = os.environ.get("GEOLOOK_DOH_URL", "").strip()
        if doh_url:
            kwargs["doh_url"] = doh_url
        response = browser_requests.get(url, **kwargs)
        return _response_result(url, response, started, ua_fallback=True,
                                browser_fallback=True)
    except Exception as e:  # noqa: BLE001
        extra = _network_context(url)
        return {**_empty_fetch(url, f"BrowserFallbackError: {type(e).__name__}: {e}{extra}"),
                "ua_fallback": True, "browser_fallback": True,
                "fetch_transport": "browser-fingerprint"}
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:  # noqa: BLE001
                pass


def fetch(url: str, timeout: int = 12, retries: int = 1, ua: str | None = None,
          referer: str | None = None) -> dict:
    """返回 {url, final_url, status, html, x_robots_tag, elapsed, error}。只读网页，且有体积上限。
    ua 可换成 AI 爬虫的 User-Agent 做差异探测（WAF/CDN 是否单独拦 AI 爬虫）。"""
    if not is_fetchable(url):
        return {"url": url, "final_url": url, "status": 0, "html": "", "content_type": "",
                "x_robots_tag": "", "elapsed": 0, "error": "跳过：不是网页（下载/媒体/静态资源）"}
    primary = _requests_fetch(url, timeout, retries, ua, referer)
    # 显式 AI 爬虫 UA 是差异探测，换 Chrome 指纹会污染诊断结论，绝不能回退。
    if ua is not None:
        return primary
    should_fallback = (primary.get("status", 0) in BROWSER_FALLBACK_STATUS
                       or primary.get("status", 0) == 0
                       or primary.get("waf_challenge"))
    if not should_fallback:
        return primary
    fallback = _browser_fetch(url, timeout, referer)
    if fallback is None:
        return primary
    if fallback.get("status") == 200 and fallback.get("html"):
        return fallback
    # 两层都失败时保留原始错误和浏览器指纹层错误，避免后一层掩盖真正根因。
    errors = [x for x in (primary.get("error"), fallback.get("error")) if x]
    if errors:
        fallback["error"] = "；".join(dict.fromkeys(errors))
    return fallback


def fetch_text(url: str, timeout: int = 8) -> str:
    res = fetch(url, timeout=timeout, retries=1)
    return res["html"] if res["status"] == 200 else ""


# ---------------------------------------------------------------- robots.txt
# 按 RFC 9309 语义解析，而不是逐行正则：三个最容易误判的点——
#   1. 多个 User-agent 行共享同一组规则（组内第一个 UA 后面的会被逐行正则漏掉）
#   2. 具体 UA 组存在时通配符组整组失效（specificity，不看先后顺序）
#   3. 规则按最长路径匹配定胜负，同长时 Allow 胜出；支持 * 与 $ 通配符
# 所以「User-agent: * / Disallow: /」会封掉所有没有专属组的 AI 爬虫，
# 而「User-agent: GPTBot / Allow: /」会让 GPTBot 无视通配符组里的任何 Disallow。


def robots_parse(txt: str) -> list[dict]:
    """解析成 [{agents: [ua...], rules: [(allow, path)]}]。空 Disallow 值 = 全放行，不算规则。"""
    groups: list[dict] = []
    cur = None
    last_was_agent = False
    for raw in (txt or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field, value = field.strip().lower(), value.strip()
        if field == "user-agent":
            if cur is None or not last_was_agent:
                cur = {"agents": [], "rules": []}
                groups.append(cur)
            cur["agents"].append(value.lower())
            last_was_agent = True
        elif field in ("allow", "disallow"):
            last_was_agent = False
            if cur is not None and value:
                cur["rules"].append((field == "allow", value))
        else:
            last_was_agent = False
    return groups


def _robots_rule_rx(pattern: str) -> re.Pattern:
    rx = re.escape(pattern).replace(r"\*", ".*")
    if rx.endswith(r"\$"):
        rx = rx[:-2] + "$"
    return re.compile("^" + rx)


def robots_decision(groups: list[dict], ua: str, path: str) -> tuple[bool, str | None]:
    """某个爬虫（产品名，如 'GPTBot'）能否抓某路径。返回 (允许?, 命中的规则文本)。"""
    ua_l = (ua or "").lower()
    specific, spec_len, wildcard = None, -1, None
    for g in groups:
        for a in g["agents"]:
            if a == "*":
                if wildcard is None:
                    wildcard = g
            elif a and (a in ua_l or ua_l in a) and len(a) > spec_len:
                specific, spec_len = g, len(a)
    g = specific or wildcard
    if not g:
        return True, None
    path = path or "/"
    match_len, allowed, rule = -1, True, None
    for allow, pat in g["rules"]:
        if _robots_rule_rx(pat).match(path):
            plen = len(pat)
            # 最长匹配优先；同长时 Allow 胜出
            if plen > match_len or (plen == match_len and allow and not allowed):
                match_len, allowed = plen, allow
                rule = ("Allow: " if allow else "Disallow: ") + pat
    return allowed, rule


def same_site(a: str, b: str) -> bool:
    ha, hb = urlparse(a).netloc.lower(), urlparse(b).netloc.lower()
    ha, hb = ha.removeprefix("www."), hb.removeprefix("www.")
    return ha == hb or ha.endswith("." + hb) or hb.endswith("." + ha)


# 跟踪参数：同一个页面挂不同参数会被当成多个 URL 重复抓，先剥掉
TRACKING_PARAMS = {"fbclid", "gclid", "dclid", "msclkid", "igshid", "mc_cid", "mc_eid",
                   "ref", "spm", "scm"}


def normalize_url(base: str, href: str) -> str | None:
    if not href:
        return None
    href = href.strip()
    if href.startswith(("mailto:", "tel:", "javascript:", "#")):
        return None
    u = urljoin(base, href)
    u, _, _ = u.partition("#")
    parts = urlparse(u)
    if parts.query:
        qs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
              if not (k.lower().startswith("utm_") or k.lower() in TRACKING_PARAMS)]
        u = urlunparse(parts._replace(query=urlencode(qs)))
    return u


# ---------------------------------------------------------------- 正文抽取

_DROP_TAGS = ["script", "style", "noscript", "svg", "iframe", "form", "template"]
_BOILER = re.compile(r"(nav|header|footer|sidebar|menu|breadcrumb|cookie|banner|advert)", re.I)


def parse_html(html: str) -> BeautifulSoup:
    return BeautifulSoup(html or "", "lxml")


def main_text(soup: BeautifulSoup) -> str:
    # 恰好一个 <article> 才当正文容器；多个 <article>（列表页/分节页）时取 <main>
    # 整体，否则只保留第一节，数字/步骤/FAQ 全部丢失，正文评分被严重低估。
    articles = soup.find_all("article")
    body = (articles[0] if len(articles) == 1 else None) \
        or soup.find("main") or soup.body or soup
    clone = BeautifulSoup(str(body), "lxml")
    for t in clone(_DROP_TAGS):
        t.decompose()
    for t in clone.find_all(attrs={"class": _BOILER}):
        t.decompose()
    for t in clone.find_all(attrs={"id": _BOILER}):
        t.decompose()
    text = clone.get_text("\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", text)


CJK = re.compile(r"[一-鿿]")
KANA = re.compile(r"[぀-ヿ]")


def cjk_ratio(text: str) -> float:
    """中文字符占「中文字符 + 英文单词」的比例，用来判断这页到底是中文页还是英文页。"""
    cjk = len(CJK.findall(text))
    latin = len(re.findall(r"[A-Za-z][A-Za-z'\-]*", text))
    total = cjk + latin
    return round(cjk / total, 3) if total else 0.0


def page_language(text: str, lang_attr: str = "") -> str:
    """返回 zh / ja / en / mixed / unknown。html lang 属性只作参考，正文说了算。"""
    if len(text) < 80:
        la = (lang_attr or "").lower()
        return ("zh" if la.startswith("zh") else "en" if la.startswith("en")
                else "ja" if la.startswith("ja") else "unknown")
    # 日文：假名够多且占（假名 + 汉字）比例明显，避免把引用了一两个日语词的中文页判成 ja
    kana = len(KANA.findall(text))
    if kana >= 5 and kana / (kana + len(CJK.findall(text))) > 0.2:
        return "ja"
    r = cjk_ratio(text)
    return "zh" if r >= 0.5 else "en" if r <= 0.1 else "mixed"


def word_count(text: str) -> int:
    """中英日混排统一折算成「词」：CJK 1.6 字算 1 词（接近中英信息密度比）。
    假名并入 CJK 统计：假名为主的日文页不算的话词数会被严重低估。"""
    cjk = len(CJK.findall(text)) + len(KANA.findall(text))
    latin = len(re.findall(r"[A-Za-z][A-Za-z'\-]*", text))
    return int(cjk / 1.6 + latin)


def jsonld(soup: BeautifulSoup) -> list:
    out = []
    for tag in soup.find_all("script", type=lambda v: v and "ld+json" in v):
        try:
            data = json.loads(tag.string or "{}")
        except Exception:  # noqa: BLE001
            continue
        out.extend(data if isinstance(data, list) else [data])
    return out


def jsonld_types(blocks: list) -> list[str]:
    types = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        t = b.get("@type")
        if isinstance(t, list):
            types.extend(str(x) for x in t)
        elif t:
            types.append(str(t))
        for sub in b.get("@graph", []) or []:
            if isinstance(sub, dict) and sub.get("@type"):
                st = sub["@type"]
                types.extend(st if isinstance(st, list) else [st])
    return sorted(set(types))
