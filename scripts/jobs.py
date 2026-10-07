"""后台任务：让界面能触发管线命令并看到实时日志。

用**子进程**而不是线程跑命令——管线里有 requests、文件写入和模块级状态，
子进程隔离最干净，也不会因为一个任务崩掉整个服务。

每个任务一份日志文件，界面轮询增量拉取。同一项目同时只允许一个任务在跑，
避免 crawl 和 verify 抢同一份 audit.json。
"""

from __future__ import annotations

import json
import hashlib
import filecmp
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import tempfile
import time
import uuid
from pathlib import Path

import geolib as G

JOBS_DIR = Path(os.environ.get("GEOLOOK_JOBS_DIR", G.ROOT / ".jobs")).expanduser().resolve()
GEO_PY = G.ROOT / "scripts" / "geo.py"

# 界面上可触发的动作。参数经过白名单，不接受任意命令。
ACTIONS: dict[str, dict] = {
    "crawl":    {"label": "抓取站点", "args": ["--max-pages"], "desc": "重新抓取官网页面"},
    "audit":    {"label": "页面体检", "args": [], "desc": "六维打分"},
    "sample":   {"label": "AI 答案采样", "args": ["--limit", "--repeat", "--platforms"],
                 "desc": "打问题库到各平台", "slow": True},
    "bootstrap":{"label": "自动推导底座", "args": ["--skip-llm"],
                 "desc": "从官网正文推出品牌事实、竞品、问题库", "slow": True},
    "deliverables":{"label": "出三份交付物", "args": [], "desc": "诊断报告 / 优化方案 / 执行方案"},
    "plan":     {"label": "生成工单", "args": [], "desc": "诊断结果 → 带验收标准的工单"},
    "expand":   {"label": "拓词扩题", "args": ["--no-llm"],
                 "desc": "下拉词扩出真实需求候选题（入库需手动勾选）"},
    "blueprint":{"label": "生成建设蓝图", "args": [], "desc": "在哪些平台建、建什么内容、覆盖度"},
    "generate": {"label": "生成资产", "args": ["--asset", "--draft", "--draft-limit"],
                 "desc": "llms.txt / JSON-LD / 片段 / 大纲"},
    "lint":     {"label": "初稿风险检查", "args": [], "desc": "查 AI 初稿的编造风险"},
    "report":   {"label": "生成报告", "args": [], "desc": "Markdown + HTML"},
    "verify":   {"label": "自动验收", "args": ["--no-recrawl"], "desc": "重抓并判定工单是否闭环",
                 "slow": True},
    "deliver":  {"label": "打包交付", "args": [], "desc": "客户交付包"},
    "sample-sheet": {"label": "导出人工采样表", "args": [], "desc": "无 API 平台用"},
    "autopilot":{"label": "建立项目底座", "args": ["--no-sample", "--limit", "--skip-llm",
                                                     "--skip-crawl"],
                 "desc": "首次建立事实、问题库、工单、资产与交付物", "slow": True},
    "serve":    {"label": "更新本期数据", "args": ["--max-pages", "--limit", "--no-sample",
                                                 "--draft", "--draft-limit", "--skip-crawl"],
                 "desc": "按本期重抓、体检、采样、报告、验收并交付", "slow": True},
}

FLAG_ARGS = {"--no-recrawl", "--draft", "--no-sample", "--skip-llm", "--no-llm",
             "--skip-crawl"}  # 布尔开关，无值

_lock = threading.Lock()
_running: dict[str, str] = {}   # slug -> job_id
_procs: dict[str, subprocess.Popen] = {}


class JobBusyError(RuntimeError):
    def __init__(self, job: dict | None = None):
        super().__init__("该项目已有任务在运行，等它结束或先停止")
        self.job = job


def _job_path(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.json"


def _log_path(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.log"


def _write(job: dict):
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    G.write_json(_job_path(job["id"]), job)


_MISSING = object()


def _merge_config(base, staged, live, conflicts: list[str], path: str = ""):
    """Three-way merge: generated changes win only where live config stayed unchanged."""
    if staged == base:
        return live
    if live == base or live == staged:
        return staged
    if all(isinstance(x, dict) for x in (base, staged, live)):
        merged = {}
        for key in base.keys() | staged.keys() | live.keys():
            value = _merge_config(base.get(key, _MISSING), staged.get(key, _MISSING),
                                  live.get(key, _MISSING), conflicts,
                                  f"{path}.{key}" if path else key)
            if value is not _MISSING:
                merged[key] = value
        return merged
    conflicts.append(path or "配置")
    return live


def _publish_stage(slug: str, stage_root: Path, base_config: dict | None = None) -> tuple[int, list[str]]:
    """Release a successful pipeline's files together while dashboard reads wait."""
    source = stage_root / slug
    target = G.project_dir(slug)
    if not source.is_dir():
        raise FileNotFoundError(f"任务暂存目录不存在：{source}")
    replaced = []
    conflicts = []
    with G.project_lock(slug), G.acquire_publish_lock(slug):
        try:
            staged_cfg = source / "geo.json"
            live_cfg = target / "geo.json"
            if base_config is not None and staged_cfg.exists() and live_cfg.exists():
                merged = _merge_config(base_config, G.read_json(staged_cfg, {}),
                                       G.read_json(live_cfg, {}), conflicts)
                G.write_json(staged_cfg, merged)
            for src in sorted(source.rglob("*")):
                if not src.is_file() or src.name in (".lock", ".run.lock", ".publish.lock"):
                    continue
                rel = src.relative_to(source)
                dst = target / rel
                if dst.exists() and filecmp.cmp(src, dst, shallow=False):
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                backup = stage_root / ".rollback" / rel if dst.exists() else None
                if backup:
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dst, backup)
                with tempfile.NamedTemporaryFile(dir=dst.parent, prefix=f".{dst.name}-",
                                                 delete=False) as out:
                    tmp = Path(out.name)
                try:
                    shutil.copy2(src, tmp)
                    os.replace(tmp, dst)
                finally:
                    tmp.unlink(missing_ok=True)
                replaced.append((dst, backup))
        except Exception:
            for dst, backup in reversed(replaced):
                if backup:
                    os.replace(backup, dst)
                else:
                    dst.unlink(missing_ok=True)
            raise
    return len(replaced), conflicts


def get(job_id: str) -> dict | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", job_id or ""):
        return None
    p = _job_path(job_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception:  # noqa: BLE001  损坏的 job 文件不该把轮询打成 500
        return None


def tail(job_id: str, offset: int = 0) -> tuple[str, int]:
    """返回 (增量文本, 新 offset)。界面按 offset 轮询，不重复拉。"""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", job_id or ""):
        return "", offset
    p = _log_path(job_id)
    if not p.exists():
        return "", offset
    data = p.read_bytes()
    chunk = data[offset:]
    return chunk.decode("utf-8", "replace"), len(data)


def running_for(slug: str) -> str | None:
    with _lock:
        jid = _running.get(slug)
    if jid:
        j = get(jid)
        if j and j["status"] in ("running", "stopping"):
            return jid
    if G.run_locked(slug):
        for j in recent(slug, limit=50):
            if j.get("status") in ("running", "stopping"):
                return j["id"]
    return None


def recent(slug: str | None = None, limit: int = 12) -> list[dict]:
    if not JOBS_DIR.exists():
        return []
    out = []
    for f in sorted(JOBS_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            j = json.loads(f.read_text("utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if slug and j.get("slug") != slug:
            continue
        j.pop("cmd", None)
        out.append(j)
        if len(out) >= limit:
            break
    return out


def start(slug: str, action: str, params: dict | None = None,
          started_by: str = "scheduler", reserved_fd=None,
          scheduled_next_run: str | None = None) -> dict:
    if action not in ACTIONS:
        raise ValueError(f"不支持的动作：{action}")
    run_fd = reserved_fd or G.acquire_run_lock(slug)
    if run_fd is None:
        jid = running_for(slug)
        raise JobBusyError(get(jid) if jid else None)

    spec = ACTIONS[action]
    cmd = [sys.executable, "-u", str(GEO_PY), action, "--slug", slug]
    for k, v in (params or {}).items():
        flag = k if k.startswith("--") else "--" + k
        if flag not in spec["args"]:
            continue
        if flag in FLAG_ARGS:
            if v:
                cmd.append(flag)
        elif v not in (None, "", []):
            cmd += [flag, str(v)]

    job = {
        "id": uuid.uuid4().hex[:12], "slug": slug, "action": action,
        "label": spec["label"], "status": "running",
        "started_at": G.now_iso(), "finished_at": None, "exit_code": None,
        "cmd": " ".join(cmd[2:]), "started_by": started_by,
    }
    try:
        JOBS_DIR.mkdir(parents=True, exist_ok=True)
        cfg_path = G.project_dir(slug) / "geo.json"
        stage_root = JOBS_DIR / "staging" / job["id"]
        with G.project_lock(slug):
            base_config = None
            if cfg_path.exists():
                raw = cfg_path.read_bytes()
                base_config = json.loads(raw)
                job["config_revision"] = hashlib.sha256(raw).hexdigest()
                G.write_json(JOBS_DIR / "snapshots" / f"{job['id']}.json", base_config)
            stage_root.mkdir(parents=True, exist_ok=True)
            shutil.copytree(G.project_dir(slug), stage_root / slug,
                            ignore=shutil.ignore_patterns(".lock", ".run.lock", ".publish.lock", ".geo.bak"),
                            dirs_exist_ok=True)
        if scheduled_next_run:
            staged_cfg = stage_root / slug / "geo.json"
            cfg = G.read_json(staged_cfg, {})
            cfg.setdefault("monitor", {})["next_run"] = scheduled_next_run
            G.write_json(staged_cfg, cfg)
        job["staged"] = True
        _write(job)
        logf = _log_path(job["id"]).open("wb")
        logf.write(f"$ geo {' '.join(cmd[3:])}\n".encode())
        logf.flush()
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        env["GEOLOOK_HELD_RUN_LOCK_FD"] = str(run_fd.fileno())
        env["GEOLOOK_WORK_DIR"] = str(stage_root)
        env["GEOLOOK_API_LIMITS_DIR"] = str(G.WORK / ".api-limits")
        proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                                cwd=str(G.ROOT), env=env, start_new_session=True,
                                pass_fds=(run_fd.fileno(),))
    except Exception as e:  # noqa: BLE001  Popen 挂了不能留下永远 running 的僵尸记录
        if "logf" in locals():
            logf.close()
        run_fd.close()
        job["status"] = "failed"
        job["error"] = f"{type(e).__name__}: {e}"
        job["finished_at"] = G.now_iso()
        _write(job)
        raise
    job["pid"] = proc.pid
    _write(job)
    with _lock:
        _running[slug] = job["id"]
        _procs[job["id"]] = proc

    def waiter():
        code = proc.wait()
        logf.close()
        j = get(job["id"]) or job
        if code == 0:
            try:
                j["published_files"], j["config_conflicts"] = _publish_stage(
                    slug, stage_root, base_config)
                shutil.rmtree(stage_root)
                j["status"] = "done"
            except Exception as e:  # noqa: BLE001
                j["status"] = "failed"
                j["error"] = f"结果发布失败，暂存结果仍保留：{type(e).__name__}: {e}"
        else:
            j["status"] = "stopped" if code < 0 else "failed"
            j["error"] = j.get("error") or "任务未完成，原有项目结果未被覆盖"
        j["exit_code"] = code
        j["finished_at"] = G.now_iso()
        _write(j)
        with _lock:
            if _running.get(slug) == job["id"]:
                _running.pop(slug, None)
            _procs.pop(job["id"], None)
        run_fd.close()

    threading.Thread(target=waiter, daemon=True).start()
    return job


def stop(job_id: str) -> bool:
    with _lock:
        proc = _procs.get(job_id)
    if proc:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:  # noqa: BLE001
            proc.terminate()
        return True
    # 服务重启后 _procs 是空的，按 job 文件里落的 pid 兜底杀整组
    job = get(job_id)
    pid = (job or {}).get("pid")
    if not pid or job.get("status") != "running":
        return False
    if not G.run_locked(job["slug"]) or running_for(job["slug"]) != job_id:
        return False
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except Exception:  # noqa: BLE001
        return False
    job["status"] = "stopping"  # 子进程释放跨进程锁后由 reaper 确认停止
    _write(job)
    return True


def reap_orphans() -> int:
    """Reconcile persisted active jobs against the cross-process project lock."""
    if not JOBS_DIR.exists():
        return 0
    reaped = 0
    for f in JOBS_DIR.glob("*.json"):
        try:
            job = json.loads(f.read_text("utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if job.get("status") not in ("running", "stopping"):
            continue
        slug = job.get("slug", "")
        if G.SLUG_OK.fullmatch(slug or "") and G.run_locked(slug):
            continue
        job["status"] = "stopped" if job.get("status") == "stopping" else "interrupted"
        job["finished_at"] = G.now_iso()
        _write(job)
        reaped += 1
    if reaped:
        G.info(f"回收了 {reaped} 个中断的任务记录")
    return reaped


def prune_failed_stages(days: int = 7) -> int:
    """Bound disk use; only remove old, app-created staging copies of finished jobs."""
    staging = (JOBS_DIR / "staging").resolve()
    if not staging.is_dir():
        return 0
    removed = 0
    threshold = time.time() - days * 86400
    for path in staging.iterdir():
        if path.is_symlink() or not path.is_dir() or not re.fullmatch(r"[0-9a-f]{12}", path.name):
            continue
        if path.parent.resolve() != staging:
            continue
        meta = _job_path(path.name)
        job = get(path.name)
        if not job or job.get("status") not in ("failed", "stopped", "interrupted"):
            continue
        if meta.stat().st_mtime >= threshold:
            continue
        shutil.rmtree(path)
        removed += 1
    return removed
