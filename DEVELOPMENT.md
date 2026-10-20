# labloop 开发文档（Personal Agent Scientist）

版本：v0.1（2026-09-22）
状态：实现中；逐条验收见 §25。

一句话：把「我想研究 X 对 Y 的影响」变成**自动执行的研究循环**——LLM 设计实验、机器真实执行、结果自动分析、给出下一轮实验建议，如此往复。

```
问题 → 假设 + 实验矩阵 → 渲染命令 → [确认] → 逐个执行 → 指标解析 → 轮次分析 → 下一轮 → 报告
```

## 1. 项目定位

研究者（或学生）最常做的循环是：想一个变量、手动改配置、跑一串训练、复制粘贴结果到表格、拍脑袋决定下一组参数。这个循环里的机械劳动（改参、排队执行、汇总、比较）全部可以自动化，而**判断**（假设、解读、下一步）恰恰是 LLM 擅长的。

labloop 的定位：

1. **设计**：输入一个研究问题（自然语言），生成结构化假设 + 第一轮实验矩阵（变什么、固定什么）。
2. **执行**：把矩阵渲染成真实命令，逐个执行，解析指标（准确率、mAP、耗时……）。
3. **分析**：汇总轮次结果，找最佳点、判断边界/内部、给置信度；LLM 或离线启发式规则均可。
4. **迭代**：基于分析生成下一轮矩阵（外推或收窄），循环直到预算/轮次上限或收敛。
5. **产出**：自包含 HTML 报告（含手写 SVG 图表）+ report.json，可贴进组会/issue。

三条硬性原则：

- **不装懂**：分析结论必须引用具体 run 的数据；可比性不成立（环境变了、固定参数不一致）时显式标记，不静默比较。
- **LLM 可选**：双后端（LLM / 离线启发式）；无 LLM 时全流程仍可用，只是假设与解读更模板化。
- **执行即权力**：跑实验等于以用户权限执行用户命令；默认必须交互确认，预算/超时/中断全部有护栏。

## 2. MVP 范围

**做：**

- `design`：问题 → 假设（Hypothesis）+ 第一轮实验矩阵（ExperimentPlan）→ 落盘为 Study。
- **配方（recipe）机制**：参数模板、命令渲染（`{param}`/`{run_id}`/`{round}` 占位符）、指标解析规则；内置 `yolo`（ultralytics，示例配方）与 `generic`（`--command` + `--metric` 正则），支持 `--recipe-file` 自定义。
- `plan`：dry-run 渲染本轮将执行的全部命令（绝不执行）。
- `run`：逐 run 执行、超时控制、日志、指标解析、结果落盘、resume（跳过已完成）、`--redo`。
- `analyze`：轮次聚合分析（最佳点、findings、下一轮建议、置信度、可比性守卫）。
- `next`：采纳建议生成下一轮计划。
- `auto`：设计→执行→分析→下一轮的全自动循环（预算与轮次上限）。
- `report`：console / report.json / 自包含 report.html（SVG 折线图）。
- 双后端：LLM（OpenAI 兼容端点，Qwen 可用）与离线启发式（网格矩阵 + 统计规则）。

**不做（MVP 明确排除）：**

- 不训练模型、不下载数据集权重、不内置任何数据集；labloop 只负责「设计-执行-分析」的编排。
- 不做论文写作、文献检索、RAG。
- 不做分布式/多机调度；一次循环在一台机器上串行执行。
- 不做超参搜索算法库（贝叶斯优化等）；MVP 的下一轮策略是确定性的：边界外推 / 区间收窄（LLM 后端可给出更灵活的建议，但同样受矩阵规模上限约束）。
- 不修改用户环境；不自动安装依赖。

## 3. 目标用户和使用场景

| 用户 | 场景 |
| --- | --- |
| 本人（RTX 3090 训练机） | `labloop auto` 让 batch size / lr / imgsz 的对比实验自动跑一整晚，早上看报告。 |
| 同学 / 实验室 | 把「改一个参数手跑四遍再抄数」交给 labloop，报告直接贴组会。 |
| 复现实验 | 用固定上下文（dataset/seed/epochs）保证可比性，变更被可比性守卫捕获。 |

典型命令：

```bash
labloop design "我要研究 batch size 对 YOLO 训练速度和准确率的影响" \
    --recipe yolo --var batch=8,16,32,64 --backend llm
labloop plan  <study>            # 先看要跑什么
labloop auto  <study> --yes --max-rounds 3
labloop report <study>
```

## 4. CLI 设计

基于 typer。命令：

```
labloop design "<问题>"     生成假设 + 第一轮矩阵，创建 study
labloop plan   <study>      dry-run：渲染本轮命令（不执行）
labloop run    <study>      执行本轮（需确认；--yes 跳过）
labloop analyze <study>     轮次分析 + 下一轮建议
labloop next   <study>      采纳建议 → 生成下一轮计划
labloop auto   <study>      全自动循环：执行→分析→下一轮→…→报告
labloop report <study>      生成/刷新报告（console + JSON + HTML）
labloop status <study>      查看 study 状态（轮次、run 数、最佳指标）
labloop version
```

`design` 参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--study <name>` | 由问题生成 slug | 指定 study 名 |
| `--recipe yolo\|generic` | `generic` | 配方；`yolo` 为内置示例 |
| `--recipe-file <path>` | — | 自定义配方 JSON |
| `--command "<模板>"` | generic 必填 | 命令模板，支持 `{param}` / `{run_id}` / `{round}` |
| `--metric "name=正则"` | generic 必填 | 从 stdout 解析指标，可重复；正则需带 1 个捕获组 |
| `--var name=v1,v2,…` | 见 §10 | 研究变量与取值，可重复；离线后端必填 |
| `--fixed k=v` | 见 §10 | 固定上下文，可重复（覆盖配方默认） |
| `--goal maximize\|minimize` | maximize | 主指标方向 |
| `--primary-metric <name>` | 第一个指标 | 主指标 |
| `--backend auto\|llm\|offline` | auto | auto = 配置了 LLM 就用 LLM，否则离线 |
| `--max-runs <N>` | 16 | 单轮 run 数上限（超出拒绝生成） |
| `--workdir <dir>` | `./.labloop` | 运行目录根 |

`run` / `auto` 参数：`--yes`（跳过确认）、`--round <N>`、`--max-runs <N>`、`--timeout <sec>`（单 run，默认 1800）、`--redo`（重跑已完成）、`--max-rounds <N>`（auto，默认 3）、`--budget-min <M>`（auto，总墙钟预算）、`--no-report`（auto 结束不生成报告）。

`analyze` / `auto` 参数：`--backend auto|llm|offline`（覆盖 study 记录的后端）。`analyze` 不带该参数时沿用 study 的后端；显式 `llm` 失败即报错，不静默降级（见 §10 降级边界）。

`next` 参数：`--force`（覆盖已存在的下一轮计划；默认拒绝覆盖）。

`--max-runs` 语义：**单轮 run 数上限**。一旦截断，`run` 打印剩余数量并可续跑；`auto` 直接停止（`stopped_reason=max_runs`），**不对半轮数据下结论**、也不进入下一轮。

**退出码**（稳定契约）：

| 码 | 含义 |
| --- | --- |
| 0 | 正常完成（含达到轮次/预算上限的正常停止） |
| 1 | 存在失败的 run（部分完成） |
| 2 | 未执行（确认被拒 / 非交互且无 `--yes` / 无待执行 run / 无下一轮建议 / 无结果可报告） |
| 3 | 内部错误 |
| 130 | 用户中断（子进程已终止，结果保留、可恢复） |

`design` / `plan` / `analyze` / `report` / `status` 成功即 0，错误 3。

## 5. 总体架构

```
       问题 (question)
          │
   ┌──────┴───────┐
   │  design      │  Hypothesis + ExperimentPlan（LLM 或离线后端）
   └──────┬───────┘
          │
   ┌──────┴───────┐
   │  recipes     │  参数 → 真实命令（模板渲染），指标解析规则
   └──────┬───────┘
          │  plan.json（dry-run 可先看）
   ┌──────┴───────┐
   │  确认门       │  交互 [y/N]；--yes 跳过；非交互无 --yes → 不执行(exit 2)
   └──────┬───────┘
          │
   ┌──────┴───────┐     ┌──────────────┐
   │  execute     │────▶│ run 目录      │ spec/command/stdout.log/result.json
   │ (串行+超时)   │     │ + 环境指纹    │
   └──────┬───────┘     └──────────────┘
          │  RunResult[]
   ┌──────┴───────┐
   │  analyze     │  findings / best / next / confidence / 可比性守卫
   └──────┬───────┘
          │
   ┌──────┴───────┐
   │  report      │  console + report.json + report.html(SVG)
   └──────────────┘
```

模块划分（`src/labloop/`）：

| 模块 | 职责 |
| --- | --- |
| `domain/` | pydantic 模型 + 枚举（Study / Hypothesis / ExperimentPlan / RunSpec / RunResult / RoundAnalysis / Report） |
| `recipes/` | 配方模型、内置配方（yolo/generic）、模板渲染、指标解析规则 |
| `design/` | DesignBackend 协议；LlmDesigner（严格 JSON + 降级）；HeuristicDesigner（网格 + 模板假设） |
| `execute/` | runner（子进程/超时/日志/resume）、metrics（stdout 正则、CSV 末行、JSON）、envrecord（环境指纹） |
| `analyze/` | analyzer（聚合 + 可比性守卫）、heuristics（边界外推/区间收窄）、llm（分析后端） |
| `report/` | console、HTML（手写 SVG）、JSON 写出 |
| `cli/` | 命令接线、确认门、预算、中断处理 |

## 6. 运行生命周期

`labloop auto` 的完整生命周期：

1. **load**：读取 study；校验配方与参数矩阵。
2. **loop**（最多 `--max-rounds` 轮，受 `--budget-min` 约束）：
   - **plan**：本轮 RunSpec[]（首轮来自 design；后续来自上一轮分析建议）。
   - **gate**：首轮执行前打印计划摘要（run 数、命令模板、超时、cwd、预算），等 `[y/N]`；非交互且无 `--yes` → 不执行任何 run，明确提示，exit 2。
   - **execute**：串行执行每个 run：渲染命令 → 写 `spec.json`/`command.txt` → 子进程（shell）→ 超时终止 → 指标解析 → 写 `result.json`。已完成的 run 跳过（resume）；`--redo` 全部重跑。
   - **analyze**：聚合本轮 results → `analysis.json`（含可比性检查与下一轮建议）。
   - **next**：若有建议且未达上限 → 生成下一轮 plan；否则停止（收敛/无建议/达上限）。
   - **截断**：本轮被 `--max-runs` 截断时**立即停止**（`stopped_reason=max_runs`）：不为半轮数据写 analysis、不进入下一轮；重新 `auto` 会续跑本轮剩余 run。
3. **report**：生成 report.json / report.html（`--no-report` 可跳过），控制台打印结论。
4. **exit**：按 §4 退出码。

**中断**：执行中 Ctrl+C → 终止**整个进程树**（见 §11）→ 该 run 记为 `interrupted`（不算完成，重跑时重试）→ 保留日志与已完成结果 → exit 130。

**断点续跑**：run 是否完成以 `result.json` 中 `status=ok` 为准；`labloop run` 重入天然续跑。

**旧结论作废**：一轮里只要**真的执行了 run**（不是 resume 跳过），该轮的 `analysis.json` 会被删除并提示「旧分析作废」——结果变了，结论就不能继续被引用（`report` 会显示「未分析」）。

## 7. 本地目录协议

```
<workdir>/                                  # 默认 ./.labloop
└── studies/
    └── <slug>/                             # study 目录（slug 由问题或 --study 生成）
        ├── study.json                      # 研究问题、假设、配方、变量、固定上下文
        ├── recipe.json                     # 配方快照（执行/分析时使用，保证可复现）
        ├── rounds/
        │   └── round-001/
        │       ├── plan.json               # 本轮 RunSpec[]
        │       ├── runs/
        │       │   └── <run-id>/           # 例：r001-batch-8
        │       │       ├── spec.json
        │       │       ├── command.txt
        │       │       ├── stdout.log      # 原始输出（UTF-8→GBK→replace 解码）
        │       │       └── result.json
        │       └── analysis.json
        ├── report.json
        └── report.html
```

- 所有落盘 UTF-8（`\n`），即使控制台是 cp936。
- run 目录只增不改；重复执行不覆盖已完成 run（`--redo` 除外）。
- 实验的 cwd 默认是**调用 labloop 的当前目录**（数据/配置相对路径按用户直觉工作），记录在 study.json。
- **study 名称 = 单一目录名**：`--study` 与命令行里的 study 参数都会校验（拒绝 `..`、路径分隔符、绝对路径、Windows 保留名、结尾的 `.`），产物不会写到 `<workdir>/studies/` 之外。
- **design 要么完整要么不留**：第一轮 plan 先完整渲染成功，再写 study.json / recipe.json / plan.json；中途失败会清理已写文件，不留半成品 study。
- **next 不静默覆盖**：目标轮次的 plan.json 已存在时拒绝生成（`--force` 可覆盖），避免已跑过的 run 变成孤儿数据。

## 8. 数据协议

模型见 `domain/models.py`，schema 导出到 `docs/schemas/`。

### 8.1 Study（study.json）

```jsonc
{
  "schema_version": "1.0",
  "slug": "batch-size-yolo-speed-accuracy",
  "question": "我要研究 batch size 对 YOLO 训练速度和准确率的影响",
  "created_at": "2026-09-22T22:00:00+08:00",
  "recipe_id": "yolo",
  "backend": "llm" | "offline",
  "goal": "maximize",
  "primary_metric": "mAP50",
  "secondary_metrics": ["mAP50-95", "duration_s"],
  "parameter": { "name": "batch", "values": ["8", "16", "32", "64"] },
  "fixed": { "data": "coco128.yaml", "model": "yolov8n.pt", "epochs": 50, "seed": 42 },
  "hypothesis": {
    "statement": "在一定范围内增大 batch 可提升吞吐，但过大会损失 mAP",
    "variable": "batch",
    "expected_effect": "non_monotonic",
    "rationale": "……",
    "source": "llm" | "offline" | "user"
  },
  "cwd": "C:/projects/yolo-demo",
  "runs_planned": 4
}
```

### 8.2 RunSpec（plan.json 的元素 / spec.json）

```jsonc
{
  "id": "r001-batch-8",
  "params": { "batch": "8" },
  "command": "yolo train data=coco128.yaml model=yolov8n.pt epochs=50 batch=8 seed=42 project=runs name=labloop-r001-batch-8",
  "timeout_s": 1800
}
```

### 8.3 RunResult（result.json）

```jsonc
{
  "run_id": "r001-batch-8",
  "params": { "batch": "8" },
  "status": "ok" | "failed" | "timeout" | "interrupted",
  "exit_code": 0,
  "started_at": "…", "ended_at": "…", "duration_s": 412.5,
  "metrics": { "mAP50": 0.831, "mAP50-95": 0.612 },
  "log_path": "stdout.log",
  "env": {
    "platform": "Windows-11", "python": "3.11.9",
    "packages_hash": "ab12cd34ef56",     // 解释器包集合指纹（envdoctor 同款思路）
    "gpu": { "name": "NVIDIA GeForce RTX 3090", "driver": "551.61" }   // 采集不到则 null
  },
  "error": null
}
```

### 8.4 RoundAnalysis（analysis.json）

```jsonc
{
  "round": 1,
  "generated_by": "llm" | "offline",
  "summary": "batch=32 的 mAP50 最高（0.844），64 回落；最佳点不在边界",
  "findings": [
    { "kind": "best", "detail": "batch=32 → mAP50 0.844" },
    { "kind": "trend", "detail": "8→32 单调上升，32→64 回落" }
  ],
  "best": { "param": "batch", "value": "32", "metric_value": 0.844 },
  "boundary": false,
  "next": { "param": "batch", "values": ["24", "48"], "rationale": "向两侧收窄" },
  "confidence": "high" | "medium" | "low",
  "comparability": { "ok": true, "issues": [] },
  "fallback_reason": null
}
```

### 8.5 Report（report.json）

```jsonc
{
  "schema_version": "1.0",
  "tool": { "name": "labloop", "version": "0.1.0" },
  "study_slug": "…", "question": "…",
  "generated_at": "…",
  "rounds": [ { "round": 1, "analysis": { "…": "RoundAnalysis" },
                "runs": [ { "…": "RunResult + params" } ] } ],
  "best_overall": { "round": 1, "param": "batch", "value": "32", "metric_value": 0.844 },
  "stopped_reason": "converged" | "max_rounds" | "max_runs" | "budget" | "no_suggestion" | null
}
```

## 9. 配方（recipes）

配方是一个 JSON 对象（内置或 `--recipe-file` 提供）：

```jsonc
{
  "id": "yolo",
  "description": "Ultralytics YOLO 训练（示例配方）",
  "command": "yolo train data={data} model={model} epochs={epochs} batch={batch} imgsz={imgsz} seed={seed} project={project} name=labloop-{run_id}",
  "parameters": { "batch": { "description": "批大小", "values": ["8","16","32","64"] } },
  "fixed": { "data": "coco128.yaml", "model": "yolov8n.pt", "epochs": 50, "imgsz": 640,
             "seed": 42, "project": "runs" },
  "metrics": [
    { "name": "mAP50", "kind": "csv_last", "file": "{project}/{name}/results.csv",
      "column": "metrics/mAP50(B)" }
  ],
  "timeout_s": 3600
}
```

- **模板占位符**：`{param}`（研究变量与固定项）、`{run_id}`、`{round}`、`{slug}`。渲染后即最终命令字符串。
- **指标来源 kind**：
  - `stdout_regex`：`{"pattern": "mAP50:\\s*([0-9.]+)"}`（1 个捕获组，取最后一次匹配）；
  - `csv_last`：读 CSV 最后一行指定列（列名精确匹配）；
  - `json_file`：读 JSON 文件取点路径（如 `results.final.mAP50`）。
- **duration_s 自动记录**（runner 计时，不依赖配方）。
- 内置 `generic`：由 `--command` / `--metric "name=正则"` 构造（kind=stdout_regex），`--var` 提供参数。
- 配方校验：渲染失败（占位符缺值）、指标规则缺文件/列 → 结构化错误，不静默。

## 10. 设计后端（design）

**契约**：`design(question, recipe, hints) -> (Hypothesis, parameter, values)`；
`analyze(round_results, study) -> RoundAnalysis`。
**研究变量（MVP）**：单变量——`--var` 给出多于一个变量时结构化报错（`TOO_MANY_VARIABLES`）；矩阵 = 该变量取值的枚举，超过 `--max-runs` 拒绝生成。

- **LLM 后端**：OpenAI 兼容 `POST {base_url}/chat/completions`；要求严格 JSON 输出并用 pydantic 校验；失败重试 1 次；仍失败 → 报错（显式 `--backend llm`）或降级到离线后端（`auto` 模式，并在 `fallback_reason` 记录原因）。输入中的家目录路径先脱敏。默认 `temperature=0.2`。
- **降级边界（ADR 0001 #4）**：
  - `--backend llm`（显式）→ LLM 不可用或输出非法 → **直接报错**（exit 3），错误信息给出替代方案（`--backend offline` / `--backend auto`），**不静默**换成离线结果；
  - `--backend auto` → 允许降级：设计阶段写进 `plan.notes`，分析阶段写进 `fallback_reason`；
  - `auto` 循环沿用 study 记录的后端时**允许降级**（`allow_fallback=True`）：整晚的实验不该因为 LLM 掉线而丢掉已跑完的结果，降级原因会出现在分析、报告与控制台；
  - `analyze` 未显式指定后端时沿用 study 的后端，**不降级**（study 声明了用 LLM，替换成离线结果属于静默偷换；要离线请显式 `--backend offline`）。
- **study.backend 记录实际产出假设的后端**：LLM 设计失败并降级时，假设来自离线规则，`study.backend` 就是 `offline`（降级原因留在 plan.notes 与控制台输出里），后续 `analyze` 默认沿用同一套方法。
- **离线后端**：
  - design：`--var` 或配方内置参数二者必有一（都缺 → 结构化错误，提示补 `--var batch=8,16,32,64` 或换 LLM 后端）；矩阵 = 该变量取值的枚举（run 数 > `--max-runs` → 拒绝并说明）；假设 = 模板「探索 {param} ∈ {values} 对 {metric} 的影响」，source=offline。
  - analyze：确定性规则（§12.3）。
- **backend=auto**：配置了 LLM（base_url + model + api_key 齐全）→ llm，否则 offline，并在 study.json 记录实际选择。

## 11. 执行（execute）

- **命令执行**：`cmd /c "<command>"`（Windows）/ `sh -c <command>`（POSIX），在 study.cwd 下；环境继承当前进程，并注入 `LABLOOP_RUN_ID` / `LABLOOP_ROUND`（便于实验脚本自识别）。
  - **Windows 引号**：cmd 会吃掉命令文本的首尾引号，Python 用 list 传参时又会二次转义引号 → 任何含引号的命令（典型如 `"C:\Program Files\…\python.exe" train.py`）都会被改坏。做法：命令外面**再包一层引号**，并以**原始字符串**（不是 list）交给 `Popen`，让 cmd 原样拿到命令文本（见 §22）。
- **密钥不下发**：实验子进程的环境里**不含** `LABLOOP_LLM_API_KEY`（`SECRET_ENV_KEYS`）。训练脚本即使打印环境变量也拿不到 labloop 的密钥。
- **串行**：一次只跑一个 run（GPU 独占是常态）。
- **超时**：默认 1800s（配方可覆盖）；超时 → **终止整个进程树** → 5s 宽限 → 强杀整棵树 → `status=timeout`。
  - 只杀 shell 是不够的：`cmd /c yolo train …` 的真正训练进程是孙进程，杀掉 shell 会让它继续占着显存。
  - POSIX：子进程独立进程组（`start_new_session`）→ `killpg`；Windows：`taskkill /PID <pid> /T /F` 走进程树。
  - Ctrl+C 走同一条路径：先结束整棵树，再写 `interrupted` 结果并退出 130。
- **输出**：stdout+stderr **流式**写入 `stdout.log`（边跑边写，内存不驻留全文）；编码在前 32KB 内嗅探（UTF-8 → GBK 回退，与 LabBox/envdoctor 同一教训），落盘统一为 UTF-8。
- **指标解析**：run 成功后按配方规则解析；`stdout_regex` 在**日志文件上分块扫描**（保留重叠窗口，跨块匹配也能找到），因此大日志不会撑爆内存；解析不到 → `metrics` 缺失 + `warnings` 记录，**不伪造**。
- **环境指纹**：每 run 记录 platform / python / 包集合哈希（importlib.metadata）/ nvidia-smi 摘要（若可用，含 3090 场景）。指纹用于 §12.2 可比性守卫。
- **预算**：`--max-runs`（本轮）、`--budget-min`（auto 总墙钟）在任何 run 启动前检查；预算耗尽 → 停止并且 `stopped_reason=budget`。

## 12. 分析（analyze）

### 12.1 聚合

按参数值分组（本轮所有 ok run），主指标 + 次指标表；单点/全失败场景都有明确输出（全失败 → 无建议 + 原因）。

**结论必须与实测对账**：最佳点与「是否在边界」永远由**本轮真实数据**推导（`pick_best` + 有序序列位置），后端（LLM）给出的最佳点/边界若与数据不符，会被**按数据修正**并记一条 finding；没有任何实测数据支撑时不给出最佳点。

### 12.2 可比性守卫（不静默比较）

以下任一不成立 → `comparability.ok=false` + `issues[]` + `confidence` 至少降一级：
- 所有 run 的固定上下文（fixed 参数 + 配方 id）一致；
- 所有 run 的环境指纹一致（platform / python / packages_hash / gpu.name）；
- ok run 数量 ≥ 2；
- 主指标在 ≥2 个 run 上解析成功（只有 1 个 run 解析出主指标时同样标记为不可比较）。

### 12.3 下一轮建议（离线确定性规则）

设参数取值为数值序列 `v1<v2<…<vn`（非数值 → 无法外推，`next=null` 并说明）：

| 场景 | 建议 |
| --- | --- |
| best 在边界（v1 或 vn） | 外推：按最小步长向该方向再加 1 个点（如 64 → 96 若步长 32；数值解析后按 1.5× 间距且取整） |
| best 在内部 | 收窄：在 best 与两个相邻值之间各取中点（8,16,32,64 best=32 → 24, 48） |
| 全部指标相同 / 只有一个点 / 全失败 | `next=null` + 说明（无可用建议） |

LLM 后端可给出同样结构的建议（仍受 `--max-runs` 与数值校验约束）；LLM 建议与离线规则都写入同一 `next` 字段，`generated_by` 标注来源。

**后端不得更换研究变量**：若 LLM 建议的 `next.param` 与 `study.parameter.name` 不一致，该建议被丢弃，记录一条 finding，并由离线规则重新给出建议（研究变量永远由 study 决定）。

## 13. 报告（report）

- **console**：study 摘要、每轮结果表（参数 × 主/次指标 × 耗时）、最佳点、下一轮建议、产物路径。
- **report.json**：§8.5 结构，UTF-8。
- **report.html**：jinja2 模板，**完全自包含**（无外部 CSS/JS/字体/图片）；每个指标一张手写 SVG 折线图（X=参数值，Y=指标，标注最佳点），下方是轮次明细表；适合贴 issue/组会。
- SVG 不引外部库：由 `report/svg.py` 直接生成字符串（轴、刻度、折线、点、标签）。

## 14. 技术栈

| 依赖 | 用途 |
| --- | --- |
| `typer` | CLI |
| `pydantic` v2 | 数据模型与 schema |
| `rich` | 控制台 |
| `jinja2` | HTML 模板 |
| `httpx` | LLM 调用 |
| `packaging` | 版本比较（保留给后续收敛判定） |

开发依赖：pytest / pytest-cov / ruff / pyright。Python ≥ 3.11。构建后端 hatchling。
**运行时不依赖 GPU、不依赖 ultralytics**（yolo 配方只是命令模板；环境里有没有 `yolo` 由执行时决定）。

## 15. 配置设计

查找顺序：`--config` > `./labloop.toml` > `%APPDATA%/labloop/config.toml`（或 `~/.config/labloop/config.toml`）。

```toml
[defaults]
timeout_s = 1800
max_runs = 16
max_rounds = 3

[llm]
enabled = false
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
model = "qwen3.8-flash"
```

环境变量（优先级最高）：`LABLOOP_LLM_BASE_URL` / `LABLOOP_LLM_MODEL` / `LABLOOP_LLM_API_KEY`。
密钥只从环境变量读取，**不落盘、不入日志、不入报告**。

## 16. 安全与隐私

- **执行第三方命令 = 以你的权限运行你的代码**：默认交互确认 + 明确文案；`--yes` 是显式知情选择；非交互无 `--yes` 一律不执行（exit 2）。
- labloop 自身不安装依赖、不改环境；实验命令里的环境影响由用户自己的命令决定。
- **预算护栏**：`--max-runs` / `--timeout` / `--max-rounds` / `--budget-min` 全部在执行前检查。
- **中断安全**：Ctrl+C → 终止子进程 → 结果可恢复；退出码 130。
- **LLM 数据边界**：只发送研究问题、假设、结果表格与环境摘要；家目录路径脱敏；环境变量与文件内容永不上传；密钥不入任何持久化产物。
- 无遥测。

## 17. 性能要求（软目标）

| 操作 | 目标 |
| --- | --- |
| `design`（离线） | ≤ 1.0s（纯内存 + 落盘） |
| `plan`（dry-run，16 run） | ≤ 0.5s |
| `analyze`（单轮 ≤16 run） | ≤ 0.5s |
| `report`（3 轮） | ≤ 1.0s |
| 执行本身 | 由用户命令决定 |

`scripts/bench_overhead.py` 输出实测对照。

## 18. 测试策略

| 层级 | 内容 | 运行条件 |
| --- | --- | --- |
| unit | 配方渲染与校验、指标解析（regex / csv_last / json_file）、runner（注入假执行层：成功/失败/超时/中断/resume/redo/预算）、envrecord、离线设计（矩阵/上限/缺 --var）、离线分析（边界/内部/单点/非数值/全失败）、可比性守卫、LLM 客户端（MockTransport：正常/垃圾 JSON/HTTP 错误/降级）、SVG 生成、report 写出、CLI 退出码映射 | 永远 |
| integration | 本地**假 OpenAI 服务器**（`http.server` 线程）驱动 design/analyze 的 LLM 路径；离线链路 CLI 端到端（design → plan → 注入合成结果 → analyze → report）；确认门非交互行为 | 永远（无外网） |
| slow | **plumbing**：真实子进程跑 2 行脚本（打印一行指标）验证 runner 管线（标注：这是管线测试，不是科学实验） | 默认跑（<5s） |
| gpu | 3090 真机：YOLO 实战 + Qwen LLM 后端 | 交接清单（§25.3），本机不可执行 |

**本机验收边界（用户决策 2026-09-22）**：本机（无 GPU）验收聚焦「设计 + 配置生成 + 报告 + 分析」；真实实验执行只在 3090 上发生。runner 的真实子进程路径用极小的 plumbing 脚本验证（非科学实验），YOLO/LLM 实战属于 3090 交接项。

## 19. 里程碑

| 里程碑 | 内容 | 完成标志 |
| --- | --- | --- |
| M1 | domain + paths/config/errors | 模型与落盘布局有测试 |
| M2 | recipes + design 双后端 | 渲染/校验/降级有测试 |
| M3 | execute（runner/metrics/envrecord） | 假执行层全场景 + plumbing 通过 |
| M4 | analyze 双后端 + 可比性守卫 | 四类规则用例 + 降级用例通过 |
| M5 | report（console/JSON/HTML+SVG） | 自包含 + 数值断言通过 |
| M6 | CLI 接线 + 闸门 + 文档 + CI | 全部质量闸门绿，verify_mvp 全 PASS/SKIP（有原因） |

## 20. MVP 完成定义（可执行标准）

以下 12 条全部有可执行证据才算 MVP 完成：

1. `design` 离线：问题 + `--var` → 假设与矩阵落盘，矩阵覆盖全部取值（确定性可断言）。
2. `design` LLM（本地假服务器）：真实 httpx 路径产出通过校验的假设/矩阵；服务器返回垃圾 → **显式 `--backend llm` 报错且不留 study**，`--backend auto` 降级到离线并记录原因。
3. `plan` dry-run：渲染精确命令且**绝不执行**（断言无 run 目录产生）。
4. `run` 的 resume 与 `--redo`：注入假执行 + 合成结果 → 二次 run 跳过已完成；`--redo` 全部重跑。
5. **plumbing**：真实子进程执行 2 行脚本 → `result.json` 含 `duration_s` 与解析出的指标（标注为管线验证）。
6. `analyze` 离线规则：边界外推 / 内部收窄 / 非数值 / 单点 四类用例输出确定性且符合 §12.3。
7. `analyze` LLM：假服务器通过；垃圾 JSON → 显式 `--backend llm` 报错、`auto` 降级 + 原因记录。
8. 可比性守卫：构造固定参数或指纹不一致的 run → `comparability.ok=false` 且 confidence 下降。
9. `report`：HTML 自包含（无外部引用）、含 SVG、`report.json` 可往返解析。
10. 确认门与退出码：非交互无 `--yes` → 不执行任何 run、exit 2、原因明确；中断 → 子进程终止、exit 130、已完成结果保留。
11. 预算与超时：`--max-runs` 与 `--timeout` 在假执行层被真实断言。
12. 质量闸门：`ruff` + `pyright`(src) + `pytest` 全绿；`scripts/verify_mvp.py` 逐条 PASS/SKIP（SKIP 必须带原因）。

## 21. 已定稿决策与剩余待确认问题

### 21.1 已定稿（2026-09-22，用户确认）

| 决策 | 选择 | 理由 |
| --- | --- | --- |
| 命名 | 文件夹 `Personal Agent Scientist`；包/命令 `labloop` | 名字直述核心机制：实验循环 |
| 本机验收边界 | **只生成不跑**：本机验收聚焦设计/配置/报告/分析；真实执行验收交 3090 | 用户选定；本机无 GPU，模拟实验没有意义 |
| LLM 定位 | 双后端：LLM（OpenAI 兼容）+ 离线启发式，无 key 全流程可用 | 用户选定；可测试、可离线、可降级 |
| 实验接入 | 通用命令 + 配方机制；YOLO 为内置示例配方 | 用户选定；不绑定单一框架 |
| 存储 | `.labloop/studies/<slug>/`，run 目录只增不改 | 与 LabBox 的 run 目录哲学一致 |
| LLM 降级边界（2026-10-20） | 显式 `--backend llm` 失败即报错；只有 `auto`（与 `auto` 循环）允许降级并记录原因 | 用户选定；显式请求被静默替换成离线结果属于「装懂」；但整晚的实验循环不该因 LLM 掉线而丢失 |

### 21.2 剩余待确认（不阻塞）

- 是否 `git init` 并首次提交（用户决定）。
- 3090 上的 YOLO 实战 + Qwen 后端验收（§25.3 交接清单）。

## 22. 开发约束（继承 LabBox/envdoctor 的实测教训）

1. **CJK + 空格路径**：本项目位于 `C:\code\github小项目\Personal Agent Scientist`，editable install 的 `.pth` 会因 locale 解码乱码 → 一律从源码运行（`scripts/labloop_dev.py` / pytest `pythonpath`）。
2. **本机无 GPU**：环境指纹中的 GPU 字段采集 `nvidia-smi`（缺失即 `null`，不是错误）；GPU 相关一切留给 3090。
3. **控制台 cp936**：CLI 入口 typer callback 里 `reconfigure(encoding="utf-8", errors="replace")`（envdoctor 的坑：dev shim 直接调 `app()` 会绕过 `main()`，所以放 callback）。
4. **PyPI 直连不可达**：开发期依赖用清华镜像（`UV_DEFAULT_INDEX=<mirror>`）。
5. **子进程输出永不假设 UTF-8**：UTF-8 → GBK → replace 三级解码（LabBox 教训）。流式写日志时改为「前 32KB 嗅探编码 + 增量解码」，落盘统一 UTF-8。
6. **Windows 命令行引号（2026-10-20 实测）**：`cmd /c` 会吃掉命令文本的首尾引号；Python 用 list 传参时又会把引号转义成 `\"` → 任何含引号的命令都会被改坏（`"C:\Program Files\…\python.exe" train.py` 直接报「不是内部或外部命令」）。正确做法：命令外面**再包一层引号**（`cmd /c "<command>"`）并以**原始字符串**交给 `Popen`。回归测试见 `tests/integration/test_command_quoting.py`。
7. **只杀 shell 是不够的**：`cmd /c yolo train …` 的训练进程是孙进程；超时/中断必须杀**整棵进程树**（POSIX 进程组 / Windows `taskkill /T /F`），否则显存不释放。回归测试见 `tests/integration/test_process_tree.py`。

## 23. 实现顺序

M1 domain/paths/config → M2 recipes → M3 design → M4 execute → M5 analyze → M6 report → M7 CLI → M8 闸门/文档。

## 24. 实现状态（MVP 完成定义逐条核验）

> 回填于 2026-09-22。验收脚本：`python scripts/verify_mvp.py`（`--skip-tests` 可在调用方已跑过 pytest 时使用）。

### 24.1 逐条核验（§20）

本机（Windows 11、无 GPU、无 LLM 端点，使用本地假 LLM 服务器）实测：**12 PASS / 0 SKIP / 0 FAIL**。

| # | 标准 | 结果 | 证据摘要 |
| --- | --- | --- | --- |
| 20-1 | design 离线 | PASS | 假设 source=offline、矩阵 = 3 取值、`recipe.json` 快照齐备 |
| 20-2 | design LLM + 降级 | PASS | 真 httpx 路径命中本地假服务器（hypothesis.source=llm）；垃圾输出 → 显式 llm 报错不降级、auto 降级为 offline 且 plan.notes 记录原因 |
| 20-3 | plan dry-run | PASS | 渲染 3 条命令、全程零执行（无任何 result.json 产生） |
| 20-4 | resume | PASS | 注入合成结果后 `run --yes` → exit 2「都已完成」（--redo 由单元测试覆盖） |
| 20-5 | plumbing（真实子进程） | PASS | `echo` 级命令 ×3 真实执行：stdout.log / duration_s / 指标解析全通（管线验证，非科学实验） |
| 20-6 | analyze 离线规则 | PASS | 内部收窄 [24,48]、边界外推 [40]、非数值与单点均无建议 |
| 20-7 | analyze LLM | PASS | MockTransport 命中；垃圾输出 → auto 降级（`fallback_reason`）+ 离线规则兜底；显式 llm 失败即报错 |
| 20-8 | 可比性守卫 | PASS | 包指纹漂移 → `comparability.ok=false` + 置信度 high→medium |
| 20-9 | report | PASS | HTML 自包含（无外链）+ SVG 最佳点标注；report.json 往返且 best 正确 |
| 20-10 | 确认门与中断 | PASS | 非交互无 `--yes` → 零执行 + exit 2；合成中断 → exit 130（interrupted 结果落盘由单元测试覆盖） |
| 20-11 | 预算与超时 | PASS | `--max-runs` 截断、超时 → `timeout` 状态、预算耗尽不启动新 run |
| 20-12 | 质量闸门 | PASS | ruff + ruff format + pyright(0 errors) + pytest 全绿 |

测试规模：76 项（单元 + 集成，含本地假 OpenAI 服务器端到端与 1 项 plumbing）。

### 24.2 性能实测（§17 目标对照）

`python scripts/bench_overhead.py`（取 3 次最小值）：

| 操作 | 实测 | 目标 |
| --- | --- | --- |
| design（离线，16 取值） | < 0.001s | ≤ 1.0s |
| plan 渲染（16 run） | < 0.001s | ≤ 0.5s |
| analyze（16 run） | < 0.001s | ≤ 0.5s |
| report（3 轮，JSON+HTML+SVG） | 0.011s | ≤ 1.0s |

### 24.3 本机无法执行 / 3090 交接清单

1. **YOLO 实战**：`--recipe yolo` 的完整循环（coco128 上 batch 8/16/32/64）需要在装有 ultralytics 的 3090 机器上跑；本机只验证了命令渲染（20-3）与通用执行管线（20-5）。
2. **Qwen LLM 后端实战**：本机用本地假服务器验证了 httpx 路径与降级逻辑；真实端点（base_url/model/key）在 3090 机器上配好后即为可用性交接项。
3. **GPU 指纹采集**：`nvidia-smi` 存在时环境指纹会记录 GPU 名称/驱动（本机无该命令，字段为 null，已由测试覆盖）。
4. **仓库尚未 `git init`**：是否初始化与首次提交由所有者决定。

### 24.4 与规格书的已知偏差（诚实性记录）

- 研究变量在 MVP 中限定为**单变量**（§10 已按此更新）；多变量矩阵留待后续。
- 离线 design 允许使用**配方内置参数**作为取值来源（`--var` 与配方参数都没有时才报错，§10 已更新）。
- 额外提供 `labloop version` 命令与 `--config` 选项（无破坏性）。
- 环境指纹不含内存信息（原计划中的 psutil 依赖已移除）。
- **固定上下文可比性尚未逐 run 记录**：`RunResult` 里没有 fixed/recipe 快照，§12.2 的第一条目前只能靠「同一个 study 共享一份 fixed」间接保证。
- **LLM 尚未实现「失败重试 1 次」**（§10），当前是失败即报错（显式 llm）或降级（auto）。

### 24.5 发布前审计（2026-10-20，批次 1）

上传 GitHub 前做了一轮「复现式」审计，实测发现并修掉的问题：

| # | 问题 | 修复 | 证据 |
| --- | --- | --- | --- |
| 1 | 实验子进程继承 `LABLOOP_LLM_API_KEY` | `build_child_env()` 剔除 labloop 密钥 | 子进程 `echo %LABLOOP_LLM_API_KEY%` 不再出现密钥 |
| 2 | `auto --max-runs` 带着半轮数据进入下一轮 | 截断即停止（`stopped_reason=max_runs`），不写 analysis | round-002 不再出现；重跑可续跑 |
| 3 | 可比性守卫漏报「主指标只解析出 1 个」 | 守卫接收 `primary_metric`，要求 ≥2 个 run 解析成功 | `ok=False` + issue |
| 4 | LLM 可以更换研究变量 | 丢弃该建议并回退离线规则，记录 finding | 建议被改写为研究变量的取值 |
| 5 | `--study` 路径穿越（`..\escaped`） | 名称校验（单一目录名） | 拒绝并报 `INVALID_ARGUMENT`，目录不落地 |
| 6 | `design` 失败留下半成品 study | 先渲染 plan 再落盘，失败清理 | 失败后无 study.json / 目录 |
| 7 | `next` 静默覆盖已有计划 | 拒绝覆盖，`--force` 才覆盖 | 第二次 `next` 报「已存在」 |
| 8 | 显式 `--backend llm` 失败会静默降级（与 ADR 0001 #4 不一致） | 显式 LLM 失败即报错（exit 3，附替代方案）；只有 `auto` 与 `auto` 循环允许降级 | 显式 llm + 垃圾输出 → 报错且不留 study；auto → 降级并记录原因 |
| 9 | 降级后 study 仍记「后端 llm」，且降级原因只在 plan.json 里 | `study.backend` 记录**真正产出假设的后端**；计划备注打印到控制台 | 降级后 study.backend=offline；控制台可见「LLM 降级：…」 |

### 24.6 发布前审计（2026-10-20，批次 2）

| # | 问题 | 修复 | 证据 |
| --- | --- | --- | --- |
| 10 | 超时/Ctrl+C 只杀 shell，训练进程继续占显存 | 杀整个进程树：POSIX 进程组 `killpg`、Windows `taskkill /T /F` | 心跳文件在超时后不再增长 |
| 11 | stdout 全量驻留内存（几小时训练日志） | 输出流式落盘；`stdout_regex` 在日志文件上分块扫描 | 30 万行日志仍能解析出最后一条指标 |
| 12 | **Windows 上含引号的命令全部被改坏** | `cmd /c "<command>"` + 以原始字符串传 `Popen` | `"C:\…\python.exe" "script.py"` 正常执行并解析出指标 |
| 13 | LLM 给出的最佳点/边界可能与实测不符 | 最佳点与边界永远由实测数据推导，不符则修正并记 finding | LLM 说 batch=64 最佳 → 报告按数据给出 batch=32 |
| 14 | `run --redo` 后报告仍引用旧结论 | 结果变更即删除该轮 analysis 并提示作废 | 报告该轮显示「未分析」 |
| 15 | `--goal maximizee` / `--backend llmm` 直接抛 Python 栈 | 参数统一校验，抛结构化 `INVALID_ARGUMENT`（exit 3） | 退出码 3 + 明确信息 |

回归：pytest 124 项全绿、ruff/format/pyright 全绿、`verify_mvp.py` 12 PASS / 0 SKIP / 0 FAIL。
