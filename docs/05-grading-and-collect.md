# 05 评测触发、等待与收分

```bash
python3 -m gc_auto_ta grade -c config.yaml              # 等任务 + 收分
python3 -m gc_auto_ta grade -c config.yaml --no-wait    # 只尝试读结果（适合补跑）
python3 -m gc_auto_ta rerun -c config.yaml --only 2026000001   # 空提交重跑
```

## 触发方式：只有 push

Gitea 1.23 的 API **没有** workflow dispatch、没有 runs 列表、没有运行日志接口
（已在本机 `/swagger.v1.json` 核对：只有 `/repos/{owner}/{repo}/actions/tasks` 与
`/actions/{runners,secrets,variables}`）。所以：

| 场景 | 做法 |
|---|---|
| 首次评测 | `provision` 推送即触发 |
| 同一份代码重跑 | `rerun`：`git commit --allow-empty -m "chore: rerun ... [skip ci]" && push` |
| 改了评分文件 | 改 `config.yaml` 的 `grading.files`，重跑 `provision`，再 `rerun` 受影响的学生 |
| 手动触发按钮 | 模板里已带 `workflow_dispatch`，可在仓库 Actions 页面点「Run workflow」 |

## 等待：tasks 轮询

`GET /api/v1/repos/{owner}/{repo}/actions/tasks` 返回任务数组（字段：`id, name, head_sha, head_branch,
run_number, event, display_title, status, workflow_id, created_at, updated_at, url`）。

程序按 `head_sha` 前缀匹配我们推送的 commit，取 `run_number` 最大的那条，轮询到状态落在
`success / failure / failed / cancelled / error / skipped` 为止；超时（`limits.grading_timeout`）
则记 `failed` 但**不阻断**其他学生。

## 收分三通道

| `grading.publish` | workflow 行为 | 本工具读取 | 适用 |
|---|---|---|---|
| `file` | 把 `joj3_result.json` 复制成 `score-<sha7>.json`，commit（`[skip ci]`）后 push 回仓库 | `GET /repos/{org}/{repo}/contents/score-<sha7>.json` → base64 解码 | 私有仓库/要留痕（推荐） |
| `issue` | 结果 JSON 发成 issue，标题 `Grading result <sha7>`，正文含 commit 与 JSON 代码块 | `GET /repos/{org}/{repo}/issues?state=all`，按标题+sha 匹配，从正文抠出 JSON | 公开仓库也想脚本化收分（需 `write:issue` 的 secret） |
| `none` | 不回传 | 读仓库里的 `joj3_result.json`（要求 workflow 自己把它 commit 回去），否则失败 | 只做人工检查、或自己扩展 workflow |

文件名必须在两处一致：`config.yaml` 的 `grading.score_file_pattern`（默认 `score-{sha7}.json`）
与 workflow 模板里的 `SCORE_FILE="score-${GITHUB_SHA:0:7}.json"`。改一处要改另一处。

## joj3_result.json 的解析

格式（JOJ3 指南第 1 章）：

```json
[
  { "name": "run",
    "results": [ { "score": 100, "comment": "Passed!" } ],
    "force_quit": false }
]
```

解析规则（`grading.parse_joj3_result`）：

* 总分 = 各 stage 内各 result 的 `score` 之和
* 评语 = 各 result 的 `comment` 去空、去重后按顺序拼接（分隔符「；」）
* 每个 stage 的明细单独留存（进 `state.scores.stages_json`，报告里可见）
* `force_quit: true` 表示该 stage 失败后续被跳过（例如「编译不过就直接 0 分」），
  这会让总分变低——这是设计使然，不是 bug

## 与 Canvas 的分数对应

JOJ3 的满分由 `grading.max_score` 声明（默认 100），回写时按 `writeback.grade_mode` 换算：

```
points :  canvas_score = joj3_score / max_score * assignment.points_possible
percent:  canvas_score = "joj3_score / max_score * 100 %"
raw    :  canvas_score = joj3_score          （要求两边满分一致）
```

## 排错：常见「没有分数」

| 现象 | 原因 | 处理 |
|---|---|---|
| 任务一直 `waiting`，tasks 里啥都没有 | 该组织没有 runner | 让管理员给该组织注册 runner（提供：组织名、标签 `golang-ubuntu-latest`、并发数） |
| 任务 `failure`，`joj3_result.json` 里 `Internal Error` + `Time: 0 ms` | `conf.json` 缺必填字段（`procLimit` / `cpuLimit` / `clockLimit` / `memoryLimit` / `stdin`） | 补齐（JOJ3 指南第 3 章字段表） |
| 沙箱里找不到文件 | `conf.json` 里写的是容器路径而不是沙箱路径 | 用 `copyInDir: "."` + 仓库相对路径（如 `joj/run_tests.m`） |
| 分数 0 但代码看着对 | 输出内容真的不同；或比较的文件名不在 `copyOut` 里（此时取到空串） | 先本地跑一遍测试脚本对比；多组数据/严格比较（含空白）的写法见 `docs/09-multiple-test-cases.md` |
| `could not read Username` / `401` | 私有组织或私有仓库下匿名 clone | 换 `publish: file`（用 secret 模板）或改为公开组织+公开仓库 |
| `remote: Unauthorized`（private 用公网地址 clone） | 用了 `http://<域名>`，https 跳转丢了凭据 | 容器内用 `gitea.internal_url`（本机即 `http://localhost:3000`） |
| `curl: command not found` | job 镜像没有 curl | 用 `python3` + `urllib.request`（模板已如此） |

## 重跑的正确姿势

```bash
# 1) 改了评分文件：
#    改 config.yaml 的 grading.files → 重新 provision（评分文件进仓库）→ rerun
python3 -m gc_auto_ta provision -c config.yaml --only 2026000001
python3 -m gc_auto_ta rerun     -c config.yaml --only 2026000001
python3 -m gc_auto_ta grade     -c config.yaml --only 2026000001

# 2) 只想重算分数、不重跑评测（比如回写策略改了）：清掉 grade 阶段的状态再收一次
python3 -m gc_auto_ta reset -c config.yaml --stage grade --only 2026000001
python3 -m gc_auto_ta grade -c config.yaml --only 2026000001
```
