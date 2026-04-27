import os
import csv
import logging
import json
from pathlib import Path
import time
import yaml

from common import EnergyHandler, GitHandler, ProgressBar, sh
from project import Project, ProjectFactory
from logger import get_logger
import project

logger = get_logger(__name__)

def load_config(config_file: str | Path):
    with open(config_file, "r") as f:
        cfg = yaml.safe_load(f) or {}
    for p in (cfg.get("paths") or {}).values():
        if p:
            os.makedirs(p, exist_ok=True)
    return cfg

def get_covered_files(cwd):
    """
    Scans the given directory for .gcda files and maps them to their corresponding .c source files.
    Returns a list of source file paths relative to the cwd.

    :param cwd: Description
    """
    covered = set()
    for root, dirs, files in os.walk(cwd):
        for file in files:
            if file.endswith(".gcda"):
                source_name = file.replace(".gcda", ".c")
                rel_dir = os.path.relpath(root, cwd)
                full_path = source_name if rel_dir == "." else os.path.join(rel_dir, source_name)
                covered.add(full_path)
    return list(covered)

def process_commit(project : Project, commit: str, coverage: bool = True) -> list[dict]:
    """ 
    Process a single commit: checkout, build with coverage, run tests, collect coverage data.

    :param commit: The git commit hash to process
    :param coverage: Whether to build with coverage instrumentation
    :return: A dictionary with test results and coverage data, or None if build fails
    """
    
    logger.info(f"Building {commit[:8]} (Coverage)...")
    GitHandler.clean_repo(project.input_dir)
    GitHandler.checkout(project.input_dir, commit)
    
    commit_tests = []

    if not project.build(coverage=coverage):
        logger.error(f"Build failed for commit {commit[:8]}.")
        return []
    
    suite = project.get_test()
    if not suite:
        logger.error("No tests found.")
        return []
    
    logger.info(f"Running {len(suite)} tests...")

    # pb = ProgressBar(len(suite), step=10)
    # logger.warning(f"-- TEST SUITE LIMITED TO FIRST 20 TESTS OUT OF {len(suite)} TOTAL TESTS FOR DEMO PURPOSES.")
    l_suite = len(suite)
    for i, t in enumerate(suite):
        # pb.set(i)
        logger.info(f"Running test {i+1}/{l_suite}: {t}")

        test = {
            "name": t,
            "passed": False,
            "covered_files": {},
            "duration": 0.0
        }

        # Delete all .gcda (runtime counters) recursively from the build tree.
        # .gcno files are compile-time artifacts and must NOT be deleted between
        # test runs — gcov/gcovr needs them to interpret the .gcda data.
        sh(["find", ".", "-name", "*.gcda", "-delete"], Path(project.build_dir))
        logger.info(f"Coverage data cleaned before running test '{t}'.")

        # run test
        start_time = time.time()
        test['passed'], error = project.run_test(t)
        # logger.info(f"!!!!! Dry run, skipping test execution. !!!!!")
        # test['passed'], error = True, None
        test['duration'] = time.time() - start_time
        logger.info(f" --- Test '{t}' completed in {test['duration']:.2f}s")
        
        if not test['passed']:
            logger.debug(f"Test '{t}' failed with error: {error}")
            continue
        
        covered = project.coverage_file(test["name"])
        test['covered_files'] = covered
        commit_tests.append(test)
        
    return commit_tests

def compute_coverage(project: Project, commit: str):
    process_results = {
        "hash": commit,
        "tests": []
    }
    
    GitHandler.clean_repo(project.input_dir)
    if not GitHandler.checkout(project.input_dir, commit):
        logger.error(f"Failed to checkout commit: {commit}")
        return None
    
    logger.info(f"-- Processing commit age: {GitHandler.get_age_of_commit(project.input_dir, commit)}")
    
    git_changed_lines = GitHandler.get_changed_lines(project.input_dir, commit)

    if not git_changed_lines:
        logger.error("No target files found in git diff.")
        return None
    
    process_results['tests'] = process_commit(project, commit)
    if all(not t.get('passed', False) for t in process_results.get('tests', [])):
        logger.error("No successful tests in fix commit. Skipping processing.")
        return None
    
    extract_test_covering_git_changes(process_results, git_changed_lines)
    logger.info(f"Extracted tests covering changed files for commit {commit[:8]}).")
    
    return process_results
    
def compute_energy_for_tests(project, tests, commit):
    is_build = project.build(coverage=False)
    if not is_build:
        # logger.error(f"Build failed for commit {commit[:8]}. Skipping energy measurement.")
        return None
    
    for test in tests:
        project.compute_energy(test['name'], commit)
    
def extract_test_covering_git_changes(
    coverage_results: dict,
    target_lines_by_file: dict[str, set[int]],
):
    """
    Mark "keep" in tests that cover changed files. 
    
    :param coverage_results: Dictionary with test results and coverage data
    :param target_lines_by_file: Changed line numbers keyed by file path from git diff
    """
    def _to_ranges(lines: set[int]) -> list[tuple[int, int]]:
        """Convert discrete line numbers to inclusive contiguous ranges."""
        if not lines:
            return []
        ordered = sorted(int(line) for line in lines)
        ranges: list[tuple[int, int]] = []
        start = prev = ordered[0]
        for line in ordered[1:]:
            if line == prev + 1:
                prev = line
                continue
            ranges.append((start, prev))
            start = prev = line
        ranges.append((start, prev))
        return ranges

    target_ranges_by_file = {
        str(path): _to_ranges(set(lines))
        for path, lines in target_lines_by_file.items()
        if str(path).endswith((".c", ".cpp", ".h", ".hpp")) and lines
    }

    for test in coverage_results.get('tests', []):
        covered_files = test.get('covered_files', {})
        keep = False

        if isinstance(covered_files, dict):
            for covered_file, covered_lines in covered_files.items():
                changed_ranges = target_ranges_by_file.get(str(covered_file))
                if not changed_ranges:
                    continue

                covered_line_set = set(int(line) for line in covered_lines)
                if any(
                    any(start <= line <= end for start, end in changed_ranges)
                    for line in covered_line_set
                ):
                    keep = True
                    break

        test['keep'] = keep

def download_dataset(config: dict):
    """
    Downloads the dataset if not already present.
    
    :param dataset_config: Configuration dictionary for the dataset
    """
    url = config.get('dataset', {}).get('csv_url', "")
    dest_dir = config.get('paths', {}).get('input_dir', "")
    dest_path = os.path.join(dest_dir, config.get('dataset', {}).get('csv_file', ""))
    
    if not os.path.exists(dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
    
    GitHandler.clone_repo(dest_dir, url, dest_path)

def parse_csv(configuration: dict) -> list[dict]:
    
    cwe_csv_path = os.path.join(configuration.get('paths', {}).get('input_dir', ""), 
                                configuration.get('dataset', {}).get('csv_file', ""), 
                                "cwe_projects.csv")
    if not os.path.isfile(cwe_csv_path):
        download_dataset(configuration)
        
    data = []
    with open(cwe_csv_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            data.append(row)
    return data

def compute_energy(commit: str, tests: list[dict], project: Project, build: bool = True) -> bool:
    if build:
        GitHandler.clean_repo(project.input_dir)
        GitHandler.checkout(project.input_dir, commit)
    
        is_build = project.build(coverage=False)
        if not is_build:
            logger.error(f"Build failed for commit {commit[:8]}. Stopping pair processing.")
            return False
    
    logger.info(f"Computing energy for {len(tests)} tests on commit {commit[:8]}...")
    for test in tests:
        project.compute_energy(test['name'], commit)
    
    return True

def main():
    configuration = load_config(os.path.join(os.path.dirname(__file__), "config.yaml"))

    cwe_csv = parse_csv(configuration)
    
    for prj in configuration.get("project", []):
        # Extract project name and config from dict structure
        project_config = prj[list(prj.keys())[0]]
        
        in_dir = configuration.get('paths', {}).get('input_dir')
        out_dir = configuration.get('paths', {}).get('output_dir')
        project = ProjectFactory.get_project(name=project_config.get('name'), 
                                             input_dir=in_dir, 
                                             output_dir=out_dir)
        
        
        logger.info(f"Starting processing: {project.name}")

        pairs = [ (row['vuln_commit'], row['fix_commit']) for row in cwe_csv 
                  if row.get('project') == project_config.get('name') ]
        
        # logger.debug("--- PAIRS LIMITED TO FIRST 2 FOR DEMO PURPOSES.")
        for i, (vuln, fix) in enumerate(pairs):
            coverage_dict = {"hash": fix, "tests": []}
            kept_tests: list[dict] = []

            start_time = time.time()
            logger.info(f"[{i+1}/{len(pairs)}] Processing Pair: {vuln[:8]} -> {fix[:8]}")

            kept_tests_path = (
                project.input_dir.parent 
                / "kept_tests" 
                / f"{project.name}" 
                / f"{project.name}_{fix[:8]}_kept_tests.json"
            )
            
            if os.path.exists(kept_tests_path.parent):
                # Folder exists: this project was already processed — never re-run coverage tests.
                if not os.path.exists(kept_tests_path):
                    logger.warning(f"No kept tests file found for {fix[:8]}. Skipping pair.")
                    continue
                with open(kept_tests_path, "r") as f:
                    kept_tests = json.load(f)
                coverage_dict['tests'] = kept_tests
            else:
                coverage_dict = compute_coverage(project, fix)
                if coverage_dict is None :
                    logger.error(f"Skipping pair due to no coverage data for FIX commit: {fix[:8]}")
                    continue

                kept_tests = [t for t in coverage_dict.get('tests', []) if t.get('keep', True)]
                if not kept_tests:
                    logger.error(f"No tests to measure energy for in FIX commit: {fix[:8]}. Skipping pair.")
                    continue

                # dump kept_tests to json for later reference
                with open(kept_tests_path, "w") as f:
                    json.dump(kept_tests, f, indent=2, default=list)

            compute_energy(project = project, tests = kept_tests, commit = fix)
            vuln_build_failure = compute_energy(project = project, tests = kept_tests, commit = vuln)
            
            if not vuln_build_failure:
                logger.error(f"Energy measurement failed for commit {vuln[:8]}. Skipping pair.")
                continue

            coverage_path = os.path.join(project.output_dir, f"{project.name}_{vuln[:8]}_{fix[:8]}_coverage.json")
            coverage_dict['execution_time'] = time.time() - start_time
            with open(coverage_path, "w") as f:
                json.dump(coverage_dict, f, indent=2, default=list)
            logger.info(f"Saved coverage results to {coverage_path}")
            
if __name__ == "__main__":
    main()