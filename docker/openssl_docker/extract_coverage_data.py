from __future__ import annotations

import json
from pathlib import Path
from common import sh


def normalize_repo_path(path_value: str, repo_dir: Path) -> str:
    """Normalize path to repository-relative format when possible."""
    path_obj = Path(path_value)
    if path_obj.is_absolute():
        try:
            return str(path_obj.resolve().relative_to(repo_dir.resolve()))
        except ValueError:
            return str(path_obj)
    return str(path_obj)


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
        "-j",
        "1",
        "-r",
        ".",
        *gcda_folders,
        "--gcov-ignore-parse-errors",
        "--merge-mode-functions=merge-use-line-min",
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
    return Path(output_path)
