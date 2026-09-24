# 09 多个 test case 怎么配

JOJ3 的「多组测试数据」分两层，**必须成对出现**：

| 层 | 位置 | 作用 | 对数关系 |
|---|---|---|---|
| executor 的 `cases` | `stages[].executor.with.cases[]` | 决定**跑几遍**：每项与 `default` 合并成一条命令 | N 项 → 执行 N 次 |
| parser 的 `cases` | `stages[].parsers[].with.cases[]` | 决定**比几遍**：第 i 项比较第 i 次执行的输出 | 必须也有 N 项 |

不写 `executor.with.cases` 时只执行 1 次；此时 parser 的 `cases` 也只能有 1 项。

## 规则（逐条来自源码，非猜测）

| 规则 | 依据 |
|---|---|
| `with.default` 是基线，`with.cases[i]` 是**覆盖**（只写要改的字段），合并后生成一条命令 | `cmd/joj3/stage.go`：`defaultCmd` + `conf.OptionalCmd` merge；`len(Cases)==0` → `cmds = []{{default}}` |
| parser 的 `cases` 数 ≠ 执行次数 → 直接报 `cases number not match`，整个 stage 失败 | `internal/parser/diff/parser.go:19` |
| 分数**累加**：满分 = Σ 所有 case 所有 `outputs[].score`（不是百分比、不自动归一化） | `parser.go:53-63`、`processOutput` 返回 `output.Score` 后相加 |
| `failOnNotAccepted` 默认 `true`：该 case 执行状态不是 `Accepted` → 该 case 直接 0 分且**不比较** | `parser.go:46` |
| `compareSpace` 默认 `false` = **忽略所有空白字符**（空行、行内空格、行尾空格都不算差异） | `internal/parser/diff/patience.go:stringsEqual` 逐 rune 跳过 `unicode.IsSpace` |
| `answerPath` 由 **joj3 进程**读取（沙箱外），学生改不到 | `parser.go:74`：`os.ReadFile(output.AnswerPath)` |
| `fileName` 必须是 executor 产出并 `copyOut` 的输出名（`stdout` / `stderr` / 自定义文件） | `parser.go:79`：`result.Files[output.Filename]` |
| JSON 键名大小写不敏感（`fileName` 与 `filename` 等价），官方示例用 `fileName` | Go `encoding/json` 的默认行为 |

### `with.cases[]` 里可以覆盖的字段

`args`、`env`、`stdin`、`stdout`、`stderr`、`cpuLimit`、`realCpuLimit`、`clockLimit`、`memoryLimit`、
`stackLimit`、`procLimit`、`cpuRateLimit`、`cpuSetLimit`、`copyIn`、`copyInCached`、`copyInDir`、
`copyOut`、`copyOutCached`、`copyOutMax`。（`cmd/joj3/conf/model.go: OptionalCmd`）

### `outputs[]` 里可用的字段

| 字段 | 含义 |
|---|---|
| `score` | 这一项比中给多少分 |
| `fileName` | 要比较的输出名（executor 的 `copyOut` 名单里要有它） |
| `answerPath` | 期望文件路径（相对仓库根，由 joj3 读取） |
| `compareSpace` | `true` = 连空白一起严格比；默认 `false` 忽略空白 |
| `alwaysHide` | 失败时不在评语里贴 diff（防泄题） |
| `forceQuitOnDiff` | 这一项不中就直接中止后续 stage |
| `maxDiffLength` / `maxDiffLines` | 评语里 diff 的截断长度/行数 |
| `hideCommonPrefix` | 隐藏 diff 的公共前缀 |

### parser 顶层字段

`passComment`、`failComment`、`failOnNotAccepted`（默认 true）、`forceQuitOnFailed`（默认 false）。

## 完整例子：3 组数据，30 + 30 + 40 = 100 分

```json
{
  "name": "hw1",
  "sandboxExecServer": "localhost:5051",
  "outputPath": "joj3_result.json",
  "stages": [
    {
      "name": "run",
      "executor": {
        "name": "sandbox",
        "with": {
          "default": {
            "args": ["octave-cli", "solution.m"],
            "env": ["PATH=/usr/bin:/bin:/usr/local/bin", "HOME=/tmp"],
            "stdin": { "content": "" },
            "stdout": { "name": "stdout", "max": 65536 },
            "stderr": { "name": "stderr", "max": 65536 },
            "cpuLimit": 30000000000,
            "clockLimit": 120000000000,
            "memoryLimit": 2147483648,
            "procLimit": 256,
            "copyInDir": ".",
            "copyOut": ["stdout", "stderr"]
          },
          "cases": [
            { "stdin": { "src": "joj/cases/basic.in" } },
            { "stdin": { "src": "joj/cases/carry.in" } },
            { "stdin": { "src": "joj/cases/wide.in" } }
          ]
        }
      },
      "parsers": [
        {
          "name": "diff",
          "with": {
            "passComment": "本组通过",
            "failComment": "本组不通过",
            "failOnNotAccepted": true,
            "cases": [
              { "outputs": [ { "score": 30, "fileName": "stdout", "answerPath": "joj/cases/basic.out" } ] },
              { "outputs": [ { "score": 30, "fileName": "stdout", "answerPath": "joj/cases/carry.out" } ] },
              { "outputs": [ { "score": 40, "fileName": "stdout", "answerPath": "joj/cases/wide.out",
                               "compareSpace": true } ] }
            ]
          }
        },
        { "name": "result-detail", "with": { "score": 0, "showFiles": ["stderr"], "maxFileLength": 2000 } }
      ]
    }
  ]
}
```

一组数据多个检查点（分数相加）——同一个 case 的 `outputs` 写多项：

```json
{
  "outputs": [
    { "score": 10, "fileName": "stdout",     "answerPath": "joj/cases/a.out" },
    { "score": 10, "fileName": "result.txt", "answerPath": "joj/cases/a.txt" }
  ]
}
```

> `result.txt` 要能被比较，executor 里必须声明：`"copyOut": ["stdout", "stderr", "result.txt"]`，
> 否则 `result.Files["result.txt"]` 取到空串，会判成不一致。

## 两次执行用不同命令/不同输入文件

`cases[i]` 可以覆盖 `args` 与 `copyIn`，因此也能做「同一份代码换不同入口」：

```json
"cases": [
  { "args": ["python3", "q1.py"], "stdin": { "src": "joj/cases/q1.in" } },
  { "args": ["python3", "q2.py"], "stdin": { "src": "joj/cases/q2.in" } }
]
```

## 多 stage 的组合（编译 + 多组运行）

```
stage "build"  : 编译（1 次执行）；parser: result-status（失败 forceQuit，之后不再跑）
stage "run"    : 运行（N 次执行）；parser: diff 的 N 个 case
```

`result-status` 里 `forceQuit: true` 就是「编译不过 ⇒ 0 分、后面的 stage 全跳过」的标准写法。

## 在 GC_AUTO_TA 里怎么落

多组数据全部塞进 `config.yaml` 的 `grading.files`，跟作业一起进仓库：

```yaml
grading:
  conf_root: joj
  conf_name: conf.json
  max_score: 100                 # 必须等于 conf.json 里所有 output.score 之和
  files:
    joj/conf.json: |             # 上面那份 conf.json 原样贴进来
      { "name": "hw1", ... }
    joj/cases/basic.in:  "0 0 0\n"
    joj/cases/basic.out: "a=0 b=0 cin=0 sum=0 cout=0\n"
    joj/cases/carry.in:  "1 1 0\n"
    joj/cases/carry.out: "a=1 b=1 cin=0 sum=0 cout=1\n"
    joj/cases/wide.in:   "1 1 1\n"
    joj/cases/wide.out:  "a=1 b=1 cin=1 sum=1 cout=1\n"
```

注意三处对齐，否则一定 0 分或报错：

1. `grading.max_score` == Σ `outputs[].score`
2. `executor.with.cases` 数量 == `parsers[].with.cases` 数量
3. `answerPath` / `stdin.src` 的路径 == `files` 里的键（都在 `conf_root` 相对仓库根的路径下）

## 常见坑

| 症状 | 原因 |
|---|---|
| `cases number not match`（stage 直接失败） | executor 与 parser 的 cases 数不相等 |
| 全班 0 分 | `copyOut` 没包含被比较的文件名；或 `max_score` 与分数之和不一致导致换算后全部为 0 |
| 「代码明明对」却 0 分 | 学生输出与期望的行数/内容不同；先用 `compareSpace: false`（默认）排除空白因素，确认是真实差异 |
| 期望「空行/空格也要严格一致」 | 必须显式写 `"compareSpace": true`（默认是忽略空白） |
| 分数上限不对 | diff 的分数是累加的；多组数据要把 100 分**拆开分配** |

> 与 JOJ3 指南的差异：指南第 3 章写「comparison is exact line-by-line text: one stray blank line fails」，
> 按 1.23 源码这只在 `compareSpace: true` 时成立；默认 `false` 下空白差异会被忽略。
