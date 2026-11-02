# 单次任务评分输入：eval_finance.case.v1

适配器把一次 harness 运行输出转换为 JSON 对象；评测调用方附加 benchmark 的原生参考输入。公共评分器只消费该对象，不启动任务，不解析 harness 私有日志，也不依赖运行目录布局。

```python
from eval_finance import BenchmarkRewardCalculator

case = adapt_output(harness_output)
case['benchmark'] = 'finance_complex_qa'
case['row'] = {'question': 'Original question', 'gold': 'Reference answer'}
reward = BenchmarkRewardCalculator('/path/to/evaluation.yaml').evaluate_case(case)
```

| 字段 | 类型 | 含义 |
|---|---|---|
| `schema_version` | string | `eval_finance.case.v1`；兼容不带版本号的原生输入对象 |
| `benchmark` | string | 九个已注册 benchmark 的标识；显式使用 `generic` 时需开启 `fallback_judge` |
| `question_id`, `entity_id`, `session_id` | string，可省略 | 样例、实体和运行标识；DDR 文件查询需要 CIK |
| `objective` | string，可省略 | 任务目标；有原生 `row` 时由原生题目决定 judge 问题 |
| `final_report` | string，可省略 | 原始自由文本答案；harness 自己清理运行协议标记。缺答使用各 benchmark 的原生缺答规则 |
| `rollout_status` | string | `FINISHED` / `TRUNCATED` / `ABORTED`；默认 `FINISHED` |
| `tools` | string[] | 允许的工具列表；非空时检查工具名称是否在列表内 |
| `trajectory` | object，可省略 | 下述标准轨迹。省略或 `{}` 表示没有工具交互／助手步骤 |
| `artifacts` | object，可省略 | 来源记录，以及可按需读取的 `final_report` 文本文件、`trajectory` 标准轨迹 JSON 文件 |
| `row`, `rubric`, `checklist` 等 | benchmark 原生格式 | 仅供评测的参考输入，详见 [README](README.md) |
| `row_file`, `rubric_file`, `qa_file`, `gold_file`, `task_file` | string，可省略 | 外部原生参考资源；显式路径以当前工作目录为基准，跨进程使用建议绝对路径 |
| `eval_params.config_file` | string，可省略 | 自定义评测 YAML；构造评分器时显式传入的路径优先 |

内联 `final_report` 和 `trajectory` 优先于 artifact 文件。其他 artifact 键只记录来源，公共包不会解析 `result.json` 或 `pi_events`。引用文件只在评分项需要时读取；未知 schema 版本会报错。缺失 trajectory 与提供了无法读取的 trajectory 路径有不同含义：前者是空轨迹，后者是输入错误。

## 标准轨迹

`trajectory` 包含四个列表。适配器只需提供启用项需要的部分；不需要的列表可以省略。

| 列表 | 消费方 | 语义 |
|---|---|---|
| `tool_calls` | `tool_protocol` | 助手声明的工具调用，保留重复／缺失 ID 和异常参数，用于协议检查 |
| `tool_executions` | `tool_protocol` | 按实际执行顺序排列的 start/end 记录；未完成调用只记录实际发生的部分 |
| `interactions` | DDR `trajectory_evidence` | 按原始调用顺序排列的已完成交互，包含触发调用的助手文本、参数和返回结果；不包含最终答案 |
| `assistant_steps` | DataClaw 过程项 | 按时间排列的助手回合，包括最终回答；同一回合的并行工具调用归属同一步，结果按 ID 关联 |

```json
{
  "tool_calls": [
    {"id": "c1", "name": "lookup", "arguments": {"year": 2024}}
  ],
  "tool_executions": [
    {"phase": "start", "call_id": "c1", "name": "lookup", "arguments": {"year": 2024}},
    {"phase": "end", "call_id": "c1", "name": "lookup", "result": {"revenue": 100}, "is_error": false}
  ],
  "interactions": [
    {"tool_call_id": "c1", "tool_name": "lookup", "tool_arguments": {"year": 2024},
     "assistant_message": "Retrieve revenue.", "tool_result": {"revenue": 100}, "is_error": false}
  ],
  "assistant_steps": [
    {"text": "Retrieve revenue.", "tool_calls": [{"id": "c1", "name": "lookup", "arguments": {"year": 2024}}],
     "tool_results": ["{\"revenue\": 100}"]},
    {"text": "Revenue was 100.", "tool_calls": [], "tool_results": []}
  ]
}
```

`tool_executions.phase` 必须为 `start` 或 `end`。end 记录中没有 `result` 表示缺失结果；`result: null` 表示存在空结果。执行错误通过 `is_error` 单独表达。适配器应保留实际记录，由公共包判断名称、参数、ID、执行配对和顺序的有效性。

DDR 的交互编号和 DataClaw 的助手步骤编号由公共包生成。适配层提供原始事实，不预计算工具协议分、截断内容、判断 rubric 或聚合奖励。结构化工具结果可以是任意 JSON 值；`assistant_steps.tool_results` 使用文本列表。

## 按需适配

标准 case 可在适配时一次性生成并落盘，随后由任意进程评分。如果输出很大，也可继承 `CaseInputs`，在 `read_report()` 或 `trajectory(evidence=False, protocol=False, steps=False)` 中按需转换，并调用：

```python
reward = BenchmarkRewardCalculator(config_path).evaluate_case(case, inputs=MyCaseInputs(case))
```

按需适配器必须返回相同的标准字段。`evidence` 请求已完成交互，`protocol` 请求声明和执行记录，`steps` 请求助手回合。评分器只在相应 component 或 DataClaw 条件分支适用时调用。普通 harness 接入直接产出 JSON case 即可。
