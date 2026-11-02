# Finance benchmark 任务与评测数据

整理日期：2026-10-13。已有任务与参考文件从 `dataelf_eval` 复制到此目录；缺失资源从官方 GitHub / Hugging Face 下载，版本固定在 commit。原目录保留，当前任务数据和输入材料均在此目录内。

| Benchmark | 可用任务数 | 数据范围与材料 |
|---|---:|---|
| `ddr_10k` | 100 | 实体列表、隐藏 QA checklist、完整 10-K SQLite 数据库 |
| `finsearchcomp_t2` | 219 | 完整发行版中的 T2；共享原生参考记录和市场快照 |
| `finsearchcomp_t3` | 172 | 完整发行版中的 T3；共享原生参考记录和市场快照 |
| `dataclawbench` | 492 | 原生 Markdown 任务、rubric、过程 gold 和实际 CSV/JSON 输入材料 |
| `finfirst` | 123 | 官方 V1 test split，包含 `rubric_annotated` |
| `finance_agent_bench_v1_1` | 50 | 官方仓库公开子集、答案及原生 Rubric；本地材料 50/50 |
| `finance_agent_bench_v2` | 27 | 官方仓库公开子集及带 modifiers 的原生 Rubric；本地材料 27/27 |
| `finance_complex_qa` | 2,026 | FinComplexQA-Pro 的 CN/EN task-category 视图，以及 PDF/LaTeX 材料 |
| `frontier_finance` | 220 | 已通过授权账号下载的官方数据，保留 query_date 和完整 rubrics |

九个 benchmark 合计 **3,429 条任务**。FinanceAgent 的私有 validation/test 集不在此目录中。FinanceComplexQA 的 scene-category 视图是同批题目的另一种分组，未重复计入；`overall` 是带 agent 输出的扩展分析数据，也未当作新增任务。FinSearchComp 完整原始文件中的 T1 不属于当前已注册评分项，因此保留原文件，但不生成 T1 任务索引。

## 文件组织

每个可用 benchmark 都有两个独立索引：

- `<benchmark>/tasks.jsonl`：`task_id`、`objective`、`input_files`；只包含任务与运行材料位置。
- `<benchmark>/references.jsonl`：按同一 `task_id` 对齐的 `eval_input`，保留原生评分字段；只供评测使用。

任务文件只把 `objective` 作为 agent 的任务消息。答案、checklist、rubric、DataClaw 的 gold 和原生 Markdown 中的评测段均留在评测侧。DDR 的任务消息仍是 `Analyze company with CIK <CIK>`。

FinanceAgent 两版另提供 `tasks.local.jsonl` 和逐题 `materials.jsonl`。本地材料包含 SEC 原文、业绩及并购附件、按日期过滤的 API JSON、历史价格和公司官方公告；每道题可单独只读挂载，官方在线任务索引保留原样。来源、时间边界、下载结果和使用方式见 [FinanceAgent 本地材料说明](finance_agent_materials/README.md)。

`input_files` 以本目录为基准。FrontierFinance 的任务索引还保留 `query_date`，供 harness 设置相对日期的基准；原生 query 文本保持原样。DDR 指向 `ddr_10k/raw/10k_financial_data.db`；DataClaw 还提供 `input_mounts`，保留原始 `database/...` 目标路径；FinanceComplexQA 的原始文档路径已解析为本地真实材料路径。原始数据和压缩包保留用于核对与重建。

```text
data/
  sources.json                 固定版本的来源、下载 URL 和校验值
  manifest.json                本地文件的 SHA-256、大小和数据集范围
  validation_report.json       数据完整性及原生评分输入检查
  download_report.json          下载状态与文件校验结果
  index_summary.json            每个 benchmark 的任务数量与可用状态
  eval_config.yaml              使用本地 QA、FinSearch 数据和 DataClaw assets 的配置
  ddr_10k/                      entity_ids.json、qa.json、raw/、两个索引
  finsearchcomp/                完整/AKShare 原生数据、native_rows.json、市场快照
  finsearchcomp_t2/              两个索引
  finsearchcomp_t3/              两个索引
  dataclawbench/                 tasks/、assets/、两个索引
  finfirst/                     test.jsonl、来源信息、两个索引
  finance_agent_bench_v1_1/      public.csv、public.txt、两个索引
  finance_agent_bench_v2/        public.csv、public.txt、两个索引
  finance_agent_materials/       共享原文、API/价格快照、来源与覆盖报告
  finance_complex_qa/           archives/、native/、两个索引
  frontier_finance/             官方原始 JSONL、来源信息、两个索引
```

## 接入单次任务评分

先运行 `tasks.jsonl` 中选定任务，再将 harness 输出适配为 [标准 case](../CASE_SCHEMA.md)，附加同一任务的参考输入：

```python
import json
from pathlib import Path
from eval_finance import BenchmarkRewardCalculator

data = Path('/path/to/eval_finance/data')
benchmark = 'finfirst'
task_id = '1'
references = (json.loads(line) for line in (data / benchmark / 'references.jsonl').open())
reference = next(item for item in references if item['task_id'] == task_id)

case = adapt_output(harness_output)  # harness 自己实现一次输出适配
case.update(benchmark=benchmark, question_id=task_id)
case.update(reference['eval_input'])
reward = BenchmarkRewardCalculator(data / 'eval_config.yaml').evaluate_case(case)
```

`eval_input` 可直接并入标准 case，不依赖旧 `dataelf_eval` 路径。FinSearch 原文件的 `prompt_id` 是字符串；派生 `native_rows.json` 和参考索引将其转为单元素列表，以支持评分器按题目 ID 查找，其余原生字段保持不变。FinanceAgent CSV 中的 Rubric JSON 字符串在参考索引中解析为列表，原 CSV 保持原样。

DataClaw 既可以使用参考索引中的内联 rubric/process gold，也可显式使用原生任务文件和 `eval_config.yaml` 中的 assets 路径。默认评分配置仍由公共包提供；本目录配置仅显式接入资源，不修改 component 权重。

## 下载、重建和验证

在工作区父目录执行：

```bash
python3 eval_finance/scripts/fetch_data.py
python3 eval_finance/scripts/prepare_data.py
python3 eval_finance/scripts/validate_data.py
```

下载器负责 `sources.json.downloads` 中的远程资产，跳过已符合大小和校验值的文件，支持续传；已有原生文件已复制在本目录中。默认不发起 gated 数据下载，已完整下载的 gated 文件会记录为 present。FrontierFinance 已补齐；重新下载时可使用 `HF_TOKEN`、`HUGGING_FACE_HUB_TOKEN` 或 `hf auth login` 的本地登录缓存，执行 `fetch_data.py --include-gated`，随后重建索引和验证。下载器只使用已有授权，不提交访问申请或接受条件。

FinanceComplexQA 的官方 ZIP 中部分中文文件名采用未标记的 GBK 编码，解包时已恢复为中文。只解出 QA JSONL 和 PDF/LaTeX 文件；编译日志、缓存和上游辅助代码不作为运行材料。`finARRAG-Metadata.zip` 保留完整原始归档，不并入当前任务集。

DataClaw 的输入文件以本地原仓库 commit 为准。8 个缺失 LFS 文件可由官方 HF 同内容对象取得；`company_core.csv` 与 HF 版本不同，下载原仓库精确 LFS 对象，全部按原指针 SHA-256 校验。没有用不同版本的文件替代。

验证覆盖文件哈希、下载大小、LFS 实体化、任务与参考一一对应、输入材料路径、原生评分输入解析，以及 SQLite 完整性和任务实体覆盖。原生输入检查使用空候选答案，不调用模型或 judge，不代表真实 benchmark 的端到端运行。

## 官方来源

- DDR：[任务与 QA](https://github.com/thinkwee/DDR_Bench)、[10-K 数据库](https://huggingface.co/datasets/thinkwee/DDRBench_10K)。
- FinSearchComp：[原始任务、参考和快照](https://github.com/randomtutu/FinSearchComp)。
- DataClaw：[原生任务与输入材料](https://github.com/GTML-LAB-sysu/DataClaw)、[官方 HF 同内容对象](https://huggingface.co/datasets/GTML-Lab/DataClaw)。
- FinFIRST：[官方 test split](https://huggingface.co/datasets/inclusionAI/FinFIRST)。
- FinanceAgent：[v1.1 公开数据](https://github.com/vals-ai/finance-agent/tree/8ba65f81ab759a8e0d44e72aabc5a47cf839d563/data)、[v2 公开数据](https://github.com/vals-ai/finance-agent-v2/tree/502aab6fdaa3fb9294905c7453f89882baa8d39b/data)。
- FinanceComplexQA：[官方数据集与全部原始归档](https://huggingface.co/datasets/Multilingual-Multimodal-NLP/FinanceComplexQA)。
- FrontierFinance：[访问条件与官方数据](https://huggingface.co/datasets/samaya-ai/FrontierFinance)。

各来源的固定 revision、原始文件来源、下载校验值和实际文件校验值记录在 `sources.json` 与 `manifest.json`；上游 LICENSE 和 dataset card 随数据保留。
