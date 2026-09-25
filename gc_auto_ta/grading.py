"""触发评测、等待、收集 joj3 结果。

触发：push 即触发（Gitea Actions）。重跑用空提交。
等待：轮询 GET /repos/{owner}/{repo}/actions/tasks（Gitea 1.23 只有这个接口，
     没有 runs / workflow dispatch / logs 接口）。
收集：三选一，按 config.yaml 的 grading.publish 决定：
     file   → workflow 把 score-<sha7>.json push 回仓库，这里用 contents API 读；
     issue  → workflow 开一个 issue，这里读 issue body 里的 JSON；
     none   → 本地克隆仓库读 joj3_result.json（要求 workflow 也把它提交回去）
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from .util import log, retry


# --------------------------------------------------------------------------- 结果解析
def parse_joj3_result(data: Any) -> dict:
    """解析 joj3_result.json。

    结构（JOJ3 指南第 1 章）：
        [ {"name": "run", "results": [{"score": 100, "comment": "Passed!"}], "force_quit": false}, ... ]
    总分 = 各 stage 各 result 的 score 之和；评语按顺序拼接（去重空串）。
    """
    stages: list[dict] = []
    total = 0.0
    comments: list[str] = []
    if isinstance(data, dict):
        data = [data]
    for stage in data or []:
        if not isinstance(stage, dict):
            continue
        name = stage.get("name", "")
        stage_score = 0.0
        stage_comments: list[str] = []
        for res in stage.get("results", []) or []:
            try:
                sc = float(res.get("score") or 0)
            except (TypeError, ValueError):
                sc = 0.0
            stage_score += sc
            txt = str(res.get("comment") or "").strip()
            if txt:
                stage_comments.append(txt)
        total += stage_score
        comments.extend(stage_comments)
        stages.append({
            "name": name,
            "score": stage_score,
            "comment": " / ".join(stage_comments),
            "force_quit": bool(stage.get("force_quit")),
        })
    return {
        "score": total,
        "comment": "；".join(dict.fromkeys(comments)),
        "stages": stages,
    }


# --------------------------------------------------------------------------- 等待任务
def wait_run(cfg, gitea, repo: str, sha: str | None = None) -> dict | None:
    log().info("等待 %s/%s 的 Action 任务（最长 %.0f 秒）…", cfg.org, repo, cfg.grading_timeout)
    task = gitea.wait_task(cfg.org, repo, head_sha=sha,
                           poll_interval=cfg.poll_interval, timeout=cfg.grading_timeout)
    if task:
        log().info("任务状态：%s（run #%s）", task.get("status"), task.get("run_number"))
    else:
        log().warning("等待超时，未拿到任务状态：%s/%s", cfg.org, repo)
    return task


# --------------------------------------------------------------------------- 收集
def collect_score(cfg, gitea, repo: str, sha: str, student_key: str = "",
                  issue_wait: float = 120.0) -> dict:
    """按 publish 策略读取本次评测结果。返回 {"score":float|None, "comment":str, "stages":[], "source":str}"""
    publish = cfg.publish
    empty = {"score": None, "comment": "", "stages": [], "source": ""}

    if publish == "file":
        name = cfg.score_file_name(sha, student_key)
        text = gitea.get_file(cfg.org, repo, name, ref=cfg.default_branch)
        if text:
            parsed = parse_joj3_result(_loads(text))
            parsed["source"] = f"file:{name}"
            return parsed
        log().warning("%s 里没有 %s（workflow 可能没 push 回分数）", repo, name)
        return empty

    if publish == "issue":
        deadline = time.monotonic() + max(0.0, issue_wait)
        attempt = 0
        while True:
            attempt += 1
            log().debug("开始收集 issue：repo=%s sha=%s sha7=%s student=%s attempt=%d",
                        repo, sha, sha[:7], student_key, attempt)
            issues = gitea.list_issues(cfg.org, repo)
            log().debug("收到 issue 数量：repo=%s count=%d", repo, len(issues))
            for issue in issues:
                title = str(issue.get("title") or "")
                number = issue.get("number")
                is_grading = title.lower().startswith("grading result")
                log().debug("检查 issue #%s：title=%r grading_title=%s pull_request=%r",
                            number, title, is_grading, issue.get("pull_request"))
                if not is_grading:
                    log().debug("跳过 issue #%s：标题不是 Grading result", number)
                    continue
                body = str(issue.get("body") or "")
                has_sha7 = bool(sha) and sha[:7] in body
                has_full_sha = bool(sha) and sha in body
                log().debug("issue #%s 正文：length=%d sha7=%s full_sha=%s prefix=%r",
                            number, len(body), has_sha7, has_full_sha, body[:100])
                if sha and not has_sha7 and not has_full_sha:
                    log().debug("跳过 issue #%s：正文不包含目标 SHA", number)
                    continue
                payload = _extract_json_block(body)
                log().debug("issue #%s JSON 提取：%s", number, payload is not None)
                if payload is not None:
                    parsed = parse_joj3_result(payload)
                    parsed["source"] = f"issue:#{issue.get('number')}"
                    log().debug("issue #%s 收分成功：score=%s", number, parsed.get("score"))
                    return parsed
                log().debug("跳过 issue #%s：正文没有可解析 JSON", number)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            delay = min(float(getattr(cfg, "poll_interval", 10.0)), remaining)
            log().debug("目标评分 issue 尚未发布，%.1f 秒后重试（剩余 %.1f 秒）",
                        delay, remaining)
            time.sleep(delay)
        log().warning("%s 里没有找到包含本次 sha 的评分 issue", repo)
        return empty

    # publish=none：本地 clone 读 joj3_result.json
    text = gitea.get_file(cfg.org, repo, "joj3_result.json", ref=cfg.default_branch)
    if text:
        parsed = parse_joj3_result(_loads(text))
        parsed["source"] = "file:joj3_result.json"
        return parsed
    log().warning("%s 仓库里没有 joj3_result.json（publish=none 时 workflow 需自行提交结果）", repo)
    return empty


def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        log().warning("结果文件不是合法 JSON，尝试提取代码块")
        return _extract_json_block(text) or []


def _extract_json_block(text: str) -> Any | None:
    """Parse the first JSON fenced code block from a Markdown document."""
    from markdown_it import MarkdownIt

    tokens = MarkdownIt().parse(text or "")
    blocks = [token for token in tokens if token.type == "fence"]
    log().debug("Markdown AST 代码块数量：%d", len(blocks))
    for token in blocks:
        info = (token.info or "").strip().split(None, 1)[0].lower()
        log().debug("Markdown AST 代码块：info=%r content_length=%d", info, len(token.content))
        if info != "json":
            continue
        try:
            return json.loads(token.content)
        except json.JSONDecodeError as exc:
            log().debug("json 代码块解析失败：line=%d column=%d error=%s",
                        exc.lineno, exc.colno, exc.msg)
            return None
    return None


# --------------------------------------------------------------------------- 批量
def grade_all(cfg, gitea, state, keys: list[str] | None = None,
              wait: bool = True, dry_run: bool = False) -> dict[str, dict]:
    """对已推送的学生逐个等任务 + 收分数，写入 state。"""
    out: dict[str, dict] = {}
    rows = state.students(cfg.assignment_name)
    for row in rows:
        key = row["student_key"]
        if keys and key not in keys:
            continue
        repo = row.get("repo")
        run = state.last_run(cfg.assignment_name, key) or {}
        sha = run.get("commit_sha") or ""
        info = {"repo": repo, "sha": sha, "task_status": "", "score": None, "error": ""}
        failed = state.first_student_error(cfg.assignment_name, key, ("pull", "unpack", "provision"))
        system_error = state.first_system_error(cfg.assignment_name, key, ("pull", "unpack", "provision"))
        if failed or system_error:
            stage, detail = failed or system_error
            info["task_status"] = "skipped"
            info["error"] = f"跳过评分：{stage} 阶段失败：{detail}"
            log().error("学生 %s 跳过收分：%s", key, info["error"])
            state.set_stage(cfg.assignment_name, key, "grade", "skipped", info["error"])
            out[key] = info
            continue
        if not repo:
            continue
        provision_status = state.stage_status(cfg.assignment_name, key, "provision")
        if provision_status != "done":
            info["task_status"] = "skipped"
            info["error"] = (f"跳过评分：provision 阶段状态为 {provision_status or '未记录'}，"
                              "建仓/推送未成功")
            log().error("学生 %s 跳过收分：%s", key, info["error"])
            state.set_stage(cfg.assignment_name, key, "grade", "skipped", info["error"])
            out[key] = info
            continue
        try:
            if dry_run:
                info["task_status"] = "dry-run"
            else:
                task = wait_run(cfg, gitea, repo, sha) if wait else None
                if task and run.get("id"):
                    info["task_status"] = str(task.get("status") or "")
                    state.update_run(int(run["id"]), task_id=task.get("id"),
                                     task_status=info["task_status"],
                                     run_number=task.get("run_number"))
                collected = collect_score(cfg, gitea, repo, sha, key)
                info.update({k: collected[k] for k in ("score", "comment", "source")})
                state.save_score(cfg.assignment_name, key,
                                 score=collected.get("score"),
                                 comment=collected.get("comment", ""),
                                 stages=json.dumps(collected.get("stages", []), ensure_ascii=False),
                                 source=collected.get("source", ""),
                                 commit_sha=sha,
                                 raw_json=json.dumps(collected, ensure_ascii=False))
            status = "done" if (dry_run or info["score"] is not None) else "failed"
            state.set_stage(cfg.assignment_name, key, "grade", status,
                            info.get("error") or f"task={info['task_status']} score={info['score']}")
        except Exception as exc:
            info["error"] = str(exc)
            log().error("学生 %s 收集分数失败：%s", key, exc)
            state.set_stage(cfg.assignment_name, key, "grade", "failed", str(exc), "system")
        out[key] = info
    return out


def local_clone_score(cfg, repo: str) -> dict | None:
    """备用通道：直接克隆仓库读 joj3_result.json（绕过 API，适合排查）。"""
    from .provision import askpass_env, _prepare_askpass
    from .util import run
    with tempfile.TemporaryDirectory(prefix=f"gc-clone-{repo}-") as tmp:
        tmp_dir = Path(tmp)
        askpass = _prepare_askpass(tmp_dir)
        env = askpass_env(cfg.gitea_owner, cfg.gitea_token)
        env["GIT_ASKPASS"] = str(askpass)
        try:
            run(["git", "clone", "-q",
                 f"{cfg.gitea_url}/{cfg.org}/{repo}.git", str(tmp_dir / "repo")],
                env=env, check=True)
        except (RuntimeError, subprocess.CalledProcessError) as exc:
            log().error("克隆 %s 失败：%s", repo, exc)
            return None
        p = tmp_dir / "repo" / "joj3_result.json"
        if not p.exists():
            return None
        return parse_joj3_result(json.loads(p.read_text(encoding="utf-8")))
