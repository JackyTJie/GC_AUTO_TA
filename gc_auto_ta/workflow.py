"""渲染 .gitea/workflows/joj3.yaml。

模板放在 templates/workflow/*.tmpl，占位符是 {{name}}（不用 Jinja，零依赖）。

为什么不是「一个模板加 if」：这个部署上三种部署形态差异太大
（匿名 clone / PAT clone / 从私有 config 仓库取评分文件），
拆成独立模板更好读，也更容易让 TA 自己改一份。

可用占位符：
    {{org}} {{repo}} {{assignment}} {{branch}}
    {{gitea_internal}} {{conf_root}} {{conf_name}} {{owner}}
    {{score_pattern}}   例如 score-{sha7}.json 的模板名（workflow 里用 shell 变量拼）
    {{job_label}}       runner 标签，默认 golang-ubuntu-latest
    {{secret_name}}     org secret 名，默认 GRADE_TOKEN
    {{config_repo}}     layout=config_repo 时的评分文件仓库名
"""

from __future__ import annotations

import re
from pathlib import Path

from .util import log, read_text

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "workflow"
PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


class TemplateError(RuntimeError):
    pass


def render(template_text: str, values: dict) -> str:
    """替换 {{key}}；未知占位符会报错而不是静默留下 {{...}}。"""

    def repl(m: re.Match) -> str:
        key = m.group(1)
        if key not in values:
            raise TemplateError(f"模板占位符 {{{{{key}}}}} 没有对应取值")
        return str(values[key])

    return PLACEHOLDER.sub(repl, template_text)


def template_path(name: str) -> Path:
    p = TEMPLATE_DIR / f"{name}.yaml.tmpl"
    if not p.exists():
        raise TemplateError(
            f"找不到 workflow 模板 {p}。可用模板：{', '.join(sorted(t.stem.split('.')[0] for t in TEMPLATE_DIR.glob('*.yaml.tmpl')))}"
        )
    return p


def context_from_config(cfg) -> dict:
    """从 Config 生成渲染上下文。"""
    return {
        "org": cfg.org,
        "assignment": cfg.assignment_name,
        "branch": cfg.default_branch,
        "gitea_internal": cfg.gitea_internal_url,
        "conf_root": cfg.conf_root,
        "conf_name": cfg.conf_name,
        "owner": cfg.gitea_owner,
        "job_label": cfg.get("grading.job_label", "golang-ubuntu-latest"),
        "secret_name": cfg.get("grading.secret_name", "GRADE_TOKEN"),
        "config_repo": cfg.config_repo or "",
        "score_pattern": cfg.score_file_pattern,
    }


def build_workflow(cfg, repo: str, student_key: str = "") -> tuple[str, str]:
    """返回 (仓库内路径, 渲染后的 YAML 文本)。"""
    name = cfg.workflow_template
    ctx = context_from_config(cfg)
    ctx.update({"repo": repo, "student": student_key})
    text = render(read_text(template_path(name)), ctx)
    log().debug("workflow 模板 %s 渲染完成（repo=%s）", name, repo)
    return ".gitea/workflows/joj3.yaml", text
