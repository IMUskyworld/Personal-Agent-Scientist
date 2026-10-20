# Changelog

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.1.0] — 2026-10-20

首个可发布版本：把「我想研究 X 对 Y 的影响」变成自动执行的研究循环。

### 功能

- `design` / `plan` / `run` / `analyze` / `next` / `auto` / `report` / `status` 八条命令
- 配方机制：命令模板（`{param}` / `{run_id}` / `{round}` / `{slug}`）+ 指标规则
  （`stdout_regex` / `csv_last` / `json_file`），内置 `yolo` 与 `generic` 示例
- 双后端：LLM（OpenAI 兼容，Qwen 可用）与离线启发式；显式 `--backend llm` 失败即报错，
  只有 `auto` 允许降级并记录原因
- 可比性守卫：环境指纹 + 固定上下文 + 主指标解析情况，异常时显式标记并降低置信度
- 结论与实测对账：最佳点与边界永远由真实数据推导，后端结论与数据不符会被修正
- 自包含 HTML 报告（手写 SVG 折线图，无外部资源）+ report.json
- 护栏：`--max-runs` / `--timeout` / `--max-rounds` / `--budget-min`；Ctrl+C 安全中断
  并结束整个进程树；结果可恢复，结果变化后旧结论自动作废

### 工程

- 质量闸门：`ruff` + `ruff format` + `pyright` + `pytest`（131 项）+ §20 验收脚本
- CI：Windows / Ubuntu × Python 3.11 / 3.12，另含 wheel 打包与安装冒烟测试
- 文档：`README.md`、`DEVELOPMENT.md`（设计文档与验收记录）、`docs/adr/`（决策记录）、
  `docs/schemas/`（数据协议 JSON Schema）

### 已知边界

- 仅支持**单研究变量**（多变量矩阵待后续）
- 真实 YOLO 训练与真实 LLM 端点尚未在 GPU 机器上验收（见 `DEVELOPMENT.md` §24.3）
