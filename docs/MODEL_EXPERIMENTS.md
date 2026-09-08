# Model selection and thinking-budget experiments

## Fixed candidate pool

Calls go through the OpenAI Python SDK's Chat Completions with base_url fixed to
`https://dashscope.aliyuncs.com/compatible-mode/v1`.

| Exact model id | Role |
|---|---|
| `qwen3.7-max-2026-06-08` | Original reference, and also a candidate |
| `qwen3.8-max` | Candidate |
| `deepseek-v4-pro-0813` | Candidate |
| `glm-5.2` | Candidate |
| `qwen3.8-flash` | Candidate |

A model's name does not decide the winner. An early reference is not a frozen MVP. What
is frozen at the end is the combination of an exact model or identifiable version, its
parameters, the prompt and the implementation.

## Documentation check, 2026-09-06

The following comes from reading Alibaba Cloud's documentation and was not verified
against the real API. Re-check the region, supply route and model version before
implementing. A same-named third-party direct model is not the same deployment.

| Family | Documented | Note for the experiment |
|---|---|---|
| Qwen3.8 Max/Flash | Chat docs list `low / medium / xhigh`, and also `thinking_budget` | The two cannot both be set; verify disabling thinking and the budget range by measurement |
| DeepSeek V4 Pro 0813 | Chat docs list `low / high / max` | Do not assume a separate `thinking_budget`; do not substitute a third-party-prefixed model id |
| GLM-5.2 | A thinking switch exists; the GLM page and the general Chat page describe effort levels differently | Keep the conflict on record and verify against the actual interface; accepting a parameter is not proof the level applied |
| Qwen3.7 reference version | General docs describe a thinking switch and budget | The exact dated version is still to be verified |

Official sources:

- [OpenAI-compatible Chat parameters](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)
- [GLM-specific parameters](https://help.aliyun.com/en/model-studio/glm)
- [GLM documentation, Chinese](https://help.aliyun.com/zh/model-studio/glm)
- [Base URL and regions](https://www.alibabacloud.com/help/en/model-studio/base-url)

How `max_tokens` and `max_completion_tokens` divide thinking from answer may vary by
model and configuration. The same number does not mean five models have the same
thinking budget. Requirements about retaining historical reasoning content also need to
follow each provider's protocol and be recorded, or the tool-call chain breaks.

## Capability checklist

Each model must have on record:

1. The exact request id, the response model id, the endpoint, the region, the date the
   documentation was checked, and the supply route.
2. Whether the account can call it. If not, keep the error type rather than switching
   model or enabling a service.
3. Whether `tools`, argument types, multi-turn tool results, streamed tool calls and
   usage all work.
4. The thinking switch, effort levels, budget cap, mutual-exclusion rule and mapping.
5. Whether temperature, top_p, output limits and seed are supported and what was actually
   sent, including when nothing was sent.
6. Finish reasons, truncation, timeouts, retries and the usage fields actually returned.
7. How input, output, thinking and cached tokens are billed, with the price source, date
   and currency.

Record rejected, ignored and remapped parameters separately. HTTP 200 is not proof a
budget limit took effect. Where the actual level cannot be verified, report the limits of
the evidence.

## Experiment order

### A. Fix the environment

Fix the code, policy, data, tools, tasks, simulated-user model and parameters, and the
evaluator. The first round uses a reasonable sampling setting each model allows, recorded
explicitly; a model that does not support temperature is not forced to take the same
value. Interleave the run order, and record server-side caching and rate limiting so no
model gets an exclusive warm-cache advantage.

### B. Small-scale screening

Take a fixed set of dev tasks stratified by business type and difficulty. Start each
model from no thinking or the lowest effective thinking configuration, checking success
rate, safety, truncation, cost and latency. Eliminate incompatible or clearly incapable
configurations first, rather than sweeping every parameter for every model.

### C. Level-by-level comparison

Hold everything else fixed and raise the thinking budget or effort step by step. Record
whether each step completes more tasks correctly or only adds time and tokens. Widen the
sample at a higher level only when the gain is worth further verification. The highest
level may be a control, never a default.

Test semantic modes and levels first, then fine-grained budgets on the shortlisted
configurations, never mixing mutually exclusive parameters. A thinking budget together
with a total output cap can cause truncation, and the reason must be recorded so
"not enough thinking" is distinguished from "not enough total output".

### D. Independent verification and freeze

Confirm the shortlisted configuration on tasks that were not used for tuning. Freeze the
simulated user and the evaluator so they do not change with the candidate agent. Repeat
runs across models and examine task-level paired differences and uncertainty.

Meet the business quality bar and the hard rules first, then compare cost per successful
task and P95, and pick one fixed default. Where several options have different strengths,
state the trade-off openly rather than inventing a single optimum. If no configuration
meets the quality floor, do not freeze an MVP and do not lower the bar to hide it.

## Later changes

- A model switch, a thinking change, a prompt change and a code optimization each get
  their own experiment report.
- Choosing a model or thinking budget per task is a separate M5 feature, compared against
  the fixed best combination and charged for the routing's own cost and errors.
- One failure does not justify raising the maximum budget unconditionally; the escalation
  rule itself needs testing.
- An aliased model can be updated server-side. Pin a snapshot id where one exists;
  where none does, record the time and response information rather than claiming full
  reproducibility.
- Do not use the independent holdout for tuning indefinitely. Failures already exposed
  move into the regression set, with unseen tasks added and the set versioned.

## Deliverables

The capability matrix, the actual parameters of each run, per-task raw results, the
price snapshot, why each configuration was eliminated, the comparison among shortlisted
combinations, the independent verification, and the reasoning behind the final choice.
Both the MVP reference and the final selection are kept; older experiments are not
overwritten.
