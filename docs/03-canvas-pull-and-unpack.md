# 03 Canvas 拉取与解包

## 拉取（pull）

```bash
python3 -m gc_auto_ta pull -c config.yaml                 # 全部
python3 -m gc_auto_ta pull -c config.yaml --limit 2       # 先试 2 个学生
python3 -m gc_auto_ta pull -c config.yaml --only 2026000001,2026000002
```

做的事情：

1. 读作业元数据（满分、grading_type、是否匿名批改、due_at、是否发布）→ 写进 `manifest.canvas`
2. 拉提交列表（`include[]=user,submission_comments,submission_history,rubric_assessment`）
3. 逐个学生落到 `work/<作业>/submissions/<学生标识>/`
4. 写 `_canvas.json`（提交时间 / attempt / late / 当前分数 / workflow_state），便于回写时对账
5. 更新 `manifest.json` 与状态库

## 提交类型对照

| submission_type | 落地内容 | 说明 |
|---|---|---|
| `online_upload` | 附件（原名） | 走 Canvas 签名 URL 下载；`att.download()` |
| `online_text_entry` | `solution.txt` | Canvas 的 body 是 HTML，已清洗成纯文本 |
| `online_url` | `url.txt` | 内容是学生提交的链接 |
| `online_quiz` | 无文件，记 note | 答案在 quiz submission 接口，不在作业提交里 |
| `media_recording` | 无文件，记 note | 需要人工听/看 |
| `none` | 无文件，note=未提交 | `submitted_at` 为空 |

> 缺什么补什么：不支持的提交类型不阻断其他学生，只在 `problems.md` 与日志里暴露。

## 学生标识（student_key）

```
login_id（学号，优先） → sis_user_id → user<canvas_user_id>
```

再经 slug 化（只保留 `[A-Za-z0-9._-]`）成为仓库名后缀：
`hw1-2026000001`。它贯穿仓库名、目录名、报告、state，全程一致。

## 解包（unpack）

```bash
python3 -m gc_auto_ta unpack -c config.yaml
```

### 支持的包格式

| 格式 | 实现 | 依赖 |
|---|---|---|
| `.zip` | 标准库 `zipfile` | 无 |
| `.tar / .tar.gz / .tgz / .tar.bz2 / .tar.xz` | 标准库 `tarfile` | 无 |
| `.rar / .7z` | `bsdtar`（优先）→ `7z` → `unrar` | `apt install libarchive-tools p7zip-full unrar` |

安全措施：逐条路径穿越检查（`../`、绝对路径一律拒绝）、解压体积上限（`unpack.max_extract_bytes`）、
跳过 `__MACOSX / .DS_Store / .git / .vscode` 等垃圾目录。

### 四种形态

```
1) 学生直接传源码
   submissions/A/full_adder.m                → code/A/full_adder.m

2) 单个压缩包
   submissions/B/hw1.zip  ──解开──►  unpacked/B/hw1/...  → code/B/...

3) 压缩包里套一层目录（学生爱打包成「作业1/」）
   hw1.zip → 作业1/full_adder.m   ──自动上提──►  unpacked/B/hw1/full_adder.m

4) 全班一个大包（一个学生把全班交的包传上来）
   submissions/A/all.zip → {2026000001/, 2026000002/}
   ──按目录名与学生名单匹配，唯一命中才拆分──► unpacked/2026000001/... / unpacked/2026000002/...
```

第 4 种情形若目录名匹配不到、或一个目录同时像多个学生，写进 `manifest.unmatched / ambiguous`，
**不拆分、不归属**，等人处理。

### 归集规则

`unpack.student_file_globs`（默认 `*.m *.cpp *.c *.h *.py *.txt *.csv ...`）递归挑选文件，
拷进 `work/<作业>/code/<学生>/`，保持相对路径。provision 只搬这一层，保证仓库内容干净。

## 名单匹配（不猜原则）

匹配打分（`naming._candidate_scores`）：

| 证据 | 分数 | 说明 |
|---|---|---|
| 学号（login_id）出现在条目名里 | 100 | 最可信 |
| Canvas user id 出现（带数字边界） | 90 | 可信 |
| 规范化后的人名整体匹配/包含 | 80 | 全角转半角、去空白与分隔符、忽略大小写 |
| 人名倒序匹配（中文姓名「姓在前/名在后」） | 60 | 兜底 |

规则：

* 只有**唯一最高分**才算命中；
* 有并列最高 → 进 `ambiguous`，不归属；
* 一个都没命中 → 进 `unmatched`。

人工兜底：在 `config.yaml` 的 `roster` 段写 `repo` / `name` / `aliases` / `canvas_user_id`，
然后重跑 `unpack`（`reset --stage unpack` 可强制重算）。

## 检查产物

```bash
# 学生数、代码文件数
python3 - <<'PY'
import json
m = json.load(open("work/hw1/manifest.json"))
for k, v in m["students"].items():
    print(k, v.get("name"), len(v.get("code_files", [])), "files", v.get("note", ""))
print("unmatched:", m.get("unmatched"))
print("ambiguous:", m.get("ambiguous"))
PY
```

`unpacked/` 里保留原始解包结果，`code/` 是即将进仓库的干净副本——两者都看一眼比猜强。
