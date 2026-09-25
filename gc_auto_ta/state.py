"""SQLite 状态库：让整条流水线可断点续跑、可对账、幂等。

表结构（都以 (assignment, student_key) 为粒度）：
    students   学生身份与仓库映射
    stages     每个阶段的状态（done / failed / skipped）
    runs       每次评测触发的 Gitea 侧记录（commit sha、任务状态）
    scores     收集到的 joj3 结果（分数、评语、原始 JSON）
    writebacks 回写 Canvas 的记录（写入前的值也留档，便于回滚）
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from .util import log

SCHEMA = """
CREATE TABLE IF NOT EXISTS students (
    assignment   TEXT NOT NULL,
    student_key  TEXT NOT NULL,
    canvas_user_id INTEGER,
    login_id     TEXT,
    name         TEXT,
    section      TEXT,
    repo         TEXT,
    repo_url     TEXT,
    code_dir     TEXT,
    extra        TEXT,
    updated_at   REAL,
    PRIMARY KEY (assignment, student_key)
);

CREATE TABLE IF NOT EXISTS stages (
    assignment  TEXT NOT NULL,
    student_key TEXT NOT NULL,
    stage       TEXT NOT NULL,
    status      TEXT NOT NULL,
    detail      TEXT,
    error_kind  TEXT NOT NULL DEFAULT '',
    updated_at  REAL,
    PRIMARY KEY (assignment, student_key, stage)
);

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment    TEXT NOT NULL,
    student_key   TEXT NOT NULL,
    repo          TEXT NOT NULL,
    commit_sha    TEXT,
    task_id       INTEGER,
    task_status   TEXT,
    run_number    INTEGER,
    triggered_at  REAL,
    finished_at   REAL
);

CREATE TABLE IF NOT EXISTS scores (
    assignment   TEXT NOT NULL,
    student_key  TEXT NOT NULL,
    commit_sha   TEXT,
    score        REAL,
    comment      TEXT,
    stages_json  TEXT,
    source       TEXT,
    raw_json     TEXT,
    collected_at REAL,
    PRIMARY KEY (assignment, student_key)
);

CREATE TABLE IF NOT EXISTS writebacks (
    assignment    TEXT NOT NULL,
    student_key   TEXT NOT NULL,
    canvas_user_id INTEGER,
    posted_grade  TEXT,
    comment       TEXT,
    mode          TEXT,
    previous_score REAL,
    written_at    REAL,
    PRIMARY KEY (assignment, student_key)
);
"""


def _now() -> float:
    return time.time()


class State:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate_schema()
        self.db.commit()

    def _migrate_schema(self) -> None:
        """Apply additive, idempotent migrations to databases created by older versions."""
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(stages)")}
        if "error_kind" not in columns:
            self.db.execute("ALTER TABLE stages ADD COLUMN error_kind TEXT NOT NULL DEFAULT ''")

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "State":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------ students
    def upsert_student(self, assignment: str, student_key: str, **fields: Any) -> None:
        cols = ["assignment", "student_key"] + [k for k in fields if fields[k] is not None]
        vals = [assignment, student_key] + [v for v in fields.values() if v is not None]
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols[2:]) + ",updated_at=excluded.updated_at"
        sql = (
            f"INSERT INTO students ({','.join(cols)},updated_at) VALUES ({placeholders},?) "
            f"ON CONFLICT(assignment,student_key) DO UPDATE SET {updates}"
        )
        self.db.execute(sql, vals + [_now()])
        self.db.commit()

    def get_student(self, assignment: str, student_key: str) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM students WHERE assignment=? AND student_key=?", (assignment, student_key)
        ).fetchone()
        return dict(row) if row else None

    def students(self, assignment: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM students WHERE assignment=? ORDER BY student_key", (assignment,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ stages
    def set_stage(self, assignment: str, student_key: str, stage: str,
                  status: str, detail: str = "", error_kind: str = "") -> None:
        category = error_kind if error_kind in {"student", "system"} else ""
        self.db.execute(
            "INSERT INTO stages (assignment,student_key,stage,status,detail,error_kind,updated_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(assignment,student_key,stage) DO UPDATE SET "
            "status=excluded.status, detail=excluded.detail, error_kind=excluded.error_kind, "
            "updated_at=excluded.updated_at",
            (assignment, student_key, stage, status, detail[:2000], category, _now()),
        )
        self.db.commit()

    def stage_status(self, assignment: str, student_key: str, stage: str) -> str | None:
        row = self.db.execute(
            "SELECT status FROM stages WHERE assignment=? AND student_key=? AND stage=?",
            (assignment, student_key, stage),
        ).fetchone()
        return row["status"] if row else None

    def stage_done(self, assignment: str, student_key: str, stage: str) -> bool:
        return self.stage_status(assignment, student_key, stage) == "done"

    def first_failed_stage(self, assignment: str, student_key: str,
                           stages: Iterable[str] | None = None) -> tuple[str, str] | None:
        """Return the first failed stage and its detail in pipeline order."""
        order = list(stages or ("pull", "unpack", "provision", "grade"))
        for stage in order:
            row = self.db.execute(
                "SELECT status, detail FROM stages "
                "WHERE assignment=? AND student_key=? AND stage=?",
                (assignment, student_key, stage),
            ).fetchone()
            if row and row["status"] == "failed":
                return stage, str(row["detail"] or "")
        return None

    def _first_error(self, assignment: str, student_key: str, error_kind: str,
                     stages: Iterable[str] | None = None) -> tuple[str, str] | None:
        order = list(stages or ("pull", "unpack", "provision", "grade"))
        for stage in order:
            row = self.db.execute(
                "SELECT status, detail, error_kind FROM stages "
                "WHERE assignment=? AND student_key=? AND stage=?",
                (assignment, student_key, stage),
            ).fetchone()
            if row and row["status"] == "failed" and row["error_kind"] == error_kind:
                return stage, str(row["detail"] or "")
        return None

    def first_student_error(self, assignment: str, student_key: str,
                            stages: Iterable[str] | None = None) -> tuple[str, str] | None:
        """Return the first student-caused failed stage in pipeline order."""
        return self._first_error(assignment, student_key, "student", stages)

    def first_system_error(self, assignment: str, student_key: str,
                           stages: Iterable[str] | None = None) -> tuple[str, str] | None:
        """Return the first system-caused failed stage in pipeline order."""
        return self._first_error(assignment, student_key, "system", stages)

    def stages(self, assignment: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM stages WHERE assignment=? ORDER BY student_key,stage", (assignment,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ runs
    def add_run(self, assignment: str, student_key: str, repo: str,
                commit_sha: str | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO runs (assignment,student_key,repo,commit_sha,triggered_at) VALUES (?,?,?,?,?)",
            (assignment, student_key, repo, commit_sha, _now()),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def update_run(self, run_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets = ",".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE runs SET {sets} WHERE id=?", list(fields.values()) + [run_id])
        self.db.commit()

    def last_run(self, assignment: str, student_key: str) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM runs WHERE assignment=? AND student_key=? ORDER BY id DESC LIMIT 1",
            (assignment, student_key),
        ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------ scores
    def save_score(self, assignment: str, student_key: str, score: float | None,
                   comment: str = "", stages: str = "", source: str = "",
                   commit_sha: str = "", raw_json: str = "") -> None:
        self.db.execute(
            "INSERT INTO scores (assignment,student_key,commit_sha,score,comment,stages_json,"
            "source,raw_json,collected_at) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(assignment,student_key) DO UPDATE SET commit_sha=excluded.commit_sha,"
            "score=excluded.score,comment=excluded.comment,stages_json=excluded.stages_json,"
            "source=excluded.source,raw_json=excluded.raw_json,collected_at=excluded.collected_at",
            (assignment, student_key, commit_sha, score, comment, stages, source, raw_json, _now()),
        )
        self.db.commit()

    def scores(self, assignment: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM scores WHERE assignment=? ORDER BY student_key", (assignment,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ writebacks
    def save_writeback(self, assignment: str, student_key: str, canvas_user_id: int | None,
                       posted_grade: str, comment: str, mode: str,
                       previous_score: float | None = None) -> None:
        self.db.execute(
            "INSERT INTO writebacks (assignment,student_key,canvas_user_id,posted_grade,comment,"
            "mode,previous_score,written_at) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(assignment,student_key) DO UPDATE SET canvas_user_id=excluded.canvas_user_id,"
            "posted_grade=excluded.posted_grade, comment=excluded.comment, mode=excluded.mode,"
            "previous_score=excluded.previous_score, written_at=excluded.written_at",
            (assignment, student_key, canvas_user_id, posted_grade, comment, mode,
             previous_score, _now()),
        )
        self.db.commit()

    def writebacks(self, assignment: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM writebacks WHERE assignment=? ORDER BY student_key", (assignment,)
        ).fetchall()
        return [dict(r) for r in rows]

    def summary(self, assignment: str) -> dict:
        def count(sql: str, *args: Any) -> int:
            return int(self.db.execute(sql, args).fetchone()[0])

        return {
            "students": count("SELECT COUNT(*) FROM students WHERE assignment=?", assignment),
            "stages_done": count(
                "SELECT COUNT(*) FROM stages WHERE assignment=? AND status='done'", assignment),
            "scores": count("SELECT COUNT(*) FROM scores WHERE assignment=?", assignment),
            "writebacks": count("SELECT COUNT(*) FROM writebacks WHERE assignment=?", assignment),
        }


def reset_stage(state: State, assignment: str, stage: str, keys: Iterable[str] | None = None) -> int:
    """清掉某阶段状态，强制重跑。"""
    if keys is None:
        cur = state.db.execute("DELETE FROM stages WHERE assignment=? AND stage=?", (assignment, stage))
    else:
        keys = list(keys)
        q = ",".join("?" for _ in keys)
        cur = state.db.execute(
            f"DELETE FROM stages WHERE assignment=? AND stage=? AND student_key IN ({q})",
            [assignment, stage] + keys,
        )
    state.db.commit()
    log().info("已重置阶段 %s 的记录 %d 条", stage, cur.rowcount)
    return cur.rowcount
