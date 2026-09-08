# Working rules

## Scope

- Everything in this repository is written in English: comments, docstrings, documents,
  commit messages, user-facing strings. Read `PROJECT_CONTEXT.md` and the current
  stage's document first.
- The roadmap is not authorization to implement every stage at once.
- Each development step changes one explainable theme. Do not mix changes to code,
  model, prompt and thinking configuration.
- Before modifying existing code, run the relevant baseline checks; afterwards verify
  and update the change report and the record of fixes.

## Experiment discipline

- The model and thinking budget are chosen by experiment, never defaulted to the
  highest tier.
- Keep the raw model configuration, the frozen simulated user, per-task results,
  failures, retries and usage.
- Compare every step against the frozen MVP and the previous passing version. Where no
  MVP exists yet, compare against the established M0 reference and state that it is not
  frozen.
- Record unsupported parameters, service errors and insufficient statistical evidence
  separately. Do not quietly change the model, drop failing samples, or re-run until it
  passes.
- Evaluation tasks and reference answers must not leak to the agent under test, and the
  evaluator must not be relaxed for a candidate implementation.
- Accept a new capability with new scenarios while checking the existing capabilities
  and performance. A hard safety failure cannot be offset by other metrics.

## Release and data

- Do not commit, push, create or merge a PR, or deploy cloud resources unless asked.
- Keys come from the environment or a secret manager only, and never enter Git, logs or
  reports.
- Use simulated business data only. Verify the standard τ²-bench environment and this
  project's own behaviour separately.
- Do not delete the first baseline, the current and rollback images, raw evaluation
  results, or any archive that is still needed.
- Keep free checks separate from billed model evaluations, and do not publish a report
  that is not needed.

## Document status

- Distinguish designed, implemented, tested and deployed.
- `docs/CHANGE_REPORT_TEMPLATE.md` is the per-step delivery template.
- `docs/EVALUATION_AND_GATES.md` is the source of truth for evaluation and gates; a
  threshold change must be versioned and re-verified.
