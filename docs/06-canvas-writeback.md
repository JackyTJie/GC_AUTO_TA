# 06 回写 Canvas（分数与评论）

```bash
python3 -m gc_auto_ta writeback -c config.yaml             # dry：只打印
python3 -m gc_auto_ta writeback -c config.yaml --live      # 真写
python3 -m gc_auto_ta writeback -c config.yaml --live --only 2026000001   # 只写一个人
python3 -m gc_auto_ta writeback -c config.yaml --live --no-bulk             # 逐条写（排查用）
```

## 权限前提（不满足就 403）

Canvas 的提交更新接口要求**管理成绩**权限。官方原文：

> If any submission or rubric_assessment arguments are provided, the user must have permission to
> **manage grades** in the appropriate context (**course or section**).

* 你的 TA 角色必须开着这条权限；
* 若 TA 被限定在某个 section，「appropriate context」就是该 section——你改不了别的 section 的学生；
* 权限不足时只影响该学生的回写，其他学生继续（错误进 `state.stages` 与报告）。

## 分数换算（`writeback.grade_mode`）

| 模式 | 写入的 posted_grade | 说明 |
|---|---|---|
| `points`（默认） | `"87.5"` | `joj3_score / max_score * points_possible`，两位小数；超过满分会被 Canvas 当作加分（取决于作业设置） |
| `percent` | `"87.5%"` | Canvas 按百分比折算；适合满分经常变的作业 |
| `raw` | `"87.5"` | 直接把 JOJ3 原始分当分数；要求两边满分一致 |

`posted_grade` 支持的写法（Canvas 文档）：点数 `"13.5"`、百分比 `"40%"`、
`pass/fail/complete/incomplete`、以及按 grading scheme 的字母等级。
本工具只产出前两种 / 原样数值，避免歧义。

## 迟交扣分

```yaml
writeback:
  late_penalty_per_day: 0.1    # 每天扣 10%（比例，不是分数）
  due_at: null                 # 留空用 Canvas 作业的 due_at
```

* 以 `submitted_at` 与 `due_at` 比较，**向上取整**天数（晚 1 分钟也算 1 天）
* 扣的是比例：`ratio = max(0, ratio - penalty * days)`
* 评语里会自动附一行「迟交 N 天，扣分系数 X」
* 时间取当前提交（最新 attempt）；多次提交的历史在 `_canvas.json` 与 `submission_history` 里

## 评语模板

```yaml
writeback:
  comment_template: "【自动评分】{score}/{max} 分\n{comment}"
```

可用占位符：

| 占位符 | 内容 |
|---|---|
| `{score}` | JOJ3 原始分 |
| `{max}` | `grading.max_score` |
| `{points}` | 换算后的 Canvas 分数 |
| `{comment}` | JOJ3 各 stage 评语拼接 |
| `{stages}` | 各 stage 名字与分数（`run: 100；lint: 5`） |
| `{student}` / `{assignment}` | 学生名 / 作业名 |

长度超过 `comment_max_len`（默认 4000）会截断并追加「…（评语过长已截断）」。
`writeback.only_grade: true` 则完全不发评论，只写分数。

## 单条 vs 批量

| 方式 | 接口 | 何时用 |
|---|---|---|
| 单条 | `PUT /courses/:cid/assignments/:aid/submissions/:uid` | 人数少、要逐条确认 |
| 批量（默认） | `POST .../submissions/update_grades`，异步 job，轮询 `GET /progress/:id` | 整班回写；避免逐条触发限流 |

批量接口的 payload 每项支持 `posted_grade` / `excuse` / `rubric_assessment` /
`text_comment` / `group_comment` / `file_ids`；本工具用前两个 + `text_comment`。

## 匿名批改（Anonymous Grading）

作业开了匿名批改时：

* 学生列表里给的是 `anonymous_id`，批改必须走
  `PUT /courses/:cid/assignments/:aid/anonymous_submissions/:anonymous_id`
* canvasapi 没有封装该端点，本工具直接发原始请求（`CanvasGateway.post_grade(..., anonymous_id=...)`）
* 此时**不启用批量接口**（批量接口只认 user_id），逐条写
* 严禁拿 `anonymous_id` 反查真实学生再差别对待——这属于违规

## 留痕（务必知悉）

* API 打的分：`grader_id` = 你的账号（token 所属用户），成绩历史里老师能看到是你批的
* 评论在 SpeedGrader 里显示评论者；只有作业开了 **Anonymous Comments** 才对匿名化
* 想要「无痕自动批改」不可能；反过来这也是好事——出问题能追溯、能批量撤销

## 回写前的核对清单

1. `report` 输出的表格与 `reports/<作业>/results.csv` 看一遍：分数分布是否合理（有没有一片 0 分）
2. `problems.md` 里的学生是否确实缺交/格式不对，而不是我们的流程出了问题
3. 先在 Canvas 上用一份**你自己的测试提交**跑通 `--live`，确认评语渲染正确
4. 确认 `on_missing_submission` 的取舍：缺交要不要记 0（`zero`）还是不动（`skip`）
5. 分批写：`--only 学号1,学号2` 先来两个人，再去掉 `--only` 全量

## 错误与 0 分覆盖

writeback 只对前置阶段明确标记为 `error_kind=student` 的学生构造 0 分 payload。学生提交
损坏、非法或无法归集时，流程会标记错误并跳过后续评分阶段；live writeback 最终覆盖为 0
分，旧分数保存在 `previous_score` 中。

Canvas、Gitea、Actions、runner、网络、权限、配置和本地文件系统错误标记为
`error_kind=system`，只报告问题，不把已有成绩改成 0。JOJ 评测正常返回的 score=0 是普通
评分结果，仍按正常成绩保存和回写。

批量回写只有 Canvas Progress 返回 `completed` 才记录成功；失败、超时或异常会逐学生记录
writeback 错误。匿名作业无法取得 anonymous id 时不会退回普通学生 submission 接口。

## 撤销与重写

* 重写：改完配置直接再跑 `writeback --live`（会覆盖分数与新增评论，旧评论仍留在 SpeedGrader 里）
* 批量撤销：Canvas 无「撤销 API」，需要手工在 SpeedGrader/成绩册里改，或用 `state.writebacks`
  里的 `previous_score` 作为对照再写一次
* 因此**默认 dry** 不是啰嗦，是给你一次把错误拦在纸上的机会
