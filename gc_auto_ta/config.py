"""单文件配置。

**一个 config.yaml 装下整条流水线需要的一切**：

    canvas:      站点、token、course_id、assignment_id
    gitea:       站点、容器内地址、token、PAT 所属用户名、组织
    assignment:  作业名、仓库前缀、可见性、默认分支
    grading:     JOJ3 评分样例（conf.json / 测试脚本 / 期望输出：内联文本或 files_dir 目录）
    unpack:      解包与源码收集规则
    writeback:   回写 Canvas 的策略（dry/live、分数换算、评语模板、迟交扣分）
    roster:      学生 → 仓库/别名 的人工覆盖表（可选）
    paths/limits: 工作目录、状态库、限流与轮询参数

设计要点：
  * token 直接写在文件里也可以（本机自用），但支持 "${ENV_NAME}" 从环境变量取值，
    推荐 `.env` 或 shell 里 export 后写成 "${CANVAS_API_TOKEN}"；文件权限请 chmod 600。
  * 优先级：内置默认值 < 配置文件 < 环境变量（${...} 展开） < CLI 参数。
  * 配置里出现的 token 在日志、报告、state 里都不会被打印（见 redact()）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .util import log, read_yaml

DEFAULTS: dict[str, Any] = {
    "canvas": {
        "base_url": "https://oc.sjtu.edu.cn",
        "per_page": 100,
        "min_interval": 0.10,
    },
    "gitea": {
        "url": "http://localhost:3000",
        # job 容器里访问 Gitea 用这个地址（本机部署 runner 用 network: host → localhost:3000）
        "internal_url": "http://localhost:3000",
        "owner": "",
        "org": "JOJ",
        # 组织可见性要如实填：私有组织下的「公开仓库」匿名一样读不到（Gitea 按 owner 可见性拦截）
        "org_private": False,
        "min_interval": 0.05,
    },
    "assignment": {
        "name": "hw1",
        "repo_prefix": None,          # 留空 = name
        "repo_private": False,
        "default_branch": "main",
        "description": "",
    },
    "grading": {
        "layout": "in_repo",          # in_repo（评分文件在学生仓库里）| config_repo（scheme C）
        "publish": "issue",           # none | file（把 score-<sha>.json push 回仓库）| issue（开 issue）
        "conf_root": "joj",           # 容器内传给 joj3 -conf-root 的路径
        "conf_name": "conf.json",
        "max_score": 100,
        "files_dir": None,            # 目录：内容原样搬进仓库（与 files 二选一，files 优先）
        "files": {},                  # 内联：{"joj/run_tests.m": "文本内容", ...}
        "workflow_template": None,    # 留空按 layout+publish+可见性自动选
        "config_repo": None,          # layout=config_repo 时，评分文件所在的 TA 私有仓库名
        "commit_prefix": "feat",      # 提交信息：feat(<作业>): <学生> submission [<作业>]
        "score_file_pattern": "score-{sha7}.json",
    },
    "unpack": {
        "student_file_globs": ["*.m", "*.cpp", "*.cc", "*.c", "*.h", "*.hpp", "*.py",
                               "*.java", "*.go", "*.rs", "*.js", "*.txt", "*.csv", "*.dat"],
        "remove_archives": False,
        "max_extract_bytes": 2 * 1024 ** 3,
        "on_missing_submission": "skip",   # skip | zero（缺交是否按 0 分回写）
    },
    "writeback": {
        "mode": "dry",                # off | dry | live
        "grade_mode": "points",       # points（换算到作业满分）| percent | raw
        "missing_score": 0,
        "comment_template": "【自动评分】{score}/{max} 分\n{comment}",
        "comment_max_len": 4000,
        "late_penalty_per_day": 0,
        "due_at": None,
        "bulk": True,                 # True = 用 Canvas 批量接口（异步 job）
        "only_grade": False,          # True = 只写分数不写评论
    },
    "roster": {},
    "paths": {
        "work_dir": "work",
        "state_db": "state/state.sqlite3",
        "report_dir": "reports",
    },
    "limits": {
        "concurrency": 4,
        "grading_poll_interval": 10,
        "grading_timeout": 1800,
        "push_retries": 3,
    },
}

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for k, v in (overlay or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def expand_env(value: Any) -> Any:
    """把 "${VAR}" / "${VAR:-默认值}" 展开成环境变量值（递归处理 dict/list）。"""
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    if not isinstance(value, str):
        return value

    def repl(m: re.Match) -> str:
        expr = m.group(1)
        name, _, default = expr.partition(":-")
        return os.environ.get(name, default)

    return _ENV_PATTERN.sub(repl, value)


def _load_env_file(path: Path) -> None:
    """把项目根目录的 .env 读进 os.environ（不覆盖已有值），方便 ${VAR} 展开。"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def redact(text: str, secrets: list[str]) -> str:
    """把日志/报告里可能出现的 token 抹掉。"""
    out = text or ""
    for s in secrets:
        if s and len(s) >= 8:
            out = out.replace(s, "***")
    return out


@dataclass
class Config:
    path: Path
    project_root: Path
    raw: dict = field(default_factory=dict)

    # ------------------------------------------------------------------ 构造
    @classmethod
    def load(cls, path: str | Path | None = None, project_root: str | Path | None = None,
             overrides: dict | None = None) -> "Config":
        root = Path(project_root or Path(__file__).resolve().parent.parent).resolve()
        _load_env_file(root / ".env")

        cfg_path = Path(path).expanduser() if path else (root / "config.yaml")
        if not cfg_path.is_absolute():
            cfg_path = (Path.cwd() / cfg_path).resolve()
        if not cfg_path.exists():
            raise FileNotFoundError(
                f"找不到配置文件 {cfg_path}\n"
                f"先生成模板：python -m gc_auto_ta new-config -o {cfg_path}"
            )

        loaded = read_yaml(cfg_path, None)
        if loaded is None:
            raise RuntimeError(f"配置文件为空或不是合法 YAML：{cfg_path}")
        raw = _deep_merge(DEFAULTS, expand_env(loaded))
        if overrides:
            raw = _deep_merge(raw, {k: v for k, v in overrides.items() if v is not None})

        cfg = cls(path=cfg_path, project_root=root, raw=raw)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        problems: list[str] = []
        if not self.get("canvas.course_id"):
            problems.append("canvas.course_id 未填写")
        if not self.get("canvas.assignment_id"):
            problems.append("canvas.assignment_id 未填写")
        if not self.canvas_token:
            problems.append("canvas.token 为空（也没有 ${CANVAS_API_TOKEN}）")
        if not self.gitea_token:
            problems.append("gitea.token 为空（也没有 ${GITEA_TOKEN}）")
        if not self.org:
            problems.append("gitea.org 未填写")
        if self.layout == "config_repo" and not self.config_repo:
            problems.append("grading.layout=config_repo 时必须填 grading.config_repo")
        if not self.grading_files():
            problems.append("没有任何评分文件：请填 grading.files（内联）或 grading.files_dir（目录）")
        if problems:
            raise RuntimeError("配置有问题：\n  - " + "\n  - ".join(problems))

    # ------------------------------------------------------------------ 读值
    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    @property
    def secrets(self) -> list[str]:
        return [str(self.canvas_token or ""), str(self.gitea_token or "")]

    # ------------------------------------------------------------------ Canvas
    @property
    def canvas_base_url(self) -> str:
        return str(self.get("canvas.base_url")).rstrip("/")

    @property
    def canvas_token(self) -> str:
        return str(self.get("canvas.token") or os.environ.get("CANVAS_API_TOKEN") or "")

    @property
    def canvas_course_id(self) -> int:
        return int(self.get("canvas.course_id"))

    @property
    def canvas_assignment_id(self) -> int:
        return int(self.get("canvas.assignment_id"))

    # ------------------------------------------------------------------ Gitea
    @property
    def gitea_url(self) -> str:
        return str(self.get("gitea.url")).rstrip("/")

    @property
    def gitea_internal_url(self) -> str:
        return str(self.get("gitea.internal_url") or self.get("gitea.url")).rstrip("/")

    @property
    def gitea_token(self) -> str:
        return str(self.get("gitea.token") or os.environ.get("GITEA_TOKEN") or "")

    @property
    def gitea_owner(self) -> str:
        return str(self.get("gitea.owner") or "")

    @property
    def org(self) -> str:
        return str(self.get("gitea.org") or "")

    @property
    def org_private(self) -> bool:
        return bool(self.get("gitea.org_private"))

    # ------------------------------------------------------------------ 作业
    @property
    def assignment_name(self) -> str:
        return str(self.get("assignment.name"))

    @property
    def repo_prefix(self) -> str:
        return str(self.get("assignment.repo_prefix") or self.assignment_name)

    @property
    def repo_private(self) -> bool:
        return bool(self.get("assignment.repo_private"))

    @property
    def default_branch(self) -> str:
        return str(self.get("assignment.default_branch"))

    @property
    def repo_description(self) -> str:
        return str(self.get("assignment.description") or f"{self.assignment_name} auto-graded by JOJ3")

    # ------------------------------------------------------------------ 评分
    @property
    def layout(self) -> str:
        return str(self.get("grading.layout"))

    @property
    def publish(self) -> str:
        return str(self.get("grading.publish"))

    @property
    def conf_root(self) -> str:
        return str(self.get("grading.conf_root")).strip("/")

    @property
    def conf_name(self) -> str:
        return str(self.get("grading.conf_name"))

    @property
    def max_score(self) -> float:
        return float(self.get("grading.max_score") or 100)

    @property
    def config_repo(self) -> str | None:
        v = self.get("grading.config_repo")
        return str(v) if v else None

    @property
    def commit_prefix(self) -> str:
        return str(self.get("grading.commit_prefix") or "feat")

    @property
    def score_file_pattern(self) -> str:
        return str(self.get("grading.score_file_pattern") or "score-{sha7}.json")

    def score_file_name(self, sha: str, student_key: str = "") -> str:
        return self.score_file_pattern.format(sha7=(sha or "")[:7], sha=sha or "",
                                              student=student_key, assignment=self.assignment_name)

    @property
    def workflow_template(self) -> str:
        """显式指定优先；留空按 layout + publish + 仓库/组织可见性自动选。

        私有组织/私有仓库下匿名 clone 必 401（容器无终端，git 卡在 could not read Username），
        所以 in_repo 形态必须按可见性分流到带 token 的模板；config_repo 各模板本来就带 token
        clone，不受可见性影响。
        凑不出匹配就抛错——绝不静默退回匿名模板，那等于往每个学生仓库里埋雷。
        """
        explicit = self.get("grading.workflow_template")
        if explicit:
            return str(explicit)
        private = self.repo_private or self.org_private
        if self.layout == "config_repo":
            table = {"none": "config-repo", "file": "config-repo-secret",
                     "issue": "config-repo-issue"}
        elif private:
            # in_repo+none 在私有形态下没有安全模板（匿名 clone 必死），故意不提供 → 走下面的报错
            table = {"file": "in-repo-secret", "issue": "in-repo-secret-issue"}
        else:
            table = {"none": "in-repo-public", "file": "in-repo-secret",
                     "issue": "in-repo-public-issue"}
        name = table.get(self.publish)
        if not name:
            raise ValueError(
                f"layout={self.layout!r} publish={self.publish!r} private={private} "
                f"凑不出 workflow 模板：私有组织/仓库不能用匿名 clone，"
                f"请显式指定 grading.workflow_template 或调整 publish/layout")
        return name

    def grading_files(self) -> dict[str, str]:
        """返回 {仓库内相对路径: 文件内容}。

        grading.files 内联优先；否则读 grading.files_dir 下的全部文件（保持相对路径）。
        """
        inline = self.get("grading.files") or {}
        out: dict[str, str] = {}
        if isinstance(inline, dict) and inline:
            for rel, content in inline.items():
                if isinstance(content, str):
                    out[str(rel)] = content
                else:
                    # 允许把 conf.json 直接写成 YAML 映射，这里再序列化回 JSON/YAML 文本
                    import json as _json
                    out[str(rel)] = _json.dumps(content, ensure_ascii=False, indent=2)
        if out:
            return out

        files_dir = self.get("grading.files_dir")
        if not files_dir:
            return {}
        base = Path(str(files_dir)).expanduser()
        if not base.is_absolute():
            base = self.path.parent / base
        if not base.exists():
            raise FileNotFoundError(f"grading.files_dir 不存在：{base}")
        for p in sorted(base.rglob("*")):
            if not p.is_file() or any(part.startswith(".") for part in p.relative_to(base).parts):
                continue
            out[str(p.relative_to(base))] = p.read_text(encoding="utf-8", errors="replace")
        return out

    # ------------------------------------------------------------------ 解包
    @property
    def student_file_globs(self) -> list[str]:
        return list(self.get("unpack.student_file_globs") or [])

    @property
    def remove_archives(self) -> bool:
        return bool(self.get("unpack.remove_archives"))

    @property
    def max_extract_bytes(self) -> int:
        return int(self.get("unpack.max_extract_bytes") or 0)

    @property
    def on_missing_submission(self) -> str:
        return str(self.get("unpack.on_missing_submission") or "skip")

    # ------------------------------------------------------------------ 回写
    @property
    def writeback_mode(self) -> str:
        return str(self.get("writeback.mode") or "dry")

    @property
    def grade_mode(self) -> str:
        return str(self.get("writeback.grade_mode") or "points")

    @property
    def comment_template(self) -> str:
        return str(self.get("writeback.comment_template") or "{score}/{max}\n{comment}")

    @property
    def comment_max_len(self) -> int:
        return int(self.get("writeback.comment_max_len") or 4000)

    @property
    def late_penalty_per_day(self) -> float:
        return float(self.get("writeback.late_penalty_per_day") or 0)

    @property
    def writeback_bulk(self) -> bool:
        return bool(self.get("writeback.bulk"))

    @property
    def only_grade(self) -> bool:
        return bool(self.get("writeback.only_grade"))

    # ------------------------------------------------------------------ 名单
    @property
    def roster_raw(self) -> dict:
        return self.get("roster") or {}

    # ------------------------------------------------------------------ 路径 / 参数
    def _path(self, dotted: str, default: str) -> Path:
        p = Path(str(self.get(dotted) or default)).expanduser()
        return p if p.is_absolute() else (self.path.parent / p)

    @property
    def work_dir(self) -> Path:
        return self._path("paths.work_dir", "work")

    @property
    def state_db(self) -> Path:
        return self._path("paths.state_db", "state/state.sqlite3")

    @property
    def report_dir(self) -> Path:
        return self._path("paths.report_dir", "reports")

    @property
    def assignment_work_dir(self) -> Path:
        return self.work_dir / self.assignment_name

    @property
    def concurrency(self) -> int:
        return max(1, int(self.get("limits.concurrency") or 4))

    @property
    def poll_interval(self) -> float:
        return float(self.get("limits.grading_poll_interval") or 10)

    @property
    def grading_timeout(self) -> float:
        return float(self.get("limits.grading_timeout") or 1800)

    @property
    def gitea_min_interval(self) -> float:
        return float(self.get("gitea.min_interval") or 0)

    @property
    def canvas_min_interval(self) -> float:
        return float(self.get("canvas.min_interval") or 0)

    # ------------------------------------------------------------------ 概览
    def describe(self) -> str:
        files = self.grading_files()
        lines = [
            f"配置文件      : {self.path}",
            f"Canvas        : {self.canvas_base_url}  course={self.canvas_course_id} "
            f"assignment={self.canvas_assignment_id}",
            f"Gitea         : {self.gitea_url}（容器内 {self.gitea_internal_url}）"
            f"  org={self.org}  owner={self.gitea_owner}",
            f"作业          : {self.assignment_name}  仓库 {self.repo_prefix}-<学生>  "
            f"{'私有' if self.repo_private else '公开'}  分支 {self.default_branch}",
            f"评分          : layout={self.layout} publish={self.publish} "
            f"conf={self.conf_root}/{self.conf_name} 满分={self.max_score}",
            f"评分文件      : {', '.join(sorted(files)) if files else '(空)'}",
            f"回写          : mode={self.writeback_mode} grade_mode={self.grade_mode} "
            f"bulk={self.writeback_bulk}",
            f"工作目录      : {self.assignment_work_dir}",
            f"状态库        : {self.state_db}",
        ]
        return "\n".join(lines)
