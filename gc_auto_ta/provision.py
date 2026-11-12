"""组装学生仓库并推送。

每个学生的仓库工作区结构（layout=in_repo，见 JOJ3 指南第 5 章 scheme A）：

    <临时目录>/
    |-- .gitea/workflows/joj3.yaml     ← 触发评测
    |-- joj/conf.json                  ← 评分规则（来自 config.yaml 的 grading.files）
    |-- joj/run_tests.m
    |-- joj/expected.txt
    +-- <学生源码>.m / .cpp / ...

学生本人**没有仓库写权限**（TA 代传模式），所以评分文件天然不可被改。

认证：用 PAT 推送到 Gitea，token 通过 GIT_ASKPASS 脚本 + 环境变量传给 git，
既不出现在命令行参数里（不会被 ps 看到），也不会被写进任何仓库文件。
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Iterable

from .util import log, run, write_json, write_text

ASKPASS = """#!/bin/sh
# git 询问用户名/密码时从这里取值（值来自环境变量，不落盘、不进 argv）
case "$1" in
  *Username*|*username*) printf '%s\\n' "$GC_GIT_USER" ;;
  *Password*|*password*) printf '%s\\n' "$GC_GIT_PASS" ;;
  *) printf '\\n' ;;
esac
"""


def askpass_env(owner: str, token: str) -> dict:
    return {
        "GC_GIT_USER": owner or "x",
        "GC_GIT_PASS": token,
        "GIT_TERMINAL_PROMPT": "0",
    }


def _prepare_askpass(tmp_dir: Path) -> Path:
    p = tmp_dir / "askpass.sh"
    write_text(p, ASKPASS)
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return p


def _remove_existing_workspace(dest: Path) -> None:
    """Remove a previous workspace, including Windows read-only Git objects."""
    dest = Path(dest)
    if not dest.exists():
        log().debug("旧工作区不存在，无需清理：%s", dest)
        return

    log().debug("发现旧工作区，开始清理：%s", dest)

    def onerror(func, path, exc_info):
        exc = exc_info[1]
        log().debug("清理工作区删除失败：func=%s path=%s error=%r；尝试清除只读属性",
                    getattr(func, "__name__", repr(func)), path, exc)
        try:
            mode = os.stat(path).st_mode
            os.chmod(path, mode | stat.S_IWRITE)
            func(path)
        except Exception:
            log().debug("清除只读属性后仍无法删除：path=%s", path, exc_info=True)
            raise
        log().debug("清理工作区重试删除成功：%s", path)

    try:
        shutil.rmtree(dest, onerror=onerror)
    except Exception:
        log().error("清理旧工作区失败：%s", dest, exc_info=True)
        raise
    log().debug("旧工作区清理完成：%s", dest)


def git(cfg, args: list[str], cwd: Path, askpass: Path | None = None, check: bool = True):
    cmd = ["git"]
    if askpass is not None:
        # 关掉任何全局 credential helper，只走我们自己的 GIT_ASKPASS 脚本
        cmd += ["-c", f"core.askPass={askpass}", "-c", "credential.helper="]
    cmd += args
    env = askpass_env(cfg.gitea_owner, cfg.gitea_token)
    env["GIT_ASKPASS"] = str(askpass) if askpass else "/bin/true"
    return run(cmd, cwd=cwd, env=env, check=check)


# --------------------------------------------------------------------------- 工作区
def build_workspace(cfg, repo: str, student_key: str, source_files: Iterable[Path],
                    dest: Path) -> Path:
    """把评分文件 + 学生源码 + workflow 组装到 dest（已存在则清空重建）。"""
    from .unpack import copy_student_files
    from .workflow import build_workflow

    dest = Path(dest)
    if dest.exists():
        _remove_existing_workspace(dest)
    dest.mkdir(parents=True)

    # 1) 评分文件（内联或目录）
    files = cfg.grading_files()
    if not files:
        raise RuntimeError("配置里没有任何评分文件（grading.files / grading.files_dir）")
    for rel, content in files.items():
        write_text(dest / rel, content if content.endswith("\n") else content + "\n")
    write_json(dest / "_gc_auto_ta.json", {
        "assignment": cfg.assignment_name,
        "student": student_key,
        "repo": repo,
        "source": "gc_auto_ta",
    })

    # 2) 学生源码（保持相对路径）
    copied = copy_student_files(list(source_files), dest)
    if not copied:
        log().warning("学生 %s 没有任何源码文件被拷入仓库", student_key)

    # 3) workflow
    rel, text = build_workflow(cfg, repo, student_key)
    write_text(dest / rel, text)

    # 4) 仓库级 .gitattributes（避免 CRLF 干扰 diff 类 parser 的逐行比较）
    write_text(dest / ".gitattributes", "* text=auto eol=lf\n")

    log().info("工作区就绪：%s（评分文件 %d 个，学生文件 %d 个）", dest, len(files), len(copied))
    return dest


def build_config_repo_workspace(cfg, dest: Path) -> Path:
    """layout=config_repo：把评分文件单独组装成一个 TA 私有仓库工作区。"""
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    files = cfg.grading_files()
    for rel, content in files.items():
        write_text(dest / rel, content if content.endswith("\n") else content + "\n")
    write_text(dest / "README.md",
               f"# {cfg.assignment_name} 评分配置\n\n由 GC_AUTO_TA 生成，供 JOJ3 workflow 通过 org secret 拉取。\n")
    return dest


# --------------------------------------------------------------------------- 推送
def push_workspace(cfg, workspace: Path, repo: str, message: str,
                   branch: str | None = None, allow_empty: bool = False) -> str:
    """init → commit → push，返回本次推送的 commit sha。"""
    workspace = Path(workspace)
    branch = branch or cfg.default_branch
    url = f"{cfg.gitea_url}/{cfg.org}/{repo}.git"

    with tempfile.TemporaryDirectory(prefix="gc-askpass-") as tmp:
        askpass = _prepare_askpass(Path(tmp))

        git(cfg, ["init", "-q", "-b", branch], cwd=workspace, askpass=None, check=False)
        if not (workspace / ".git").exists():          # 老版本 git 不支持 -b
            git(cfg, ["init", "-q"], cwd=workspace)
            git(cfg, ["checkout", "-q", "-b", branch], cwd=workspace, check=False)

        git(cfg, ["add", "-A"], cwd=workspace)
        args = ["commit", "-q", "-m", message]
        if allow_empty:
            args.insert(1, "--allow-empty")
        proc = git(cfg, args, cwd=workspace, check=False)
        if proc.returncode != 0 and "nothing to commit" not in (proc.stdout or "") + (proc.stderr or ""):
            raise RuntimeError(f"git commit 失败：{(proc.stderr or proc.stdout or '')[-800:]}")

        sha = git(cfg, ["rev-parse", "HEAD"], cwd=workspace).stdout.strip()

        try:
            git(cfg, ["remote", "add", "origin", url], cwd=workspace, check=False)
            git(cfg, ["remote", "set-url", "origin", url], cwd=workspace)
            git(cfg, ["push", "-q", "--force", "origin", f"HEAD:refs/heads/{branch}"],
                cwd=workspace, askpass=askpass)
        except RuntimeError as exc:
            hint = ""
            msg = str(exc)
            if "could not read Username" in msg or "Authentication failed" in msg or "401" in msg:
                hint = ("\n提示：克隆/推送认证失败。检查 PAT 是否有 write:repository 权限；"
                        "若是私有组织，workflow 里的 clone 也需要 org secret（docs/05）。")
            raise RuntimeError(f"推送到 {url} 失败：{exc}{hint}") from exc

    log().info("已推送 %s/%s @ %s", cfg.org, repo, sha[:7])
    return sha


def rerun(cfg, repo: str, student_key: str, work_root: Path) -> str:
    """空提交触发重跑（JOJ3 指南 scenario 3）。"""
    import tempfile as _t
    with _t.TemporaryDirectory(prefix=f"rerun-{repo}-") as tmp:
        ws = Path(tmp) / repo
        url = f"{cfg.gitea_url}/{cfg.org}/{repo}.git"
        with _t.TemporaryDirectory(prefix="gc-askpass-") as tmp2:
            askpass = _prepare_askpass(Path(tmp2))
            git(cfg, ["clone", "-q", url, str(ws)], cwd=Path(tmp), askpass=askpass)
            proc = git(cfg, ["commit", "--allow-empty", "-q", "-m",
                             f"chore({cfg.assignment_name}): rerun {student_key} [{cfg.assignment_name}] [skip ci]"],
                       cwd=ws, check=False)
            sha = git(cfg, ["rev-parse", "HEAD"], cwd=ws).stdout.strip()
            git(cfg, ["push", "-q", "origin", f"HEAD:{cfg.default_branch}"], cwd=ws, askpass=askpass)
    log().info("已触发重跑 %s/%s @ %s", cfg.org, repo, sha[:7])
    return sha


def _delete_org_repositories(cfg, gitea) -> int:
    repos = gitea.list_repos(cfg.org)
    errors: list[str] = []
    for repo in repos:
        name = str(repo.get("name") or "")
        if name:
            log().warning("删除组织仓库：%s/%s", cfg.org, name)
            try:
                gitea.delete_repo(cfg.org, name)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                log().error("删除组织仓库失败 %s/%s：%s", cfg.org, name, exc)
    if errors:
        raise RuntimeError("部分组织仓库删除失败；已继续尝试其余仓库：" + "; ".join(errors))
    return len([repo for repo in repos if repo.get("name")])


# --------------------------------------------------------------------------- 主流程
def provision_all(cfg, gitea, state, students_with_files: dict, dry_run: bool = False,
                  variant: str | None = None) -> dict:
    """给每个学生建仓 + 推送。students_with_files: {student_key: {"student": Student, "files": [Path]}}

    返回 {student_key: {"repo":..., "sha":..., "error":...}}
    """
    from .naming import repo_name

    if variant is None:
        variants = cfg.grading_variants() if hasattr(cfg, "grading_variants") else [cfg]
        if len(variants) > 1:
            combined: dict[str, dict] = {}
            for variant_cfg in variants:
                combined = provision_all(variant_cfg, gitea, state, students_with_files,
                                         dry_run=dry_run, variant=variant_cfg.variant_suffix)
            return combined

    out: dict[str, dict] = {}
    work = cfg.assignment_work_dir / "workspaces"
    work.mkdir(parents=True, exist_ok=True)

    if cfg.delete_repos_before_setup and not dry_run:
        try:
            deleted = _delete_org_repositories(cfg, gitea)
            log().warning("已删除组织 %s 的 %d 个仓库", cfg.org, deleted)
        except Exception as exc:
            detail = f"清理组织仓库失败：{exc}"
            log().error(detail)
            for key, item in students_with_files.items():
                repo = repo_name(cfg.repo_prefix, key)
                state.set_stage(cfg.assignment_name, key, "provision", "failed", detail, "system")
                out[key] = {"repo": repo, "sha": "", "error": detail, "status": "failed"}
            return out

    # layout=config_repo：先把评分文件推到一个 TA 私有仓库
    if cfg.layout == "config_repo" and not dry_run:
        try:
            cfg_repo = cfg.config_repo or f"{cfg.assignment_name}-config"
            gitea.ensure_repo(cfg.org, cfg_repo, private=True, default_branch=cfg.default_branch,
                              description=f"{cfg.assignment_name} grading config")
            ws = build_config_repo_workspace(cfg, work / "_config_repo")
            push_workspace(cfg, ws, cfg_repo, f"chore({cfg.assignment_name}): grading config")
            log().info("评分配置仓库已更新：%s/%s", cfg.org, cfg_repo)
        except Exception as exc:
            detail = f"共享评分配置仓库失败：{exc}"
            for key, item in students_with_files.items():
                repo = repo_name(cfg.repo_prefix, key)
                state.set_stage(cfg.assignment_name, key, "provision", "failed", detail, "system")
                out[key] = {"repo": repo, "sha": "", "error": detail, "status": "failed"}
            return out

    for key, item in students_with_files.items():
        student = item["student"]
        files = item.get("files") or []
        repo = repo_name(cfg.repo_prefix, key)
        info: dict = {"repo": repo, "sha": "", "error": "", "status": "pending"}
        failed = state.first_student_error(cfg.assignment_name, key, ("pull", "unpack"))
        system_error = state.first_system_error(cfg.assignment_name, key, ("pull", "unpack"))
        if failed or system_error:
            stage, detail = failed or system_error
            info.update(status="skipped", error=f"跳过 provision：{stage} 阶段失败：{detail}")
            state.set_stage(cfg.assignment_name, key, "provision", "skipped", info["error"])
            out[key] = info
            continue
        try:
            if not dry_run:
                gitea.ensure_repo(cfg.org, repo, private=cfg.repo_private,
                                  default_branch=cfg.default_branch,
                                  description=cfg.repo_description)
            ws = build_workspace(cfg, repo, key, files, work / repo)
            if dry_run:
                log().info("[dry-run] 将推送 %s/%s（%d 个文件）", cfg.org, repo, len(files))
                info["sha"] = "dry-run"
            else:
                msg = (f"{cfg.commit_prefix}({cfg.assignment_name}): {student.name or key} "
                       f"submission [{cfg.assignment_name}]")
                info["sha"] = push_workspace(cfg, ws, repo, msg)
                # 首次推送后把默认分支指到我们推的分支（空仓库默认分支可能是 master）
                try:
                    gitea.set_default_branch(cfg.org, repo, cfg.default_branch)
                except Exception as exc:
                    log().debug("设置默认分支失败（可忽略）：%s", exc)
                state.add_run(cfg.assignment_name, key, repo, info["sha"], variant or "")
            state.upsert_student(cfg.assignment_name, key,
                                 canvas_user_id=student.canvas_user_id,
                                 login_id=student.login_id, name=student.name,
                                 repo=repo, repo_url=f"{cfg.gitea_url}/{cfg.org}/{repo}",
                                 code_dir=str(ws))
            state.set_stage(cfg.assignment_name, key, "provision",
                            "done" if not info["error"] else "failed", info.get("error", ""))
            info["status"] = "done" if not info["error"] else "failed"
        except Exception as exc:
            info["error"] = str(exc)
            info["status"] = "failed"
            log().error("学生 %s 建仓/推送失败：%s", key, exc)
            state.set_stage(cfg.assignment_name, key, "provision", "failed", str(exc), "system")
        out[key] = info
    return out
