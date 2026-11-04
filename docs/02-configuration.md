# 02 配置详解（config.yaml）

一个文件装下整条流水线。字段按段落划分，下面逐段说明；完整模板见 `config.example.yaml`。

生成方式：

```bash
python3 -m gc_auto_ta new-config -o config.yaml     # 自动 chmod 600
```

## 取值优先级

```
内置默认值  <  config.yaml  <  环境变量（${VAR} 展开）  <  CLI 参数（--org/--course/--assignment/--live）
```

`${VAR}` 与 `${VAR:-默认值}` 都会展开（递归支持 dict/list 里的字符串）。
项目根目录存在 `.env` 时会先加载（不覆盖已有环境变量）。

## 1. canvas

| 字段 | 必填 | 说明 |
|---|---|---|
| `base_url` | ✅ | 站点地址，如 `https://oc.sjtu.edu.cn` |
| `token` | ✅ | Canvas API token；写 `"${CANVAS_API_TOKEN}"` 可从环境变量取 |
| `course_id` | ✅ | 课程 ID（Canvas 课程 URL 里的数字） |
| `assignment_id` | ✅ | 作业 ID（作业页面 URL 里的数字） |
| `min_interval` | | 每次请求最小间隔（秒），默认 0.1，批量下载时靠它避免限流 |

Token 生成：Canvas → 头像 → Settings → **Approved Integrations** → `+ New Access Token`。
手动生成的 token 权限 = 你账号的权限，不缩权、也不能超权。

**TA 常见前提**：角色需要有「管理成绩（manage grades / Grades - edit）」权限才能回写；
若你的 TA 身份被限定在某个 section，`get_submissions()` 只会返回该 section 的学生（不报错，静默少人），
`doctor` 会把提交数打出来，请和 SpeedGrader 的人数对一下。

## 2. gitea

| 字段 | 必填 | 说明 |
|---|---|---|
| `url` | ✅ | 站点地址（本机部署即 `http://localhost:3000`） |
| `internal_url` | | **job 容器内**访问 Gitea 的地址；本机部署同样是 `http://localhost:3000`（runner 用 host 网络） |
| `token` | ✅ | PAT，scope：`write:repository` + `write:organization` + `read:user`（要开 issue 再加 `write:issue`） |
| `owner` | ✅ | PAT 所属用户名。私有仓库 clone 时作为 basic auth 用户名写进 workflow |
| `org` | ✅ | 仓库建在哪个组织。**该组织必须有 runner**，否则 workflow 静默等待 |
| `delete_repos_before_setup` | `false` | provision 开始前删除该组织内全部仓库；危险操作 |
| `min_interval` | | 请求最小间隔，默认 0.05 |

PAT 生成：Gitea → 头像 → Settings → Applications → Generate New Token。只放本机（配置文件或环境变量）。

> 私有组织/私有仓库时，workflow 里的 clone 也必须带凭据 → 需要组织级 secret（见 docs/04）。

## 3. assignment

| 字段 | 默认 | 说明 |
|---|---|---|
| `name` | `hw1` | 作业标识：用于目录名、提交信息、报告名 |
| `repo_prefix` | = name | 仓库名 = `<前缀>-<学生标识>` |
| `repo_private` | `false` | 公开仓库 + 公开组织 = workflow 可匿名 clone，最省事 |
| `default_branch` | `main` | 推送分支；首推后自动设为仓库默认分支 |
| `description` | 自动 | 仓库描述 |

## 4. grading（评分样例）

| 字段 | 默认 | 说明 |
|---|---|---|
| `layout` | `in_repo` | `in_repo`：评分文件放学生仓库（学生无写权限）<br>`config_repo`：评分文件放 TA 私有仓库，学生完全看不到（scheme C） |
| `publish` | `issue` | `none` / `file` / `issue`，见 docs/05 |
| `conf_root` | `joj` | 容器内传给 `joj3 -conf-root` 的相对路径 |
| `conf_name` | `conf.json` | `joj3 -conf-name` |
| `max_score` | `100` | JOJ3 侧满分（各 parser 分数之和应等于它） |
| `score_file_pattern` | `score-{sha7}.json` | 回传文件名；必须与 workflow 模板一致 |
| `commit_prefix` | `feat` | 提交信息前缀，实际是 `feat(hw1): <学生> submission [hw1]` |
| `files` | `{}` | **内联评分文件**：`{仓库内相对路径: 内容}` |
| `files_dir` | | 也可以指向一个目录，内容原样搬进仓库（`files` 优先） |
| `workflow_template` | 自动 | 留空按 `layout + publish` 自动选（见下表） |
| `config_repo` | | `layout=config_repo` 时评分文件所在仓库名 |

### 内联 vs 目录

* 内联（`files`）适合「一个作业一个小脚本」：整份作业的所有信息都在一个 YAML 里，便于复制与回顾。
* 目录（`files_dir`）适合「评分文件很长 / 想用真编辑器 + 语法高亮」：例如
  `grading.files_dir: ./course-conf/hw1`，目录内容（含子目录）原样进入仓库。
* 内联里若把 `conf.json` 写成 YAML 映射（而不是字符串），程序会序列化成 JSON 写入仓库——两种写法都行。

### workflow 模板自动选择表

| layout | publish | 模板 | 说明 |
|---|---|---|---|
| in_repo | none | `in-repo-public` | 公开仓库，匿名 clone，分数看日志 |
| in_repo | file | `in-repo-secret` | 私有仓库/PAT clone，分数 push 回仓库 |
| in_repo | issue | `in-repo-public-issue` | 公开 clone + 结果开 issue（需 secret 调 API） |
| config_repo | none | `config-repo` | 评分文件从 TA 私有仓库拉 |
| config_repo | file | `config-repo-secret` | 拉配置 + 分数回传 |
| config_repo | issue | `config-repo-issue` | 拉配置 + 结果开 issue |

## 5. unpack

| 字段 | 默认 | 说明 |
|---|---|---|
| `student_file_globs` | 源码常见后缀 | 从解包结果里挑哪些文件当学生源码搬进仓库 |
| `remove_archives` | `false` | 解包成功后是否删除原始压缩包 |
| `max_extract_bytes` | 2 GiB | 解压后体积上限（防压缩炸弹），`0` 不限制 |
| `on_missing_submission` | `skip` | `skip`（缺交不回写）/ `zero`（按 `writeback.missing_score` 回写） |

## 6. writeback

| 字段 | 默认 | 说明 |
|---|---|---|
| `mode` | `dry` | `off` / `dry`（只打印）/ `live`（真写） |
| `grade_mode` | `points` | `points`（换算到作业满分）/ `percent`（`"87.5%"`）/ `raw` |
| `missing_score` | `0` | `on_missing_submission=zero` 时使用的原始分 |
| `comment_template` | 见模板 | 占位符：`{score} {max} {points} {comment} {stages} {student} {assignment}` |
| `comment_max_len` | `4000` | 超长截断（会追加「已截断」提示） |
| `late_penalty_per_day` | `0` | 每天扣的比例（`0.1` = 每天 10%），需要 `due_at` |
| `due_at` | `null` | 留空用 Canvas 作业自带的 due_at |
| `bulk` | `true` | 用批量接口（异步 job）；`false` 则逐条写 |
| `only_grade` | `false` | 只写分数，不写评论 |

## 7. roster（名单覆盖）

自动匹配不中或存在歧义时的人工兜底：

```yaml
roster:
  "2026000001":
    repo: hw1-2026000001            # 覆盖仓库名
    name: 示例学生
    aliases: [示例学生, Sample Student, demo] # 参与匹配的别名
  "2026000002":
    canvas_user_id: 12345
```

键是 `student_key`（学号）。程序**永远不会**在没有把握时自行归属；`ambiguous` 只能靠这里解决。

## 8. paths / limits

| 字段 | 默认 | 说明 |
|---|---|---|
| `paths.work_dir` | `work` | 中间产物根目录 |
| `paths.state_db` | `state/state.sqlite3` | 状态库 |
| `paths.report_dir` | `reports` | 报告输出（实际写 `<report_dir>/<作业>/`） |
| `limits.grading_poll_interval` | `10` | 轮询 Actions 任务间隔（秒） |
| `limits.grading_timeout` | `1800` | 单仓库等待评测最长时间（秒） |
| `limits.attachment_download_timeout` | `120` | 单个 Canvas 附件下载 HTTP 超时时间（秒） |
| `limits.concurrency` | `4` | 预留（当前实现顺序执行，避免压垮 runner） |

## 密钥安全建议

1. 首选 `${CANVAS_API_TOKEN}` / `${GITEA_TOKEN}` 展开，真值放 shell 或 `.env`（已在 `.gitignore`）。
2. 直接把 token 写进 `config.yaml` 也可以，但请：文件权限 600（`new-config` 自动设置）、
   绝不提交到 git（`config.yaml` 已在 `.gitignore`）、定期轮换。
3. 日志、报告、state 里都不会出现 token；需要排查时用 `-v` 看 DEBUG 日志也不会打印密钥。
