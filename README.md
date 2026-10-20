# labloop（Personal Agent Scientist）

**把「我想研究 X 对 Y 的影响」变成自动执行的研究循环。**

[![CI](https://github.com/IMUskyworld/Personal-Agent-Scientist/actions/workflows/ci.yml/badge.svg)](https://github.com/IMUskyworld/Personal-Agent-Scientist/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

你给一个研究问题，labloop 生成结构化假设 + 第一轮实验矩阵，渲染成真实命令逐个执行，解析指标，汇总分析，给出下一轮实验建议，如此往复——直到轮次/预算上限，或者收敛。LLM（OpenAI 兼容，Qwen 可用）负责假设与解读；没有 LLM 时自动降级为离线启发式后端，全流程仍然可用。

```
问题 → 假设 + 实验矩阵 → 渲染命令 → [确认] → 逐个执行 → 指标解析 → 轮次分析 → 下一轮 → 报告
```

## 它解决什么问题

研究者最常做的循环是：想一个变量 → 手改配置 → 跑一串训练 → 复制粘贴结果到表格 → 拍脑袋决定下一组参数。

labloop 只做这件事的**编排**：把机械部分（改参、排队执行、汇总、比较）自动化，判断（假设、解读、下一步）交给 LLM 或确定性规则。

```text
问题 → 假设 + 实验矩阵 → 渲染命令 → [确认] → 逐个执行 → 指标解析 → 轮次分析 → 下一轮 → 报告
```

| | |
| --- | --- |
| 真的替你跑实验 | 矩阵 → 命令 → 串行执行 → 超时/预算护栏 → 结果落盘，全部自动化 |
| 杀干净 | 超时或 Ctrl+C 会结束**整个进程树**（不会留下占显存的训练进程）；输出流式落盘，几小时日志也不撑内存 |
| 判断交给 LLM（可选） | 假设生成与结果解读可用 Qwen 等 OpenAI 兼容端点；无 key 时离线规则保证可用 |
| 不装懂 | 最佳点与边界永远由**实测数据**推导；LLM 说的与数据不符会被修正；环境/固定参数不一致时可比性守卫显式标记 |
| 不偷换后端 | 显式 `--backend llm` 失败会**报错**（不会偷偷换成离线结果）；只有 `auto` 模式才自动降级，且降级原因写进报告 |
| 通用配方 | 任何训练脚本：`--command "python train.py --batch {batch}" --metric "acc=acc:\\s*([0-9.]+)"`；内置 YOLO 示例配方 |
| 可恢复 | 中断安全（Ctrl+C 杀进程树、结果保留、exit 130）；重入自动跳过已完成 run；结果变了旧结论自动作废 |
| 自包含报告 | report.html 含手写 SVG 折线图，无外部资源，直接贴组会/issue |

## 快速开始

### 方式一：直接跑源码（推荐，Windows 中文路径也不会出问题）

```powershell
git clone https://github.com/IMUskyworld/Personal-Agent-Scientist.git; cd "Personal-Agent-Scientist"
uv venv --python 3.11
uv pip install typer pydantic rich jinja2 httpx packaging   # 国内网络：加 UV_DEFAULT_INDEX=<镜像>
.venv\Scripts\activate
```

### 方式二：安装成命令（路径需为纯 ASCII）

```powershell
pipx install git+https://github.com/IMUskyworld/Personal-Agent-Scientist.git
labloop version
```

> 本项目路径含中文与空格时，editable install 的 `.pth` 会因 locale 解码乱码而失败，
> 所以 Windows 上建议用方式一（`scripts/labloop_dev.py`）。

### 试一下

```powershell
# 离线模式（不需要 LLM）
python scripts/labloop_dev.py design "batch size 对训练速度和精度的影响" `
    --recipe generic --command "python train.py --batch {batch} --seed 42" `
    --metric "accuracy=acc:\s*([0-9.]+)" --var batch=8,16,32,64
python scripts/labloop_dev.py plan <study>       # 先看要跑什么（不执行）
python scripts/labloop_dev.py auto <study> --yes --max-rounds 3
python scripts/labloop_dev.py report <study>     # 生成自包含 HTML 报告

# LLM 模式（Qwen / OpenAI 兼容端点）
$env:LABLOOP_LLM_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
$env:LABLOOP_LLM_MODEL="qwen3.8-flash"
$env:LABLOOP_LLM_API_KEY="sk-..."
python scripts/labloop_dev.py design "我要研究 batch size 对 YOLO 训练速度和准确率的影响" --recipe yolo --var batch=8,16,32,64
```

在 3090 训练机上（YOLO 实战）：

```powershell
# 环境里需有 ultralytics（labloop 自身不需要它）
python scripts/labloop_dev.py design "batch size 对 YOLO 训练速度和 mAP 的影响" `
    --recipe yolo --var batch=8,16,32,64 --backend llm
python scripts/labloop_dev.py plan <study>
python scripts/labloop_dev.py auto <study> --yes --max-rounds 3 --timeout 3600
python scripts/labloop_dev.py report <study>
```

## 命令

```
labloop design "<问题>"   生成假设 + 第一轮矩阵，创建 study
labloop plan   <study>    dry-run：渲染本轮命令（不执行）
labloop run    <study>    执行本轮（需确认；--yes 跳过）
labloop analyze <study>   轮次分析 + 下一轮建议
labloop next   <study>    采纳建议 → 生成下一轮计划（--force 可覆盖已有计划）
labloop auto   <study>    全自动循环：执行→分析→下一轮→…→报告
labloop report <study>    报告（console + report.json + report.html）
labloop status <study>    状态一览
```

**退出码**：`0` 完成 · `1` 存在失败的 run · `2` 未执行（确认被拒/非交互无 `--yes`）· `3` 内部错误 · `130` 中断。

## 工作目录长什么样

```text
.labloop/studies/<slug>/
├── study.json              研究问题、假设、配方、变量、固定上下文
├── recipe.json             配方快照（保证可复现）
├── rounds/round-001/
│   ├── plan.json           本轮要跑的命令
│   ├── runs/<run-id>/      spec.json / command.txt / stdout.log / result.json
│   └── analysis.json       最佳点、发现、下一轮建议、可比性检查
├── report.json
└── report.html             自包含报告（手写 SVG 折线图）
```

## 安全边界（请读）

- 执行实验 = **以你的权限运行你写的命令**；labloop 默认必须交互确认，`--yes` 是显式知情选择，非交互终端无 `--yes` 一律不执行。
- 预算护栏：`--max-runs` / `--timeout`（单 run）/ `--max-rounds` / `--budget-min`（auto 总时长）。`--max-runs` 截断时自动循环会停下，不会拿半轮数据下结论。
- labloop 不安装依赖、不改环境；LLM 只收到问题、假设、结果表格与环境摘要（家目录脱敏），密钥不入日志与报告，**也不会传给实验子进程**。
- study 名称只能是单一目录名（拒绝 `..`、路径分隔符等），产物不会写到运行目录之外。
- 无遥测。

## 与其它工具的区别

| | labloop | wandb sweep | optuna |
| --- | --- | --- | --- |
| 跑的是 | 任意命令 + 指标解析规则 | 固定框架的 train function | 固定框架的 objective |
| 下一轮怎么定 | LLM 假设 + 确定性规则，**必须与实测数据对账** | 预定义的搜索空间 | 纯数值优化 |
| 环境漂移 | 每 run 记录环境指纹，不一致时显式标记并降置信度 | 有 sweep 元数据 | 无 |
| 产物 | 自包含 HTML 报告 + JSON，可直接贴 issue | 云端看板 | 平台内记录 |

labloop 不替代超参搜索库：它的目标是**把「改一个参数手跑四遍再抄数」这件事变成一个可恢复、可审计的循环**。

## 开发

```powershell
uv venv --python 3.11
uv pip install typer pydantic rich jinja2 httpx packaging pytest pytest-cov ruff pyright
.venv\Scripts\activate
python -m pytest -q                       # 单元 + 集成（含本地假 LLM 服务器）
powershell -NoProfile -File scripts/check.ps1   # 完整质量闸门（ruff + pyright + pytest + §20 审计）
python scripts/verify_mvp.py              # 逐条核验 DEVELOPMENT.md §20
python scripts/export_schemas.py          # 重新生成 docs/schemas/*.json
python scripts/bench_overhead.py          # §17 性能对照
```

设计文档、决策记录（ADR）与数据协议见 [`DEVELOPMENT.md`](DEVELOPMENT.md) 与 [`docs/adr/`](docs/adr/)。

**本机验收边界**（详见 DEVELOPMENT.md §18）：本机（无 GPU）验收聚焦「设计 + 配置生成 + 报告 + 分析」；真实实验执行（YOLO 实战、Qwen 后端）属于 3090 交接项。

## English summary

labloop turns a research question into an automated experiment loop: it designs a hypothesis
and experiment matrix (LLM or offline heuristics), renders them into real commands, executes
them serially with timeouts and budgets, parses metrics, analyzes each round, proposes the
next one, and produces a self-contained HTML report. Generic command/recipe mechanism with a
built-in YOLO example recipe. Resumable, interrupt-safe, and honest about comparability.

## License

MIT
