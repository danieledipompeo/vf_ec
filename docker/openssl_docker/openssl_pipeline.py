import os
import csv
import logging
import json
from pathlib import Path
import yaml

from common import EnergyHandler, GitHandler, ProgressBar, sh
from project import Project, ProjectFactory
from logger import get_logger

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

    pb = ProgressBar(len(suite), step=10)
    # logger.warning(f"-- TEST SUITE LIMITED TO FIRST 5 TESTS OUT OF {len(suite)} TOTAL TESTS FOR DEMO PURPOSES.")
    for i, t in enumerate(suite):
        pb.set(i)

        test = {
            "name": t,
            "passed": False,
            "covered_files": []
        }

        # Clean previous coverage data
        sh(["find", ".", "-name", "*.gcda", "-delete"], Path(project.output_dir))
        sh(["find", ".", "-name", "*.gcno", "-delete"], Path(project.output_dir))
        logger.debug(f"Coverage data cleaned before running test '{t}'.")

        # run test
        test['passed'], error = project.run_test(t)
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
    
    print(f"-- Processing commit age: {GitHandler.get_age_of_commit(project.input_dir, commit)}")
    
    git_changed_files= GitHandler.get_git_diff_files(project.input_dir, commit)
    
    if not git_changed_files:
        logger.error("No target files found in git diff.")
        return None
    
    process_results['tests'] = process_commit(project, commit)
    if all(not t.get('passed', False) for t in process_results.get('tests', [])):
        logger.error("No successful tests in fix commit. Skipping processing.")
        return None
    
    extract_test_covering_git_changes(process_results, git_changed_files)
    logger.info(f"Extracted tests covering changed files for commit {commit[:8]}).")
    
    return process_results
    
    ##if not kept_tests:
    #    logger.warning(f"No tests cover the changed files. Skipping energy measurement for commit {commit[:8]}")
    #    return None
    
    #logger.info(f"Now computing energy for {commit[:8]}.")
        
    ## prepare_for_energy_measurement()
    #is_build = project.build(coverage=False)
    #if not is_build:
    #    logger.error(f"Build failed for commit {commit[:8]}. Skipping energy measurement.")
    #    return None
    
    #for test in kept_tests:
    #    project.compute_energy(test['name'], commit)

    #if compute_energy_for_tests(project=project, tests=kept_tests, commit=commit) is None:
    #    logger.error(f"Build failed for commit {commit[:8]}. Skipping energy measurement.")
    #    return None

    return process_results

def compute_energy_for_tests(project, tests, commit):
    is_build = project.build(coverage=False)
    if not is_build:
        # logger.error(f"Build failed for commit {commit[:8]}. Skipping energy measurement.")
        return None
    
    for test in tests:
        project.compute_energy(test['name'], commit)
    

#def process_pair(project: Project, vuln: str, fix: str) -> dict | None:
#    """
#    Run coverage+energy for a vuln/fix pair and collate results.
#    """
#
#    coverage_dict = compute_coverage(project, fix)
#    if coverage_dict is None :
#        logger.error(f"Skipping pair due to FIX commit failure: {fix[:8]}")
#        return None
#                  
#    kept_tests = [t for t in coverage_dict.get('tests', []) if t.get('keep', True)]
#    if not kept_tests:
#        logger.warning(f"No tests to measure energy for in FIX commit: {fix[:8]}. Skipping pair.")
#        return None
#
#    for commit in (("fix_commit", fix), ("vuln_commit", vuln)):        
#        compute_energy_for_tests(project, kept_tests, commit)
#  
#    return coverage_dict
    
    
def extract_test_covering_git_changes(coverage_results: dict, target_files: set[str]):  
    """
    Mark "keep" in tests that cover changed files. 
    
    :param coverage_results: Dictionary with test results and coverage data
    :param target_files: Set of files changed in the git commit (can be full paths)
    """
    target_files = {tf for tf in target_files if tf.endswith(('.c', '.cpp', '.h', '.hpp'))}
    for test in coverage_results.get('tests', []):
        covered_files = set(str(cf) for cf in test.get('covered_files', []))
        
        # Mark keep=True if test covers ANY of the changed files
        # Short-circuits at first match
        test['keep'] = bool(target_files & covered_files)

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
            logger.info(f"[{i+1}/{len(pairs)}] Processing Pair: {vuln[:8]} -> {fix[:8]}")

            coverage_dict = compute_coverage(project, fix)
            if coverage_dict is None :
                logger.error(f"Skipping pair due to FIX commit failure: {fix[:8]}")
                continue

            kept_tests = [t for t in coverage_dict.get('tests', []) if t.get('keep', True)]
            if not kept_tests:
                logger.error(f"No tests to measure energy for in FIX commit: {fix[:8]}. Skipping pair.")
                continue

            # Measure energy for both vuln and fix commits
            for commit in (vuln, fix):
                GitHandler.clean_repo(project.input_dir)
                GitHandler.checkout(project.input_dir, commit)
                
                is_build = project.build(coverage=False)
                if not is_build:
                    logger.error(f"Build failed for commit {commit[:8]}. Stopping pair processing.")
                    break
        
                # logger.debug("--- ENERGY MEASUREMENT LIMITED TO FIRST 2 TESTS FOR DEMO PURPOSES.")
                for test in kept_tests:
                    project.compute_energy(test['name'], commit)

            coverage_path = os.path.join(project.output_dir, f"{project.name}_{vuln[:8]}_{fix[:8]}_coverage.json")
            with open(coverage_path, "w") as f:
                json.dump(coverage_dict, f, indent=2)
            logger.info(f"Saved coverage results to {coverage_path}")
            

            

if __name__ == "__main__":
    main()