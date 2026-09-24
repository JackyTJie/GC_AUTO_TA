"""学生身份与命名映射。

一条学生记录要贯穿四个命名空间：
    Canvas 用户  →  student_key（稳定短标识）  →  Gitea 仓库名  →  压缩包里的目录/文件名

原则（与 TA 的一贯做法一致）：
    * 自动匹配只做**确定性**的事：登录名、学号、Canvas user id 精确命中；
    * 人名只做规范化后的**候选打分**，命中唯一才算成功；
    * 一个条目同时像多个学生、或一个学生匹配到多个条目 → 记为 ambiguous，
      **留人工处理，绝不猜**（宁可在展示层分裂，不可把分数错挂到别人身上）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .util import log, normalize_person_name, read_yaml, sanitize_slug


# --------------------------------------------------------------------------- 学生记录
@dataclass
class Student:
    canvas_user_id: int
    student_key: str
    login_id: str = ""
    name: str = ""
    section: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def sort_id(self) -> str:
        return self.login_id or self.student_key


def student_from_submission(submission: Any, user: Any | None = None) -> Student:
    """从 canvasapi 的 Submission（或 User）构造学生记录。

    student_key 优先取 login_id（学号/工号，人工可读、稳定），
    其次 sis_user_id，最后退回 user_<id>。
    """
    attrs = getattr(submission, "__dict__", {}) or {}
    uid = int(attrs.get("user_id") or getattr(user, "id", 0) or 0)
    login = str(attrs.get("user_login_id") or attrs.get("login_id") or "")
    sis = str(attrs.get("sis_user_id") or "")
    name = str(attrs.get("user_name") or attrs.get("name") or "")
    if user is not None:
        login = login or str(getattr(user, "login_id", "") or "")
        sis = sis or str(getattr(user, "sis_user_id", "") or "")
        name = name or str(getattr(user, "sortable_name", "") or getattr(user, "name", "") or "")
        login = login or str(getattr(user, "login_id", "") or "")
    key_source = login or sis or f"user{uid}"
    return Student(
        canvas_user_id=uid,
        student_key=sanitize_slug(key_source, maxlen=40, fallback=f"user{uid}"),
        login_id=login,
        name=name,
        raw=attrs,
    )


def repo_name(prefix: str, student_key: str, separator: str = "-") -> str:
    """仓库名：<作业前缀>-<学生标识>，例如 hw1-2026000001。"""
    return f"{sanitize_slug(prefix, 32)}{separator}{sanitize_slug(student_key, 48)}"


# --------------------------------------------------------------------------- roster 覆盖表
@dataclass
class RosterEntry:
    student_key: str
    repo: str | None = None
    canvas_user_id: int | None = None
    login_id: str = ""
    name: str = ""
    aliases: list[str] = field(default_factory=list)


def load_roster(path: str | Path | None) -> dict[str, RosterEntry]:
    """读取 roster.yaml：人工指定仓库名 / 学号 / 别名，覆盖自动推断。

    结构：
        hw1:
          2026000001:
            repo: hw1-2026000001
            name: 示例学生
            aliases: [示例学生, Sample Student, demo]
    """
    if not path:
        return {}
    data = read_yaml(path, {}) or {}
    out: dict[str, RosterEntry] = {}
    for key, val in data.items():
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


# --------------------------------------------------------------------------- 匹配
@dataclass
class MatchResult:
    mapping: dict[str, list[str]] = field(default_factory=dict)   # student_key -> 命中的条目名
    ambiguous: dict[str, list[str]] = field(default_factory=dict)  # 条目名 -> 命中的多个学生
    unmatched: list[str] = field(default_factory=list)             # 谁都没匹配上的条目

    @property
    def ok(self) -> bool:
        return not self.ambiguous and not self.unmatched


def _candidate_scores(entry: str, student: Student, aliases: Iterable[str] = ()) -> int:
    """给"条目名"对"某个学生"打分（0 = 不匹配）。分数越高越可信。"""
    e = entry or ""
    el = e.lower()
    score = 0
    if student.login_id and student.login_id.lower() in el:
        score = max(score, 100)
    if student.canvas_user_id and re.search(rf"(?<!\d){student.canvas_user_id}(?!\d)", el):
        score = max(score, 90)
    names = [student.name] + [a for a in aliases if a]
    ne = normalize_person_name(e)
    for nm in names:
        nn = normalize_person_name(nm)
        if not nn or len(nn) < 2:
            continue
        if nn == ne or nn in ne:
            score = max(score, 80)
        else:
            # 姓在前/名在后 颠倒的写法（中文姓名常见）
            rev = nn[::-1]
            if len(nn) >= 4 and rev in ne:
                score = max(score, 60)
    return score


def match_entries(entries: Iterable[str], students: Iterable[Student],
                  roster: dict[str, RosterEntry] | None = None) -> MatchResult:
    """把压缩包内条目/目录名匹配到学生。唯一高分才算命中。"""
    roster = roster or {}
    students = list(students)
    res = MatchResult()
    by_key = {s.student_key: s for s in students}

    for entry in entries:
        best: list[tuple[int, str]] = []
        for s in students:
            al = roster.get(s.student_key).aliases if roster.get(s.student_key) else []
            sc = _candidate_scores(entry, s, al)
            if sc:
                best.append((sc, s.student_key))
        if not best:
            res.unmatched.append(entry)
            continue
        best.sort(reverse=True)
        top = best[0][0]
        winners = [k for sc, k in best if sc == top]
        if len(winners) > 1:
            res.ambiguous[entry] = winners
            continue
        res.mapping.setdefault(winners[0], []).append(entry)

    # roster 的显式 repo / login 覆盖，用于兜底补缺
    for key, r in roster.items():
        if key in by_key and r.repo:
            log().debug("roster 指定 %s → %s", key, r.repo)
    return res


def suggested_mapping_files(res: MatchResult) -> dict[str, str]:
    """给 unmatched 的条目生成 roster.yaml 片段（人工填学号用）。"""
    out: dict[str, str] = {}
    for entry in res.unmatched:
        key = sanitize_slug(Path(entry).stem or entry, maxlen=40)
        out[key] = f"# 未自动匹配：{entry}\n#   把键改成学生学号，或填写 canvas_user_id"
    for entry, keys in res.ambiguous.items():
        out[f"AMBIGUOUS:{entry}"] = f"# 同时像 {keys}，请人工指定"
    return out


def detect_archive_owner(path: Path, students: Iterable[Student],
                         roster: dict[str, RosterEntry] | None = None) -> Student | None:
    """单个提交文件（例如学生直接传了一个 zip）归属哪个学生：唯一命中才返回。"""
    res = match_entries([path.name, path.stem], students, roster)
    if res.mapping and not res.ambiguous:
        keys = list(res.mapping.keys())
        if len(keys) == 1:
            return next((s for s in students if s.student_key == keys[0]), None)
    return None
