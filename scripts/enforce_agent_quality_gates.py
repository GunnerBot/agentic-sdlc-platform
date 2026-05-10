from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

API_CONTRACT_PREFIXES = (
    "src/agentic_sdlc_platform/api/",
    "src/agentic_sdlc_platform/models/",
)
API_CONTRACT_FILES = {
    "src/agentic_sdlc_platform/glue/task_event_normalizer.py",
}


@dataclass(frozen=True)
class GateResult:
    ok: bool
    missing: tuple[str, ...]
    production_files: tuple[str, ...]
    test_files: tuple[str, ...]
    api_contract_files: tuple[str, ...]
    contract_test_files: tuple[str, ...]


def evaluate_changed_files(paths: list[str]) -> GateResult:
    production_files = tuple(path for path in paths if _is_production_file(path))
    test_files = tuple(path for path in paths if _is_test_file(path))
    api_contract_files = tuple(path for path in paths if _is_api_contract_file(path))
    contract_test_files = tuple(path for path in paths if _is_contract_test_file(path))
    missing: list[str] = []

    if production_files and not test_files:
        missing.append("Production code changed without same-PR tests. Add/update relevant tests.")
    if api_contract_files and not contract_test_files:
        missing.append(
            "API/schema/webhook contract changed without same-PR Schemathesis contract tests."
        )

    return GateResult(
        ok=not missing,
        missing=tuple(missing),
        production_files=production_files,
        test_files=test_files,
        api_contract_files=api_contract_files,
        contract_test_files=contract_test_files,
    )


def changed_files() -> list[str]:
    base_ref = os.getenv("ASDLC_AGENT_QUALITY_BASE_REF") or os.getenv("GITHUB_BASE_REF")
    untracked = _paths(_run(["git", "ls-files", "--others", "--exclude-standard"]))
    candidates: list[list[str]] = []
    if base_ref:
        candidates.append(["git", "diff", "--name-only", f"origin/{base_ref}...HEAD"])
    candidates.extend(
        [
            ["git", "diff", "--name-only", "--cached"],
            ["git", "diff", "--name-only"],
            ["git", "diff", "--name-only", "HEAD^...HEAD"],
        ]
    )
    for command in candidates:
        output = _run(command)
        paths = _paths(output)
        if paths:
            return sorted({*paths, *untracked})
    return untracked


def main() -> int:
    result = evaluate_changed_files(changed_files())
    if result.ok:
        print("agent quality gates passed")
        return 0

    print("agent quality gates failed", file=sys.stderr)
    for item in result.missing:
        print(f"- {item}", file=sys.stderr)
    _print_paths("production files", result.production_files)
    _print_paths("test files", result.test_files)
    _print_paths("api contract files", result.api_contract_files)
    _print_paths("contract test files", result.contract_test_files)
    return 1


def _is_production_file(path: str) -> bool:
    return path.startswith("src/") and Path(path).suffix == ".py"


def _is_test_file(path: str) -> bool:
    return path.startswith("tests/") and Path(path).suffix == ".py"


def _is_contract_test_file(path: str) -> bool:
    return path.startswith("tests/contracts/") and Path(path).suffix == ".py"


def _is_api_contract_file(path: str) -> bool:
    return path in API_CONTRACT_FILES or any(
        path.startswith(prefix) and Path(path).suffix == ".py" for prefix in API_CONTRACT_PREFIXES
    )


def _run(command: list[str]) -> str:
    try:
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout


def _paths(output: str) -> list[str]:
    return sorted({line.strip() for line in output.splitlines() if line.strip()})


def _print_paths(label: str, paths: tuple[str, ...]) -> None:
    if not paths:
        return
    print(f"{label}:", file=sys.stderr)
    for path in paths:
        print(f"  {path}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
