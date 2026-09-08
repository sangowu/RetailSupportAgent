"""冻结 / 校验 `baseline-v1`：M0 的权威基线记录与校验和。

基线的用途只有一个：**让后续任何一次改动都有一个可比对象**。所以它必须是
可复现生成的，而不是手工抄进 Markdown 的一堆数字——手抄的数字会和代码
悄悄脱节，本项目已经因此错过一次（每题成本按 45 题算，实际只有 40 题）。

生成的内容：

    evaluation/baselines/baseline-v1/baseline.json     基线记录（数字全部现算）
    evaluation/baselines/baseline-v1/checksums.sha256  依赖文件的摘要

**校验和分两类，这个区分是必要的**：

  tracked  入库文件（summary.json、任务集、上游 manifest）——任何人 clone 下来
           都能校验，CI 也校验它们。
  local    原始逐题轨迹（约 100KB/题，按 .gitignore 不入库）——只有跑过这一轮
           的机器上才有。摘要照记，但校验时**缺失不算失败**，只报告"未在本机"。
           把它们混进一类，CI 会因为文件本来就不该存在而永远红。

用法：
    uv run python scripts/freeze_baseline.py            # 生成/更新
    uv run python scripts/freeze_baseline.py --verify   # 只校验，不写
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from retail_support_agent.pricing import PRICE_SNAPSHOT_DATE  # noqa: E402
from retail_support_agent.results_io import load_simulations  # noqa: E402

BASELINES_ROOT = REPO / "evaluation" / "baselines"
RESULTS_ROOT = REPO / "evaluation" / "results"

# 基线登记表。运行目录写死而不是"取最新一轮"——基线的意义就在于它**不会**跟着
# 最新一次运行漂移。要换基线就在这里加一条，并在 PR 里说清为什么。
#
# 旧基线**只留档、不删除**：删掉它，历史报告里引用的那个数字就再也无从校验；
# 而留着它并继续校验校验和，等于每次 CI 都确认"当初那一轮的原始记录没被动过"。
BASELINES: dict[str, dict[str, str]] = {
    "baseline-v1": {
        "run": "20260907T080504Z_deepseek-v4-pro_holdout",
        "split": "holdout",
        "status": "archived",
        "scope_note": (
            "保留验证集 40 题。**不是标准 τ²-bench Retail 分数**，是子集分；"
            "它的价值在于「未用于调参的独立验证」这一性质，故留档不删。"
        ),
    },
    "baseline-v2": {
        "run": "20260907T205640Z_deepseek-v4-pro_full",
        "split": "full",
        "status": "reference",
        "subject": "upstream",
        "scope_note": (
            "全集 114 题，可称标准 τ²-bench Retail 分数。"
            "包含 dev 74 题，其中部分曾用于调参——因此它是标准口径分，"
            "而不是「独立验证分」，两个口径不互相冒充。"
            "**被测对象是上游 llm_agent**，不是本项目的 Agent；它现在的角色是"
            "baseline-v3 的对照组，故保留并继续校验。"
        ),
    },
    "baseline-v3": {
        "run": "20260908T121800Z_deepseek-v4-pro_full_guarded",
        "split": "full",
        "status": "current",
        "subject": "guarded",
        "scope_note": (
            "全集 114 题，被测对象是**本项目带写操作护栏的 Agent**。"
            "与 baseline-v2 同栈同模型同种子，逐题可配对比较；"
            "两者是两个不同的被测对象，成绩并列呈现，不合并平均。"
        ),
    },
}
CURRENT_BASELINE = "baseline-v3"


def baseline_dir(baseline_id: str) -> Path:
    return BASELINES_ROOT / baseline_id


# 基线依赖的入库文件。少写一个，就等于基线的一部分前提没有被钉住。
TRACKED_DEPS = (
    "evaluation/tau2_manifest.json",
    "evaluation/task_sets/dev.json",
    "evaluation/task_sets/full.json",
    "evaluation/task_sets/holdout.json",
    "evaluation/task_sets/regression.json",
    "evaluation/task_sets/scope_labels.json",
    "evaluation/schemas/run_manifest.schema.json",
    "evaluation/schemas/task_result.schema.json",
)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_commit() -> dict[str, Any]:
    """记下代码状态。**工作区是否 dirty 必须记**——在未提交的改动上产出的基线
    无法被别人复现，而这件事光看 commit 号是看不出来的。

    但**基线目录自己不算**：冻结这个动作必然会写出 baseline.json 和
    checksums.sha256，把它们算进去会让 dirty 永远为真，于是这个字段
    就再也提示不了任何东西——一个恒为真的告警等于没有告警。
    """
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    rel = BASELINES_ROOT.relative_to(REPO).as_posix()
    others = [line for line in status if rel not in line.replace("\\", "/")]
    return {"commit": head, "dirty": bool(others), "dirty_excludes": rel}


def upstream_pin() -> dict[str, Any]:
    manifest = json.loads((REPO / "evaluation" / "tau2_manifest.json").read_text(encoding="utf-8"))
    upstream = manifest.get("upstream") or {}
    return {
        "repo": upstream.get("repo"),
        "tag": upstream.get("tag"),
        "commit": upstream.get("commit"),
        "license": upstream.get("license"),
        # 逐文件摘要本身在 tau2_manifest.json 里，这里只记条数；
        # 那个文件的完整性由 checksums.sha256 的 tracked 段负责。
        "pinned_file_count": len(manifest.get("files_sha256") or {}),
    }


def raw_trace_files(run_dir: Path) -> list[Path]:
    """本轮的原始逐题轨迹。上游把 save_to 当目录用，所以它们是
    `<名字>.json/results.json` 这种形状。"""
    found = [p for p in run_dir.glob("*.json/results.json") if p.is_file()]
    return sorted(found)


def build_record(run_dir: Path, split: str) -> dict[str, Any]:
    """从原始结果现算基线记录。**任何数字都不从文档里抄。**"""
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    simulations = load_simulations(run_dir)

    rewards: list[float] = [
        float(reward)
        for sim in simulations
        if (reward := (sim.get("reward_info") or {}).get("reward")) is not None
    ]
    scored = len(rewards)
    passed = sum(1 for r in rewards if r == 1.0)

    task_ids = list(summary.get("task_ids") or [])
    # 分母是**题数**，不是模拟记录条数。这两者在补跑之后会不一致：
    # 同一道题会留下两条记录（失败的那条和补跑成功的那条）。
    # 早先每题成本按 45 算而实际只有 40 题，就是踩了这个。
    expected = len(task_ids) * int(summary.get("num_trials", 1) or 1)

    return {
        "run_dir": run_dir.name,
        "run_id": summary.get("run_id"),
        "run_at": summary.get("run_at"),
        # 被测对象必须留痕：上游 llm_agent 与带护栏的 Agent 是两个不同的对象，
        # 成绩不可比。M0.6 之前的运行没有这个字段，按 upstream 处理。
        "agent_under_test": summary.get("agent_under_test", "upstream"),
        "task_split": split,
        "task_count": len(task_ids),
        "num_trials": int(summary.get("num_trials", 1) or 1),
        "expected_simulations": expected,
        "scored_simulations": scored,
        "complete": scored == expected,
        "avg_reward": (sum(rewards) / scored) if scored else None,
        "pass_count": passed,
        "stack": summary.get("stack"),
        "resumed_task_ids": summary.get("resumed_task_ids") or [],
    }


def collect_checksums(run_dir: Path) -> dict[str, dict[str, str]]:
    tracked: dict[str, str] = {}
    for rel in TRACKED_DEPS:
        path = REPO / rel
        if not path.is_file():
            raise SystemExit(f"基线依赖的入库文件不存在：{rel}")
        tracked[rel] = sha256_of(path)
    for name in ("summary.json", "resume_summary.json"):
        path = run_dir / name
        if path.is_file():
            tracked[path.relative_to(REPO).as_posix()] = sha256_of(path)

    local = {p.relative_to(REPO).as_posix(): sha256_of(p) for p in raw_trace_files(run_dir)}
    return {"tracked": tracked, "local_only": local}


def write_baseline(
    baseline_id: str, record: dict[str, Any], checksums: dict[str, dict[str, str]]
) -> None:
    entry = BASELINES[baseline_id]
    out_dir = baseline_dir(baseline_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    baseline = {
        "baseline_id": baseline_id,
        # 口径必须写在基线里：子集分与全集分都是真数字，但不能互相冒充。
        "status": entry["status"],
        "task_split": entry["split"],
        "scope_note": entry["scope_note"],
        "frozen_at": datetime.now(UTC).isoformat(),
        "code": git_commit(),
        "upstream": upstream_pin(),
        "price_snapshot_date": PRICE_SNAPSHOT_DATE,
        "run": record,
        "checksum_file": "checksums.sha256",
        "note": (
            "成本以 scripts/analyze_eval.py 现算为准，不在此处冻结数字——"
            "价格快照会随时间新增条目，把成本抄进基线会让它悄悄过期。"
        ),
    }
    (out_dir / "baseline.json").write_text(
        json.dumps(baseline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )

    lines = ["# tracked：入库文件，任何 clone 都能校验（CI 校验这一段）"]
    lines += [f"{digest}  {path}" for path, digest in sorted(checksums["tracked"].items())]
    lines.append("# local_only：原始逐题轨迹，按 .gitignore 不入库；缺失不算失败")
    lines += [f"{digest}  {path}" for path, digest in sorted(checksums["local_only"].items())]
    (out_dir / "checksums.sha256").write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
    )


def parse_checksums(baseline_id: str) -> dict[str, dict[str, str]]:
    path = baseline_dir(baseline_id) / "checksums.sha256"
    if not path.is_file():
        raise SystemExit(
            f"没有校验和文件：{path}。先运行 scripts/freeze_baseline.py --id {baseline_id}"
        )
    section = "tracked"
    out: dict[str, dict[str, str]] = {"tracked": {}, "local_only": {}}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# tracked"):
            section = "tracked"
        elif line.startswith("# local_only"):
            section = "local_only"
        elif line.strip():
            digest, name = line.split("  ", 1)
            out[section][name] = digest
    return out


def verify_one(baseline_id: str) -> int:
    expected = parse_checksums(baseline_id)
    bad: list[str] = []
    missing_local: list[str] = []

    for name, digest in expected["tracked"].items():
        path = REPO / name
        if not path.is_file():
            bad.append(f"缺失（入库文件本该存在）：{name}")
        elif sha256_of(path) != digest:
            bad.append(f"内容已变：{name}")

    for name, digest in expected["local_only"].items():
        path = REPO / name
        if not path.is_file():
            missing_local.append(name)
        elif sha256_of(path) != digest:
            bad.append(f"原始轨迹已变：{name}")

    status = BASELINES[baseline_id]["status"]
    print(
        f"[{baseline_id}]（{status}）入库文件 {len(expected['tracked'])} 个，"
        f"原始轨迹 {len(expected['local_only'])} 个"
    )
    if missing_local:
        print(f"  原始轨迹有 {len(missing_local)} 个不在本机——按设计不入库，不算失败")
    if bad:
        print("  校验失败：")
        for item in bad:
            print(f"    {item}")
        return 1
    print("  校验通过。")
    return 0


def verify_all() -> int:
    """校验**每一个**基线，留档的也不例外。

    留档基线继续校验，才使「历史报告里那个数字仍可追溯」成立；
    只校验当前基线，等于默许旧基线被悄悄改动而无人察觉。
    """
    failures = [bid for bid in BASELINES if verify_one(bid) != 0]
    if failures:
        print(
            "\n以下基线被改动过：" + "、".join(failures) + "。"
            "若确属有意，请重新冻结并在 PR 里说明为什么换基线。"
        )
        return 1
    print(f"\n全部 {len(BASELINES)} 个基线校验通过。")
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="冻结或校验基线")
    parser.add_argument("--verify", action="store_true", help="只校验全部基线，不写文件")
    parser.add_argument(
        "--id", default=CURRENT_BASELINE, choices=sorted(BASELINES), help="要冻结哪个基线"
    )
    parser.add_argument(
        "--run",
        default=None,
        help="运行目录名。仅当该基线尚未绑定运行目录时需要，绑定后会回写进登记表",
    )
    args = parser.parse_args()

    if args.verify:
        return verify_all()

    entry = BASELINES[args.id]
    run_name = args.run or entry["run"]
    if not run_name:
        raise SystemExit(
            f"{args.id} 还没有绑定运行目录。首次冻结请显式指定："
            f"scripts/freeze_baseline.py --id {args.id} --run <运行目录名>"
        )
    if entry["run"] and args.run and args.run != entry["run"]:
        raise SystemExit(
            f"{args.id} 已绑定 {entry['run']}，与 --run {args.run} 不符。"
            "换运行目录等于换基线，请改 BASELINES 登记表并在 PR 里说明原因。"
        )
    run_dir = RESULTS_ROOT / run_name
    if not run_dir.is_dir():
        raise SystemExit(f"找不到基线运行目录：{run_dir}")

    record = build_record(run_dir, entry["split"])
    if not record["complete"]:
        raise SystemExit(
            f"这一轮不完整（{record['scored_simulations']}/{record['expected_simulations']}"
            " 有分数），不能当基线——缺题会让平均分虚高。"
            "先用 scripts/resume_run.py 补齐。"
        )

    checksums = collect_checksums(run_dir)
    write_baseline(args.id, record, checksums)
    print(f"已冻结 {args.id}：{baseline_dir(args.id).relative_to(REPO).as_posix()}")
    print(f"  口径       {entry['split']}（{entry['status']}）")
    print(f"  运行目录   {record['run_dir']}")
    print(
        f"  平均奖励   {record['avg_reward']:.4f}"
        f"（{record['pass_count']}/{record['scored_simulations']}）"
    )
    print(f"  入库文件   {len(checksums['tracked'])} 个已记摘要")
    print(f"  原始轨迹   {len(checksums['local_only'])} 个已记摘要（不入库）")
    if git_commit()["dirty"]:
        print("\n[注意] 工作区有未提交改动，已如实记进 baseline.json 的 code.dirty。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
