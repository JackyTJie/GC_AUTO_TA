"""GC_AUTO_TA —— Canvas 拉作业 → 解包 → Gitea 建仓 → JOJ3 评分 → Canvas 回写 的一体化流水线。

模块划分：
    util          通用工具（日志 / JSON / 限流 / 子进程）
    config        配置加载与校验（config.yaml + .env + 作业 meta.yaml）
    state         SQLite 状态库（幂等、断点续跑、前后对照）
    naming        学生身份映射（Canvas 用户 → 仓库名 → 压缩包内的目录/文件名）
    canvas_api    Canvas 读提交 / 下载附件 / 回写分数与评论
    unpack        压缩包识别与解包（zip / rar / 7z / tar.*），学生文件归集
    gitea_api     Gitea REST 客户端（建仓、team、org secret、Actions tasks、文件读取）
    workflow      渲染 .gitea/workflows/joj3.yaml
    provision     组装仓库工作区并用 PAT 推送
    grading       触发/重跑评分，轮询 run 状态，收集 joj3 结果
    report        汇总 results.json / results.csv
    writeback     结果 → Canvas 分数与评论（含 dry-run 与批量接口）
    cli           命令行入口
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
