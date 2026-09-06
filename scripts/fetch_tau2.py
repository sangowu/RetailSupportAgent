"""按 manifest 固定的 commit 拉取 τ²-bench 并校验完整性。

上游代码与数据不入本仓库（保留其 MIT 归属，也避免把上游产物当自研）。
用法：
    uv run python scripts/fetch_tau2.py            # 拉取并校验
    uv run python scripts/fetch_tau2.py --verify-only
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from retail_support_agent.tau2_pin import (
    DEFAULT_CHECKOUT,
    ManifestError,
    load_manifest,
    verify_checkout,
)


def _force_utf8_output() -> None:
    """Windows 控制台默认码页会把中文输出变成乱码，这里统一改成 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def run(cmd: list[str], cwd: Path | None = None) -> None:
    """执行子进程，失败即中止；不吞掉上游错误信息。"""
    subprocess.run(cmd, cwd=cwd, check=True)


# 关闭换行转换：Windows 默认的 autocrlf 会把上游文件改成 CRLF，
# 使同一 commit 在不同平台算出不同 SHA256，摘要固定随之失效。
LF_CONFIG = ["-c", "core.autocrlf=false", "-c", "core.eol=lf"]


def fetch(checkout: Path) -> None:
    """把固定 commit 取到本地。已存在则直接检出该 commit，不做隐式更新。"""
    manifest = load_manifest()
    repo = manifest["upstream"]["repo"]
    commit = manifest["upstream"]["commit"]

    if not (checkout / ".git").is_dir():
        checkout.parent.mkdir(parents=True, exist_ok=True)
        run(["git", *LF_CONFIG, "clone", "--filter=blob:none", repo, str(checkout)])
    # 已存在的检出可能是早先带换行转换克隆的，这里把设置固化到仓库本身
    run(["git", "config", "core.autocrlf", "false"], cwd=checkout)
    run(["git", "config", "core.eol", "lf"], cwd=checkout)
    run(["git", "fetch", "--quiet", "origin", commit], cwd=checkout)
    run(["git", "checkout", "--quiet", "--detach", commit], cwd=checkout)
    # 索引可能缓存着转换后的内容，强制按新设置重新落盘
    run(["git", "rm", "--cached", "-r", "-q", "."], cwd=checkout)
    run(["git", "reset", "--hard", "--quiet"], cwd=checkout)
    print(f"已检出 {repo} @ {commit}")


def main() -> int:
    _force_utf8_output()
    parser = argparse.ArgumentParser(description="拉取并校验固定版本的 τ²-bench")
    parser.add_argument("--checkout", type=Path, default=DEFAULT_CHECKOUT)
    parser.add_argument("--verify-only", action="store_true", help="只校验，不拉取")
    args = parser.parse_args()

    try:
        if not args.verify_only:
            fetch(args.checkout)
        mismatches = verify_checkout(args.checkout)
    except (ManifestError, subprocess.CalledProcessError) as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 2

    if mismatches:
        print(f"校验不通过，{len(mismatches)} 个文件与 manifest 不符：", file=sys.stderr)
        for item in mismatches:
            print(item.describe(), file=sys.stderr)
        return 1

    manifest = load_manifest()
    print(
        "校验通过："
        f"{len(manifest['files_sha256'])} 个文件匹配 "
        f"{manifest['upstream']['tag']} ({manifest['upstream']['commit'][:12]})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
