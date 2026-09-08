# Per-step change report template

This file is a template, not a report. Copy it per step into its own version-tracked
report. Where an item does not apply, write "not applicable" and why. Never fill in an
invented number.

## 1. Change card (written before the work)

- Stage and step:
- Type: feature / optimization / fix / maintenance
- The specific problem or failing task:
- The single theme of this change:
- Control variables this change does not touch:
- Expected benefit and possible cost:
- Primary metric, protective metrics, acceptance thresholds:
- Baseline, previous version, task set and evaluation protocol version:

## 2. Implementation and verification

- Files changed and their responsibilities:
- Code, image and dependency identifiers:
- Model, actual parameters, endpoint, model id returned:
- Simulated user and evaluator configuration:
- Environment, concurrency, caching, repetitions:
- Checks before the change and the failures that already existed:
- Automated tests after the change:
- Verifiable evidence for the new capability or the fixed scenario:

## 3. Comparison

| Metric | MVP | Previous passing version | Current candidate | Difference vs previous, with interval | Difference vs MVP | Conclusion |
|---|---|---|---|---|---|---|
| Task success rate (with breakdown) | | | | | | |
| Safety violations / denominator | | | | | | |
| System-side P95 | | | | | | |
| Cost per task / per successful task | | | | | | |
| Capability added by this change | | | | | | |

Record the sample size, the number of successes and the path to the raw results.
Different environments or different task denominators are not directly comparable.
Reused test evidence must state why the image and inputs are equivalent. Where the MVP
is not yet frozen, write "not established" and list the M0 reference instead.

## 4. Failure analysis and conclusion

- Newly passing tasks, regressed tasks, and why:
- Whether parameters were ignored or remapped, whether output was truncated, whether the
  provider misbehaved:
- Statistical uncertainty and the limits of the conclusion:
- PASS / FAIL / INCONCLUSIVE:
- Why the change was accepted, rejected or held:
- Links to raw results, manifest, checksums and the Langfuse experiment:
- Whether a separate follow-up step is needed:

Do not omit failed runs, and do not present "a component was added" as an improvement.
Update last-accepted only when the protocol allows it, and never overwrite an existing
baseline.
