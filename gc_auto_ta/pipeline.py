"""第一、二阶段：从 Canvas 拉提交 → 解包/归集 → 生成 manifest.json（后续阶段的唯一事实来源）。

manifest.json 结构（work/<作业名>/manifest.json）：

    {
      "assignment": "hw1",
      "canvas": {"course_id": 1, "assignment_id": 2, "points_possible": 100},
      "students": {
         "2026000001": {
            "canvas_user_id": 123,
            "login_id": "2026000001",
            "name": "示例学生",
            "submission_type": "online_upload",
            "submitted_at": "2026-09-20T10:00:00Z",
            "late": false,
            "raw_files": ["submissions/2026000001/full_adder.m"],
            "code_files": ["code/2026000001/full_adder.m"],
            "note": ""
         }
      },
      "unmatched": ["某某某的作业.zip"],
      "ambiguous": {"zip1.zip": ["2026000001", "2026000002"]}
    }

unmatched / ambiguous 一律**不自动归属**，只在报告里列出，等人处理（roster 覆盖或人工指定）。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .canvas_api import CanvasGateway
from .naming import Student, load_roster, match_entries, repo_name, student_from_submission
from .unpack import (bundle_directories, copy_student_files, extract, gather_source_files,
                     is_archive)
from .util import log, read_json, write_json


def manifest_path(cfg) -> Path:
    return cfg.assignment_work_dir / "manifest.json"


def load_manifest(cfg) -> dict:
    return read_json(manifest_path(cfg), default={}) or {}


def save_manifest(cfg, data: dict) -> Path:
    return write_json(manifest_path(cfg), data)


def _reset_submissions_dir(path: Path) -> Path:
    """Clear a previous pull result before downloading fresh submissions."""
    path = Path(path)
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _reset_unpacked_dir(path: Path) -> Path:
    """Clear a previous unpack result before extracting fresh submissions."""
    path = Path(path)
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _students_from_manifest(manifest: dict) -> list[Student]:
    out: list[Student] = []
    for key, info in (manifest.get("students") or {}).items():
        out.append(Student(
            canvas_user_id=int(info.get("canvas_user_id") or 0),
            student_key=key,
            login_id=str(info.get("login_id") or ""),
            name=str(info.get("name") or ""),
        ))
    return out


# --------------------------------------------------------------------------- 第一阶段
def pull(cfg, limit: int | None = None, only: list[str] | None = None) -> dict:
    """拉取 Canvas 提交并下载到 work/<作业>/submissions/<学生>/。"""
    gw = CanvasGateway(cfg.canvas_base_url, cfg.canvas_token,
                       cfg.canvas_course_id, cfg.canvas_assignment_id,
                       min_interval=cfg.canvas_min_interval)
    props = gw.assignment_props()
    log().info("作业：%s（满分 %s，类型 %s，匿名批改=%s）",
               props["name"], props["points_possible"], props["grading_type"],
               props["anonymous_grading"])

    from .state import State
    state = State(cfg.state_db)
    sub_root = _reset_submissions_dir(cfg.assignment_work_dir / "submissions")

    users = gw.users_by_id()
    manifest = load_manifest(cfg)
    manifest.update({
        "assignment": cfg.assignment_name,
        "canvas": {
            "course_id": cfg.canvas_course_id,
            "assignment_id": cfg.canvas_assignment_id,
            "points_possible": props["points_possible"],
            "grading_type": props["grading_type"],
            "due_at": props["due_at"],
            "anonymous_grading": props["anonymous_grading"],
            "name": props["name"],
        },
    })
    students = manifest.setdefault("students", {})

    subs = gw.submissions()
    if only:
        subs = [s for s in subs if student_from_submission(s, users.get(int(getattr(s, "user_id", 0) or 0))).student_key in only]
    if limit:
        subs = subs[:limit]

    for sub in subs:
        uid = int(getattr(sub, "user_id", 0) or 0)
        student = None
        try:
            student = student_from_submission(sub, users.get(uid))
            got = gw.download(sub, sub_root, student.student_key)
            entry = students.setdefault(student.student_key, {})
            entry.update({
                "canvas_user_id": student.canvas_user_id,
                "login_id": student.login_id,
                "name": student.name,
                "submission_type": got.submission_type,
                "submitted_at": got.submitted_at,
                "late": bool(getattr(sub, "late", False)),
                "attempt": getattr(sub, "attempt", None),
                "raw_files": [str(p.relative_to(cfg.assignment_work_dir)) for p in got.files],
                "note": got.note,
            })
            state.upsert_student(cfg.assignment_name, student.student_key,
                                 canvas_user_id=student.canvas_user_id,
                                 login_id=student.login_id, name=student.name)
            state.set_stage(cfg.assignment_name, student.student_key, "pull",
                            "failed" if got.error_kind else ("done" if got.files else "skipped"),
                            got.note, got.error_kind)
            log().info("%s → %d 个文件%s", student.student_key, len(got.files),
                       f"（{got.note}）" if got.note else "")
        except Exception as exc:
            if student is None:
                student = Student(canvas_user_id=uid, student_key=f"user{uid}")
            entry = students.setdefault(student.student_key, {})
            entry.update({
                "canvas_user_id": student.canvas_user_id,
                "login_id": student.login_id,
                "name": student.name,
                "raw_files": [],
                "code_files": [],
                "note": f"pull error: {exc}",
            })
            state.upsert_student(cfg.assignment_name, student.student_key,
                                 canvas_user_id=student.canvas_user_id,
                                 login_id=student.login_id, name=student.name)
            state.set_stage(cfg.assignment_name, student.student_key, "pull", "failed", str(exc), "system")
            log().error("%s 获取提交失败：%s", student.student_key, exc)

    save_manifest(cfg, manifest)
    state.close()
    log().info("manifest 已更新：%s（%d 名学生）", manifest_path(cfg), len(students))
    return manifest


# --------------------------------------------------------------------------- 第二阶段
def unpack_stage(cfg) -> dict:
    """解包 + 源码归集到 work/<作业>/code/<学生>/。"""
    manifest = load_manifest(cfg)
    students = manifest.get("students") or {}
    if not students:
        raise RuntimeError("manifest 里没有学生：请先运行 `pull`")

    roster = load_roster_from_cfg(cfg)
    student_objs = _students_from_manifest(manifest)
    by_key = {s.student_key: s for s in student_objs}

    raw_root = cfg.assignment_work_dir / "submissions"
    unpack_root = _reset_unpacked_dir(cfg.assignment_work_dir / "unpacked")
    code_root = cfg.assignment_work_dir / "code"
    code_root.mkdir(parents=True, exist_ok=True)

    unmatched: list[str] = []
    ambiguous: dict[str, list[str]] = {}
    from .state import State

    for key, info in students.items():
        unpack_error = ""
        unpack_error_kind = ""
        try:
            raw_files = [cfg.assignment_work_dir / rel for rel in info.get("raw_files", [])]
            student_dir = unpack_root / key
            student_dir.mkdir(parents=True, exist_ok=True)

            pool: list[Path] = []
            for f in raw_files:
                if not f.exists():
                    unpack_error = f"原始提交文件不存在：{f}"
                    unpack_error_kind = "system"
                    continue
                if is_archive(f):
                    res = extract(f, student_dir / f.stem, max_bytes=cfg.max_extract_bytes)
                    if res.ok:
                        log().info("%s 解包 %s（%s，%d 个文件）", key, f.name, res.method, len(res.files))
                        pool.extend(res.files)
                        if cfg.remove_archives:
                            f.unlink(missing_ok=True)
                        # 全班一个大包的情形：解出来的顶层目录能对应到不同学生 → 拆分
                        entries = bundle_directories(student_dir / f.stem)
                        if len(entries) > 1:
                            match = match_entries(list(entries.keys()), student_objs, roster)
                            is_bundle = (len(match.mapping) >= 2 and
                                         not match.ambiguous and not match.unmatched)
                            if is_bundle:
                                log().info("%s 看起来是全班大包，按目录拆分：%s", f.name,
                                           {k: v for k, v in match.mapping.items()})
                                for owner, dirs in match.mapping.items():
                                    if owner == key:
                                        continue
                                    target = unpack_root / owner
                                    target.mkdir(parents=True, exist_ok=True)
                                    for d in dirs:
                                        src = entries.get(d)
                                        if src and src.exists():
                                            dst = target / src.name
                                            if not dst.exists():
                                                shutil.move(str(src), str(dst))
                                            students.setdefault(owner, {})["code_files"] = \
                                                students.get(owner, {}).get("code_files", [])
                                pool = [p for p in pool if p.parent != student_dir / f.stem]
                            else:
                                unmatched.extend(match.unmatched)
                                ambiguous.update(match.ambiguous)
                    else:
                        unpack_error = f"{f.name} 解包失败：{res.error}"
                        unpack_error_kind = "student"
                        log().warning("%s 的 %s 解包失败：%s", key, f.name, res.error)
                        pool.append(f)
                else:
                    dst = student_dir / f.name
                    if not dst.exists():
                        shutil.copy2(f, dst)
                    pool.append(dst)

            # 收集源码（按 globs）
            src_files = gather_source_files(student_dir, cfg.student_file_globs)
            code_dir = code_root / key
            if code_dir.exists():
                shutil.rmtree(code_dir)
            code_dir.mkdir(parents=True, exist_ok=True)
            copied = copy_student_files(src_files, code_dir)
            info["code_files"] = [str((code_dir / rel).relative_to(cfg.assignment_work_dir)) for rel in copied]
            info["unpacked_files"] = len(src_files)
            if not unpack_error and not copied and not str(info.get("note") or "").startswith("pull error"):
                unpack_error = "提交中没有可收集的源码文件"
                unpack_error_kind = "student"
            if unpack_error:
                info["error"] = unpack_error
            log().info("%s：收集到 %d 个源码文件", key, len(copied))
            with State(cfg.state_db) as state:
                state.set_stage(cfg.assignment_name, key, "unpack",
                                "failed" if unpack_error else ("done" if copied else "skipped"),
                                unpack_error or f"files={len(copied)}",
                                unpack_error_kind)
        except Exception as exc:
            unpack_error = str(exc)
            info["error"] = unpack_error
            log().error("%s 解包/归集失败：%s", key, exc)
            with State(cfg.state_db) as state:
                state.set_stage(cfg.assignment_name, key, "unpack", "failed", unpack_error, "system")

    manifest["unmatched"] = sorted(set(unmatched))
    manifest["ambiguous"] = ambiguous
    save_manifest(cfg, manifest)
    if unmatched:
        log().warning("有 %d 个条目没有匹配到学生（不会自动归属）：%s", len(unmatched), unmatched)
    if ambiguous:
        log().warning("有 %d 个条目同时像多个学生（需人工指定）：%s", len(ambiguous), ambiguous)
    return manifest


def load_roster_from_cfg(cfg):
    """roster 直接内联在 config.yaml 的 roster 段里。"""
    from .naming import RosterEntry
    out: dict[str, RosterEntry] = {}
    for key, val in (cfg.roster_raw or {}).items():
        val = val or {}
        out[str(key)] = RosterEntry(
            student_key=str(key),
            repo=(str(val["repo"]) if val.get("repo") else None),
            canvas_user_id=(int(val["canvas_user_id"]) if val.get("canvas_user_id") else None),
            login_id=str(val.get("login_id") or ""),
            name=str(val.get("name") or ""),
            aliases=[str(a) for a in (val.get("aliases") or [])],
        )
    return out


def students_with_files(cfg) -> dict:
    """给 provision 用：{student_key: {"student": Student, "files": [Path]}}"""
    manifest = load_manifest(cfg)
    students = manifest.get("students") or {}
    out: dict = {}
    for key, info in students.items():
        student = Student(
            canvas_user_id=int(info.get("canvas_user_id") or 0),
            student_key=key,
            login_id=str(info.get("login_id") or ""),
            name=str(info.get("name") or ""),
        )
        files = [cfg.assignment_work_dir / rel for rel in info.get("code_files", [])]
        files = [f for f in files if f.exists()]
        out[key] = {"student": student, "files": files, "repo": repo_name(cfg.repo_prefix, key)}
    return out
