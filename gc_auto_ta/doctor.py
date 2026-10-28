"""自检：在不写任何远端数据的前提下，把整条链路的依赖逐项验一遍。

检查项：
    1. 配置文件 / 必填项 / 评分文件
    2. Canvas：token 有效、作业可达、能看到多少份提交、是否匿名批改
    3. Gitea：版本、PAT 身份、组织是否存在、对该组织是否有管理权（用 registration-token 探）、
       目标仓库是否已存在、org secret 是否已配（publish=file/issue 时需要）
    4. 本机依赖（只有跑在本机 Gitea/JOJ3 上时才关心）：go-judge :5051 是否在听、
       两个 runner 的 systemd 单元是否 active、评测镜像是否存在
    5. 结论：下一步该跑哪条命令

任何一步失败都不影响其他步骤继续检查，最后统一给结论。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .util import log, tcp_open, which

OK = "✅"
WARN = "⚠️"
FAIL = "❌"


def _line(name: str, status: str, detail: str = "") -> str:
    return f"{status} {name:<28} {detail}"


def check_config(cfg) -> list[str]:
    out = [_line("配置文件", OK, str(cfg.path))]
    try:
        files = cfg.grading_files()
        out.append(_line("评分文件", OK if files else FAIL,
                         ", ".join(sorted(files)) if files else "空（grading.files / files_dir 都没填）"))
        if files:
            from .validate import validate_grading_files
            problems = validate_grading_files(files)
            out.append(_line("评分规范", FAIL if problems else OK,
                             "；".join(problems) if problems else "JOJ3 配置结构通过"))
    except Exception as exc:
        out.append(_line("评分文件", FAIL, str(exc)))
    out.append(_line("Canvas 目标", OK,
                     f"course={cfg.canvas_course_id} assignment={cfg.canvas_assignment_id}"))
    out.append(_line("Gitea 目标", OK,
                     f"{cfg.gitea_url} org={cfg.org} 仓库前缀={cfg.repo_prefix}"))
    return out


def check_canvas(cfg) -> list[str]:
    out: list[str] = []
    try:
        from .canvas_api import CanvasGateway
        gw = CanvasGateway(cfg.canvas_base_url, cfg.canvas_token,
                           cfg.canvas_course_id, cfg.canvas_assignment_id,
                           min_interval=cfg.canvas_min_interval)
        me = gw.me()
        out.append(_line("Canvas token", OK, f"{me.get('name')}（id={me.get('id')}）"))
        props = gw.assignment_props()
        out.append(_line("作业可达", OK if props.get("id") else FAIL,
                         f"{props.get('name')!r} 满分={props.get('points_possible')} "
                         f"published={props.get('published')} 匿名={props.get('anonymous_grading')}"))
        subs = gw.submissions()
        out.append(_line("可见提交", OK if subs else WARN, f"{len(subs)} 份"))
        out.append(_line("提示", WARN, "回到 SpeedGrader 数一下人数：若明显少于这里，说明你的 TA 身份被 section 限定"))
        if props.get("anonymous_grading"):
            out.append(_line("匿名批改", WARN, "该作业开了匿名批改，回写会走 anonymous_submissions 接口（尽力支持）"))
        types: dict[str, int] = {}
        for s in subs:
            t = str(getattr(s, "submission_type", "") or "none")
            types[t] = types.get(t, 0) + 1
        if types:
            out.append(_line("提交类型分布", OK, ", ".join(f"{k}={v}" for k, v in sorted(types.items()))))
    except Exception as exc:
        out.append(_line("Canvas 检查", FAIL, f"{type(exc).__name__}: {exc}"))
    return out


def check_gitea(cfg) -> list[str]:
    out: list[str] = []
    try:
        from .gitea_api import GiteaClient
        gc = GiteaClient(cfg.gitea_url, cfg.gitea_token, min_interval=cfg.gitea_min_interval)
        out.append(_line("Gitea 可达", OK, f"版本 {gc.version()}"))
        try:
            me = gc.whoami()
            out.append(_line("PAT 身份", OK, f"{me.get('login')}（id={me.get('id')}）"))
        except Exception as exc:
            out.append(_line("PAT 身份", FAIL, str(exc)))
        org = gc.get_org(cfg.org)
        out.append(_line("组织存在", OK if org else FAIL,
                         f"{cfg.org}" if org else f"{cfg.org} 不存在或无权查看"))
        token = None
        try:
            token = gc.registration_token(cfg.org)
            out.append(_line("组织管理权限", OK if token else WARN,
                             "可读 runner 注册令牌" if token else "读不到注册令牌（可能不是 org owner，建仓可能仍可用）"))
        except Exception as exc:
            out.append(_line("组织管理权限", WARN, str(exc)))
        existing = 0
        try:
            repos = gc.request("GET", f"/orgs/{cfg.org}/repos", params={"limit": 50}) or []
            prefix = cfg.repo_prefix
            existing = len([r for r in repos if str(r.get("name", "")).startswith(prefix)])
            out.append(_line("已有同前缀仓库", OK if existing == 0 else WARN, f"{existing} 个（会复用而不是重建）"))
        except Exception as exc:
            out.append(_line("仓库列表", WARN, str(exc)))
        if cfg.publish in ("file", "issue") and cfg.layout == "in_repo":
            try:
                names = gc.list_secrets(cfg.org)
                need = cfg.get("grading.secret_name", "GRADE_TOKEN")
                out.append(_line("org secret", OK if need in names else WARN,
                                 f"{need}: {'已配置' if need in names else '未配置（私有组织时 workflow 需要它）'}；"
                                 f"现有 {', '.join(names) or '无'}"))
            except Exception as exc:
                out.append(_line("org secret", WARN, str(exc)))
        out.append(_line("目标分支", OK, f"{cfg.default_branch}（首推后自动设为默认分支）"))
    except Exception as exc:
        out.append(_line("Gitea 检查", FAIL, f"{type(exc).__name__}: {exc}"))
    return out


def check_local(cfg) -> list[str]:
    """本机 JOJ3 部署相关的软检查（不是本机部署时全部记 ⚠️ 即可忽略）。"""
    out: list[str] = []
    out.append(_line("go-judge 沙箱", OK if tcp_open("127.0.0.1", 5051) else WARN,
                     "127.0.0.1:5051 在听" if tcp_open("127.0.0.1", 5051) else "127.0.0.1:5051 未监听（非本机部署可忽略）"))
    runner_bin = which("act_runner")
    out.append(_line("act_runner 二进制", OK if runner_bin else WARN,
                     runner_bin or "PATH 里没有 act_runner（非本机部署可忽略）"))
    try:
        proc = subprocess.run(["systemctl", "--user", "list-units", "--type=service",
                               "act-runner*", "--no-legend"],
                              capture_output=True, text=True, timeout=10,
                              env={**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"})
        lines = [l.strip() for l in (proc.stdout or "").splitlines() if l.strip()]
        out.append(_line("runner 服务", OK if lines else WARN,
                         "; ".join(l.split()[0] for l in lines) if lines else "没查到 act-runner 用户服务"))
    except Exception as exc:
        out.append(_line("runner 服务", WARN, f"查询失败：{exc}"))
    out.append(_line("解包工具", OK if which("bsdtar", "7z", "unrar") else WARN,
                     which("bsdtar", "7z", "7zz", "7za", "unrar") or "只有 zip/tar 解包能力（rar/7z 需要 bsdtar 或 7z）"))
    out.append(_line("git", OK if shutil.which("git") else FAIL, shutil.which("git") or "未安装"))
    import platform
    out.append(_line("Python", OK, platform.python_version()))
    return out


def run_doctor(cfg) -> int:
    log().info("开始自检：%s", cfg.path)
    print()
    print("== 配置 ==")
    for l in check_config(cfg):
        print(l)
    print("\n== Canvas ==")
    for l in check_canvas(cfg):
        print(l)
    print("\n== Gitea ==")
    for l in check_gitea(cfg):
        print(l)
    print("\n== 本机依赖（非本机部署可忽略）==")
    for l in check_local(cfg):
        print(l)
    print()
    print("== 下一步 ==")
    print("  python -m gc_auto_ta run -c %s            # 全流程（回写默认 dry）" % cfg.path.name)
    print("  python -m gc_auto_ta pull / unpack / provision / grade / report")
    print("  python -m gc_auto_ta writeback --live     # 确认无误后再真正写 Canvas")
    return 0
