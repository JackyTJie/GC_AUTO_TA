# 01 架构与全流程

## 一张图

```
                    config.yaml（唯一配置：token / 课程 / 作业 / 评分样例 / 回写策略）
                                        |
     ┌──────────────────────────────────┴──────────────────────────────────┐
     │                                                                      │
  [pull]              [unpack]            [provision]        [grade]      [report/writeback]
Canvas API ──► 附件/正文落盘 ──► 解包+归集源码 ──► Gitea 建仓+推送 ──► Actions 评测 ──► 收分 ──► Canvas 回写
   |                    |                     |                   |              |
   └─ submissions/      └─ unpacked/, code/   └─ workspaces/      └─ tasks API    └─ reports/ + state.sqlite3
```

数据流最后都落进 `work/<作业>/manifest.json` 与 `state/state.sqlite3`，它们是后续阶段的唯一事实来源。

## 六个阶段

### 1. pull —— 从 Canvas 拉提交

* 入口：`CanvasGateway.submissions()`（`GET /api/v1/courses/:cid/assignments/:aid/submissions`，
  `include[]=user,submission_comments,submission_history,rubric_assessment`）
* 每个学生一个目录：`work/<作业>/submissions/<学生标识>/`
  * `online_upload` → 附件按原名下载（`File.download()`，走 Canvas 的签名 URL）
  * `online_text_entry` → `solution.txt`（HTML 清洗成纯文本）
  * `online_url` → `url.txt`
  * quiz / media / LTI → 记 `note`，不落文件（这类内容 API 拿不到）
* 同时写 `_canvas.json`（提交时间、attempt、late、当前分数等，便于回写时做对照）
* 写 `manifest.json` + `state.students`，并记 `pull` 阶段状态

学生标识（`student_key`）取 `login_id`（学号），退而求其次 `sis_user_id`，最后 `user<id>`；
它同时用于仓库名与报告，全程稳定不变。

### 2. unpack —— 解包与归集

* 压缩包：`zip / tar.*` 用标准库；`rar / 7z` 走 `bsdtar`（优先）或 `7z` / `unrar`
* 安全：逐条做路径穿越检查（拒绝 `../`、绝对路径），可选解压体积上限（默认 2 GiB 防压缩炸弹）
* 形态处理：
  1. 直接传源码文件 → 直接收集
  2. 传压缩包 → 解到 `unpacked/<学生>/<包名>/`
  3. 压缩包里只有一层目录（`作业1/`） → 自动上提
  4. **全班一个大包** → 若解出来的顶层目录能唯一对应到多个学生，按名单拆分到各自目录
* 归集：按 `unpack.student_file_globs` 递归挑选源码，拷进 `work/<作业>/code/<学生>/`
  （provision 只搬这一层，保证仓库内容干净）
* 匹配不到 / 同时像多人的条目写进 `manifest.unmatched` / `manifest.ambiguous`，**不自动归属**

### 3. provision —— 建仓并推送

* 幂等建仓：`POST /api/v1/orgs/:org/repos`（已存在则复用）
* 组装工作区：评分文件（`grading.files` 或 `files_dir`）+ 学生源码 + 渲染好的 workflow
* 推送：`git init → commit → push`，认证用 `GIT_ASKPASS` 脚本（token 不出现于命令行/日志/仓库）
* 首次推送后把仓库默认分支设为 `main`（空仓库默认可能是 `master`）
* 记录 `runs`（commit sha）供后续对账

仓库名 = `<repo_prefix>-<student_key>`，例如 `hw1-2026000001`。

### 4. grade —— 触发、等待、收分

* 触发 = push（Gitea Actions 监听 `push`）；重跑 = 空提交（`gc_auto_ta rerun`）
* 等待：轮询 `GET /repos/{owner}/{repo}/actions/tasks` 直到状态属于
  `success / failure / cancelled / error / skipped`
* 收分三通道（由 `grading.publish` 决定）：
  | publish | workflow 行为 | 本工具读取方式 |
  |---|---|---|
  | `file` | 把 `joj3_result.json` 复制成 `score-<sha7>.json` 并 push 回仓库 | `GET /repos/.../contents/score-<sha7>.json` |
  | `issue` | 把结果 JSON 发成 issue（title `Grading result <sha7>`） | `GET /repos/.../issues` 里按 sha 匹配后解析 |
  | `none` | 什么都不回传 | 读仓库里的 `joj3_result.json`（要求 workflow 自己提交它）或本地 clone |

  `score-<sha7>.json` 会被自动重新评测流程排除（`[skip ci]`），不会造成重跑风暴。

### 5. report —— 汇总

* `reports/<作业>/results.json`：状态库全量快照（含每阶段明细、回写记录）
* `reports/<作业>/results.csv`：Excel/Canvas 导入友好（UTF-8-BOM）
* `reports/<作业>/problems.md`：没拿到分数的学生清单（人工处理入口）
* 终端表格 + 汇总统计（人数 / 有分数 / 缺分数 / 0 分 / 已回写 / 平均分）

### 6. writeback —— 回写 Canvas

* 分数换算（`points | percent | raw`）、迟交扣分、评语模板、长度截断（见 docs/06）
* 单人：`PUT /courses/:cid/assignments/:aid/submissions/:uid`
* 批量：`POST /courses/:cid/assignments/:aid/submissions/update_grades`（异步 job，轮询 Progress）
* 默认 `dry`：只打印将写入的内容；`--live` 才真写
* 每条回写记录进 `state.writebacks`（含写入前的原始分，便于对账/回滚参考）

## 状态与幂等

状态库 `state/state.sqlite3` 五张表：

| 表 | 用途 |
|---|---|
| `students` | 学生 → Canvas user / 仓库 / 代码目录 |
| `stages` | `(作业, 学生, 阶段)` 的状态：done / failed / skipped / dry-run |
| `runs` | 每次推送（commit sha）与对应的 Actions 任务 |
| `scores` | 收集到的分数、评语、各 stage 明细、来源 |
| `writebacks` | 回写记录（分数、模式、时间、原值） |

由此得到：

* 任何阶段中断，重跑只会补齐没做完的；
* `reset --stage <阶段>` 可强制重跑某阶段（全部或指定学生）；
* `status` 一眼看到每个阶段的成功/失败计数。

## 边界与失败语义

| 情况 | 行为 |
|---|---|
| 学生未提交 | `pull` 记 `note=未提交`，`grade` 无分数；`writeback` 按 `on_missing_submission` 决定跳过或记 0 |
| 提交类型不支持 | 记 `note`，不阻断其他学生 |
| 解包失败 | 保留原压缩包并告警；该学生 `unpack` 标 `skipped` |
| 匹配歧义 | 进 `manifest.ambiguous`，不归属（用 `roster` 覆盖） |
| 仓库已存在 | 复用，继续推送新提交 |
| runner 缺失 | workflow 静默等待、无任务记录 → `grade` 超时；见 docs/07 排错表 |
| 回写 403 | Canvas 权限不足（TA 角色没有 manage grades）→ 该学生标 failed，其余继续 |
