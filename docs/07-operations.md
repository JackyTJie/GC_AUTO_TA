# 07 运维手册

## 命令详解

### 全局参数

| 参数 | 说明 |
|---|---|
| `-c, --config PATH` | 配置文件（默认 `./config.yaml`） |
| `-v, --verbose` | DEBUG 日志 |
| `--org NAME` | 临时覆盖 `gitea.org` |
| `--course N` / `--assignment N` | 临时覆盖 Canvas 课程/作业 ID |

### 各子命令

```bash
# 生成配置（权限 600）
python3 -m gc_auto_ta new-config -o config.yaml [--force]

# 自检：配置 / Canvas / Gitea / 本机依赖 全查一遍（只读）
python3 -m gc_auto_ta doctor -c config.yaml

# 阶段命令
python3 -m gc_auto_ta pull      -c config.yaml [--limit N] [--only k1,k2]
python3 -m gc_auto_ta unpack    -c config.yaml
python3 -m gc_auto_ta provision -c config.yaml [--dry-run] [--limit N] [--only ...]
python3 -m gc_auto_ta grade     -c config.yaml [--no-wait] [--only ...]
python3 -m gc_auto_ta report    -c config.yaml [--limit 行数]
python3 -m gc_auto_ta writeback -c config.yaml [--live] [--no-bulk] [--only ...]

# 辅助
python3 -m gc_auto_ta rerun  -c config.yaml [--only ...]        # 空提交重跑
python3 -m gc_auto_ta status -c config.yaml                     # 配置摘要 + 状态统计
python3 -m gc_auto_ta reset  -c config.yaml --stage grade --only k1   # 清阶段状态强制重跑

# 一把梭
python3 -m gc_auto_ta run -c config.yaml [--live] [--dry-run] [--limit N] [--only ...] \
        [--stages pull unpack provision grade report writeback]
```

## 第一次上生产：按这个顺序做

1. `new-config` → 填好 token / course_id / assignment_id / 评分样例
2. `doctor` → 全绿或只剩可忽略的 ⚠️；**提交数要和 SpeedGrader 对一遍**
3. `pull --limit 2` → 看 `work/<作业>/submissions/` 里是不是真拿到了文件
4. `unpack` → 看 `work/<作业>/code/` 里是不是学生源码（不是一堆 README/图片）
5. `provision --dry-run` → 看会建哪些仓库、推什么
6. `provision --limit 2` → 到 Actions 页面看那两个仓库真的跑起来了
7. `grade --only <那两个人>` → 拿到分数
8. `report` → 分数说得通吗？0 分了是什么原因？
9. `writeback`（dry）→ 看将写入的分数与评语
10. `writeback --live --only <那两个人>` → 去 Canvas 上核对
11. 全量：`run -c config.yaml`（回写仍 dry）→ `report` 核对 → `writeback --live`
12. 收尾：`status`，把 `state/` 与 `reports/` 备份留档

## 断点续跑

* 任何阶段中断都可以直接重跑该阶段：状态库会告诉程序哪些学生已经完成
* `reset --stage <阶段> [--only ...]` 用来强制重算（例如评分文件改了、回写策略改了）
* `pull` 会跳过已下载的附件（`download` 里判断文件已存在）——重跑不会重复下载
* `provision` 会复用已有仓库（不会删仓库），每次推送是一个新 commit

## 排错表

| 症状 | 原因 | 处理 |
|---|---|---|
| `doctor` 报 Canvas token 无效 | token 过期 / 复制不全 | 重新生成 token，注意 Canvas 只显示一次 |
| Canvas 返回的提交数比 SpeedGrader 少 | TA 被限定在 section | 用 section 视角核对；只能批改自己 section 的学生 |
| 回写 403 | TA 角色没有「管理成绩」权限 | 让课程管理员开权限（无法绕过） |
| Gitea 401 / `could not read Username` | 私有组织下匿名 clone；或 PAT 权限不足 | 见 docs/04「私有仓库」方案 B（org secret） |
| 建仓 403 | PAT 缺 `write:organization` | 重新生成 PAT 并勾上 |
| Actions 一直 waiting、任务列表为空 | 该组织没有 runner | 让运维为该组织注册 runner；Gitea 1.23 不支持实例级 runner（必须一个组织一个 runner） |
| `Executor Status: Internal Error` | `conf.json` 缺 `procLimit/cpuLimit/clockLimit/memoryLimit/stdin` | 补齐字段（JOJ3 指南第 3 章） |
| 沙箱找不到 `joj/run_tests.m` | `conf.json` 用了容器路径 | `copyInDir: "."` + 仓库相对路径 |
| 全班 0 分 | 期望文件与学生输出真的不同；或 `copyOut` 未包含被比较的文件名；或 `max_score` 与 conf.json 分数之和不一致 | 本地先跑一遍 `octave-cli joj/run_tests.m` 对比；多组数据见 `docs/09-multiple-test-cases.md` |
| 收分时 `看不到 score-*.json` | workflow 没 push 回分数（缺 secret / 模板不匹配） | 用 `publish: file` 的 secret 模板；确认 `score_file_pattern` 与模板一致 |
| `writeback` 显示 0 人 | `state.scores` 为空 | 先 `grade`；或 `reset --stage grade` 后重收 |
| 压缩包解不开 | 缺 `bsdtar`/`7z`/`unrar` | `sudo apt install libarchive-tools p7zip-full unrar` |
| 有学生 `unmatched`/`ambiguous` | 压缩包命名无法唯一归属 | 在 `config.yaml` 的 `roster` 里指定，然后 `reset --stage unpack` 重跑 |

## 限流与性能

Canvas 与 Gitea 都有请求配额：

* `canvas.min_interval`（默认 0.1s）与 `gitea.min_interval`（默认 0.05s）控制最小请求间隔
* 所有请求都带指数退避重试（Canvas 4 次、Gitea 3 次）
* 批量附件下载：一个学生几十 MB 是常态，先 `--limit 2` 试出速率再全量
* runner 并发有限（本机两个 runner 合计 6 个 job）：批量推送时**分波**，
  例如每 20 个学生推一次、等一波跑完再推下一波（`--limit` + `--only` 组合即可）
* `grade` 的轮询间隔 `limits.grading_poll_interval`（默认 10s）不要调太小

## 日志与留档

* 终端日志是唯一实时输出；需要留档时 `python3 -m gc_auto_ta run -c config.yaml 2>&1 | tee run-$(date +%F-%H%M).log`
* 状态库 `state/state.sqlite3` 可以直接用 sqlite3 查：
  ```bash
  sqlite3 state/state.sqlite3 "select student_key,status,detail from stages where stage='grade' and status!='done';"
  sqlite3 state/state.sqlite3 "select student_key,score,source from scores order by score;"
  ```
* 报告目录 `reports/<作业>/` 建议每学期归档一次

## 与 JOJ3_Guide 的关系

本工具的每个 Gitea/JOJ3 细节都对齐 `https://sjtu.tangjie.xyz/JOJ/JOJ3-Guide`：

| 主题 | 指南章节 |
|---|---|
| 架构与四个角色 | 01 |
| 权限与 PAT | 02 |
| conf.json 字段与坑 | 03 |
| workflow / 两层可见性 / 三种收分方式 | 04 |
| 评分文件放置（防作弊） | 05 |
| 分组作业与 Team | 06 |
| 批量导入剧本 | 07 |
| 运维与失败排查表 | 08 |
| PlatformIO / 静态检查 | 09 |
