"""命令行入口。

    python -m gc_auto_ta new-config -o config.yaml     # 生成完整配置模板
    python -m gc_auto_ta doctor -c config.yaml         # 全链路自检（不写远端）
    python -m gc_auto_ta run -c config.yaml            # 拉取→解包→建仓→评测→收分→报告
    python -m gc_auto_ta writeback -c config.yaml --live  # 单独写入 Canvas
    python -m gc_auto_ta pull|unpack|provision|grade|report|writeback -c config.yaml   # 分步跑
    python -m gc_auto_ta rerun --only 2026000001     # 空提交重跑
    python -m gc_auto_ta status -c config.yaml         # 看状态库摘要
    python -m gc_auto_ta reset --stage grade           # 清掉某阶段，强制重跑
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from . import __version__
from .config import Config
from .util import log, setup_logging

DEFAULT_CONFIG_NAME = "config.yaml"


def _only(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


def _load_cfg(args) -> Config:
    overrides = {}
    if getattr(args, "org", None):
        overrides["gitea"] = {"org": args.org}
    if getattr(args, "course", None):
        overrides.setdefault("canvas", {})["course_id"] = args.course
    if getattr(args, "assignment", None):
        overrides.setdefault("canvas", {})["assignment_id"] = args.assignment
    if getattr(args, "live", False):
        overrides.setdefault("writeback", {})["mode"] = "live"
    if getattr(args, "dry_run", False):
        overrides.setdefault("writeback", {})["mode"] = "dry"
    return Config.load(args.config, overrides=overrides)


# --------------------------------------------------------------------------- 子命令
def cmd_new_config(args) -> int:
    target = Path(args.output or DEFAULT_CONFIG_NAME).expanduser()
    if target.exists() and not args.force:
        print(f"已存在：{target}（要覆盖请加 --force）")
        return 1
    example = Path(__file__).resolve().parent.parent / "config.example.yaml"
    if not example.exists():
        print(f"找不到模板 {example}")
        return 1
    shutil.copyfile(example, target)
    target.chmod(0o600)   # 里面可能直接写 token
    print(f"已生成 {target}（权限 600）")
    print("下一步：填写 canvas.course_id / canvas.assignment_id / 两个 token / grading.files，")
    print(f"然后 python -m gc_auto_ta doctor -c {target.name}")
    return 0


def cmd_doctor(args) -> int:
    from .doctor import run_doctor
    return run_doctor(_load_cfg(args))


def cmd_pull(args) -> int:
    from .pipeline import pull
    cfg = _load_cfg(args)
    manifest = pull(cfg, limit=args.limit, only=_only(args.only))
    students = manifest.get("students") or {}
    print(f"\n拉取完成：{len(students)} 名学生，manifest = {cfg.assignment_work_dir / 'manifest.json'}")
    return 0


def cmd_unpack(args) -> int:
    from .pipeline import unpack_stage
    cfg = _load_cfg(args)
    manifest = unpack_stage(cfg)
    print(f"\n解包完成：{len(manifest.get('students') or {})} 名学生；"
          f"未匹配 {len(manifest.get('unmatched') or [])}，歧义 {len(manifest.get('ambiguous') or {})}")
    return 0


def cmd_provision(args) -> int:
    from .gitea_api import GiteaClient
    from .pipeline import students_with_files
    from .provision import provision_all
    from .state import State
    cfg = _load_cfg(args)
    items = students_with_files(cfg)
    if args.only:
        keep = set(_only(args.only))
        items = {k: v for k, v in items.items() if k in keep}
    if args.limit:
        items = dict(list(items.items())[: args.limit])
    gitea = GiteaClient(cfg.gitea_url, cfg.gitea_token, min_interval=cfg.gitea_min_interval)
    with State(cfg.state_db) as state:
        out = provision_all(cfg, gitea, state, items, dry_run=args.dry_run)
    ok = len([v for v in out.values() if not v["error"]])
    print(f"\n推送完成：成功 {ok} / {len(out)}")
    for key, info in out.items():
        if info["error"]:
            print(f"  {key}: {info['error']}")
    return 0 if ok == len(out) else 2


def cmd_grade(args) -> int:
    from .gitea_api import GiteaClient
    from .grading import aggregate_scores, grade_all
    from .state import State
    cfg = _load_cfg(args)
    gitea = GiteaClient(cfg.gitea_url, cfg.gitea_token, min_interval=cfg.gitea_min_interval)
    with State(cfg.state_db) as state:
        variants = cfg.grading_variants()
        out = {}
        for variant_cfg in variants:
            out = grade_all(variant_cfg, gitea, state, keys=_only(args.only),
                            wait=not args.no_wait, dry_run=args.dry_run)
        aggregate_scores(cfg, state, keys=_only(args.only))
        graded = {row["student_key"] for row in state.scores(cfg.assignment_name)
                  if row.get("score") is not None}
    got = len(graded)
    print(f"\n收分完成：拿到分数 {got} / {len(out)}")
    return 0 if got else 2


def _run_grading_variants(cfg, args) -> dict:
    """Run provision and grade as one ordered transaction per grading variant."""
    from .gitea_api import GiteaClient
    from .grading import aggregate_scores, grade_all
    from .pipeline import students_with_files
    from .provision import provision_all
    from .state import State

    items = students_with_files(cfg)
    if args.only:
        keep = set(_only(args.only))
        items = {k: v for k, v in items.items() if k in keep}
    if args.limit:
        items = dict(list(items.items())[: args.limit])
    gitea = GiteaClient(cfg.gitea_url, cfg.gitea_token, min_interval=cfg.gitea_min_interval)
    final_out: dict = {}
    with State(cfg.state_db) as state:
        for variant_cfg in cfg.grading_variants():
            log().info("开始评分配置 %s", variant_cfg.variant_suffix)
            provision_all(variant_cfg, gitea, state, items, dry_run=args.dry_run,
                          variant=variant_cfg.variant_suffix)
            final_out = grade_all(variant_cfg, gitea, state, keys=_only(args.only),
                                  wait=True, dry_run=args.dry_run)
        aggregate_scores(cfg, state, keys=_only(args.only))
    return final_out


def cmd_report(args) -> int:
    from .report import build_rows, print_table, write_reports
    from .state import State
    cfg = _load_cfg(args)
    with State(cfg.state_db) as state:
        rows = build_rows(cfg, state)
        cfg._report_state = {}
        for score in state.score_variants(cfg.assignment_name):
            cfg._report_state.setdefault(score.get("variant") or "", []).append(score)
    if not rows:
        print("状态库里没有记录：先跑 pull / provision / grade")
        return 1
    print_table(rows, limit=args.limit)
    write_reports(cfg, rows)
    print(f"\n报告目录：{cfg.report_dir / cfg.assignment_name}")
    return 0


def cmd_writeback(args) -> int:
    from .writeback import run as wb_run
    from .state import State
    cfg = _load_cfg(args)
    with State(cfg.state_db) as state:
        res = wb_run(cfg, state, live=args.live, only=_only(args.only),
                     bulk=(False if args.no_bulk else None))
    written = len([v for v in res.values() if v.get("posted_grade")])
    print(f"\n回写完成：{written} 人（mode={cfg.writeback_mode}）")
    return 0


def cmd_rerun(args) -> int:
    from .state import State
    from .provision import rerun
    cfg = _load_cfg(args)
    keys = _only(args.only)
    with State(cfg.state_db) as state:
        rows = state.students(cfg.assignment_name)
    targets = [r for r in rows if not keys or r["student_key"] in keys]
    if not targets:
        print("没有匹配的学生（先 provision）")
        return 1
    for r in targets:
        try:
            sha = rerun(cfg, r["repo"], r["student_key"], cfg.assignment_work_dir)
            print(f"  {r['student_key']} → {r['repo']} @ {sha[:7]}")
        except Exception as exc:
            print(f"  {r['student_key']} 重跑失败：{exc}")
    return 0


def cmd_status(args) -> int:
    from .state import State
    cfg = _load_cfg(args)
    with State(cfg.state_db) as state:
        summary = state.summary(cfg.assignment_name)
        stages = state.stages(cfg.assignment_name)
    print(cfg.describe())
    print()
    print("状态库摘要：", summary)
    by_stage: dict[str, dict[str, int]] = {}
    for s in stages:
        by_stage.setdefault(s["stage"], {}).setdefault(s["status"], 0)
        by_stage[s["stage"]][s["status"]] += 1
    for stage, counts in sorted(by_stage.items()):
        print(f"  {stage:<10} " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 0


def cmd_reset(args) -> int:
    from .state import State, reset_stage
    cfg = _load_cfg(args)
    with State(cfg.state_db) as state:
        reset_stage(state, cfg.assignment_name, args.stage, _only(args.only))
    return 0


def cmd_run(args) -> int:
    """执行从 Canvas 拉取到成绩报告的流程，不执行 Canvas 成绩写回。"""
    cfg = _load_cfg(args)
    print(cfg.describe())
    print()

    steps = args.stages or ["pull", "unpack", "provision", "grade", "report"]
    variants = cfg.grading_variants() if hasattr(cfg, "grading_variants") else [cfg]
    multi_pass = len(variants) > 1
    for step in steps:
        if step == "provision" and multi_pass and "grade" in steps:
            print("\n===== provision + grade (per configuration) =====")
            out = _run_grading_variants(cfg, args)
            got = len([v for v in out.values() if v.get("score") is not None])
            if got == 0 and out:
                print("多组评分均未拿到分数")
            steps = [s for s in steps if s != "grade"]
            continue
        print(f"\n===== {step} =====")
        ns = argparse.Namespace(config=args.config, verbose=args.verbose, only=args.only,
                                limit=args.limit, no_wait=False, dry_run=args.dry_run,
                                org=None, course=None, assignment=None,
                                output=None, force=False)
        code = {
            "pull": cmd_pull, "unpack": cmd_unpack, "provision": cmd_provision,
            "grade": cmd_grade, "report": cmd_report,
        }[step](ns)
        if code not in (0, 2):       # 2 = 部分学生失败，继续跑完
            print(f"\n阶段 {step} 失败（退出码 {code}），中止。可单独重跑该阶段。")
            return code
    print("\n报告阶段完成。确认 reports/ 内容后，请单独执行："
          f"\n  python -m gc_auto_ta writeback -c {Path(args.config).name} --live")
    return 0


# --------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gc_auto_ta",
                                description="Canvas → 解包 → Gitea → JOJ3 → Canvas 回写 一体化流水线")
    p.add_argument("-c", "--config", default=None,
                   help=f"配置文件路径（默认 ./{DEFAULT_CONFIG_NAME}）")
    p.add_argument("-v", "--verbose", action="store_true", help="DEBUG 日志")
    p.add_argument("--version", action="version", version=f"gc_auto_ta {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("new-config", help="生成完整配置文件模板")
    sp.add_argument("-o", "--output", default=None)
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_new_config)

    sp = sub.add_parser("doctor", help="全链路自检（只读）")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("pull", help="从 Canvas 拉提交并下载")
    sp.add_argument("--limit", type=int, default=None, help="只处理前 N 份（演练用）")
    sp.add_argument("--only", default=None, help="只处理这些学生标识（逗号分隔）")
    sp.set_defaults(func=cmd_pull)

    sp = sub.add_parser("unpack", help="解包 + 归集学生源码")
    sp.set_defaults(func=cmd_unpack)

    sp = sub.add_parser("provision", help="建仓 / 灌入评分文件与代码 / 推送")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--limit", type=int, default=None)
    sp.add_argument("--only", default=None)
    sp.set_defaults(func=cmd_provision)

    sp = sub.add_parser("grade", help="等待评测任务并收集分数")
    sp.add_argument("--no-wait", action="store_true", help="不等待，只尝试读结果")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--only", default=None)
    sp.set_defaults(func=cmd_grade)

    sp = sub.add_parser("report", help="汇总结果（JSON/CSV/Markdown）")
    sp.add_argument("--limit", type=int, default=40, help="控制台打印行数，0=全部")
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("writeback", help="回写 Canvas 分数与评论")
    sp.add_argument("--live", action="store_true", help="真正写入（默认 dry）")
    sp.add_argument("--no-bulk", action="store_true", help="不用批量接口，逐条写")
    sp.add_argument("--only", default=None)
    sp.set_defaults(func=cmd_writeback)

    sp = sub.add_parser("rerun", help="空提交触发重跑")
    sp.add_argument("--only", default=None)
    sp.set_defaults(func=cmd_rerun)

    sp = sub.add_parser("status", help="查看状态库与配置摘要")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("reset", help="重置某阶段状态（强制重跑）")
    sp.add_argument("--stage", required=True,
                    choices=["pull", "unpack", "provision", "grade", "writeback", "all"])
    sp.add_argument("--only", default=None)
    sp.set_defaults(func=cmd_reset)

    sp = sub.add_parser("run", help="执行到成绩报告（不写入 Canvas）")
    sp.add_argument("--dry-run", action="store_true", help="建仓和评分阶段使用演练模式")
    sp.add_argument("--limit", type=int, default=None, help="只处理前 N 份（试跑用）")
    sp.add_argument("--only", default=None)
    sp.add_argument("--stages", nargs="*", default=None,
                    choices=["pull", "unpack", "provision", "grade", "report"],
                    help="只跑指定阶段，例如 --stages pull unpack report")
    sp.set_defaults(func=cmd_run)

    # 通用覆盖项（所有子命令都可用）
    for name, sp_ in list(sub.choices.items()):
        if name == "new-config":
            continue
        sp_.add_argument("--org", default=None, help="覆盖 gitea.org")
        sp_.add_argument("--course", type=int, default=None, help="覆盖 canvas.course_id")
        sp_.add_argument("--assignment", type=int, default=None, help="覆盖 canvas.assignment_id")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    if args.command == "new-config":
        return cmd_new_config(args)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断")
        return 130
    except Exception as exc:
        log().error("%s: %s", type(exc).__name__, exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
