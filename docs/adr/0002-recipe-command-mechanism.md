# ADR 0002：通用命令 + 配方，而不是绑定某个训练框架

- 状态：已采纳（2026-09-22，用户选定）
- 背景：最直接的实现是绑定 ultralytics/YOLO（用户的 3090 场景），但那样 labloop 会变成「YOLO 调参小工具」，任何别的训练脚本都用不了；而 labloop 的价值在「设计-执行-分析」的循环本身。
- 决策：
  1. 配方（Recipe）= 命令模板（`{param}`/`{run_id}`/`{round}`/`{slug}` 占位符）+ 参数定义 + 固定上下文 + 指标解析规则（`stdout_regex` / `csv_last` / `json_file`）；
  2. 内置 `yolo`（ultralytics，示例）与 `generic`（`--command` + `--metric`），自定义走 `--recipe-file`；
  3. 指标解析失败是 warning 而非伪造值；模板缺值/字面量花括号都有明确错误与修复提示。
- 理由：任何「跑命令 + 出指标」的训练脚本都能接；YOLO 只是第一个配方；指标来源抽象（stdout/CSV/JSON）覆盖了大多数训练框架的输出形态。
- 代价：复杂框架的指标需要用户写一条规则；配方 JSON 是新的格式要学（但有内置示例可抄）。
