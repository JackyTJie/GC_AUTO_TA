"""通用工具：日志、JSON/YAML 读写、限流、子进程封装、字符串规范化。"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

LOG_NAME = "gc_auto_ta"


# --------------------------------------------------------------------------- 日志
def setup_logging(verbose: bool = False) -> None:
    """初始化 stderr 日志（INFO 或 DEBUG）。"""
    logger = logging.getLogger(LOG_NAME)
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False


def log() -> logging.Logger:
    return logging.getLogger(LOG_NAME)


# --------------------------------------------------------------------------- 文件读写
def read_json(path: str | Path, default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    with p.open(encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: str | Path, obj: Any) -> Path:
    """原子写 JSON（先写临时文件再 rename），避免中断留下半个文件。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(p.parent),
                                     prefix=p.name + ".", delete=False) as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
        tmp = fh.name
    os.replace(tmp, p)
    return p


def read_yaml(path: str | Path, default: Any = None) -> Any:
    try:
        import yaml  # PyYAML
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("需要 PyYAML：pip install -r requirements.txt") from exc
    p = Path(path)
    if not p.exists():
        return default
    with p.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or default


def write_text(path: str | Path, text: str) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def read_text(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------- 字符串
_SLUG_KEEP = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_slug(text: str, maxlen: int = 48, fallback: str = "unknown") -> str:
    """把任意字符串压成适合做仓库名/目录名的 slug（Gitea 仓库名限制较严）。"""
    s = (text or "").strip()
    s = _SLUG_KEEP.sub("-", s).strip("-._")
    s = re.sub(r"-{2,}", "-", s)
    if not s:
        s = fallback
    return s[:maxlen]


_WS = re.compile(r"[\s_\-·．.、,，]+")


def normalize_person_name(name: str) -> str:
    """人名规范化：全角转半角、去掉空白与分隔符、转小写。

    只用于**候选匹配打分**，绝不用于自动改名或自动归因；无法匹配时保留原样。
    """
    if not name:
        return ""
    out = []
    for ch in name.strip():
        code = ord(ch)
        if code == 0x3000:
            ch = " "
        elif 0xFF01 <= code <= 0xFF5E:  # 全角 ASCII
            ch = chr(code - 0xFEE0)
        out.append(ch)
    s = "".join(out).lower()
    s = _WS.sub("", s)
    return s


def human_bytes(n: int | float | None) -> str:
    if not n:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    v = float(n)
    for u in units:
        if v < 1024 or u == units[-1]:
            return f"{v:.1f} {u}" if u != "B" else f"{int(v)} B"
        v /= 1024
    return f"{v:.1f} TB"


def chunked(seq: Sequence[Any], size: int) -> Iterable[list[Any]]:
    for i in range(0, len(seq), size):
        yield list(seq[i:i + size])


# --------------------------------------------------------------------------- 限流与重试
class RateLimiter:
    """最小调用间隔限流器。

    Canvas 与 Gitea 都有请求配额；批量下载/回写时把最小间隔设成 0.1~0.3s，
    遇到 403/429 时再靠 retry() 做指数退避。
    """

    def __init__(self, min_interval: float = 0.0) -> None:
        self.min_interval = max(0.0, float(min_interval or 0))
        self._last = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        delta = time.monotonic() - self._last
        if delta < self.min_interval:
            time.sleep(self.min_interval - delta)
        self._last = time.monotonic()


def retry(fn, attempts: int = 4, base: float = 1.0, max_sleep: float = 30.0,
          exceptions: tuple = (Exception,), logger: logging.Logger | None = None):
    """指数退避重试；最后一次失败时抛出原异常。"""
    lg = logger or log()
    last_exc: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            return fn()
        except exceptions as exc:  # noqa: PERF203
            last_exc = exc
            if i == attempts:
                break
            sleep = min(max_sleep, base * (2 ** (i - 1)))
            lg.warning("第 %d/%d 次失败：%s —— %.1fs 后重试", i, attempts, exc, sleep)
            time.sleep(sleep)
    assert last_exc is not None
    raise last_exc


# --------------------------------------------------------------------------- 子进程
def run(cmd: Sequence[str], cwd: str | Path | None = None, env: dict | None = None,
        check: bool = True, capture: bool = True, stdin_text: str | None = None) -> subprocess.CompletedProcess:
    """跑一个外部命令；capture=True 时返回带 text stdout/stderr 的结果。"""
    full_env = os.environ.copy()
    if env:
        full_env.update({k: str(v) for k, v in env.items()})
    lg = log()
    lg.debug("$ %s (cwd=%s)", " ".join(cmd), cwd or ".")
    proc = subprocess.run(
        list(cmd), cwd=str(cwd) if cwd else None, env=full_env,
        input=stdin_text, text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if check and proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-2000:]
        raise RuntimeError(f"命令失败（exit={proc.returncode}）：{' '.join(cmd)}\n{tail}")
    return proc


def which(*names: str) -> str | None:
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


def tcp_open(host: str, port: int, timeout: float = 2.0) -> bool:
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
