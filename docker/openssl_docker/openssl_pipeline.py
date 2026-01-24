import os
import csv
import logging
import json
import sys
import pandas as pd
from pathlib import Path
import yaml

#from config import Config   
from common import EnergyHandler, GitHandler, ProgressBar, sh
from project import Project, ProjectFactory

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
    
    logging.info(f"Building {commit[:8]} (Coverage)...")
    GitHandler.clean_repo(project.input_dir)
    GitHandler.checkout(project.input_dir, commit)
    # run_command(f"git checkout -f {commit}", project.input_dir)
    
    commit_tests = []
    
    #if not project.configure(project.input_dir, coverage=coverage): 
    #    logging.error(f"Configuration failed for commit {commit[:8]}.")
    #    return None
    #if not project.build(project.input_dir): 
    #    logging.error(f"Build failed for commit {commit[:8]}.")
    #    return None

    if not project.build(coverage=coverage):
        logging.error(f"Build failed for commit {commit[:8]}.")
        return []
    
    suite = project.get_test()
    if not suite:
        logging.error("No tests found.")
        return []
    
    print(f"\nRunning {len(suite)} tests...")

    pb = ProgressBar(len(suite), step=10)
    for i, t in enumerate(suite[:5]):
        pb.set(i)

        test = {
            "name": t,
            "passed": False,
            "covered_files": []
        }

        # Clean previous coverage data
        sh(["find", ".", "-name", "*.gcda", "-delete"], Path(project.output_dir))

        # run test
        test['passed'], error = project.run_test(t)
        if not test['passed']:
            logging.debug(f"Test '{t}' failed with error: {error}")
            continue
        
        covered = project.coverage_file(test["name"])
        test['covered_files'] = covered
        commit_tests.append(test)
        
    return commit_tests

def coverage_energy(project: Project, commit: str, logger: logging.Logger):
    process_results = {
        "hash": commit,
        "failed":{
            "status": False,
            "reason": ""
        },
        "tests": []
    }
    
    GitHandler.clean_repo(project.input_dir)
    if not GitHandler.checkout(project.input_dir, commit):
        logging.error(f"Failed to checkout commit: {commit}")
        return None
    
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
    
    logger.info(f"Now computing energy for {commit[:8]}.")
    
    # extract RAPL package events
    rapl_pkg = EnergyHandler.detect_rapl()

    kept_tests = [t for t in process_results.get('tests', []) if t.get('keep', True) and t.get('passed', False)]

    # prepare_for_energy_measurement()
    project.build(coverage=False)
    for test in kept_tests:
        EnergyHandler.measure_test(rapl_pkg, test, commit, project.output_dir, project_dir=project.input_dir)

    return process_results


def process_pair(project: Project, vuln: str, fix: str, logger: logging.Logger):
    """
    Run coverage+energy for a vuln/fix pair and collate results.
    """

    pair_result = {
        "project": project.name,
        "fix_commit": {},
        "vuln_commit": {}
    }

    for label, commit in (("fix_commit", fix), ("vuln_commit", vuln)):
        
        coverage_dict = coverage_energy(project, commit, logger)
        if coverage_dict is None:
            logger.error(f"Skipping pair due to {label} failure: {commit[:8]}")
            return None
        pair_result[label] = coverage_dict

    return pair_result
    
    
def extract_test_covering_git_changes(coverage_results: dict, target_files: set):  
    """
    Mark "keep" in tests that cover changed files. 
    
    :param coverage_results: Description
    :param target_files: Description
    """

    for target in target_files:
        for test in coverage_results.get('tests', []):
            covered_files = test.get('covered_files', [])
            test['keep'] = target in covered_files

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
    
    if not os.path.isfile(dest_path):
        logging.info(f"Downloading dataset from {url} to {dest_path}...")
        sh(['wget', '-O', dest_path, url], Path(dest_dir))
    else:
        logging.info(f"Dataset already exists at {dest_path}. Skipping download.")

def main():
    # configuration = Config(os.path.join(os.path.dirname(__file__), "config.yaml"))
    configuration = load_config(os.path.join(os.path.dirname(__file__), "config.yaml"))
    
    logger = logging.getLogger("Main")
    handler = logging.FileHandler(os.path.join(configuration.get("paths", {}).get("log_dir", "."), "log.log"))
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)

    input_csv = os.path.join(configuration.get('paths', {}).get('input_dir', ""), configuration.get('dataset', {}).get('csv_file', ""))
    if input_csv and not os.path.exists(input_csv):
        download_dataset(configuration)
    
    for prj in configuration.get("project", []):
        # Extract project name and config from dict structure
        project_config = prj[list(prj.keys())[0]]
        
        in_dir = configuration.get('paths', {}).get('input_dir')
        out_dir = configuration.get('paths', {}).get('output_dir')
        project = ProjectFactory.get_project(name=project_config.get('name'), 
                                             input_dir=in_dir, 
                                             output_dir=out_dir) 
        
        logger.info(f"Starting processing: {project.name}")

        pairs = []
        try:
            # dataset_config = configuration.get('dataset')
            # if not dataset_config:
            #     logging.error("'dataset' configuration not found")
            #     sys.exit(1)
            # input_csv = dataset_config.get('csv_file')
            # if not input_csv:
            #     logger.warning("'input_csv' path not found in dataset configuration")
            #     sys.exit(1)
            with open(input_csv, 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get('project') == project_config.get('name') and 'vuln_commit' in row and 'fix_commit' in row:
                        pairs.append((row['vuln_commit'], row['fix_commit']))
        except Exception as e:
            logger.error(f"Error reading input CSV: {e}")
            sys.exit(1)

        for i, (vuln, fix) in enumerate(pairs[:10]):
            logger.info(f"[{i+1}/{len(pairs)}] Processing Pair: {vuln[:8]} -> {fix[:8]}")

            pair_result = process_pair(project, vuln, fix, logger)
            
            if pair_result is None:
                continue

            coverage_path = os.path.join(project.output_dir, f"{project.name}_{vuln[:8]}_{fix[:8]}_coverage.json")
            with open(coverage_path, "w") as f:
                json.dump(pair_result, f, indent=2)

if __name__ == "__main__":
    main()