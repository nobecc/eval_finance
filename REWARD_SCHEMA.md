# eval_finance 加权奖励 schema v1

模型报告采用自由文本。评测器输出 `eval_finance.reward.v1` 格式的 JSON。每个 benchmark 独立负责原生参考协议、评审模型（judge）和指标公式；公共代码负责调度已启用的 component，并汇总它们对奖励的贡献。

## 奖励项、原生指标与运行元数据

`components` 保存可以独立配置权重的奖励项。`metrics` 保存本次评测产生的原生分数、原子判定、分组分数和诊断指标。`rollout_status` 描述 agent 的运行状态；顶层 `status` 描述评测是否成功。`judge` 记录调用次数、模型、重试次数、失败次数和耗时。`evaluator.id` 和 `evaluator.version` 标识评测实现及其版本，不再使用评分模式选择器。

| Component | 含义 | 支持的 benchmark | 奖励项分数范围 |
|---|---|---|---|
| `answer_quality` | 原生最终答案的正确性、完整性和 rubric（评分细则）满足程度 | 九个 benchmark，以及显式选择的 generic judge | DDR/generic 为 `[-1,1]`；其余为 `[0,1]` |
| `trajectory_evidence` | 已完成工具交互中证据的正确性与覆盖程度 | DDR-10K | `[-1,1]` |
| `tool_protocol` | 工具调用声明、参数、执行配对和返回结果的有效性 | 全部 | `[0,1]` |
| `goal_progress` | 已达成的标注里程碑占比 | DataClaw，答案未满分的任务 | `[0,1]` |
| `step_efficiency` | 参考步数／assistant 回合数比值的有界映射 | DataClaw，答案满分的任务 | `[0,1]` |
| `milestone_timeliness` | 已达成里程碑的平均时序衰减值 | DataClaw，答案未满分的任务 | `[0,1]` |

schema 统一的是记录结构，不要求每个 benchmark 实现全部 component。支持的 component 即使被关闭也会出现在结果中；不支持且权重为零的 component 则省略。给不支持的 component 配置正权重，会在读取参考数据或轨迹之前报错。

每个 component 都包含 `status`、`score`、`weight`、`score_range`、`contribution` 和 `detail`。其中状态可以是：

- `scored`：权重为正，分数为有限数值。
- `disabled`：权重为零，分数为 null，贡献为零；未执行计算。
- `not_applicable`：权重为正，分数为 null，贡献为零；原生协议不对该样例计算此项，例如 DataClaw 的条件分支。
- `failed`：必需的计算无法完成，分数和贡献均为 null。整个评测失败，不发布补造的奖励。

实际得到零分，与未计算或评分失败，是不同的状态。

## 调度与配置

```yaml
reward:
  weights:
    answer_quality: 1.0
    trajectory_evidence: 0.4
    tool_protocol: 0.1
    goal_progress: 0.0
    step_efficiency: 0.0
    milestone_timeliness: 0.0
  max_report_chars: 0
  max_message_chars: 16384
```

该示例启用 DDR 的报告 judge、逐消息 judge 和工具协议检查。将 `answer_quality: 0` 可关闭答案 judge，只对轨迹证据进行 judge 评分；将 `trajectory_evidence: 0` 可关闭轨迹证据 judge，只对答案进行 judge 评分。评分逻辑不会再根据模式覆盖权重。权重必须为有限的非负数，且至少有一项为正。未知名称和旧配置键会直接报错。

权重为零的 component 不会读取其专属参考资源、解析 judge 环境变量、构造 judge 上下文或发出 judge 请求。所有依赖轨迹的项都关闭，且输入已直接提供答案时，不加载轨迹。仅评价工具协议时，不读取报告、benchmark 参考数据、命令行 `--row`／`--rubric` 文件，也不加载 judge。已启用的项需要公共输入时，仍会读取这些输入；原生诊断指标复用已有判定，不增加额外调用。

配置优先级从低到高依次为：仓库中的公共默认值、benchmark 默认值、自定义 YAML 的全局配置、指定 benchmark 的覆盖配置。权重映射按 component 名称合并。自定义全局配置中显式设置的零权重会覆盖 benchmark 默认值，包括 DDR 默认的 `trajectory_evidence: 0.4`。

资源按需解析，相对路径以提供该配置的 YAML 所在目录为基准；样例或命令行中显式指定的路径以当前工作目录为基准。只有实际发出 judge 请求时，才解析 judge 覆盖配置和环境变量引用。

## 原生评分与诊断指标

| Benchmark | `answer_quality` 的定义 | 保留在奖励项之外的原生指标 |
|---|---|---|
| DDR-10K | checklist 判定标签对应奖励的均值 | 报告侧和轨迹侧各自的数量统计、标签和消息归属 |
| FinSearchComp T2 / T3 | 原生二元 `answer_score` | 任务分组和原生判定 |
| FinanceComplexQA | 对照参考答案评分：0 / 0.5 / 1 | 参考答案评分判定 |
| FinanceAgent v1.1 | 本地正确性／矛盾检查全部通过才得分 | 单项通过率和判定细节 |
| FinanceAgent v2 | 关键项门控后的严重程度加权部分得分 | 门控前的部分得分、严重程度权重、全部通过判定和门控失败情况 |
| FinFIRST | 专家权重加权的原子 rubric 分数 | 原子项通过率、严格通过判定和三个能力分组分数 |
| FrontierFinance | 以查询日期为基准的评审团 rubric 满足率 | 各成员投票、成功返回的成员、严格通过和必需项通过判定 |
| DataClaw | 任务 rubric 总分 | 单项分数和已启用的过程指标 |

原生 rubric 权重和奖励 component 权重属于不同层次。FinanceAgent 的关键项门控、FinFIRST 的专家分值权重和评审团投票，都保留在各 benchmark 的评分逻辑内部。工具协议得分不会改变原生答案指标，因此内容得分为零的答案，仍可能在组合后的训练奖励中获得工具协议分。

FinFIRST 的来源核验已经计入其加权答案分；分组诊断保存在 `metrics.group_scores` 中，不作为第二个奖励 component。原生输出要求可以属于任务 rubric，但没有统一报告 JSON、标题或 FINISH 格式奖励。

DDR 保留 `CORRECT_INFO=1`、`INCORRECT_INFO=-1` 和 `INSUFFICIENT_INFO=-0.1`。失败的 QA 判定仍计入分母，但贡献为零；某个已启用评分侧的判定全部失败时，整个评测失败。`trajectory_evidence` 优先对 `trajectory.message_wise_insights` 中编号后的、模型原生逐轮 insight 评分，不包含最终答案，也不把原始工具参数或完整工具结果直接发送给 judge。每条 insight 使用 `text` 保存模型对上一轮工具结果的解释，并用 `tool_call_ids` 关联产生该观察的调用。没有显式 insight 的旧 harness 仍兼容标准化 `interactions`，但会在指标中记录 `context_source=raw_interactions`。支持／矛盾消息归属和来源保存在 `metrics.trajectory_checklist` 中。该项衡量证据质量与覆盖程度，不表示每增加一步所带来的边际奖励。

工具协议分数为 `valid_tool_calls / attempted_tool_calls`，没有调用时为零。检查内容包括 ID 唯一性、已提供注册列表时的工具名称合法性、参数是否为对象、执行顺序和配对、名称与参数是否一致，以及是否有返回结果。工具执行错误与协议错误分别记录。工具协议项关闭时，即使仍在评价轨迹证据，也会跳过这些协议检查。

## DataClaw 过程评分协议

过程项需要显式启用，默认仍只计算答案 rubric。Gold 注释可以来自 `process_gold`、原生记录中的 `milestone`／`steps`／`steps_num` 字段、显式指定的 `gold_file`，或原始任务文件头部 YAML 中的 `gold_file`；最后一种路径以 `assets_dir` 为基准解析。

一步对应一个 assistant 回合，包括最终回答回合。同一回合中的并行工具调用只算一步；工具结果按调用 ID 关联到对应回合。该协议不复用 DDR 按已完成工具调用编号的方式。可选的内容截断会记录在警告和 `metrics.truncated_step_indices` 中。

原生评分分支由答案得分决定：

- 满分（`outcome_score >= 1`）：只有 EE 适用，定义为 `EE = reference_step_count / assistant_step_count`。原生 EE 可以大于一。奖励映射为 `EE / (1 + EE)`，其中 EE=1 映射到 0.5；原始比值保存在 `metrics.execution_efficiency` 中。assistant 回合数为零时，EE 评测失败，不产生无限大或补造的效率值。
- 未满分：GPR 和 TPE 适用。GPR 为已达成里程碑数除以全部里程碑数。TPE 对已达成里程碑的 `gamma ** max(first_step - N, 0)` 取均值，其中 N 为参考步数，gamma 默认为 0.9。没有达成任何里程碑时，TPE 为零。TGPR 使用全部里程碑数作为分母，在启用时序项时作为派生诊断指标保留，不作为额外的重叠奖励项。

已启用的 GPR 和 TPE 共用一次里程碑 judge。缺失或重复的键、非布尔类型的达成判定，以及无效的必需首次达成步骤索引，都会导致判定失败。不会为缺失的首次达成步骤补造“按时达成”的分数。仅启用 GPR 时，不计算 TPE，也不验证 TPE 所需的 gamma 和时间字段要求。

答案得分必须来自已启用的 `answer_quality` 评测，或来自同一样例先前原生答案评测得到的 `outcome_score`，后者仅用于评测。答案权重为零时，不会暗中调用答案 judge。缺少答案得分这一依赖时，评测失败。不适用的过程项跳过 gold 加载、步骤解析和里程碑 judge 调用。

## 聚合与分数范围

```text
raw_score = sum(score_i * weight_i for scored components)
normalized_score_10 = 10 * (raw_score - lower) / (upper - lower)
```

已关闭和不适用的项贡献均为零。原始奖励不再按旧模式的固定范围裁剪。通常情况下，上下界由各已启用 component 声明的上下界加权求和得到；它们由配置决定，不随候选答案实际获得的分数变化。因此 DDR 默认范围为 `[-1.4, 1.5]`，最高原始奖励 1.5 会被保留。

DataClaw 根据互斥的条件分支计算上界：

```text
lower = 0
upper = w_answer + w_protocol + max(w_EE, w_GPR + w_TPE)
```

这样可以避免将不能同时适用的分支最高分相加。不适用的项不会触发因候选样例而变化的重新归一化。上下界在评分前校验；无效的 component 分数会报错，不会被静默裁剪。

`rollout_status` 可以是 FINISHED、TRUNCATED 或 ABORTED。不存在暗中注入权重的 ABORTED 奖励项。可用的答案和轨迹输入都按同一套配置权重评分。缺少答案时，使用 benchmark 的原生缺答分数：DDR 使用信息不足标签，其余使用零分。judge 或基础设施故障属于评测错误，不作为模型的负奖励。

## 统一输出示例

```json
{
  "schema_version": "eval_finance.reward.v1",
  "status": "succeeded",
  "benchmark": "finance_complex_qa",
  "rollout_status": "FINISHED",
  "evaluator": {"id": "eval_finance.finance_complex_qa", "version": "2.0.0"},
  "raw_score": 0.5,
  "normalized_score_10": 5.0,
  "components": {
    "answer_quality": {
      "status": "scored", "score": 0.5, "weight": 1.0,
      "score_range": [0, 1], "contribution": 0.5,
      "detail": {"metric": "reference_grade_0_0.5_1"}
    },
    "tool_protocol": {
      "status": "disabled", "score": null, "weight": 0.0,
      "score_range": [0, 1], "contribution": 0.0,
      "detail": {"reason": "weight is zero"}
    }
  },
  "metrics": {"reference_grade_0_0.5_1": 0.5},
  "judge": {"model": "...", "models": ["..."], "calls": 1, "failed": 0, "attempts": 1},
  "warnings": [],
  "artifacts": {"reward_range": [0, 1]}
}
```

输入无效或必需的判定失败时，输出 `status: failed`，聚合分数为 null，命令行返回非零退出码。已获得的 component 状态和 judge 调用统计仍会保留。`metrics` 保存 benchmark 原生指标，`raw_score` 和 `normalized_score_10` 保存配置决定的组合奖励；比较结果时应核对数据范围、评分配置和 judge。

## 原 Pi reward v1 字段的迁移关系

- `answer_accuracy`／`final_report_accuracy` 改为 `answer_quality`。
- `step_value` 改为 `trajectory_evidence`。
- `format_reward` 改为 `tool_protocol`。
- FinFIRST 的 `source_verification` 移到 `metrics.group_scores.source_verification`。
- `completion` 移到顶层 `rollout_status`；删除 `process_reward`。
- 删除 `reward_mode`、`mode_effective`、旧的平铺权重配置键，以及固定的 `reward.min`／`reward.max`。
- 权重统一写在 `reward.weights` 下。消费方必须检查 `schema_version` 和 component 状态；此次迁移不会重写已有的 v1 奖励文件。
