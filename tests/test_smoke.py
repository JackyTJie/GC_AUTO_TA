#!/usr/bin/env python3
"""离线冒烟测试：不碰 Canvas / Gitea，只验证纯逻辑。

    python3 tests/test_smoke.py

覆盖：配置解析（含 ${ENV} 展开与评分文件内联）→ workflow 渲染 →
名单匹配（含"不猜"规则）→ 解包（zip / 套一层目录 / 全班级大包）→
joj3_result 解析 → 分数换算与迟交扣分 → 状态库读写。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import zipfile
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


def test_validate() -> None:
    print("[validate] JOJ3 配置结构")
    from gc_auto_ta.validate import validate_grading_files

    def conf(stage: dict) -> dict[str, str]:
        return {"joj/lab3/conf.json": json.dumps(
            {"name": "lab3", "stages": [stage]}, ensure_ascii=False)}

    def stage(**executor_extra) -> dict:
        executor = {"name": "sandbox", "with": {"default": {
            "args": ["gcc", "-o", "t1", "Lab3Task1.c"],
            "env": ["PATH=/usr/bin:/bin:/usr/local/bin"],
        }}}
        executor.update(executor_extra)
        return {"name": "build", "executor": executor}

    check("合规配置通过", validate_grading_files(conf(stage())) == [])

    # 本次 lab3 事故的原始形状：命令写在 executor 顶层（被 JOJ3 忽略），
    # with.default.args 只剩一个 PATH=... 环境变量
    broken = stage(args=["bash", "-c", "gcc -o t1 Lab3Task1.c"])
    broken["executor"]["with"]["default"]["args"] = ["PATH=/usr/bin:/bin:/usr/local/bin"]
    problems = validate_grading_files(conf(broken))
    check("executor 顶层 args 被报出",
          any("executor 只允许" in p and "'args'" in p for p in problems), str(problems))
    check("PATH=... 当命令被报出",
          any("env" in p and "可执行文件" in p for p in problems), str(problems))

    empty = stage()
    empty["executor"]["with"]["default"]["args"] = []
    check("空 args 被报出",
          any("非空字符串列表" in p for p in validate_grading_files(conf(empty))))

    cases = stage()
    cases["executor"]["with"]["cases"] = [{"args": ["PATH=/bin", "./t1"]}]
    check("cases 覆盖里的 env 写法被报出",
          any("cases[0].args[0]" in p for p in validate_grading_files(conf(cases))))

    bad_json = {"joj/conf.json": "{not json"}
    check("非法 JSON 被报出",
          any("不是合法 JSON" in p for p in validate_grading_files(bad_json)))

    ignored = {"joj/notes.json": "{not json", "joj/conf.json": json.dumps(
        {"stages": [{"name": "s", "executor": {"name": "sandbox", "with": {
            "default": {"args": ["/bin/true"]}}}}]})}
    check("非 conf 的 json 不参与校验", validate_grading_files(ignored) == [])


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


def test_unpack(tmp: Path) -> None:
    print("[unpack] zip / 套一层目录 / 全班级大包")
    from gc_auto_ta.unpack import extract, gather_source_files, split_bundle

    z1 = tmp / "single.zip"
    with zipfile.ZipFile(z1, "w") as zf:
        zf.writestr("mywork/full_adder.m", "function [s,c]=full_adder(a,b,cin)\nend\n")
        zf.writestr("__MACOSX/junk", "x")
    r1 = extract(z1, tmp / "out1")
    check("解包成功", r1.ok, r1.error)
    check("单根目录自动上提", (tmp / "out1" / "full_adder.m").exists())
    check("垃圾目录被清理", not (tmp / "out1" / "__MACOSX").exists())

    z2 = tmp / "bundle.zip"
    with zipfile.ZipFile(z2, "w") as zf:
        zf.writestr("2026000001/sol.m", "a=1;\n")
        zf.writestr("2026000002/sol.m", "a=2;\n")
    r2 = extract(z2, tmp / "out2")
    entries = split_bundle(tmp / "out2", tmp / "out2")
    check("大包拆出两个条目", set(entries) == {"2026000001", "2026000002"}, str(set(entries)))
    files = gather_source_files(tmp / "out1", ["*.m"])
    check("按 glob 收集源码", len(files) == 1, str(files))

    evil = tmp / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../../etc/passwd", "x")
    extract(evil, tmp / "out3")
    check("拒绝路径穿越", not (tmp / "etc").exists() and r2.ok)


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


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="gc-auto-ta-smoke-") as tmpdir:
        tmp = Path(tmpdir)
        cfg = test_config(tmp)
        test_validate()
        test_naming()
        test_unpack(tmp)
        test_grading_parse()
        test_writeback(cfg)
        test_state(tmp, cfg)
    print()
    print(f"通过 {passed} 项，失败 {len(failed)} 项")
    for f in failed:
        print("  -", f)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
