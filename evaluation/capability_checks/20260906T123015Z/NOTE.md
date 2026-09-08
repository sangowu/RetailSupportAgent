# Superseded first run (kept as evidence; do not cite its conclusions)

This run set `max_completion_tokens` to 512 in the effort-level probes, so
`reasoning_effort=low/medium` was rejected by the server with
"max_completion_tokens [512] must be greater than thinking_budget [8192/32768]".
That looks like "the model does not support those levels" but is in fact an insufficient
total output limit.

Why it is kept:

1. It is the original evidence for the methodological defect described in section 4 of
   `docs/M0.2_MODEL_CAPABILITY.md`.
2. Those two errors are the only source for the effort-to-budget mapping of
   `qwen3.7-max-2026-06-08` (low -> 8192, medium -> 32768).

Formal conclusions come from `20260906T123218Z/` only.
