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
    # Remove temporary configure artifacts recursively so gcovr doesn't try to process them.
    artifact_globs: list[str] = [
        "**/*conftest*.gcno",
        "**/*conftest*.gcda",
    ]
    search_roots = [repo_dir] + [repo_dir / folder for folder in gcda_folders]
    seen: set[Path] = set()
    for search_root in search_roots:
        for glob in artifact_globs:
            for artifact in search_root.glob(glob):
                resolved = artifact.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                artifact.unlink(missing_ok=True)
    output_path = str(output_file).strip()
    base_cmd = [
        "gcovr",
        "-r",
        ".",
        "-j",
        "1",
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
