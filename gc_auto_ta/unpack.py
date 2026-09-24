"""解包：把学生的上传物展开成「一份干净的学生代码目录」。

现实里会遇到四种形态，这里全部处理：
    1. 学生直接上传源码文件（.m/.cpp/.py…）
    2. 学生上传一个压缩包（zip 为主，rar/7z/tar.gz 也常见）
    3. 压缩包里还有一层与学号无关的目录（例如 `我的作业/`）→ 单根目录自动上提
    4. 全班一个大包（`hw1.zip` 内含每个学生一个目录）→ 由 split_bundle 按名单拆分

安全：所有解包都做路径穿越检查（拒绝 `../` 与绝对路径），并可选限制解压总大小；
解包在本地沙盒目录里做，压缩包内容是不可信输入。
"""

from __future__ import annotations

import os
import shutil
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .util import human_bytes, log, run, which

ARCHIVE_EXTS = (".zip", ".rar", ".7z", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")

JUNK_NAMES = {"__MACOSX", ".DS_Store", "Thumbs.db", ".git", ".idea", ".vscode", "__pycache__"}


@dataclass
class ExtractResult:
    archive: Path
    dest: Path
    files: list[Path] = field(default_factory=list)
    error: str = ""
    method: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def is_archive(path: Path) -> bool:
    name = path.name.lower()
    return any(name.endswith(ext) for ext in ARCHIVE_EXTS)


def _safe_join(root: Path, member: str) -> Path | None:
    """拒绝路径穿越 / 绝对路径。"""
    member = member.replace("\\", "/")
    if member.startswith("/") or member.startswith("../") or "/../" in member:
        return None
    target = (root / member).resolve()
    root_r = root.resolve()
    if not str(target).startswith(str(root_r)):
        return None
    return target


def extract(archive: Path, dest: Path, max_bytes: int = 0) -> ExtractResult:
    """解包到 dest。优先用标准库（zip / tar），其余交给 bsdtar / 7z / unrar。"""
    archive = Path(archive)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    res = ExtractResult(archive=archive, dest=dest)
    name = archive.name.lower()

    try:
        if name.endswith(".zip"):
            res.method = "zipfile"
            with zipfile.ZipFile(archive) as zf:
                total = sum(i.file_size for i in zf.infolist())
                if max_bytes and total > max_bytes:
                    res.error = f"解压后体积 {human_bytes(total)} 超过上限 {human_bytes(max_bytes)}"
                    return res
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    target = _safe_join(dest, info.filename)
                    if target is None:
                        log().warning("跳过可疑路径：%s", info.filename)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as src, target.open("wb") as out:
                        shutil.copyfileobj(src, out)
        elif name.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
            res.method = "tarfile"
            with tarfile.open(archive) as tf:
                for member in tf.getmembers():
                    if not member.isfile() and not member.isdir():
                        continue
                    target = _safe_join(dest, member.name)
                    if target is None:
                        log().warning("跳过可疑路径：%s", member.name)
                        continue
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    src = tf.extractfile(member)
                    if src is None:
                        continue
                    with src, target.open("wb") as out:
                        shutil.copyfileobj(src, out)
        else:
            # rar / 7z：优先 bsdtar（libarchive 支持 rar/7z/zip，且对中文名友好）
            bsdtar = which("bsdtar")
            sevenz = which("7z", "7zz", "7za")
            unrar = which("unrar", "unar")
            if bsdtar:
                res.method = "bsdtar"
                run([bsdtar, "-xf", str(archive), "-C", str(dest)])
            elif sevenz:
                res.method = "7z"
                run([sevenz, "x", "-y", f"-o{dest}", str(archive)])
            elif unrar:
                res.method = "unrar"
                run([unrar, "x", "-y", str(archive), str(dest)])
            else:
                res.error = ("无法解包：系统里没有 bsdtar / 7z / unrar。"
                             "Debian/Ubuntu: sudo apt install libarchive-tools p7zip-full unrar")
                return res
    except Exception as exc:
        res.error = f"解包失败：{exc}"
        return res

    _cleanup_junk(dest)
    _strip_single_root(dest)
    res.files = [p for p in dest.rglob("*") if p.is_file()]
    return res


def _cleanup_junk(root: Path) -> None:
    for name in JUNK_NAMES:
        for p in list(root.rglob(name)):
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)


def _strip_single_root(root: Path) -> None:
    """压缩包里只有一个顶层目录时，把它的内容提到 root 下（学生爱打包成「作业1/」）。"""
    entries = [p for p in root.iterdir() if p.name not in JUNK_NAMES]
    if len(entries) != 1 or not entries[0].is_dir():
        return
    inner = entries[0]
    for item in list(inner.iterdir()):
        target = root / item.name
        if target.exists():
            continue
        shutil.move(str(item), str(target))
    shutil.rmtree(inner, ignore_errors=True)


def gather_source_files(root: Path, globs: Iterable[str]) -> list[Path]:
    """按 glob 收集学生源码（默认递归），跳过垃圾目录。"""
    out: list[Path] = []
    for pattern in globs:
        for p in root.rglob(pattern):
            if not p.is_file():
                continue
            if any(part in JUNK_NAMES for part in p.parts):
                continue
            if p.name.endswith(".json") and p.name.startswith("_"):
                continue
            out.append(p)
    # 去重并保持稳定顺序
    seen: set[str] = set()
    unique: list[Path] = []
    for p in sorted(out):
        if str(p) in seen:
            continue
        seen.add(str(p))
        unique.append(p)
    return unique


def split_bundle(bundle_dir: Path, dest_root: Path) -> dict[str, Path]:
    """全班一个大包：把 bundle_dir 下的一级条目按「条目名 → 目录」登记出来。

    归到谁由 naming.match_entries 决定，这里只负责列举，不做猜测。
    """
    out: dict[str, Path] = {}
    for entry in sorted(bundle_dir.iterdir()):
        if entry.name in JUNK_NAMES:
            continue
        out[entry.name] = entry
    return out


def copy_student_files(files: Iterable[Path], dest: Path, strip_common_root: bool = True) -> list[Path]:
    """把学生源码拷进仓库工作区（保持相对路径），返回仓库内的相对路径列表。"""
    dest = Path(dest)
    files = [Path(f) for f in files]
    if not files:
        return []
    base = Path(os.path.commonpath([str(f.parent) for f in files])) if strip_common_root else None
    copied: list[Path] = []
    for f in files:
        rel = f.relative_to(base) if base else Path(f.name)
        if any(part in JUNK_NAMES for part in rel.parts):
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
        copied.append(rel)
    return copied
