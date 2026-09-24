# 04 Gitea 建仓与推送（provision）

```bash
python3 -m gc_auto_ta provision -c config.yaml --dry-run   # 只看会做什么
python3 -m gc_auto_ta provision -c config.yaml --limit 2   # 先试 2 个
python3 -m gc_auto_ta provision -c config.yaml
```

## 每个学生的仓库长什么样

```
hw1-2026000001/
├── .gitea/workflows/joj3.yaml      触发评测（由模板渲染）
├── .gitattributes                  * text=auto eol=lf（避免 CRLF 破坏逐行 diff）
├── joj/                            ← layout=in_repo 时的评分文件
│   ├── conf.json
│   ├── run_tests.m
│   └── expected.txt
├── _gc_auto_ta.json                溯源标记（作业名/学生/来源）
└── full_adder.m                    ← 学生源码（来自 work/<作业>/code/<学生>/）
```

`layout=config_repo` 时，`joj/` 不出现在学生仓库里，而是单独推到一个 TA 私有仓库
（默认 `<作业名>-config`），workflow 用 org secret 把它 clone 下来。

提交信息（JOJ3 会读 commit message 选配置，这里用 tag 固定作业）：

```
feat(hw1): 示例学生 submission [hw1]
 |    |          |             |
 |    |          |             +-- group tag：stage 的 groups 匹配用
 |    |          +---------------- 学生
 |    +--------------------------- 作业（scope）
 +-------------------------------- commit_prefix（config 可改）
```

## 建仓细节（幂等）

1. `GET /api/v1/repos/{org}/{repo}` —— 已存在就直接复用（不会删你已有的仓库）
2. 否则 `POST /api/v1/orgs/{org}/repos`：`{name, private, default_branch, description}`
3. 推送完成后 `PATCH /api/v1/repos/{org}/{repo}` 把 `default_branch` 设为 `main`
   （空仓库的默认分支初始可能是 `master`，不设会导致匿名 clone 报
   `remote HEAD refers to nonexistent ref`）
4. 记录 commit sha 到 `state.runs`，`grade` 阶段用它去匹配 Actions 任务与结果文件

## git 认证：token 不进 argv

provision 用临时 `GIT_ASKPASS` 脚本 + 环境变量传凭据：

```sh
# 临时生成的 askpass 脚本（内容固定，值来自环境变量）
case "$1" in
  *Username*) printf '%s\n' "$GC_GIT_USER" ;;
  *Password*) printf '%s\n' "$GC_GIT_PASS" ;;
esac
```

好处：`ps aux` 看不到 token，日志里也不会出现；同时用 `-c credential.helper=` 关掉全局凭据助手，
避免误用别的账号。

## 私有仓库 / 私有组织：workflow 也要能 clone

job 里的 `git clone` 是**匿名**的（推代码的人的身份不会传进容器，`GITHUB_TOKEN` 在本部署是空的）。
能否匿名 clone 取决于两层可见性（外层优先）：

```
org=private  → 匿名一律 401（哪怕 repo 是 public）
org=public   → repo=private → 401；repo=public → 可以匿名 clone
```

两种可行方案：

| 方案 | 做法 | 何时用 |
|---|---|---|
| A. 公开组织 + 公开仓库 | `repo_private: false`；workflow 匿名 clone | 最省事；代价是学生代码对全网可见 |
| B. org secret + PAT | 组织 Settings → Actions → New Secret，名 `GRADE_TOKEN`，值 = PAT；workflow 用 `username:PAT` clone | 私有组织/私有仓库，代码不外泄 |

方案 B 的配置动作：

```bash
# 1) 生成 PAT（scope: read:repository；要 push 分数再加 write:repository；开 issue 加 write:issue）
# 2) 写进组织 secret（本工具也能写，用的是你 config 里的 PAT）：
python3 - <<'PY'
from gc_auto_ta.config import Config
from gc_auto_ta.gitea_api import GiteaClient
cfg = Config.load("config.yaml")
gc = GiteaClient(cfg.gitea_url, cfg.gitea_token)
gc.set_org_secret(cfg.org, "GRADE_TOKEN", "<粘贴 PAT>")
print("已写入；现有 secrets:", gc.list_secrets(cfg.org))
PY
# 3) config.yaml 里把模板换成带 secret 的（publish=file/issue 时程序会自动选）：
#    grading.publish: file     → 模板 in-repo-secret（私有仓库 clone + 分数回传）
```

`doctor` 会检查 `GRADE_TOKEN` 是否已存在于该组织。

> **容器内必须用 `gitea.internal_url`**：公网 `http://<域名>` 会跳转 https，URL 里的凭据被丢掉，
> 结果就是 `remote: Unauthorized`。本机部署的 internal_url 就是 `http://localhost:3000`。

## workflow 模板一览

全部在 `templates/workflow/`，占位符 `{{...}}` 由 `workflow.py` 渲染后写进仓库：

| 模板 | clone 方式 | 结果回传 | 需要 secret |
|---|---|---|---|
| `in-repo-public` | 匿名 | 无（看日志） | 无 |
| `in-repo-public-issue` | 匿名 | 开 issue | `GRADE_TOKEN`（write:issue） |
| `in-repo-secret` | PAT | push `score-<sha7>.json` | `GRADE_TOKEN`（read+write:repository） |
| `config-repo` | PAT（含配置仓库） | 无 | `GRADE_TOKEN`（read:repository） |
| `config-repo-secret` | PAT（含配置仓库） | push 分数 | `GRADE_TOKEN`（read+write:repository） |
| `config-repo-issue` | PAT（含配置仓库） | 开 issue | `GRADE_TOKEN`（read:repository + write:issue） |

模板里几处刻意的写法：

* `runs-on: golang-ubuntu-latest`（本机 runner 的两个标签都指向带 joj3 的镜像；这个标签工具链更全）
* job 镜像**没有 curl** → HTTP 一律 `python3 -c "...urllib.request..."`
* 回传分数的提交信息带 `[skip ci]`，避免「回传分数 → 又触发一次评测」的重跑风暴
* 需要凭据时用 `python3` 在 URL 里插入 `owner:token@`，不依赖 `sed`/`envsubst`

## 验证推送结果

```bash
# 看仓库列表
curl -s -H "Authorization: token ***" \
  "http://localhost:3000/api/v1/orgs/JOJ/repos?limit=50" | \
  python3 -c "import json,sys;[print(r['full_name'], r['private']) for r in json.load(sys.stdin)]"

# 看某个学生的仓库内容
curl -s -H "Authorization: token ***" \
  "http://localhost:3000/api/v1/repos/JOJ/hw1-2026000001/contents/joj" | \
  python3 -c "import json,sys;[print(f['path']) for f in json.load(sys.stdin)]"

# 看 Actions 任务
curl -s -H "Authorization: token ***" \
  "http://localhost:3000/api/v1/repos/JOJ/hw1-2026000001/actions/tasks" | head -c 600
```

## 学生要不要 Gitea 账号

本工具走的是 JOJ3 指南的 **scheme A（TA 代传）**：学生只需要在 Canvas 交作业，
不需要 Gitea 账号，也没有任何仓库权限——评分文件因此天然不可被篡改。
若以后要学生自助提交（scheme B），需要：分支保护 + workflow 从保护分支取 `joj/`，
本工具的模板可以按需增加（见 JOJ3 指南第 5 章）。
