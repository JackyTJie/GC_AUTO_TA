"""结果汇总：把 state 里的分数、任务状态、回写记录导成 JSON/CSV/Markdown，并打印表格。"""

from __future__ import annotations

import csv
import io
import json
import time
from pathlib import Path

from .util import human_bytes, log, write_json, write_text


def build_rows(cfg, state, variant: str | None = None) -> list[dict]:
    students = {s["student_key"]: s for s in state.students(cfg.assignment_name)}
    if variant is None:
        scores = {s["student_key"]: s for s in state.scores(cfg.assignment_name)}
    else:
        scores = {s["student_key"]: s for s in state.score_variants(cfg.assignment_name)
                  if s.get("variant") == variant}
    writes = {w["student_key"]: w for w in state.writebacks(cfg.assignment_name)}

    rows: list[dict] = []
    for key, stu in students.items():
        sc = scores.get(key, {})
        wb = writes.get(key, {})
        stages = []
        try:
            stages = json.loads(sc.get("stages_json") or "[]")
        except ValueError:
            stages = []
        rows.append({
            "student_key": key,
            "name": stu.get("name") or "",
            "login_id": stu.get("login_id") or "",
            "canvas_user_id": stu.get("canvas_user_id"),
            "repo": stu.get("repo") or "",
            "repo_url": stu.get("repo_url") or "",
            "commit_sha": (sc.get("commit_sha") or "")[:7],
            "raw_score": sc.get("score"),
            "max_score": cfg.max_score,
            "source": sc.get("source") or "",
            "comment": sc.get("comment") or "",
            "stages": stages,
            "posted_grade": wb.get("posted_grade") or "",
            "writeback_mode": wb.get("mode") or "",
            "written_at": (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(wb["written_at"]))
                           if wb.get("written_at") else ""),
        })
    rows.sort(key=lambda r: str(r["student_key"]))
    return rows


def summarize(rows: list[dict]) -> dict:
    graded = [r for r in rows if r["raw_score"] is not None]
    return {
        "students": len(rows),
        "with_score": len(graded),
        "missing_score": len(rows) - len(graded),
        "avg_raw": round(sum(float(r["raw_score"]) for r in graded) / len(graded), 2) if graded else None,
        "zero": len([r for r in graded if float(r["raw_score"]) == 0]),
        "written_back": len([r for r in rows if r["posted_grade"]]),
    }


def write_reports(cfg, rows: list[dict]) -> dict[str, Path]:
    """写 reports/results.json、results.csv、problems.md。"""
    out_dir = cfg.report_dir if cfg.report_dir.is_absolute() else cfg.report_dir
    out_dir = out_dir / cfg.assignment_name
    out_dir.mkdir(parents=True, exist_ok=True)

    payload = {"assignment": cfg.assignment_name,
               "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "summary": summarize(rows),
               "rows": rows}
    json_path = write_json(out_dir / "results.json", payload)

    csv_path = out_dir / "results.csv"
    fields = ["student_key", "name", "login_id", "canvas_user_id", "repo", "commit_sha",
              "raw_score", "max_score", "posted_grade", "source", "comment"]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow(r)

    problems = [r for r in rows if r["raw_score"] is None]
    lines = [f"# {cfg.assignment_name} 需要人工处理的学生", ""]
    if problems:
        lines.append("| 学号/标识 | 姓名 | 仓库 | 问题 |")
        lines.append("|---|---|---|---|")
        for r in problems:
            lines.append(f"| {r['student_key']} | {r['name']} | {r['repo']} | 没有拿到分数 |")
    else:
        lines.append("全部学生都拿到了分数。")
    md_path = write_text(out_dir / "problems.md", "\n".join(lines) + "\n")

    log().info("报告已写出：%s / %s / %s", json_path.name, csv_path.name, md_path.name)
    paths = {"json": json_path, "csv": csv_path, "markdown": md_path}
    variants = cfg.grading_variants()
    if len(variants) > 1:
        for variant_cfg in variants:
            suffix = str(variant_cfg.variant_suffix)
            if not suffix:
                continue
            variant_rows = [dict(r) for r in rows]
            variant_scores = {s["student_key"]: s for s in getattr(cfg, "_report_state", {}).get(suffix, [])}
            if variant_scores:
                for row in variant_rows:
                    score = variant_scores.get(row["student_key"])
                    if score:
                        row["raw_score"] = score.get("score")
                        row["comment"] = score.get("comment") or ""
            variant_payload = {"assignment": cfg.assignment_name,
                               "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                               "summary": summarize(variant_rows), "rows": variant_rows}
            paths[f"json:{suffix}"] = write_json(out_dir / f"results-{suffix}.json", variant_payload)
            with (out_dir / f"results-{suffix}.csv").open("w", newline="", encoding="utf-8-sig") as fh:
                writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(variant_rows)
            paths[f"csv:{suffix}"] = out_dir / f"results-{suffix}.csv"
            variant_problems = [r for r in variant_rows if r["raw_score"] is None]
            variant_lines = [f"# {cfg.assignment_name} ({suffix}) 需要人工处理的学生", ""]
            if variant_problems:
                variant_lines += ["| 学号/标识 | 姓名 | 仓库 | 问题 |", "|---|---|---|---|"]
                variant_lines += [f"| {r['student_key']} | {r['name']} | {r['repo']} | 没有拿到分数 |"
                                  for r in variant_problems]
            else:
                variant_lines.append("全部学生都拿到了分数。")
            paths[f"markdown:{suffix}"] = write_text(out_dir / f"problems-{suffix}.md",
                                                       "\n".join(variant_lines) + "\n")
    return paths


def print_table(rows: list[dict], limit: int | None = None) -> None:
    cols = [("student_key", 20), ("name", 12), ("raw_score", 8), ("posted_grade", 12), ("repo", 28)]
    header = "".join(f"{name:<{width}}" for name, width in cols)
    print(header)
    print("-" * len(header))
    shown = rows[:limit] if limit else rows
    for r in shown:
        print("".join(f"{str(r.get(name, ''))[:width]:<{width}}" for name, width in cols))
    if limit and len(rows) > limit:
        print(f"... 还有 {len(rows) - limit} 行，完整结果见 results.csv")
    s = summarize(rows)
    print("-" * len(header))
    print(f"共 {s['students']} 人；有分数 {s['with_score']}；缺分数 {s['missing_score']}；"
          f"0 分 {s['zero']}；已回写 {s['written_back']}；平均原始分 {s['avg_raw']}")
