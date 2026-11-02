# FinanceAgent judge 协议

v1.1 和 v2 的 evaluator 版本为 `2.0.1`。这次修复只明确 judge 的输入编号、返回结构和错误诊断。

原版公开仓库 [finance-agent](https://github.com/vals-ai/finance-agent) 和 [finance-agent-v2](https://github.com/vals-ai/finance-agent-v2) 提供 agent、工具和任务数据，README 将评测接入指向 Vals 平台，公开代码中没有可直接复用的本地 judge 评分器。本实现使用原生 Rubric；现有本地评分规则分别由两个版本的 evaluator 维护。

原始 Rubric 列表的顺序决定零基字符串 ID。发送给 judge 时序列化为 `{"0": <原始条目>, "1": <原始条目>, ...}`，不改写条目中的 `criteria`、`operator` 或 `modifiers`，也不修改原始 row。题目、候选报告和原生参考输入仍只交给对应 judge。

judge 同时收到完整的响应 JSON Schema，明确每个必需 ID、值类型和顶层禁止额外字段的要求。返回值是直接按 ID 索引的对象，不套 `judgments` 等包装层：

```json
{"0": {"label": true, "reason": "..."}, "1": {"label": false, "reason": "..."}}
```

以上为 v1.1；v2 每项使用有限数值 `score`，范围为 `[0,1]`。`reason` 是诊断说明，不因缺少说明而额外改变内容得分。

编号必须完整、唯一，且严格匹配 `0` 到 `N-1`。不自动移动一基编号、不删除额外条目、不为缺项补分。缺项、额外项、包装层和错误类型仍使判定失败；错误会提供期望键及截断后的实际键，供现有重试流程纠正。重复 JSON 键仍由解析器拒绝。

v1.1 保留 correctness / contradiction 和全部通过才得分的规则；v2 保留 severity 加权、`must_pass` 门控和部分得分。零分是有效判定。工具协议项、奖励权重和归一化公式均未改变。

代码更新不会修改已运行的进程或镜像。部署时使用包含该版本的镜像，或只读挂载从原镜像导出并明确记录补丁文件与哈希的原生评分包。复评应保存到新的结果目录，并记录原报告、旧奖励哈希及 evaluator 版本。
