# 已被取代的首次运行（保留作为证据，勿引用其结论）

本次运行的档位探针把 `max_completion_tokens` 设为 512，导致 `reasoning_effort=low/medium`
被服务端以 "max_completion_tokens [512] must be greater than thinking_budget [8192/32768]"
拒绝，**看起来像"该档位不支持"，实际是总输出上限不够**。

保留原因：

1. 它是 `docs/M0.2_MODEL_CAPABILITY.md` 第 4 节所述方法学缺陷的原始证据。
2. 那两条报错正是 `qwen3.7-max-2026-06-08` 档位映射预算（low→8192、medium→32768）的唯一来源。

正式结论一律以 `20260906T123218Z/` 为准。
