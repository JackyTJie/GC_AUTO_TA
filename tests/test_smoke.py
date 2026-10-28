#!/usr/bin/env python3
"""离线冒烟测试：不碰 Canvas / Gitea，只验证纯逻辑。

    python3 tests/test_smoke.py

覆盖：配置解析（含 ${ENV} 展开与评分文件内联）→ workflow 渲染 →
名单匹配（含"不猜"规则）→ 解包（zip / 套一层目录 / 全班级大包）→
joj3_result 解析 → 分数换算与迟交扣分 → 状态库读写。
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import zipfile
from types import SimpleNamespace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

passed = 0
failed: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed.append(f"{name} {detail}")
        print(f"  FAIL {name} {detail}")


def test_config(tmp: Path) -> None:
    print("[config] 单文件配置解析")
    os.environ["GC_TEST_CANVAS_TOKEN"] = "canvas-token-123456"
    cfg_file = tmp / "config.yaml"
    cfg_file.write_text(f"""
canvas:
  base_url: https://oc.sjtu.edu.cn
  token: "${{GC_TEST_CANVAS_TOKEN}}"
  course_id: 12345
  assignment_id: 4242
gitea:
  url: http://localhost:3000
  internal_url: http://localhost:3000
  token: "gitea-token-abcdef"
  owner: your-gitea-username
  org: JOJ
assignment:
  name: hw1
grading:
  layout: in_repo
  publish: issue
  files:
    joj/run_tests.m: |
      disp("hi")
    joj/conf.json:
      name: hw1
      stages: []
writeback:
  mode: dry
paths:
  work_dir: {tmp / 'work'}
  state_db: {tmp / 'state.sqlite3'}
""", encoding="utf-8")

    from gc_auto_ta.config import Config
    cfg = Config.load(cfg_file)
    check("token 从 ${ENV} 展开", cfg.canvas_token == "canvas-token-123456")
    check("课程/作业 ID", (cfg.canvas_course_id, cfg.canvas_assignment_id) == (12345, 4242))
    files = cfg.grading_files()
    check("内联评分文件 2 个", set(files) == {"joj/run_tests.m", "joj/conf.json"})
    check("YAML 映射自动序列化成 JSON", '"name": "hw1"' in files["joj/conf.json"])
    check("模板自动选择 (in_repo+issue)", cfg.workflow_template == "in-repo-public-issue")
    check("score 文件名", cfg.score_file_name("abcdef1234") == "score-abcdef1.json")

    from gc_auto_ta.workflow import PLACEHOLDER, build_workflow, template_path
    import yaml

    rel, text = build_workflow(cfg, "hw1-2026000001", "2026000001")
    check("workflow 路径", rel == ".gitea/workflows/joj3.yaml")
    left = PLACEHOLDER.findall(text)
    check("workflow 无未替换占位符", not left, str(left))
    check("${{ secrets.* }} 原样保留", "${{ secrets.GRADE_TOKEN }}" in text)
    check("workflow 是合法 YAML", isinstance(yaml.safe_load(text), dict))
    check("workflow 用到容器内地址", "http://localhost:3000" in text)

    # 六套模板都能渲染出合法 YAML
    from gc_auto_ta.workflow import render, context_from_config
    ctx = context_from_config(cfg)
    ctx.update({"repo": "hw1-123", "student": "123"})
    for tpl in sorted(p.name.replace(".yaml.tmpl", "") for p in
                      (ROOT / "templates" / "workflow").glob("*.yaml.tmpl")):
        try:
            doc = yaml.safe_load(render(template_path(tpl).read_text(encoding="utf-8"), ctx))
            ok = isinstance(doc, dict) and bool(doc.get("jobs"))
        except Exception as exc:
            ok, doc = False, exc
        check(f"模板 {tpl} 渲染为合法 YAML", ok, str(doc)[:120])
    return cfg


def test_naming() -> None:
    print("[naming] 匹配与不猜原则")
    from gc_auto_ta.naming import Student, match_entries

    students = [
        Student(canvas_user_id=1, student_key="2026000001", login_id="2026000001", name="示例学生"),
        Student(canvas_user_id=2, student_key="2026000002", login_id="2026000002", name="张三"),
    ]
    res = match_entries(["2026000001_示例学生.zip", "张三的作业.zip", "谁都不像.zip"], students)
    check("学号精确命中", res.mapping.get("2026000001") == ["2026000001_示例学生.zip"], str(res.mapping))
    check("人名命中", res.mapping.get("2026000002") == ["张三的作业.zip"], str(res.mapping))
    check("无法识别进 unmatched", res.unmatched == ["谁都不像.zip"], str(res.unmatched))
    check("无歧义", res.ambiguous == {})

    amb = match_entries(["示例学生.zip"], [
        Student(canvas_user_id=1, student_key="a1", name="示例学生"),
        Student(canvas_user_id=2, student_key="a2", name="示例学生"),
    ])
    check("同名不猜 → ambiguous", "示例学生.zip" in amb.ambiguous and not amb.mapping, str(amb.ambiguous))


def test_reset_submissions(tmp: Path) -> None:
    print("[pull] 下载前清空 submissions")
    from gc_auto_ta.pipeline import _reset_submissions_dir

    submissions = tmp / "submissions"
    stale = submissions / "old-student" / "old.zip"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old")

    result = _reset_submissions_dir(submissions)
    check("旧提交已清空", result == submissions and submissions.exists() and not stale.exists(),
          str(list(submissions.rglob("*"))))


def test_reset_unpacked(tmp: Path) -> None:
    print("[unpack] 解包前清空 unpacked")
    from gc_auto_ta.pipeline import _reset_unpacked_dir

    unpacked = tmp / "unpacked"
    stale = unpacked / "old-student" / "old" / "old.m"
    stale.parent.mkdir(parents=True)
    stale.write_text("old", encoding="utf-8")

    result = _reset_unpacked_dir(unpacked)
    check("旧解包结果已清空", result == unpacked and unpacked.exists() and not stale.exists(),
          str(list(unpacked.rglob("*"))))


def test_unpack(tmp: Path) -> None:
    print("[unpack] zip / 套一层目录 / 全班级大包")
    from gc_auto_ta.unpack import bundle_directories, extract, gather_source_files, split_bundle

    z1 = tmp / "single.zip"
    with zipfile.ZipFile(z1, "w") as zf:
        zf.writestr("mywork/full_adder.m", "function [s,c]=full_adder(a,b,cin)\nend\n")
        zf.writestr("__MACOSX/junk", "x")
    r1 = extract(z1, tmp / "out1")
    check("解包成功", r1.ok, r1.error)
    check("单根目录自动上提", (tmp / "out1" / "full_adder.m").exists())
    check("垃圾目录被清理", not (tmp / "out1" / "__MACOSX").exists())
    check("普通多文件作业不识别为大包", bundle_directories(tmp / "out1") == {},
          str(bundle_directories(tmp / "out1")))

    z2 = tmp / "bundle.zip"
    with zipfile.ZipFile(z2, "w") as zf:
        zf.writestr("2026000001/sol.m", "a=1;\n")
        zf.writestr("2026000002/sol.m", "a=2;\n")
    r2 = extract(z2, tmp / "out2")
    entries = split_bundle(tmp / "out2", tmp / "out2")
    check("大包拆出两个条目", set(entries) == {"2026000001", "2026000002"}, str(set(entries)))
    check("大包只识别学生目录", set(bundle_directories(tmp / "out2")) ==
          {"2026000001", "2026000002"}, str(bundle_directories(tmp / "out2")))
    files = gather_source_files(tmp / "out1", ["*.m"])
    check("按 glob 收集源码", len(files) == 1, str(files))

    evil = tmp / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../../etc/passwd", "x")
    extract(evil, tmp / "out3")
    check("拒绝路径穿越", not (tmp / "etc").exists() and r2.ok)


def test_unpack_is_idempotent(tmp: Path) -> None:
    print("[unpack] 重复解包不产生重复源码")
    from gc_auto_ta.unpack import copy_student_files, extract, gather_source_files

    archive = tmp / "Student_2026000001.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Student_2026000001/fib_seq.m", "a=1;\n")

    unpacked = tmp / "unpacked" / "2026000001" / archive.stem
    first = extract(archive, unpacked)
    second = extract(archive, unpacked)
    check("重复解包均成功", first.ok and second.ok, f"{first.error}; {second.error}")

    files = gather_source_files(unpacked, ["*.m"])
    check("重复解包只保留一份源码", [p.relative_to(unpacked) for p in files] == [Path("fib_seq.m")],
          str([p.relative_to(unpacked) for p in files]))

    workspace = tmp / "workspace"
    copied = copy_student_files(files, workspace)
    check("复制到仓库根目录", copied == [Path("fib_seq.m")] and (workspace / "fib_seq.m").exists(),
          str(copied))


def test_grading_parse() -> None:
    print("[grading] joj3_result 解析")
    from gc_auto_ta.grading import parse_joj3_result

    data = [
        {"name": "build", "results": [{"score": 0, "comment": "compiled"}], "force_quit": False},
        {"name": "run", "results": [{"score": 80, "comment": "7/8 passed"},
                                    {"score": 20, "comment": "fast"}], "force_quit": False},
    ]
    parsed = parse_joj3_result(data)
    check("总分 = 各 result 之和", parsed["score"] == 100, str(parsed["score"]))
    check("评语拼接", parsed["comment"] == "compiled；7/8 passed；fast", parsed["comment"])
    check("stage 明细", [s["name"] for s in parsed["stages"]] == ["build", "run"])

    from gc_auto_ta.grading import _extract_json_block
    body = '''commit `abc`

```json
[{"name":"run","results":[{"score":40,"comment":"File `stderr`:\\n`````````\\n\\n`````````"}]}]
```
'''
    check("通过 Markdown AST 提取 JSON 代码块",
          _extract_json_block(body) == [{"name": "run", "results": [{
              "score": 40, "comment": "File `stderr`:\n`````````\n\n`````````"}]}])


def test_provision_cleans_readonly_workspace(tmp: Path) -> None:
    print("[provision] 清理旧工作区中的只读文件")
    from gc_auto_ta.provision import _remove_existing_workspace

    workspace = tmp / "workspaces" / "hw1-2026000001"
    locked = workspace / ".git" / "objects" / "09" / "deadbeef"
    locked.parent.mkdir(parents=True)
    locked.write_text("old object", encoding="utf-8")
    locked.chmod(0o444)

    _remove_existing_workspace(workspace)

    check("旧工作区清理成功", not workspace.exists())


def test_grade_skips_provision_failures(tmp: Path) -> None:
    print("[grading] 建仓失败学生跳过后续评分")
    from types import SimpleNamespace

    from gc_auto_ta.grading import grade_all
    from gc_auto_ta.state import State

    class FakeGitea:
        def __init__(self):
            self.read_repos: list[str] = []

        def get_file(self, org, repo, name, ref=None):
            self.read_repos.append(repo)
            return '[{"name":"run","results":[{"score":80,"comment":"ok"}]}]'

    cfg = SimpleNamespace(
        assignment_name="hw1", org="JOJ", publish="file", default_branch="main",
        grading_timeout=1, poll_interval=0, score_file_name=lambda sha, key="": f"score-{sha[:7]}.json",
    )
    gitea = FakeGitea()
    with State(tmp / "grading-state.sqlite3") as state:
        state.upsert_student("hw1", "failed", canvas_user_id=1, repo="hw1-failed")
        state.upsert_student("hw1", "pull-failed", canvas_user_id=3)
        state.upsert_student("hw1", "ok", canvas_user_id=2, repo="hw1-ok")
        state.set_stage("hw1", "failed", "provision", "failed", "permission denied", "system")
        state.set_stage("hw1", "pull-failed", "pull", "failed", "download failed", "system")
        state.set_stage("hw1", "ok", "provision", "done")
        state.add_run("hw1", "ok", "hw1-ok", "abcdef123456")

        out = grade_all(cfg, gitea, state, wait=False)

        check("建仓失败学生被跳过", out["failed"]["task_status"] == "skipped", str(out["failed"]))
        check("pull 失败学生被跳过", out["pull-failed"]["task_status"] == "skipped",
              str(out["pull-failed"]))
        check("跳过原因可见", "provision" in out["failed"]["error"], str(out["failed"]))
        check("失败学生不读取评分", gitea.read_repos == ["hw1-ok"], str(gitea.read_repos))
        check("成功学生仍收分", out["ok"]["score"] == 80.0, str(out["ok"]))
        check("评分阶段记录 skipped", state.stage_status("hw1", "failed", "grade") == "skipped")


def test_gitea_issue_filter() -> None:
    print("[gitea] issue 与 pull request 过滤")
    from gc_auto_ta.gitea_api import GiteaClient

    client = GiteaClient.__new__(GiteaClient)
    client.request = lambda *args, **kwargs: [
        {"number": 1, "title": "Grading result abc", "pull_request": {"url": "..."}},
        {"number": 2, "title": "A real PR", "pull_request": {"url": "..."}},
    ]
    issues = client.list_issues("org", "repo")
    check("保留评分 issue、过滤真正的 PR", [i["number"] for i in issues] == [1],
          str(issues))


def test_gitea_repo_cleanup() -> None:
    print("[gitea] 组织仓库清理")
    from gc_auto_ta.gitea_api import GiteaClient
    from gc_auto_ta.provision import _delete_org_repositories

    client = GiteaClient.__new__(GiteaClient)
    calls: list[tuple[str, dict]] = []

    def request(method, path, **kwargs):
        calls.append((path, kwargs))
        page = kwargs.get("params", {}).get("page")
        if page == 1:
            return [{"name": "old-1"}]
        if page == 2:
            return [{"name": "old-2"}]
        return []

    client.request = request
    repos = client.list_repos("org", limit=1)
    check("组织仓库列表支持分页", [r["name"] for r in repos] == ["old-1", "old-2"])

    deleted: list[str] = []
    client.list_repos = lambda org: [{"name": "old-1"}, {"name": "old-2"}]
    client.delete_repo = lambda org, repo: deleted.append(repo)
    cfg = SimpleNamespace(org="org")
    check("清理组织内全部仓库", _delete_org_repositories(cfg, client) == 2 and
          deleted == ["old-1", "old-2"], str(deleted))


def test_writeback(cfg) -> None:
    print("[writeback] 分数换算与迟交扣分")
    from gc_auto_ta.writeback import compute_grade, build_comment, late_days

    g = compute_grade(cfg, 87.5, 20.0)
    check("points 模式换算到作业满分", g["posted_grade"] == "17.5", g["posted_grade"])
    check("评语模板渲染", "【自动评分】" in build_comment(cfg, {"raw_score": 87.5, "stages": [],
                                                              "comment": "7/8 passed"}, g))
    check("未迟交不扣分", late_days("2026-09-20T10:00:00Z", "2026-09-21T10:00:00Z") == 0)
    check("迟 1 天算 1 天", late_days("2026-09-22T10:00:00Z", "2026-09-21T10:00:00Z") == 1)
    check("晚 1 分钟也算 1 天", late_days("2026-09-21T10:00:01Z", "2026-09-21T10:00:00Z") == 1)

    cfg_no = cfg.raw
    cfg_no["writeback"]["late_penalty_per_day"] = 0.1
    g2 = compute_grade(cfg, 80, 100.0, submitted_at="2026-09-23T00:00:00Z",
                       due_at="2026-09-21T00:00:00Z")
    check("迟 2 天扣 20%", g2["posted_grade"] == "60.0", str(g2))


def test_writeback_provision_failure_gets_zero(cfg, tmp: Path) -> None:
    print("[writeback] provision 失败学生按 0 分覆盖")
    import gc_auto_ta.writeback as writeback_module
    from gc_auto_ta.state import State

    class FakeCanvas:
        submitted: list[tuple[int, str]] = []

        def __init__(self, *args, **kwargs):
            pass

        def assignment_props(self):
            return {"points_possible": 100, "due_at": None, "anonymous_grading": False}

        def post_grade(self, uid, grade, comment="", dry_run=True, anonymous_id=None):
            if not dry_run:
                self.submitted.append((uid, str(grade)))
            return {"user_id": uid, "posted_grade": str(grade)}

        def post_grades_bulk(self, *args, **kwargs):
            raise AssertionError("bulk 回写不应被调用")

        def gradeable_anonymous_ids(self):
            return {}

    old_gateway = writeback_module.CanvasGateway
    writeback_module.CanvasGateway = FakeCanvas
    try:
        cfg.raw["unpack"]["on_missing_submission"] = "zero"
        cfg.raw["writeback"]["mode"] = "live"
        cfg.raw["writeback"]["bulk"] = False
        with State(tmp / "writeback-failed.sqlite3") as state:
            state.upsert_student("hw1", "failed", canvas_user_id=11, name="失败学生")
            state.set_stage("hw1", "failed", "provision", "failed", "permission denied", "student")
            state.save_score("hw1", "failed", 85, comment="old score")
            result = writeback_module.run(cfg, state, live=True)
            check("失败学生生成 0 分", result["failed"]["posted_grade"] == "0.0", str(result))
            check("失败学生实际提交 0 分", FakeCanvas.submitted == [(11, "0.0")],
                  str(FakeCanvas.submitted))
    finally:
        writeback_module.CanvasGateway = old_gateway


def test_writeback_provision_failure_dry_does_not_submit(cfg, tmp: Path) -> None:
    print("[writeback] dry 模式不提交失败学生分数")
    import gc_auto_ta.writeback as writeback_module
    from gc_auto_ta.state import State

    class FakeCanvas:
        submitted = 0

        def __init__(self, *args, **kwargs):
            pass

        def assignment_props(self):
            return {"points_possible": 100, "due_at": None, "anonymous_grading": False}

        def post_grade(self, uid, grade, comment="", dry_run=True, anonymous_id=None):
            if not dry_run:
                self.submitted += 1
            return {"user_id": uid, "posted_grade": str(grade)}

        def post_grades_bulk(self, *args, **kwargs):
            raise AssertionError("bulk 回写不应被调用")

        def gradeable_anonymous_ids(self):
            return {}

    old_gateway = writeback_module.CanvasGateway
    writeback_module.CanvasGateway = FakeCanvas
    try:
        cfg.raw["unpack"]["on_missing_submission"] = "zero"
        cfg.raw["writeback"]["mode"] = "dry"
        cfg.raw["writeback"]["bulk"] = False
        with State(tmp / "writeback-dry.sqlite3") as state:
            state.upsert_student("hw1", "failed", canvas_user_id=12, name="失败学生")
            state.set_stage("hw1", "failed", "provision", "failed", "permission denied", "student")
            state.save_score("hw1", "failed", 85, comment="old score")
            result = writeback_module.run(cfg, state, live=False)
            check("dry 模式仍生成 0 分计划", result["failed"]["posted_grade"] == "0.0", str(result))
            check("dry 模式没有实际提交", FakeCanvas.submitted == 0, str(FakeCanvas.submitted))
    finally:
        writeback_module.CanvasGateway = old_gateway


def test_writeback_any_prior_failure_gets_zero(cfg, tmp: Path) -> None:
    print("[writeback] 任一前置阶段失败都按 0 分覆盖")
    import gc_auto_ta.writeback as writeback_module
    from gc_auto_ta.state import State

    class FakeCanvas:
        submitted: list[tuple[int, str]] = []

        def __init__(self, *args, **kwargs):
            pass

        def assignment_props(self):
            return {"points_possible": 100, "due_at": None, "anonymous_grading": False}

        def post_grade(self, uid, grade, comment="", dry_run=True, anonymous_id=None):
            if not dry_run:
                self.submitted.append((uid, str(grade)))
            return {"user_id": uid, "posted_grade": str(grade)}

        def gradeable_anonymous_ids(self):
            return {}

    old_gateway = writeback_module.CanvasGateway
    writeback_module.CanvasGateway = FakeCanvas
    try:
        cfg.raw["unpack"]["on_missing_submission"] = "zero"
        cfg.raw["writeback"]["mode"] = "live"
        cfg.raw["writeback"]["bulk"] = False
        with State(tmp / "writeback-unpack-failed.sqlite3") as state:
            state.upsert_student("hw1", "failed", canvas_user_id=13, name="失败学生")
            state.set_stage("hw1", "failed", "unpack", "failed", "archive corrupt", "student")
            state.save_score("hw1", "failed", 85, comment="old score")
            result = writeback_module.run(cfg, state, live=True)
            check("unpack 失败学生生成 0 分", result["failed"]["posted_grade"] == "0.0", str(result))
            check("unpack 失败学生实际提交 0 分", FakeCanvas.submitted == [(13, "0.0")],
                  str(FakeCanvas.submitted))
    finally:
        writeback_module.CanvasGateway = old_gateway


def test_writeback_system_error_is_report_only(cfg, tmp: Path) -> None:
    print("[writeback] 系统错误不覆盖旧分数")
    import gc_auto_ta.writeback as writeback_module
    from gc_auto_ta.state import State

    class FakeCanvas:
        submitted = 0

        def __init__(self, *args, **kwargs):
            pass

        def assignment_props(self):
            return {"points_possible": 100, "due_at": None, "anonymous_grading": False}

        def post_grade(self, *args, **kwargs):
            self.submitted += 1
            return {}

        def gradeable_anonymous_ids(self):
            return {}

    old_gateway = writeback_module.CanvasGateway
    writeback_module.CanvasGateway = FakeCanvas
    try:
        cfg.raw["writeback"]["mode"] = "live"
        cfg.raw["writeback"]["bulk"] = False
        with State(tmp / "writeback-system.sqlite3") as state:
            state.upsert_student("hw1", "failed", canvas_user_id=16, name="失败学生")
            state.set_stage("hw1", "failed", "provision", "failed", "Gitea unavailable", "system")
            state.save_score("hw1", "failed", 85, comment="old score")
            result = writeback_module.run(cfg, state, live=True)
            check("系统错误只报告", "error" in result["failed"], str(result))
            check("系统错误不提交旧分数", FakeCanvas.submitted == 0, str(FakeCanvas.submitted))
    finally:
        writeback_module.CanvasGateway = old_gateway


def test_provision_skips_prior_stage_failure(cfg, tmp: Path) -> None:
    print("[provision] 前置阶段失败时跳过建仓")
    from gc_auto_ta.naming import Student
    from gc_auto_ta.provision import provision_all
    from gc_auto_ta.state import State

    class FakeGitea:
        calls = 0

        def ensure_repo(self, *args, **kwargs):
            self.calls += 1

    item = {"failed": {"student": Student(14, "failed", login_id="failed"), "files": []}}
    with State(tmp / "provision-skipped.sqlite3") as state:
        state.upsert_student("hw1", "failed", canvas_user_id=14, name="失败学生")
        state.set_stage("hw1", "failed", "pull", "failed", "download failed", "student")
        gitea = FakeGitea()
        result = provision_all(cfg, gitea, state, item)
        check("前置失败返回 skipped", result["failed"]["status"] == "skipped", str(result))
        check("前置失败不建仓", gitea.calls == 0, str(gitea.calls))
        check("provision 阶段记录 skipped", state.stage_status("hw1", "failed", "provision") == "skipped")


def test_unpack_marks_extract_error(cfg, tmp: Path) -> None:
    print("[unpack] 损坏压缩包标记错误")
    from gc_auto_ta.pipeline import save_manifest, unpack_stage
    from gc_auto_ta.state import State

    assignment_dir = cfg.assignment_work_dir
    source = assignment_dir / "submissions" / "failed" / "broken.zip"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"not a zip")
    save_manifest(cfg, {
        "assignment": "hw1",
        "students": {
            "failed": {
                "canvas_user_id": 15,
                "login_id": "failed",
                "name": "失败学生",
                "raw_files": [str(source.relative_to(assignment_dir))],
            }
        },
    })
    unpack_stage(cfg)
    with State(cfg.state_db) as state:
        check("损坏压缩包记录 unpack failed",
              state.stage_status("hw1", "failed", "unpack") == "failed")


def test_state(tmp: Path, cfg) -> None:
    print("[state] 状态库幂等")
    from gc_auto_ta.state import State

    with State(tmp / "state.sqlite3") as st:
        st.upsert_student("hw1", "2026000001", canvas_user_id=1, name="示例学生", repo="hw1-2026000001")
        st.upsert_student("hw1", "2026000001", canvas_user_id=1, name="示例学生", repo="hw1-2026000001")
        st.set_stage("hw1", "2026000001", "provision", "done")
        st.save_score("hw1", "2026000001", 87.5, comment="ok", source="file")
        st.save_writeback("hw1", "2026000001", 1, "17.5", "ok", "dry")
        check("学生不重复", st.summary("hw1")["students"] == 1, str(st.summary("hw1")))
        check("阶段状态可查", st.stage_done("hw1", "2026000001", "provision"))
        check("分数落库", st.scores("hw1")[0]["score"] == 87.5)
        check("回写落库", st.writebacks("hw1")[0]["posted_grade"] == "17.5")


def test_state_error_categories(tmp: Path) -> None:
    print("[state] 阶段错误分类与旧库迁移")
    from gc_auto_ta.state import State

    old_db = tmp / "state-old-schema.sqlite3"
    conn = sqlite3.connect(old_db)
    conn.executescript("""
        CREATE TABLE students (
            assignment TEXT NOT NULL, student_key TEXT NOT NULL,
            canvas_user_id INTEGER, login_id TEXT, name TEXT, section TEXT,
            repo TEXT, repo_url TEXT, code_dir TEXT, extra TEXT, updated_at REAL,
            PRIMARY KEY (assignment, student_key)
        );
        CREATE TABLE stages (
            assignment TEXT NOT NULL, student_key TEXT NOT NULL,
            stage TEXT NOT NULL, status TEXT NOT NULL, detail TEXT, updated_at REAL,
            PRIMARY KEY (assignment, student_key, stage)
        );
        INSERT INTO students (assignment, student_key, name) VALUES ('hw1', 'legacy', '旧学生');
        INSERT INTO stages (assignment, student_key, stage, status, detail)
            VALUES ('hw1', 'legacy', 'pull', 'failed', '旧版本失败');
    """)
    conn.commit()
    conn.close()

    with State(old_db) as st:
        check("旧学生数据保留", st.get_student("hw1", "legacy")["name"] == "旧学生")
        check("旧阶段失败仍可查", st.first_failed_stage("hw1", "legacy") == ("pull", "旧版本失败"))
        check("旧阶段无错误分类", st.first_student_error("hw1", "legacy") is None)

        st.set_stage("hw1", "student", "grade", "failed", "测试未通过", "student")
        st.set_stage("hw1", "student", "pull", "failed", "下载异常", "system")
        st.set_stage("hw1", "unknown", "unpack", "failed", "未分类", "unexpected")
        check("学生错误独立查询", st.first_student_error("hw1", "student") == ("grade", "测试未通过"))
        check("系统错误独立查询", st.first_system_error("hw1", "student") == ("pull", "下载异常"))
        check("未知分类归为空", st.first_failed_stage("hw1", "unknown") == ("unpack", "未分类") and
              st.first_student_error("hw1", "unknown") is None and
              st.first_system_error("hw1", "unknown") is None)

        st.set_stage("hw1", "student", "unpack", "failed", "更早的提交问题", "student")
        check("分类查询遵循流水线顺序",
              st.first_student_error("hw1", "student") == ("unpack", "更早的提交问题"))


def test_cli_stops_at_report() -> None:
    print("[cli] 一键流程在报告阶段结束")
    import gc_auto_ta.cli as cli

    calls: list[str] = []
    old_load_cfg = cli._load_cfg
    old_commands = {name: getattr(cli, name) for name in
                    ("cmd_pull", "cmd_unpack", "cmd_provision", "cmd_grade",
                     "cmd_report", "cmd_writeback")}

    class FakeCfg:
        def describe(self) -> str:
            return "fake config"

    def fake_step(name: str):
        def run(_args) -> int:
            calls.append(name)
            return 0
        return run

    try:
        cli._load_cfg = lambda _args: FakeCfg()
        for name in old_commands:
            setattr(cli, name, fake_step(name.removeprefix("cmd_")))
        args = SimpleNamespace(config="config.yaml", verbose=False, only=None,
                               limit=None, dry_run=False, no_bulk=False,
                               live=False,
                               stage=None, org=None, course=None, assignment=None,
                               output=None, force=False, stages=None)
        cli.cmd_run(args)
    finally:
        cli._load_cfg = old_load_cfg
        for name, fn in old_commands.items():
            setattr(cli, name, fn)

    check("run 只执行到 report",
          calls == ["pull", "unpack", "provision", "grade", "report"], str(calls))


def test_cli_run_rejects_live_flag() -> None:
    print("[cli] run 不再接受 Canvas 写回参数")
    import gc_auto_ta.cli as cli

    try:
        cli.build_parser().parse_args(["run", "--live"])
    except SystemExit as exc:
        check("run --live 被拒绝", exc.code == 2, str(exc.code))
    else:
        check("run --live 被拒绝", False, "参数仍被接受")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="gc-auto-ta-smoke-") as tmpdir:
        tmp = Path(tmpdir)
        cfg = test_config(tmp)
        test_naming()
        test_reset_submissions(tmp)
        test_reset_unpacked(tmp)
        test_unpack(tmp)
        test_unpack_is_idempotent(tmp)
        test_grading_parse()
        test_provision_cleans_readonly_workspace(tmp)
        test_grade_skips_provision_failures(tmp)
        test_gitea_issue_filter()
        test_gitea_repo_cleanup()
        test_writeback(cfg)
        test_writeback_provision_failure_gets_zero(cfg, tmp)
        test_writeback_provision_failure_dry_does_not_submit(cfg, tmp)
        test_writeback_any_prior_failure_gets_zero(cfg, tmp)
        test_writeback_system_error_is_report_only(cfg, tmp)
        test_provision_skips_prior_stage_failure(cfg, tmp)
        test_unpack_marks_extract_error(cfg, tmp)
        test_state(tmp, cfg)
        test_state_error_categories(tmp)
        test_cli_stops_at_report()
        test_cli_run_rejects_live_flag()
    print()
    print(f"通过 {passed} 项，失败 {len(failed)} 项")
    for f in failed:
        print("  -", f)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
