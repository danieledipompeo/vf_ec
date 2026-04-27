"""Simple helper to check whether a test touched files changed in a commit.

Expected workflow:
1) lcov --zerocounters --directory .
2) run one test
3) lcov --capture --directory . --output-file <test>.info
4) gcovr . --root . --json --output <test>.json

Then run this script against the generated files and a commit hash.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from datetime import datetime
from common import GitHandler, sh


def normalize_repo_path(path_value: str, repo_dir: Path) -> str:
    """Normalize path to repository-relative format when possible."""
    path_obj = Path(path_value)
    if path_obj.is_absolute():
        try:
            return str(path_obj.resolve().relative_to(repo_dir.resolve()))
        except ValueError:
            return str(path_obj)
    return str(path_obj)
#
#
#def get_changed_lines(repo_dir: Path, commit: str) -> dict[str, set[int]]:
#    """Return changed line numbers per file for the given commit."""
#    cmd = ["git", "show", "--unified=0", "--format=", commit]
#    result = subprocess.run(
#        cmd,
#        cwd=repo_dir,
#        text=True,
#        capture_output=True,
#        check=True,
#    )
#
#    changed: dict[str, set[int]] = {}
#    current_file: str | None = None
#    hunk_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
#
#    for raw_line in result.stdout.splitlines():
#        if raw_line.startswith("+++ b/"):
#            file_name = raw_line[6:].strip()
#            if file_name == "/dev/null":
#                current_file = None
#                continue
#            current_file = normalize_repo_path(file_name, repo_dir)
#            changed.setdefault(current_file, set())
#            continue
#
#        if current_file is None:
#            continue
#
#        match = hunk_re.match(raw_line)
#        if not match:
#            continue
#
#        start_line = int(match.group(1))
#        count = int(match.group(2) or "1")
#
#        if count <= 0:
#            continue
#
#        for line_no in range(start_line, start_line + count):
#            changed[current_file].add(line_no)
#
#    print(f"Found {sum(len(lines) for lines in changed.values())} changed lines across {len(changed)} files.")
#    print("Changed files:")
#    for file_name, lines in changed.items():
#        print(f"  {file_name}: {sorted(lines)}")
#    return changed


def parse_lcov_covered_lines(info_file: Path, repo_dir: Path) -> dict[str, set[int]]:
    """Parse covered lines from lcov .info (SF + DA entries)."""
    covered: dict[str, set[int]] = {}
    current_file: str | None = None

    for raw in info_file.read_text(encoding="utf-8", errors="replace").splitlines():
        if raw.startswith("SF:"):
            sf_path = raw[3:].strip()
            current_file = normalize_repo_path(sf_path, repo_dir)
            covered.setdefault(current_file, set())
            continue

        if not raw.startswith("DA:") or current_file is None:
            continue

        line_data = raw[3:].split(",", 2)
        if len(line_data) < 2:
            continue

        line_no = int(line_data[0])
        hit_count = int(line_data[1])
        if hit_count > 0:
            covered[current_file].add(line_no)

    return covered


def parse_gcovr_covered_lines(gcovr_json: Path, repo_dir: Path) -> dict[str, set[int]]:
    """Parse covered lines from gcovr JSON output."""
    data = json.loads(gcovr_json.read_text(encoding="utf-8"))
    covered: dict[str, set[int]] = {}

    for entry in data.get("files", []):
        name = entry.get("file")
        if not name:
            continue

        file_name = normalize_repo_path(name, repo_dir)
        covered.setdefault(file_name, set())
        for line_entry in entry.get("lines", []):
            line_no = line_entry.get("line_number")
            hit_count = line_entry.get("count", 0)
            if isinstance(line_no, int) and isinstance(hit_count, int) and hit_count > 0:
                covered[file_name].add(line_no)

    return covered


def merge_line_maps(base: dict[str, set[int]], extra: dict[str, set[int]]) -> dict[str, set[int]]:
    """Merge file->lines dictionaries."""
    for file_name, lines in extra.items():
        base.setdefault(file_name, set()).update(lines)
    return base


def run_cmd(repo_dir: Path, cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run shell command and fail with readable error details."""
    result = subprocess.run(
        cmd,
        cwd=repo_dir,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed: {' '.join(cmd)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def generate_lcov_info(repo_dir: Path, output_file: Path) -> Path:
    """Generate lcov .info file from gcda data in repo directory."""
    run_cmd(
        repo_dir,
        [
            "lcov",
            "--capture",
            "--directory",
            ".",
            "--output-file",
            str(output_file),
        ],
    )
    return output_file


def generate_gcovr_json(repo_dir: Path, output_file: Path, gcda_folders: list[str] | None = None) -> Path | None:
    """Generate gcovr JSON report from gcda data in repo directory."""
    gcda_folders = [folder for folder in (gcda_folders or []) if folder.strip() != "."]
    output_path = str(output_file).strip()
    base_cmd = [
        "gcovr",
        "-r",
        ".",
        *gcda_folders,
        "--gcov-ignore-parse-errors",
        "--json",
        "--output",
        output_path,
    ]
    stdout, errorcode, stderr = sh(base_cmd, repo_dir)
    if errorcode != 0:
        raise RuntimeError(
            f"gcovr command failed: {' '.join(base_cmd)}\n"
            f"stdout:\n{stdout}\n"
            f"stderr:\n{stderr}"
        )
    return output_file


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check if changed lines in a commit are covered by a test run."
    )
    parser.add_argument("--repo", default=".", help="Path to git repository (default: .)")
    parser.add_argument("--commit", required=True, help="Commit hash to inspect")
    parser.add_argument("--lcov-info", help="Path to lcov .info file")
    parser.add_argument("--gcovr-json", help="Path to gcovr JSON file")

    args = parser.parse_args()
    repo_dir = Path(args.repo).resolve()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    lcov_info_path: Path | None = Path(args.lcov_info).resolve() if args.lcov_info else None
    gcovr_json_path: Path | None = Path(args.gcovr_json).resolve() if args.gcovr_json else None

    if lcov_info_path is None:
        lcov_info_path = (repo_dir / f"auto_{timestamp}.info").resolve()
        print(f"Generating lcov report: {lcov_info_path}")
        generate_lcov_info(repo_dir, lcov_info_path)
    elif not lcov_info_path.exists():
        print(f"lcov info file not found, generating: {lcov_info_path}")
        generate_lcov_info(repo_dir, lcov_info_path)

    if gcovr_json_path is None:
        gcovr_json_path = (repo_dir / f"auto_{timestamp}.json").resolve()
        print(f"Generating gcovr report: {gcovr_json_path}")
        try:
            generate_gcovr_json(repo_dir, gcovr_json_path)
        except RuntimeError as err:
            print("Warning: gcovr generation failed, continuing with lcov only.")
            print(err)
            gcovr_json_path = None
    elif not gcovr_json_path.exists():
        print(f"gcovr json file not found, generating: {gcovr_json_path}")
        try:
            generate_gcovr_json(repo_dir, gcovr_json_path)
        except RuntimeError as err:
            print("Warning: gcovr generation failed, continuing with lcov only.")
            print(err)
            gcovr_json_path = None

    changed_lines = GitHandler.get_changed_lines(repo_dir, args.commit)
    covered_lines: dict[str, set[int]] = {}

    if lcov_info_path:
        covered_lines = merge_line_maps(
            covered_lines,
            parse_lcov_covered_lines(lcov_info_path, repo_dir),
        )
    if gcovr_json_path:
        try:
            covered_lines = merge_line_maps(
                covered_lines,
                parse_gcovr_covered_lines(gcovr_json_path, repo_dir),
            )
        except (OSError, json.JSONDecodeError, ValueError) as err:
            print("Warning: gcovr report could not be parsed, ignoring gcovr data.")
            print(err)

    touched_by_file: dict[str, list[int]] = {}
    for file_name, diff_lines in changed_lines.items():
        covered_for_file = covered_lines.get(file_name, set())
        shared = sorted(diff_lines & covered_for_file)
        if shared:
            touched_by_file[file_name] = shared

    changed_line_count = sum(len(lines) for lines in changed_lines.values())
    covered_line_count = sum(len(lines) for lines in covered_lines.values())
    touched_line_count = sum(len(lines) for lines in touched_by_file.values())

    print(
        f"changed_files={len(changed_lines)} changed_lines={changed_line_count} "
        f"covered_files={len(covered_lines)} covered_lines={covered_line_count} "
        f"touched_files={len(touched_by_file)} touched_lines={touched_line_count}"
    )

    if touched_by_file:
        print("TOUCHED_BY_TEST=yes")
        for file_name in sorted(touched_by_file):
            lines_str = ",".join(str(line_no) for line_no in touched_by_file[file_name])
            print(f"{file_name}:{lines_str}")
        return 0

    print("TOUCHED_BY_TEST=no")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
