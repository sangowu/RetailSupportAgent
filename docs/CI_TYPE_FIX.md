# CI test typing repair

## 1. Change card

Recorded before implementation on 2026-09-10, against `a31ebb2`.

- Type: fix. Restore strict type checking for the three deployment test modules.
- Existing failure: 49 mypy errors across `test_deploy_files.py` (18),
  `test_idle_shutdown.py` (17) and `test_demo_commands.py` (14). The latest main CI
  run, [34496006219](https://github.com/sangowu/RetailSupportAgent/actions/runs/34496006219),
  fails at mypy and skips the main pytest step and baseline-integrity job.
- Scope: test imports and annotations, PyYAML typing dependency and this repair record.
- Controls: application and deployment code, model, prompt, reasoning settings,
  evaluation protocol, baselines, CI strictness and existing test assertions stay fixed.
- Expected benefit: mypy succeeds so CI can reach its existing test step. No runtime
  performance improvement is claimed.
- Acceptance: zero mypy errors; ruff lint and format checks pass; all 858 tests pass
  with an isolated PostgreSQL database; all three frozen baselines verify.
- Reference: current guarded `baseline-v3`, upstream reference `baseline-v2`, archived
  MVP `baseline-v1`. No billed evaluation is needed for test-only changes.

## 2. Implementation and verification

### Root causes and changes

- `test_demo_commands.py`: replace the dynamic module loader and runtime class aliases
  with normal imports from the scripts directory, following `test_compare_runs.py`.
  This lets mypy resolve `Instance` and its properties. Annotate monkeypatch fixtures
  and the AWS response dictionaries and captured argument tuples.
- `test_idle_shutdown.py`: import the actual watcher module and its classes normally.
  Annotate `Store`, the yielding fixture and monkeypatch arguments. Before accessing
  `client.app.state`, assert that the application is a `FastAPI` instance. This narrows
  TestClient's general ASGI type and checks the assumption at runtime; it does not cast
  away an error. Make the class-scoped fixture static, matching the existing deployment
  fixtures and avoiding pytest's deprecated class-scoped instance-method binding.
- `test_deploy_files.py`: type the YAML dictionaries and fixture parameters. Check that
  `safe_load` returned a mapping with string keys before returning it.
- `pyproject.toml` and `uv.lock`: add `types-PyYAML` to dev dependencies, locked at
  `6.0.12.20260906`. No existing third-party package changes or removals. The project's
  own lockfile dependency metadata gains the new stub package.

No existing assertion was removed or weakened, and no mypy override or ignore was added.
Application code, deployment scripts and workflow definitions are unchanged.

### Verification

Before editing, mypy reproduced all 49 errors. The preceding read-only check passed
ruff and baseline verification, and ran 734 tests successfully with 124 skipped because
no test database was configured. The 858-pass result below is a new database-backed run,
not a relabeling of those skips.

Environment: Windows, project Python 3.12 virtual environment, `uv sync --locked --group
bench`, real PostgreSQL 17 Alpine in a dedicated `rsa-ci-typefix` container. PostgreSQL
was bound to `127.0.0.1:55439`, with its data on tmpfs and no existing volume mounted.
Only that disposable database was supplied through `RSA_TEST_DATABASE_URL`; the test
fixtures create and drop tables. Tests ran serially, excluding `live_model`.

| Check | Result |
|---|---|
| `python -m mypy` | zero errors, 81 source files |
| `python -m ruff check .` | passed |
| `python -m ruff format --check .` | passed |
| `python -m pytest -m "not live_model" -ra` | **858 passed, zero skipped**, 22.94 s |
| `python scripts/freeze_baseline.py --verify` | all three baselines verified |
| `git diff --check` | passed |

One existing dependency warning remains: Starlette imports the deprecated
`anyio.abc.BlockingPortal` alias. Dependency upgrades are outside this repair.

The initial Docker inventory approval timed out; its read-only retry succeeded. The
first implementation check reduced mypy to the one missing stub error before dependency
sync, and reported import-placement and line-length lint errors. Those were corrected
before the passing checks above. No test failures were hidden by retries.

## 3. Comparison

| Metric | Before (`a31ebb2`) | Candidate | Conclusion |
|---|---|---|---|
| mypy | 49 errors in three tests | zero errors | fixed locally |
| Full local suite | 858 reported historically; 734 passed / 124 skipped in the read-only check | 858 passed / zero skipped on isolated PostgreSQL | full local regression passed |
| Baseline integrity | three baselines verified | three baselines verified | unchanged |
| Remote CI | failed at mypy; main pytest and baseline gate skipped | not submitted | pending publication |

Benchmark quality, safety, latency and cost are not remeasured: the application,
evaluation implementation and frozen inputs are unchanged. The historical `baseline-v3`
score remains 102/114; this maintenance change produces no new model score, cost,
latency distribution, confidence interval or Langfuse experiment. It does not replace
any frozen baseline or claim an improvement against the MVP's business performance.

## 4. Conclusion

**PASS for local repair and regression verification.** The strict type-checking failure
is fixed and all 858 tests pass, including the 124 that need PostgreSQL. Runtime business
behavior is unchanged. The temporary test container is stopped and removed after use.

The user subsequently authorized a local commit on `fix/ci-test-typing`. No push, PR or
deployment is included. Remote CI cannot validate the candidate until a later authorized publication. Its
actual success, including the previously skipped pytest and baseline-integrity steps,
remains the release check; local success must not be reported as a green GitHub run.
