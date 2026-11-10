# eval_finance：独立的 Finance benchmark 评分

本包只对一次任务的输出评分。benchmark 的 rubric、judge 提示词、判定解析、原生指标、component 权重和聚合都由本包维护。它不启动 harness，也不读取任何 harness 私有事件格式；运行任务、输出解析和状态映射由 harness 的适配层负责。

调用关系：`harness 单次任务输出 → harness adapter → eval_finance.case.v1 → BenchmarkRewardCalculator → eval_finance.reward.v1`。

- Python API：`from eval_finance import BenchmarkRewardCalculator`，然后调用 `BenchmarkRewardCalculator(config_path).evaluate_case(case)`。
- 文件接口：适配器输出标准 case JSON，执行 `python3 -m eval_finance --case case.json`。
- Pi 适配器在 `pi-finance/eval/adapter.py`；Pi CLI 保留旧输出 `pi_finance.reward.v2`。直接调用本包返回 `eval_finance.reward.v1`，两者 component、metrics 和分数一致。
- 安装后的包可单独部署；不要求同级存在 `pi-finance`、`DataElf`、`dataelf_eval` 或 benchmark 原始仓库。

新 harness 的接入只需实现一次输出适配，并由评测调用方附加所选 benchmark 的原生参考记录或 rubric。参考答案和 judge 配置仍留在评测侧。完整字段、轨迹语义和最小示例见 [CASE_SCHEMA.md](CASE_SCHEMA.md)；可运行的接入示例见 [examples/custom_harness.py](examples/custom_harness.py)。

已整理的本地任务与评测数据见 [data/README.md](data/README.md)：九个 benchmark 共 3,429 条任务，分别提供任务与参考索引、原生文件和运行材料。FrontierFinance 已通过授权账号补齐 220 条任务。需要使用本地资源时，显式传入 `data/eval_config.yaml`；公共默认配置保持不变。

每个已注册的 benchmark 独立负责输入协议、评审模型（judge）提示词、评分结果解析和原生指标汇总。公共代码负责评测样例的输入输出、HTTP 请求、调用统计、工具协议检查，以及最终的 `eval_finance.reward.v1` 输出结构。已注册的任务缺少原生参考输入时会直接报错，不会回退到 DDR 评分或仅根据任务目标评分的 judge。

评测实现仅依赖本包代码、Python 标准库和 PyYAML。原生参考数据通过参数或配置显式指定，不依赖其他评测仓库的目录布局。

## 目录结构

```text
eval_finance/
  config.yaml                  公共请求配置和奖励默认值
  registry.py                  显式按 benchmark 分派评测
  reward_calculator.py         仅负责调度
  schema.py                    统一 component、聚合和序列化
  components.py                component 命名及各 benchmark 支持的评分项
  common.py, judge.py           输入输出、HTTP 请求和调用统计
  inputs.py                    标准任务输出的按需读取接口
  trajectory.py                标准轨迹格式化和工具协议检查
  benchmarks/<benchmark>/
    config.yaml                benchmark 自己的 judge 和奖励配置
    reward_calculator.py       benchmark 自己的评分逻辑
```

DDR 还拥有独立的 `judge.py` 和 `inputs.py`。FinSearchComp T2/T3 的原始评分协议相同，因此共享其公开的二元评分辅助逻辑；两者仍有各自的入口、配置和任务分组校验。FinanceAgent 的不同版本共享原生 rubric（评分细则）的输入解析和 judge 编号序列化，提示词和聚合规则分别实现；明确的输入编号及返回结构见 [FinanceAgent judge 协议](benchmarks/FINANCE_AGENT_PROTOCOL.md)。

## 已注册的评分方法及所需输入

| Benchmark | 内容评分 | 仅用于评测的必需输入 |
|---|---|---|
| `ddr_10k` | 基于 checklist 的报告质量，以及逐消息 QA 证据支持／矛盾判定 | CIK，以及原始 `qa_file`、`checklist` 或 `reward_rubric` |
| `finsearchcomp_t2` | 原生二元 `answer_score` | 原始数据集记录，或通过配置查找匹配的数据集记录 |
| `finsearchcomp_t3` | 相同的原生二元评分协议，并校验记录属于 T3 | 原始数据集记录，或通过配置查找匹配的数据集记录 |
| `finance_complex_qa` | 对照参考答案评分：0 / 0.5 / 1 | `gold` 或 `reference_answer` |
| `dataclawbench` | 任务 rubric 总分；可选 GPR / EE / TPE | 原生任务 rubric；过程项还需要 gold 注释和答案得分 |
| `finance_agent_bench_v1_1` | 正确性／矛盾检查全部通过才得分；单项通过率作为诊断指标 | 原始 `Rubric` 检查项列表 |
| `finance_agent_bench_v2` | 按严重程度加权的部分得分，受 `must_pass` 关键项门控 | 带 `modifiers` 的原始 v2 `Rubric` |
| `finfirst` | 专家权重加权的原子评分、严格通过判定和能力分组分数 | 原始 `rubric_annotated` 或带权重的 `rubrics` |
| `frontier_finance` | 以查询日期为基准的 rubric 满足率，以及按配置顺序进行的评审团多数投票 | 原生 `rubrics`、`query_date`，以及可选的 judge 评审团 |

所有 benchmark 都将答案主指标映射到 `answer_quality`。DDR 支持逐消息的 `trajectory_evidence`；DataClaw 还支持 `goal_progress`、`step_efficiency` 和 `milestone_timeliness`。公共的 `tool_protocol` 只评价工具调用。每个 benchmark 声明自己支持的 component 子集；给不支持的项配置正权重会报错。原生指标和判定细节保存在 `metrics` 中，包括已经计入 FinFIRST 主分的来源核验指标。运行完成状态通过顶层 `rollout_status` 表达。

权重为零会关闭对应计算和参考数据加载。component 记录区分已评分、已关闭、不适用和失败四种状态。报告和 judge 配置只在需要时读取。通过命令行指定的 benchmark 参考文件也按需加载，因此只评价工具协议时不需要原生数据集记录或 judge。

## 命令行

安装后可在任意目录运行：

```bash
python3 -m pip install -e /path/to/eval_finance
python3 -m eval_finance --list
python3 -m eval_finance --case /path/to/case.json --row /path/to/native-row.json
python3 -m eval_finance --case /path/to/case.json --output /path/to/reward.json
# 同一入口也提供 eval-finance 命令。
```

安装后可运行不需要 judge 的另一种 harness 接入示例：

```bash
python3 /path/to/eval_finance/examples/custom_harness.py \
  --input /path/to/eval_finance/examples/custom_output.json \
  --benchmark finance_complex_qa --case /tmp/example-case.json
python3 -m eval_finance --case /tmp/example-case.json \
  --config /path/to/eval_finance/examples/protocol_only.yaml
```

该示例只启用 `tool_protocol`，可验证输出适配与公共评分器的连接。启用内容评分时，附加对应 benchmark 的原生参考输入并配置 judge。

`--row` 接收恰好一条原生数据集记录，并保留其中的评分字段。提供记录后，judge 使用原始数据集中的问题。`--rubric` 接收原生 JSON 或文本评分细则；DDR 接收由 `{question, answer}` 对象组成的 QA checklist 列表，或本包定义的 `eval_finance.ddr_checklist.v1` 输入对象。`--qa-file` 仅用于 DDR。参考记录和 judge 配置不会进入 agent 提示词。

Judge 的 `max_retries` 是额外尝试次数。网络临时错误退避后重试；无效 JSON 或不完整判定要求重新输出完整 JSON；`finish_reason: length` 只在 `judge.max_tokens_limit` 大于当前预算时重试，预算翻倍并在上限停止，不接受截断判定。未配置该上限时，长度截断立即失败。`content_filter` 和鉴权等永久错误不重试。

`judge.timeout_s` 是初始生成预算下的单请求超时，随 token 预算增长；`judge.total_timeout_s` 限制一次逻辑判定的所有尝试，每次请求只分配剩余时间。两项长度和总超时选项均可通过 `EVAL_FINANCE_JUDGE_MAX_TOKENS_LIMIT` / `EVAL_FINANCE_JUDGE_TOTAL_TIMEOUT_S` 覆盖。`reward.json` 的 `judge.attempt_history` 记录每次预算、超时和失败原因。完整任务的外围 runner 仍应设置墙钟截止；FinFIRST 批处理已为评分子进程设置总预算加 60 秒清理余量。

DDR 的封装输入可通过 `--rubric` 提供，也可作为样例或原生记录的 `reward_rubric` 字段提供：

```json
{
  "schema_version": "eval_finance.ddr_checklist.v1",
  "kind": "checklist",
  "entity_id": "796343",
  "items": [{"question": "Reference question", "answer": "Reference answer"}]
}
```

`schema_version`、`kind` 和非空 `items` 必填。可选的 `entity_id` 必须匹配样例 CIK；可选的 `digest` 是规范化 QA 列表按键排序、紧凑 JSON、UTF-8 编码后的 SHA-256 前 16 位十六进制值。兼容旧的 `pi_finance.ddr_checklist.v1`；QA 列表也可直接使用。其他外部 rubric schema 不接受。

`--output` 指定结果文件；省略时将 JSON 输出到 stdout。退出码 0 表示评分成功；输入无效或缺失、必需的判定无法获得时，退出码为 1，输出 `status: failed` 并将分数设为 null。不识别的 benchmark 标识会直接报错。

### 原生评测样例

```json
{
  "benchmark": "finance_agent_bench_v2",
  "final_report": "The complete unmodified model answer",
  "rollout_status": "FINISHED",
  "row": {
    "Question": "The original dataset question",
    "Rubric": [
      {"operator": "finance_agent_v2_operator", "criteria": "Required fact",
       "modifiers": {"severity": 3.0, "category": "must_pass"}},
      {"operator": "finance_agent_v2_operator", "criteria": "Supporting fact",
       "modifiers": {"severity": 1.0}}
    ]
  },
  "tools": ["web_search", "fetch_content"],
  "schema_version": "eval_finance.case.v1",
  "trajectory": {}
}
```

FinSearchComp 的原生记录保留 `prompt_id`、`prompt`、`response_reference`、`judge_system_prompt` 和 `judge_prompt_template`。原生协议提供的误差容忍度和最终答案提取指令也会保留；本地实现没有沿用原始整数评分解析器的缺陷。

FinFIRST 接收其公开的编号 rubric 文本（包括多行英文及中文注释），或由 `{criterion, weight, group}` 对象组成的列表。文本中的分值权重总和必须为 100；缺失或格式错误的专家权重会被拒绝。最终答案 judge 不会读取工具轨迹来覆盖原子判定。

## 配置与judge

`eval_finance/config.yaml` 提供公共默认值。各 benchmark 的 `eval_finance/benchmarks/<name>/config.yaml` 分别配置资源、judge 和 component 权重。通过 `--config` 指定的 YAML 中，对应 benchmark 的配置段可以覆盖这些设置。相对资源路径以定义它们的 YAML 所在目录为基准解析；显式指定的命令行资源路径优先。

配置优先级从低到高依次为：公共默认值、benchmark 默认值、自定义全局配置、指定 benchmark 的覆盖配置。自定义全局配置中的零权重也会覆盖原生默认值。

DDR 的 `qa_file`、FinSearchComp 的 `dataset_file` 和 DataClaw 的 `assets_dir` 默认均为空。可直接提供 checklist、原生记录或过程 gold，也可在自定义配置中指定资源位置：

```yaml
ddr_10k:
  qa_file: /path/to/ddr-qa.json
finsearchcomp_t2:
  dataset_file: /path/to/finsearchcomp_data.json
finsearchcomp_t3:
  dataset_file: /path/to/finsearchcomp_data.json
dataclawbench:
  assets_dir: /path/to/dataclaw/assets
```

```yaml
reward:
  weights:
    answer_quality: 1.0
    trajectory_evidence: 0.0
    tool_protocol: 0.0
```

该配置只评价原生最终答案，不使用 `reward_mode`。如果只想评价 DDR 工具证据，将答案权重设为零、证据权重设为正值即可；评分逻辑不会再次覆盖该权重。权重全部为零、为负数、为非有限数值，或使用未知 component 名称时，都会报错。奖励上下界根据配置的 component 分数范围推导，不再采用固定的模式范围。

各 benchmark 的配置都可以提供 `judge: {model, base_url, api_key, ...}`。支持 `$ENV_VAR` 环境变量引用和 `EVAL_FINANCE_JUDGE_*` 覆盖；兼容旧的 `PI_FINANCE_EVAL_JUDGE_*`；DDR 还保留 `DDR_REWARD_JUDGE_*` 接口配置别名。HTTP 请求直接连接目标接口，忽略继承的代理设置。依赖仅包括 Python 标准库和 PyYAML。

Frontier 可以配置三个独立的、兼容 OpenAI 接口的 judge 成员：

```yaml
frontier_finance:
  judges:
    - model: $FRONTIER_JUDGE_1_MODEL
      base_url: $FRONTIER_JUDGE_1_URL
      api_key: $FRONTIER_JUDGE_1_KEY
    - model: $FRONTIER_JUDGE_2_MODEL
      base_url: $FRONTIER_JUDGE_2_URL
      api_key: $FRONTIER_JUDGE_2_KEY
    - model: $FRONTIER_JUDGE_3_MODEL
      base_url: $FRONTIER_JUDGE_3_URL
      api_key: $FRONTIER_JUDGE_3_KEY
```

失败的成员会被排除；票数恰好相同时，采用配置顺序中第一个成功返回的成员的判定。所有成员都失败时，评测报错。仓库默认配置的评审团列表为空，此时调用公共 judge 一次，并记录自定义单 judge 的警告；该配置不代表托管评测或独立三模型评审团。

`fallback_judge` 默认为 false。它只启用显式选择的 `benchmark: generic` 非 benchmark 评测器，不会为九个已注册 benchmark 提供回退评分。

## 奖励与验证

模型报告保持自由文本。judge 返回的 JSON 是机器接口，不是候选报告的格式要求。`tool_protocol` 只检查工具协议，不额外奖励公共报告 JSON、标题或字段格式。原生题目要求的精度和正确性仍属于对应 benchmark 的内容评分条件。

主指标和所有 rubric 诊断结果保存在 `metrics` 中；经过组合、变换的训练奖励分别保存在 `raw_score` 和 `normalized_score_10`。比较评测结果时应核对数据范围、judge 和评分配置；本地实现不调用 FinanceAgent 的专有托管检查项。

### DataClaw 过程项

所有过程项的默认权重均为零。启用时，需要提供包含 `steps`、可选 `steps_num` 和 `milestone` 的 `process_gold` 对象，或提供原始 `gold_file`。原生任务文件头部 YAML 中的 gold 路径以显式提供的 `assets_dir` 为基准解析；缺少该目录时会报错。

```yaml
dataclawbench:
  reward:
    weights:
      answer_quality: 1.0
      goal_progress: 0.3
      step_efficiency: 0.2
      milestone_timeliness: 0.1
```

答案 judge 决定原生过程评分使用哪个分支：满分答案计算 EE，未满分答案计算 GPR/TPE。不适用的项只记录状态，不读取 gold 或处理轨迹。答案权重为零时，需提供该样例先前答案评测得到的 `outcome_score`，该字段仅用于评测；评分器不会暗中调用答案 judge。

GPR/TPE 共用一次里程碑判定。每个 assistant 回合算一步，包括最终回答回合；同一回合中的并行工具调用只算一步。EE 的原生比值可以大于一，并保存在 `metrics` 中；奖励使用 `EE/(1+EE)`。TPE 要求有效的里程碑首次达成步骤索引。TGPR 是派生诊断指标，不是额外奖励项。公式及考虑条件分支的奖励上下界见 [REWARD_SCHEMA.md](REWARD_SCHEMA.md)。

```bash
python3 -m unittest discover -s /path/to/eval_finance/tests -v
```

参考来源：[DDR-Bench](https://github.com/thinkwee/DDR_Bench)、
[FinSearchComp](https://github.com/randomtutu/FinSearchComp)、
[FinanceComplexQA](https://github.com/buaacxf/FinanceComplexQA)、
[DataClaw](https://github.com/GTML-LAB-sysu/DataClaw)、
[FrontierFinance 评分器](https://github.com/samaya-ai/frontier-finance)、
[FinanceAgent v1.1](https://www.vals.ai/benchmarks/finance_agent)、
[FinanceAgent v2](https://www.vals.ai/benchmarks/fabv2)、
[FinFIRST 论文](https://arxiv.org/html/2609.25192v1)。

默认配置在包内；可通过 `--config`、API 参数或 `EVAL_FINANCE_CONFIG` 指定自定义配置。DDR QA 路径可通过 `EVAL_FINANCE_QA_FILE` 指定。新环境变量优先于旧 `PI_FINANCE_EVAL_*` 别名；两者都优先于 YAML。DDR 仍兼容 `DDR_REWARD_JUDGE_*` 和旧 `pi_finance.ddr_checklist.v1` 输入。
