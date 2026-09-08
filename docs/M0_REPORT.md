# M0 delivery report

**RetailSupportAgent** - a retail support agent (order lookup / order cancellation / return
request) that proves business correctness, reliability and cost through reproducible
evaluation.

Report date: 2026-09-07. Scope: M0.1 - M0.5. M0 is frozen here.

---

## 1. Conclusions on one page

| | |
|---|---|
| Benchmark | Sierra tau2-bench Retail, v1.0.1 `fc0055dc...0992`, all 13 files verified by SHA256 |
| **Standard score** | **0.8509 (97/114)** - full set of 114 tasks, **with upstream `llm_agent` as the subject under test** (see correction 1.1) |
| **Declared business subset** | **0.9286 (39/42)** - the 42 tasks covered by lookup / cancel / return |
| Independent verification score | 0.8750 (35/40) - holdout, not used for tuning |
| Model | `deepseek-v4-pro`, thinking effort=high, temperature 0.0 |
| Cost | **0.0855 CNY per task** (three roles combined), with 96% of input tokens on the subject side hitting cache |
| Baselines | `baseline-v2` (current for M0) plus `baseline-v1` (archived), with checksums verified by CI on every run. From M0.6, `baseline-v3` is the current baseline, with this project's agent as the subject |
| Engineering | 275 tests, ruff, mypy --strict, all three CI jobs green |

### 1.1 Correction: every M0 score measures the upstream agent, not this project's agent

**Added 2026-09-08.** M0's 0.8509 / 0.9286 / 0.8750 were produced by tau2-bench's own
`llm_agent`: the evaluation configuration hard-codes `agent="llm_agent"`, and the business
loop and write guard in this project's `src/retail_support_agent/agent.py` **never entered the
evaluation loop**.

So M0's scores can only be read as **"this evaluation apparatus scores 0.8509 on this model"**.
They measure the evaluation chain and the model, not this project's agent. Presenting them as
"my agent scored 0.8509" would be taking credit that is not ours.

This does not affect M0's other conclusions: the frozen evaluation stack, the cost
conventions, the judge noise and the baseline freeze are conclusions about the **apparatus**,
and the apparatus itself has not changed. Only the attribution of the score is affected.

M0.6 puts this project's agent into the evaluation loop as **a separate subject under test**,
run separately and frozen into its own baseline; see `docs/M0.6_AGENT_UNDER_TEST.md`. The two
subjects' results are presented side by side and never merged.

---

**What this project sets out to prove is not that it runs, but that it can prove it ran
correctly.** So every number in this report traces back to a specific run, and every
convention states what it cannot be used to claim.

---

## 2. Scores and conventions

### 2.1 Two conventions, both reported, neither standing in for the other

This project declares support for three kinds of business, which cover only **42** of the 114
tasks (8 read-only plus 34 in-scope writes). The other 72 involve order modification,
exchange, address change and payment change, which this project **does not declare**.

| Layer | Score |
|---|---|
| Read-only (lookup) | 8/8 = 1.0000 |
| In-scope writes (cancel / return) | 31/34 = 0.9118 |
| Out-of-scope writes (modify / exchange / address / payment) | 56/70 = 0.8000 |
| No scored action upstream | 2/2 = 1.0000 |
| **Declared business subset (42 tasks)** | **39/42 = 0.9286** |
| **Full set (standard tau2-bench Retail)** | **97/114 = 0.8509** |

The convention was locked **before any candidate result existed** (2026-09-06): run the full
set, report layered by label. The reason for locking it is to prevent picking the flattering
framing after seeing results.

The 70 out-of-scope tasks are 61% of the total and are exactly what pulls the score from
0.9286 down to 0.8509. **Reporting only 0.9286 is cherry-picking a convention**; reporting
only 0.8509 hides how the declared business actually performs.

### 2.2 Scoring is all or nothing

tau2-bench's reward is the product of its components: if either the programmatic comparison of
the final database state or the NL assertion (an LLM judge) fails, the whole task drops from
1.0 to 0.0. **Noise is not averaged out, it is amplified**, which is why this project spends
so much effort on the judge.

### 2.3 How the three scores relate

| Number | What it is | What it is not |
|---|---|---|
| 0.8509 (114 tasks) | The standard tau2-bench Retail score | Not "performance on the declared business" |
| 0.9286 (42 tasks) | Performance on the declared business | **Cannot** be called a tau2-bench score |
| 0.8750 (40 tasks) | Independent verification not used for tuning | Not a full-set score; a smaller sample |

0.8509 being below 0.8750 is **not a regression**: the conventions differ (the full set
includes the 74 dev tasks), the stack and model are the same, and a gap of 0.024 is sampling
variation.

The three numbers share a premise that must be read with them: **their subject under test is
upstream's `llm_agent`** (section 1.1). They are comparable with each other because the
subject is the same, and they **must not be averaged together** with this project's agent's
results from M0.6 onward.

---

## 3. The preconditions for reproducibility (M0.1)

A score can only be believed if someone else can produce the same number.

- **Upstream is pinned to a commit**, with SHA256 recorded for all 13 files and verified by CI
  after every fetch. The digests cover the bytes as upstream committed them, so Windows and
  Linux produce the same values.
- **The task splits are upstream's own train/test**, not a random split of ours - a "custom
  split" is a standing risk of incomparable scores. dev 74 / holdout 40, disjoint, pinned by a
  test.
- **The three roles are separated onto their own endpoints**: the agent under test (DeepSeek),
  the simulated user (Zhipu) and the NL judge (DashScope). Models from the same family may
  favour their own, and mixing them would contaminate the conclusion.
- **The run manifest and per-task results have schemas** that require recording the code
  commit, whether the working tree was dirty, the upstream commit, the model id requested and
  the one returned, and whether each parameter was accepted or ignored.

---

## 4. Model selection: a cost decision, not a quality comparison

**This has to be stated first, because it determines what this report can claim.**

`deepseek-v4-pro` is the **only** model tested on the final evaluation stack. Every number for
the other candidates is void, not because they performed badly but because **those runs used
two different judges** (`deepseek-v4-flash` and `qwen3.7-flash`), and numbers from two
different rulers cannot be compared.

So the selection rests on one argument that holds up: **a 4.8x cost difference, an entirely
different range.**

| Same 40 holdout tasks | deepseek-v4-pro | qwen3.8-max |
|---|---|---|
| Cost per task | 0.0806 CNY | 0.3877 CNY |

> Holdout numbers are used here rather than full-set ones because qwen3.8-max has only ever run
> holdout. Comparing its holdout cost with deepseek's full-set cost (0.0855 CNY) would mix two
> different runs. Only the same set of tasks is comparable.

The decision is **not to run the missing measurement**: even if qwen3.8-max were better, the
cost is in a different range. This is an explicit trade-off, written down rather than hidden.

### 4.1 Why the early comparisons cannot support a conclusion

- The 12-task screening round: all 10 pairwise comparisons were **not significant**, with a
  minimum p of 0.375.
- The 40-task holdout round: the closest to significance was p = 0.077, still not significant.
- **The judge's own noise is 8%** (re-judging the same conversations three times with the same
  judge, 2 of 25 tasks contradicted themselves), the same order of magnitude as the gaps
  between models.

In other words: even when a gap appears, it cannot be separated from judge jitter. Writing
such a gap up as "A beats B" would be fabrication.

---

## 5. The business loop and the guard (M0.3)

Look up the order -> ask what is missing -> apply the policy -> **explicit confirmation** ->
verify -> execute -> explain the real result.

The required limits, each covered by a test:

- **Order ownership check**: an order that is not the user's own is always refused.
- **Write confirmation is bound to specific arguments**: the confirmation fingerprint is the
  SHA256 of `tool name + canonical arguments`. If the model changes any argument after
  confirmation (a different tool, a different order id, one extra field), the fingerprint no
  longer matches and it must be confirmed again. **The model claiming "the user agreed" does
  not count.**
- **Unknown tools are refused**: anything outside upstream's list of 16 tools does not run.
- **Stop conditions**: at most 20 steps, 15 tool calls and 180 seconds of session time; once a
  limit is reached **no further model request is sent**, so nothing is billed pointlessly.

### 5.1 Measured evidence from the demo flows

`uv run python scripts/demo_flows.py` (archived under `evaluation/demo_runs/`):

| Flow | Per-turn state | Tools executed | Database |
|---|---|---|---|
| Lookup | answered x2 | get_user_details, get_order_details x4 | unchanged |
| Cancel | answered -> **needs_confirmation** -> answered | get_order_details, cancel_pending_order | **changed** |
| Return | answered -> **needs_confirmation** -> answered | get_order_details, return_delivered_order_items | **changed** |
| Out-of-scope | answered x2 | get_order_details | unchanged |

The archive records **the database fingerprint before and after** the operation. That is
deliberate: the model saying "cancelled" does not count, and an unchanged fingerprint shows the
write never landed. On the out-of-scope flow the fingerprint stays identical even after the
user adds "confirm the cancellation".

---

## 6. Cost conventions

| Role | Cost (one full-set run) | Note |
|---|---|---|
| Agent under test | 9.2245 CNY | 96% of input hits cache, priced at 1/30 of full |
| Simulated user | 0.4947 CNY | 35% of output is reasoning tokens, billed at the output price |
| NL judge | 0.0223 CNY | Its calls are not in messages and must be accounted separately |
| **Total** | **9.7414 CNY** | **0.0855 CNY per task** |

Three costs that were previously missed and that measurement proved would change the
conclusion: **cache hits** (ignoring them overestimates by 5x), **reasoning tokens** (ignoring
them underestimates the simulated user), and **the judge** (never counted unless counted
deliberately).

Cost has **two entirely independent computation paths**: upstream litellm accumulating per
call, and our own conversion of total tokens at the price snapshot. The two are exactly equal
(9.224503 CNY), which is not a coincidence but the verifiability bought by registering CNY
prices into litellm.

**The Langfuse dashboard deliberately does not show cost.** The usage it receives has no cache
breakdown, and converting at full price overestimates by about 5x - a plausible-looking wrong
number is far more dangerous than a conspicuous 0. Cost comes from `scripts/analyze_eval.py`
alone, and the convention is recorded in the manifest, the startup log and a test.

---

## 7. Engineering and gates

- **275 tests**, ruff and mypy --strict, with three CI jobs: static checks and unit tests, the
  upstream pin verification, and baseline integrity.
- **CI runs no evaluation**: a run costs real money. What the gate verifies is that the
  baseline was not quietly changed - if a task set, the upstream manifest or that run's summary
  changes, the baseline no longer represents the run it claims to.
- **Local start/stop**: `start-local.ps1` / `stop-local.ps1` / `reset-demo.ps1`. Stopping
  touches only processes **this project registered** (PID and process start time both checked),
  and refuses on a mismatch: Windows reuses PIDs, and the thing most likely to be killed by
  mistake on this machine is an evaluation that runs for tens of minutes.
- **Observability**: Langfuse is wired up, with each run's traces carrying the run directory
  name as the session id, so a trace on the dashboard maps back to a specific run.

---

## 8. Mistakes caught along the way

**This section is the most persuasive part of this report.** Every item below once made some
number look correct.

| Problem | Consequence | How it was found |
|---|---|---|
| The simulated user put `###STOP###` and a request in the same message | The exam was cut short and the score depressed | Reading the raw per-turn conversation |
| Round A's cross-model comparison used two different judges | Every cross-model number is void | Found while reviewing the manifest |
| Tasks lost to a server fault were dropped from upstream's denominator | 5 lost tasks out of 40 gave 0.886 instead of 0.875, so **losing tasks improved the score** | The simulation count did not add up |
| Per-task cost divided by 45 records when there were only 40 tasks | The published cost was 11% low, and the ratio derived from it was wrong too | Recomputed before freezing the baseline |
| `tool_calls` was always empty for read queries | The UI and API underreported tool calls, and every archive since M0.3 was empty | The first real message sent through the API |
| Gradio 6 removed `Chatbot(type=...)` | The demo UI would not start, and no test touched the UI | The first time it was actually started |
| Windows writing CRLF made the checksums meaningless | The baseline check went red while `git diff` was empty | The check failed after running regen |
| The wrong Langfuse callback name | A whole run finished with no trace on the dashboard and exit code 0 | Measuring both callbacks' return values |

What they share: **none of them raises an error.** Each appears as a number that looks normal.
So this project's approach is to attach a test that turns red to every one of them, rather than
remembering them.

---

## 9. Explicitly not done

Without this section, every number above would be over-read.

**Evaluation**

- **The full set has been run once.** The variance of repeating the same configuration has not
  been measured, so 0.8509 is a single observation, not an estimate with a confidence interval.
- **Only one model has been tested on the final stack.** Selection was a cost decision, not a
  quality comparison.
- **Judge noise of 8%** limits the resolution of any cross-model conclusion.
- The raw per-task traces exist only on this machine. Their digests are recorded, so a loss
  would be detected but not recovered.

**System**

- **No persistence**: sessions live in process memory and are lost on restart. PostgreSQL is
  M1.
- **No cross-process recovery or idempotency**: one session is processed serially, and no
  production recovery capability is claimed. M2.
- **No real authentication**: a fixed simulated identity bound to `127.0.0.1`, with no
  registration. Cognito is M3.
- **No human handover flow.** M4.
- **No AWS and no online demo URL.** M6.
- **The UI is minimal**; completing it is M7.

**Data**

- The data is tau2-bench's **simulated business data**. No real order system is connected, and
  no real payment or refund is involved.
- A public benchmark verifies capability in a simulated scenario, which **is not the same as
  real customer value or zero risk in production**.

---

## 10. Reproduction

```bash
uv sync --group bench
uv run python scripts/fetch_tau2.py                        # fetch at the pinned commit and verify
uv run ruff check . && uv run mypy && uv run pytest
uv run python scripts/freeze_baseline.py --verify          # verify the baselines are unchanged

# the following spend real money
uv run python scripts/run_eval.py --split full             # full set, 114 tasks, about 9.7 CNY
uv run python scripts/analyze_eval.py --run <run-directory>  # score, layers, usage and cost
uv run python scripts/demo_flows.py                        # the four demo flows

# local services (127.0.0.1 only, fixed simulated identity, no registration)
pwsh scripts/start-local.ps1
pwsh scripts/stop-local.ps1
```

Raw artefacts: `evaluation/results/<run-id>/` (the summary is committed, per-task traces are
not), `evaluation/baselines/`, `evaluation/demo_runs/`.

## 11. Index of stage reports

| Stage | Report |
|---|---|
| M0.1 protocol and environment freeze | [M0.1_PROTOCOL_FREEZE.md](M0.1_PROTOCOL_FREEZE.md) |
| M0.2 model interface and thinking parameters | [M0.2_MODEL_CAPABILITY.md](M0.2_MODEL_CAPABILITY.md) |
| M0.3 minimal business loop | [M0.3_BUSINESS_LOOP.md](M0.3_BUSINESS_LOOP.md) |
| M0.4 selection experiments | [M0.4_EXPERIMENT_PLAN.md](M0.4_EXPERIMENT_PLAN.md) |
| M0.5 part one: cost accounting and observability | [M0.5_OBSERVABILITY.md](M0.5_OBSERVABILITY.md) |
| M0.5 part two: local start/stop | [M0.5_LOCAL_DEMO.md](M0.5_LOCAL_DEMO.md) |
| M0.5 part three: frozen baselines | [M0.5_BASELINE.md](M0.5_BASELINE.md) |

---

**M0 is frozen here.** Per the [development plan](DEVELOPMENT_PLAN.md), work on later stages
stops once this report is delivered; M1-M7 are each developed as an independent theme, and
every step requires a performance comparison against the baseline.
