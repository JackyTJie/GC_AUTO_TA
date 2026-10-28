"""推送前校验评分文件里的 JOJ3 配置。

JOJ3 只把 `executor.with.default`（以及 `with.cases` 的覆盖项）当作要执行的命令，
`executor` 下写别的字段会被静默忽略。一旦真正的命令写到了别处，
`with.default.args` 就可能只剩一个环境变量字符串，沙箱会把它当可执行文件名去
execve，得到 `Internal Error`，而 `result-status` 的 `forceQuitOnNotAccepted`
默认是开着的，整次评测就直接 `parser force quit` 了——报错里看不出根因。

这里把容易写错的地方在推送前挡下来：
    * `executor` 只允许 `name` / `with` 两个字段
    * `with.default.args` 必须是非空字符串列表
    * `args[0]` 不能是 `KEY=VALUE`（那是 `env` 的写法）
    * `with.cases[*].args` 如果写了，同样不能以 `KEY=VALUE` 开头
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# env 条目的形状：PATH=/usr/bin:/bin
ENV_ENTRY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# 顶层允许出现的字段（JOJ3 的 conf.Conf）
TOP_LEVEL_KEYS = {
    "name", "logPath", "actorCsvPath", "sandboxExecServer", "sandboxToken",
    "outputPath", "preStages", "stages", "postStages",
}
STAGE_SECTIONS = ("preStages", "stages", "postStages")
EXECUTOR_KEYS = {"name", "with"}


CONF_NAME = re.compile(r"^conf(-[A-Za-z0-9_-]+)?\.json$")


def is_conf_file(rel: str) -> bool:
    """只校验 JOJ3 配置：conf.json / conf-release.json 这类文件名。"""
    return bool(CONF_NAME.match(Path(rel).name))


def _check_args(where: str, args: Any, problems: list[str]) -> None:
    if not isinstance(args, list) or not args:
        problems.append(f"{where}.args 必须是非空字符串列表")
        return
    if any(not isinstance(a, str) or not a for a in args):
        problems.append(f"{where}.args 里只能是非空字符串")
        return
    if ENV_ENTRY.match(args[0]):
        problems.append(
            f"{where}.args[0] = {args[0]!r} 是环境变量写法，"
            f"JOJ3 会把它当成可执行文件名；请放进同级的 env")
    elif "=" in args[0] and " " not in args[0]:
        problems.append(f"{where}.args[0] = {args[0]!r} 看起来不像可执行文件")


def _check_stage(section: str, index: int, stage: Any, problems: list[str]) -> None:
    where = f"{section}[{index}]"
    if not isinstance(stage, dict):
        problems.append(f"{where} 必须是映射")
        return
    name = stage.get("name") or f"stage-{index}"
    where = f"{where} ({name})"

    executor = stage.get("executor")
    if not isinstance(executor, dict):
        problems.append(f"{where}.executor 缺失或不是映射")
        return
    extra = sorted(set(executor) - EXECUTOR_KEYS)
    if extra:
        problems.append(
            f"{where}.executor 只允许 {sorted(EXECUTOR_KEYS)}，"
            f"发现会被 JOJ3 忽略的字段 {extra}")
    if not isinstance(executor.get("name"), str) or not executor.get("name"):
        problems.append(f"{where}.executor.name 缺失")
    with_ = executor.get("with")
    if not isinstance(with_, dict):
        problems.append(f"{where}.executor.with 缺失或不是映射")
        return
    default = with_.get("default")
    if not isinstance(default, dict):
        problems.append(f"{where}.with.default 缺失或不是映射")
    else:
        _check_args(f"{where}.with.default", default.get("args"), problems)

    cases = with_.get("cases")
    if cases is not None:
        if not isinstance(cases, list):
            problems.append(f"{where}.with.cases 必须是列表")
        else:
            for ci, case in enumerate(cases):
                if not isinstance(case, dict):
                    problems.append(f"{where}.with.cases[{ci}] 必须是映射")
                    continue
                if "args" in case:
                    _check_args(f"{where}.with.cases[{ci}]", case["args"], problems)


def validate_conf(rel: str, data: Any) -> list[str]:
    """校验一份已解析的 JOJ3 配置；返回问题列表（空列表 = 通过）。"""
    problems: list[str] = []
    if not isinstance(data, dict):
        return [f"{rel}: 顶层必须是映射"]
    unknown = sorted(set(data) - TOP_LEVEL_KEYS)
    if unknown:
        problems.append(f"{rel}: 未知顶层字段 {unknown}")
    for section in STAGE_SECTIONS:
        stages = data.get(section)
        if stages is None:
            continue
        if not isinstance(stages, list):
            problems.append(f"{rel}: {section} 必须是列表")
            continue
        stage_problems: list[str] = []
        for i, stage in enumerate(stages):
            _check_stage(section, i, stage, stage_problems)
        problems.extend(f"{rel}: {p}" for p in stage_problems)
    return problems


def validate_grading_files(files: dict[str, str]) -> list[str]:
    """校验所有 JOJ3 配置文件，返回带文件名的总问题列表。"""
    problems: list[str] = []
    for rel, content in sorted(files.items()):
        if not is_conf_file(rel):
            continue
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            problems.append(f"{rel}: 不是合法 JSON（{exc.msg}，第 {exc.lineno} 行）")
            continue
        problems.extend(validate_conf(rel, data))
    return problems


def ensure_grading_files(files: dict[str, str]) -> None:
    """provision 前调用：有问题直接拒绝推送。"""
    problems = validate_grading_files(files)
    if problems:
        raise RuntimeError(
            "评分配置不规范，拒绝推送（修好后再跑 provision）：\n  - "
            + "\n  - ".join(problems))
