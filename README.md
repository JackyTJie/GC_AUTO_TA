# GC_AUTO_TA

把「Canvas 上收的作业」自动变成「Canvas 上的分数与评语」的一条流水线：

```
Canvas 提交 ──► 解包/归集 ──► Gitea 建仓+推送 ──► JOJ3 评测 ──► 收分汇总 ──► Canvas 回写
   (pull)        (unpack)        (provision)        (grade)      (report)     (writeback)
```

* **一个配置文件** 装下全部：两个 token、课程/作业 ID、评分样例（conf.json / 测试脚本 / 期望输出）、
  解包规则、回写策略、名单覆盖。
* **一条命令** 跑完全程：`python -m gc_auto_ta run -c config.yaml`
  （回写默认 `dry`，只打印不写；确认无误再加 `--live`）。
* **三处不再手抖**：建仓命名、评分文件灌入、分数回填，全部由状态库保证幂等与断点续跑。
* **人不下场不猜**：压缩包匹配不到学生、或同时像多个学生时，只记录在 `unmatched / ambiguous`，
  绝不自动归因（宁可在展示层分裂，不可把分数错挂）。

## 依赖

```bash
python3 -m pip install -r requirements.txt     # canvasapi / PyYAML / requests
sudo apt install libarchive-tools p7zip-full   # 可选：解 rar/7z 压缩包（zip/tar 用标准库即可）
```

## 三步跑通

```bash
# 1) 生成配置（权限自动设为 600）
python3 -m gc_auto_ta new-config -o config.yaml
#    然后填：canvas.token / canvas.course_id / canvas.assignment_id
#            gitea.token / gitea.org / gitea.owner
#            grading.files（评分样例）—— 模板里已给了一份可直接用的 Octave 例子

# 2) 自检（只读：不建仓、不打分、不回写）
python3 -m gc_auto_ta doctor -c config.yaml

# 3) 全流程（回写 dry）；先 --limit 2 拿两个学生试跑
python3 -m gc_auto_ta run -c config.yaml --limit 2
python3 -m gc_auto_ta run -c config.yaml
python3 -m gc_auto_ta writeback -c config.yaml --live    # 看完成绩报告再真正写 Canvas
```

## 命令速查

| 命令 | 作用 | 主要参数 |
|---|---|---|
| `new-config` | 复制配置模板 | `-o config.yaml --force` |
| `doctor` | 全链路自检（只读） | `-c` |
| `pull` | 拉 Canvas 提交并下载附件/正文 | `--limit N --only 学号1,学号2` |
| `unpack` | 解包（zip/rar/7z/tar）、归集源码 | `-c` |
| `provision` | 建仓 + 灌评分文件 + 推代码 | `--dry-run --limit --only` |
| `grade` | 等 Actions 任务 + 收 joj3 结果 | `--no-wait --only` |
| `report` | 汇总 `results.json/csv` + 终端表格 | `--limit 行数` |
| `writeback` | 回写 Canvas 分数与评论 | `--live --no-bulk --only` |
| `rerun` | 空提交触发重跑 | `--only` |
| `status` | 配置摘要 + 状态库统计 | `-c` |
| `reset` | 清某阶段状态强制重跑 | `--stage grade --only` |
| `run` | 全流程一把梭 | `--live --dry-run --limit --only --stages ...` |

全部子命令都支持 `--org / --course / --assignment` 临时覆盖配置里的组织与 Canvas ID。

## 目录结构

```
GC_AUTO_TA/
├── config.example.yaml      配置模板（含可直接用的 Octave 评分样例）
├── gc_auto_ta/              代码
│   ├── config.py            单文件配置解析（含 ${ENV} 展开与校验）
│   ├── state.py             SQLite 状态库（幂等 / 断点续跑 / 对账）
│   ├── naming.py            学生身份 → 仓库名 → 压缩包条目 的匹配（不猜）
│   ├── canvas_api.py        Canvas 读提交 / 下载 / 回写
│   ├── unpack.py            解包与源码归集
│   ├── gitea_api.py         Gitea REST 客户端（对齐 1.23.6 swagger）
│   ├── workflow.py          workflow 模板渲染
│   ├── provision.py         组装仓库工作区 + PAT 推送（GIT_ASKPASS，token 不进 argv）
│   ├── grading.py           触发/等待/收集 joj3 结果
│   ├── report.py            JSON/CSV/Markdown 汇总
│   ├── writeback.py         分数换算 + 评语生成 + 回写
│   ├── doctor.py            自检
│   └── cli.py               命令行
├── templates/workflow/      6 套 workflow 模板（公开/私有/评分文件外置 × 回传方式）
├── docs/                    使用文档（01 架构 … 08 安全合规）
├── work/                    中间产物（gitignore）：submissions / unpacked / code / workspaces
├── state/state.sqlite3      状态库（gitignore）
└── reports/<作业>/          结果报告（gitignore）
```

## 设计原则

1. **幂等**：每一阶段按 `(作业, 学生)` 记录状态；重复运行只会补齐没做完的部分。
2. **默认 dry**：回写 Canvas 必须显式 `--live`；`provision` 支持 `--dry-run`。
3. **密钥不进仓库、不进 argv、不进日志**：token 只从配置/环境变量读，git 认证走 `GIT_ASKPASS` 脚本，
   日志与报告里的 token 会被抹成 `***`。
4. **不猜**：匹配不确定只报告，不归属。
5. **留痕**：所有 Canvas 打分都记在你（token 所属账号）名下——这是好事，出问题能追溯。

## 已知边界（先看清楚再上生产）

* Gitea 1.23 **没有** workflow dispatch / runs 列表 / 运行日志接口：触发只能 push（重跑用空提交），
  状态只能靠 `GET /repos/{owner}/{repo}/actions/tasks`。所以「收分」依赖 workflow 主动把结果回传
  （`publish: file` 或 `issue`）。
* Canvas 的 quiz、media_recording、LTI（CodeGrade 等）提交拿不到内容，会记 `note` 并跳过。
* 匿名批改（Anonymous Grading）作业的回写走 `anonymous_submissions` 接口，属尽力而为。
* 每个组织必须有独立 runner（由运维为该组织注册 runner）；没有 runner 时 workflow
  会静默等待，连任务记录都不产生。
* 学生若自己改了 `joj/` 下的评分文件：本工具用「TA 代传、学生无写权限」模式，因此天然安全；
  若改用 students-self-serve 模式，请按 JOJ3 指南第 5 章做分支保护。

## 文档索引

| 文件 | 内容 |
|---|---|
| [docs/01-architecture.md](docs/01-architecture.md) | 全流程、各阶段输入输出、状态与幂等 |
| [docs/02-configuration.md](docs/02-configuration.md) | config.yaml 逐字段说明、token 安全、模板选择 |
| [docs/03-canvas-pull-and-unpack.md](docs/03-canvas-pull-and-unpack.md) | 提交类型、解包形态、名单匹配与人工兜底 |
| [docs/04-gitea-provision.md](docs/04-gitea-provision.md) | 建仓、评分文件三种布局、workflow 模板、org secret |
| [docs/05-grading-and-collect.md](docs/05-grading-and-collect.md) | 触发/重跑/轮询/收分三通道、结果格式解析 |
| [docs/06-canvas-writeback.md](docs/06-canvas-writeback.md) | 分数换算、迟交扣分、评语模板、批量与匿名 |
| [docs/07-operations.md](docs/07-operations.md) | 运维手册：命令详解、续跑、排错表、限流 |
| [docs/08-security-and-compliance.md](docs/08-security-and-compliance.md) | 密钥、数据边界、留痕与合规 |
| [docs/09-multiple-test-cases.md](docs/09-multiple-test-cases.md) | **多个 test case**：executor/parser 两层 cases、分数分配、compareSpace 语义、多 stage 编译+运行 |

## 版本与来源

* 面向 JOJ3 服务（本机 Gitea 1.23.6 + act_runner + go-judge 沙箱），
  接口与坑均对齐 `https://sjtu.tangjie.xyz/JOJ/JOJ3-Guide`（TA 借用 JOJ3 的官方指南）。
* Canvas 侧基于 canvasapi 3.6 与其背后的 Canvas REST API（Submissions / Files）。
* 本仓库代码**未经端到端实跑验证**：交付的是完整框架与文档；上线前请按 `docs/07` 的
  「第一次上生产」清单逐项验证（先 `doctor`，再 `--limit 2` 试跑）。
