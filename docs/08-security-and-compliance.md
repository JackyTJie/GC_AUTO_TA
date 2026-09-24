# 08 安全与合规

## 密钥

| 密钥 | 放哪里 | 权限要求 | 轮换 |
|---|---|---|---|
| Canvas API token | `config.yaml` 的 `canvas.token` 或环境变量 `CANVAS_API_TOKEN` | 等于你账号的全部权限（手动生成的 token 不缩权） | 每学期或泄露后立刻 Revoke |
| Gitea PAT | `config.yaml` 的 `gitea.token` 或环境变量 `GITEA_TOKEN` | `write:repository` + `write:organization` + `read:user`（开 issue 加 `write:issue`） | 设过期时间，到期重发 |
| 组织 secret `GRADE_TOKEN` | Gitea 组织 Settings → Actions → Secrets | 只会给 job 用，日志里渲染成 `***` | 与 PAT 同步轮换 |

硬性要求：

1. `config.yaml` 权限 600（`new-config` 已设置），并在 `.gitignore` 里（默认已加）
2. 绝不把 token 写进任何会被提交的文件：仓库正文文件、workflow YAML、issue 内容
3. 绝不把 token 贴进聊天/工单；需要给别人复现时用 `doctor` 的输出（不含密钥）
4. 程序侧已做的防护：git 认证走 `GIT_ASKPASS`（token 不进 argv / 不进 shell 历史）；
   日志与报告里 token 会被抹成 `***`（`config.redact`）

## 数据边界

* 学生提交只下载到本机 `work/<作业>/submissions|unpacked|code/`
* 上传到 Gitea 的内容 = 学生源码 + 评分文件（`in_repo` 布局下评分文件也在里面，
  因此公开仓库时要注意：**测试用例会跟着公开**）。需要保密就用 `layout: config_repo`
  或让仓库保持私有
* 若要把提交内容送外部 LLM/云服务做辅助评分，先确认课程与学校对作业数据的处理规定
  （本工具默认不做任何外部调用）
* 报告 `reports/` 含学生姓名与分数，同样按个人数据处理；学期结束按课程要求归档或清理
* `work/` 与 `state/` 都在 `.gitignore` 里，不要手动提交

## 合规（做自动批改前先确认这些）

1. **任课老师授权**：自动评分、自动评论是否允许？评语是否必须以「已人工复核」的口径发布？
2. **留痕与可追溯**：所有打分都记在你的账号名下（`grader_id`）。这是责任，也是保护——
   出错能查、能批量改
3. **匿名批改**：作业开了 Anonymous Grading 时，严禁用 `anonymous_id` 反查真实学生后差别对待
4. **人工复核兜底**：`report` 的 `problems.md` + 分数分布异常（一片 0 分）必须先人工看完再回写。
   自动流程的正确用法是「把人的时间从抄分里省出来」，而不是取消判断
5. **同分同标准**：评分脚本的断言要写在 `conf.json`/`run_tests.m` 里（可复查、可复现），
   不要用「AI 看心情给分」的评语替代客观标准
6. **申诉通道**：回写后要能回答「为什么这个分」——`reports/results.json` 里的 stage 明细 +
   Gitea 仓库里的 `score-<sha7>.json` 就是证据链

## 权限最小化

* 学生团队（如果以后启用 scheme B）只给 `repo.code` / `repo.issues`，**绝不给 `repo.settings`**
* 不要打开学生团队的 `can_create_org_repo`
* 不要向 workflow 暴露 docker socket，也不要为了「让 CI 能 build 镜像」去改 runner 配置
  （本机刻意保留了这个限制）
* 不给任何人 site admin 或服务器 SSH；本工具所有操作都走 API

## 已知的不可为

| 想法 | 现实 |
|---|---|
| 打分不留痕 | 做不到：`grader_id` 就是你的账号；只有 Anonymous Comments 让评论对匿名化 |
| 伪造别人（老师）的身份提交分数 | 做不到：token 权限 = 账号权限，不能 masquerade |
| 用 API 发语音/视频点评 | Canvas 没有生成 media comment 的接口 |
| 读取 quiz / LTI 提交内容 | 这两类内容不在作业提交对象里 |
| 无人值守全自动回写 | 技术上可以，但建议保留人工过一眼 `report` 的环节（见上文第 4 条） |

## 事故处理

| 事故 | 立即动作 |
|---|---|
| token 泄露 | 立刻在平台 Revoke/重新生成；检查 Gitea 审计日志与仓库新增提交 |
| 分数写错全班 | 用 `reports/<作业>/results.csv` 对照 `state.writebacks.previous_score` 决定覆盖值，改配置后重跑 `writeback --live`；评论无法删除，只能补一条更正说明 |
| 学生代码误公开 | 立刻把仓库/组织改为私有（`PATCH /api/v1/repos/{org}/{repo}` + 组织设置），评估是否已被人抓取 |
| runner 上跑了可疑 workflow | 立即停用对应 runner（`systemctl --user stop act-runner-*`），检查 `.gitea/workflows/` 的提交历史 |
