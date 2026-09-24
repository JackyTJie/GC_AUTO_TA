"""把 JOJ3 的分数与评语回写到 Canvas。

策略由 config.yaml 的 writeback 段决定：
    mode: off   —— 什么都不做
          dry   —— 只打印将要写的内容（默认，安全）
          live  —— 真写

分数换算（grade_mode）：
    points  —— raw/max_score*points_possible，保留两位小数（默认）
    percent —— "87.5%"（Canvas 自己按满分折算）
    raw     —— 直接把 JOJ3 的原始分当分数（要求满分一致）

迟交扣分：late_penalty_per_day 每天扣的**比例**（0.1 = 每天 10%），
以 Canvas 的 submitted_at 与 due_at（作业元数据或 writeback.due_at）比较，向上取整天数。

留痕提醒：所有打分/评论都记在 token 所属账号名下（grader_id = 你），
老师能在成绩历史里看到；只有作业开了 Anonymous Comments 才会匿名。
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from .canvas_api import CanvasGateway
from .util import log


# --------------------------------------------------------------------------- 换算
def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    txt = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def late_days(submitted_at: str | None, due_at: str | None) -> int:
    sub, due = _parse_dt(submitted_at), _parse_dt(due_at)
    if not sub or not due or sub <= due:
        return 0
    return int(math.ceil((sub - due).total_seconds() / 86400.0))


def compute_grade(cfg, raw_score: float, points_possible: float | None,
                  submitted_at: str | None = None, due_at: str | None = None) -> dict:
    """把原始分换算成 Canvas 接受的 posted_grade，并给出扣分明细。"""
    max_score = float(cfg.max_score or 100)
    ratio = (float(raw_score) / max_score) if max_score else 0.0

    penalty = 0.0
    days = 0
    if cfg.late_penalty_per_day:
        days = late_days(submitted_at, due_at or cfg.get("writeback.due_at"))
        penalty = min(0.99, cfg.late_penalty_per_day * days)
        ratio = max(0.0, ratio - penalty)

    mode = cfg.grade_mode
    if mode == "percent":
        posted = f"{round(ratio * 100, 2)}%"
        numeric = round(ratio * 100, 2)
    elif mode == "raw":
        posted = str(round(max(0.0, float(raw_score) - penalty * max_score), 2))
        numeric = float(posted)
    else:  # points
        if points_possible:
            numeric = round(ratio * float(points_possible), 2)
        else:
            numeric = round(ratio * max_score, 2)
        posted = str(numeric)

    return {
        "posted_grade": posted,
        "ratio": round(ratio, 4),
        "numeric": numeric,
        "late_days": days,
        "penalty": round(penalty, 4),
        "mode": mode,
    }


def build_comment(cfg, row: dict, grade_info: dict) -> str:
    """按模板生成评语，并做长度截断（Canvas 对评论长度有限制）。"""
    stages = row.get("stages") or []
    stage_text = "；".join(f"{s.get('name')}: {s.get('score')}" for s in stages if s.get("name"))
    text = cfg.comment_template.format(
        score=row.get("raw_score"),
        max=cfg.max_score,
        points=grade_info.get("numeric"),
        comment=(row.get("comment") or "").strip(),
        stages=stage_text,
        student=row.get("name") or row.get("student_key") or "",
        assignment=cfg.assignment_name,
    )
    if grade_info.get("late_days"):
        text += f"\n（迟交 {grade_info['late_days']} 天，扣分系数 {grade_info['penalty']}）"
    limit = cfg.comment_max_len
    if len(text) > limit:
        text = text[: max(0, limit - 20)].rstrip() + "\n…（评语过长已截断）"
    return text


# --------------------------------------------------------------------------- 主流程
def run(cfg, state, rows: list[dict] | None = None, live: bool | None = None,
        only: list[str] | None = None, bulk: bool | None = None) -> dict:
    """rows 为空时自动从 state 组装。返回 {student_key: {...}}"""
    from .report import build_rows

    mode = cfg.writeback_mode
    if live is True:
        mode = "live"
    elif live is False:
        mode = "dry"
    if mode == "off":
        log().info("writeback.mode=off，跳过回写")
        return {}

    rows = rows or build_rows(cfg, state)
    targets = [r for r in rows if (not only or r["student_key"] in only)]
    if not targets:
        log().warning("没有可回写的记录（先跑 grade 收集分数）")
        return {}

    gw = CanvasGateway(cfg.canvas_base_url, cfg.canvas_token,
                       cfg.canvas_course_id, cfg.canvas_assignment_id,
                       min_interval=cfg.canvas_min_interval)
    props = gw.assignment_props()
    points_possible = props.get("points_possible")
    due_at = cfg.get("writeback.due_at") or props.get("due_at")
    anonymous = bool(props.get("anonymous_grading"))
    anon_ids: dict[int, str] = gw.gradeable_anonymous_ids() if anonymous else {}

    bulk = cfg.writeback_bulk if bulk is None else bulk
    dry = mode != "live"
    payload: dict[int, dict] = {}
    results: dict[str, dict] = {}

    for row in targets:
        key = row["student_key"]
        uid = row.get("canvas_user_id")
        raw = row.get("raw_score")
        if uid is None:
            results[key] = {"error": "缺 canvas_user_id"}
            continue
        if raw is None:
            if cfg.on_missing_submission != "zero" and cfg.get("unpack.on_missing_submission") != "zero":
                log().warning("%s 没有分数，跳过（on_missing_submission=skip）", key)
                results[key] = {"skipped": "no score"}
                continue
            raw = cfg.get("writeback.missing_score", 0)

        submitted = None
        events = state.students(cfg.assignment_name)
        for s in events:
            if s["student_key"] == key:
                submitted = (state.last_run(cfg.assignment_name, key) or {}).get("commit_sha")
                break
        sub_meta = row.get("submitted_at") if isinstance(row, dict) else None

        grade_info = compute_grade(cfg, float(raw), points_possible,
                                   submitted_at=sub_meta, due_at=due_at)
        comment = "" if cfg.only_grade else build_comment(cfg, row, grade_info)
        entry = {"posted_grade": grade_info["posted_grade"]}
        if comment:
            entry["text_comment"] = comment
        payload[int(uid)] = entry
        results[key] = {"posted_grade": entry["posted_grade"], "comment_len": len(comment),
                        "late_days": grade_info["late_days"]}

    if not payload:
        log().warning("没有需要回写的学生")
        return results

    log().info("%s：准备回写 %d 人（%s）", cfg.assignment_name, len(payload),
               "演练" if dry else "写入 Canvas")
    if bulk and len(payload) > 1 and not anonymous:
        outcome = gw.post_grades_bulk(payload, dry_run=dry,
                                      poll_interval=cfg.poll_interval,
                                      poll_timeout=cfg.grading_timeout)
        log().info("批量回写结果：%s", outcome)
    else:
        if anonymous:
            log().warning("该作业开启了匿名批改，逐条走 anonymous_submissions 接口")
        for uid, entry in payload.items():
            key = next((r["student_key"] for r in targets if int(r.get("canvas_user_id") or 0) == uid), str(uid))
            anon = anon_ids.get(uid) if anonymous else None
            try:
                gw.post_grade(uid, entry["posted_grade"], entry.get("text_comment", ""),
                              dry_run=dry, anonymous_id=anon)
            except Exception as exc:
                log().error("回写 %s 失败：%s", key, exc)
                results[key] = {"error": str(exc)}
                state.set_stage(cfg.assignment_name, key, "writeback", "failed", str(exc))
                continue

    for row in targets:
        key = row["student_key"]
        if results.get(key, {}).get("error"):
            continue
        state.save_writeback(cfg.assignment_name, key, row.get("canvas_user_id"),
                             str(results.get(key, {}).get("posted_grade", "")),
                             "", mode, previous_score=row.get("raw_score"))
        state.set_stage(cfg.assignment_name, key, "writeback",
                        "done" if not dry else "dry-run",
                        f"grade={results.get(key, {}).get('posted_grade')}")
    return results
