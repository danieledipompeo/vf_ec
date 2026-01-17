import os
import subprocess
import csv
import logging
import json
import time
import math
import sys
import urllib.request
import pandas as pd
import re


class ProgressBar:
    def __init__(self, total, length=40, step=1):
        self.total = total
        self.length = length
        self.step = step

    def update(self, i):
        progress = (i + 1) / self.total
        filled = int(self.length * progress)
        bar = '█' * filled + '░' * (self.length - filled)
        print(f"\r[{bar}] {i+1}/{self.total}", end='', flush=True)

    def log(self, msg):
        print()
        print(msg)
        self.update(self.current)

    def set(self, i):
        self.current = i
        self.update(i)

# ==========================================
# CONFIGURATION
# ==========================================
REPO_NAME = "openssl"
TARGET_DURATION_SEC = 2.0
CSV_WRITE_INTERVAL = 50
TEST_LIMIT = None

GIST_CSV_URL = "https://gist.githubusercontent.com/waheed-sep/935cfc1ba42b2475d45336a4c779cbc8/raw/ea91568360d87979373a7eca38f289c9bf30d103/cwe_projects.csv"

# ==========================================
# PATHS
# ==========================================
BASE_DIR = "/app"
INPUT_DIR = os.path.join(BASE_DIR, "inputs")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
INPUT_CSV = os.path.join(INPUT_DIR, "cwe_projects.csv")
PROJECT_DIR = os.path.join(INPUT_DIR, REPO_NAME)
LOG_DIR = os.path.join(OUTPUT_DIR, "log")
CACHE_DIR = os.path.join(LOG_DIR, "cache")
GCDA_DIR = os.path.join(OUTPUT_DIR, "gcda_files")

ITERATIONS = 5

COOL_DOWN_TO_SEC = 1.0

ENERGY_RE = re.compile(r'\bpower/energy-[^/\s]+/?\b')

for d in [INPUT_DIR, OUTPUT_DIR, LOG_DIR, CACHE_DIR, GCDA_DIR]:
    if not os.path.exists(d): os.makedirs(d)

LOG_FILE = os.path.join(LOG_DIR, "pipeline_execution.log")
logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ==========================================
# HELPERS
# ==========================================
def run_command(command, cwd, ignore_errors=False):
    try:
        env = os.environ.copy()
        env["LC_ALL"] = "C"
        result = subprocess.run(command, cwd=cwd, shell=True, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        if result.returncode != 0 and not ignore_errors:
            logging.error(f"FAIL: {command}\nSTDERR: {result.stderr.strip()}")
            return False
        return True
    except Exception as e:
        logging.error(f"EXCEPTION: {e}")
        return False

def save_json(filepath, data):
    try:
        with open(filepath, 'w') as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logging.error(f"JSON Save Error: {e}")

def load_json(filepath):
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r') as f:
                return json.load(f)
        except:
            return {}
    return {}

def clean_repo(cwd):
    run_command("git reset --hard", cwd)
    run_command("git clean -fdx", cwd)

def download_csv_if_missing():
    if not os.path.exists(INPUT_CSV):
        print(f"Downloading input CSV from Gist to {INPUT_CSV}...")
        try:
            urllib.request.urlretrieve(GIST_CSV_URL, INPUT_CSV)
            print("Download complete.")
        except Exception as e:
            print(f"Error downloading CSV: {e}")
            sys.exit(1)

def get_git_diff_files(cwd, commit_hash):
    cmd = f"git diff-tree --no-commit-id --name-only -r {commit_hash}"
    result = subprocess.run(cmd, cwd=cwd, shell=True, stdout=subprocess.PIPE, text=True)
    return {f for f in result.stdout.strip().split('\n') if f}

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

def flush_buffer_to_csv(filepath, buffer, fieldnames):
    if not buffer: return
    file_exists = os.path.exists(filepath)
    try:
        with open(filepath, 'a', newline='') as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(fieldnames)
            writer.writerows(buffer)
            f.flush()
            os.fsync(f.fileno())
        buffer.clear()
    except Exception as e:
        logging.error(f"Failed to write CSV: {e}")

# ==========================================
# OPENSSL CONFIGURATION & BUILD
# ==========================================
def configure_openssl(cwd, coverage=False):
    config_args = ["./config", "-d", "no-shared", "no-asm", "no-threads"]
    cflags = "-fPIC -Wno-error -Wno-implicit-function-declaration -Wno-format-security -std=gnu89 -O0"
    lflags = "-no-pie"

    if coverage:
        cflags += " --coverage"
        lflags += " --coverage"

    full_cmd = f'CC="gcc {cflags} {lflags}" {" ".join(config_args)}'
    
    if run_command(full_cmd, cwd, ignore_errors=True):
        return True
    
    logging.info("Standard config failed, trying ./Configure linux-x86_64...")
    fallback_cmd = f'CC="gcc {cflags} {lflags}" ./Configure linux-x86_64 no-shared no-asm'
    return run_command(fallback_cmd, cwd)

def build_openssl(cwd):
    run_command("make clean", cwd, ignore_errors=True)
    run_command("make depend", cwd, ignore_errors=True)
    if run_command("make", cwd): 
        return True
    logging.error("Make failed.")
    return False

def get_openssl_tests(cwd):
    tests = []
    recipes_dir = os.path.join(cwd, "test", "recipes")
    test_dir = os.path.join(cwd, "test")

    # Strategy 1: Modern OpenSSL
    if os.path.exists(recipes_dir):
        logging.info("Detected Modern OpenSSL.")
        try:
            files = sorted(os.listdir(recipes_dir))
            for f in files:
                if f.endswith(".t"):
                    t_name = f[:-2]
                    # Modern: 'make test' runs the test wrapper
                    tests.append({
                        "name": t_name, 
                        "cmd": f"make test TESTS='{t_name}'",
                        "type": "modern"
                    })
        except Exception: pass

    # Strategy 2: Legacy OpenSSL
    elif os.path.exists(test_dir):
        logging.info("Detected Legacy OpenSSL.")
        try:
            files = sorted(os.listdir(test_dir))
            for f in files:
                if f.startswith("test_") and f.endswith(".c"):
                    t_name = f[:-2]
                    # Legacy: We must BUILD with 'make' then RUN the binary
                    # Binary location varies: sometimes ./test_sha, sometimes ./test/test_sha
                    tests.append({
                        "name": t_name, 
                        "cmd": f"make {t_name}", # Command to BUILD (for Coverage phase)
                        "run_bin": f"test/{t_name}", # Binary to RUN (for Energy phase)
                        "type": "legacy"
                    })
                elif f.endswith("test.c"):
                     t_name = f[:-2]
                     tests.append({
                        "name": t_name, 
                        "cmd": f"make {t_name}",
                        "run_bin": f"test/{t_name}",
                        "type": "legacy"
                     })
        except Exception: pass
            
    if TEST_LIMIT and tests: tests = tests[:TEST_LIMIT]
    return tests

# ==========================================
# PHASE 1: COVERAGE
# ==========================================

def process_commit(commit, coverage=True): 
        logging.info(f"Building {commit[:8]} (Coverage)...")
        clean_repo(PROJECT_DIR)
        run_command(f"git checkout -f {commit}", PROJECT_DIR)
        
        if not configure_openssl(PROJECT_DIR, coverage=coverage): return False
        if not build_openssl(PROJECT_DIR): return False
        
        suite = get_openssl_tests(PROJECT_DIR)
        print(f"\nRunning {len(suite)} tests...")

        commit_results = {
            "hash": commit,
            "tests": []
        }

        pb = ProgressBar(len(suite), step=10)
        for i, t in enumerate(suite):
            pb.set(i)

            test = {
                "name": t['name'],
                "failed": False,
                "cmd": t['cmd'],
                "covered_files": []
            }

            # Clean previous coverage data
            run_command("find . -name '*.gcda' -delete", PROJECT_DIR)
            
            # For Coverage, 'make target' is fine as it usually runs the test too or we assume build covers it.
            # But usually we need to RUN it to get coverage.
            # If legacy, running 'make test_name' might NOT run it.
            # Let's force run if legacy.

            if not run_command(test.get('cmd'), PROJECT_DIR):
                if test.get("type") == "legacy":
                    logging.info(f"[Legacy] Running binary for test: {test.get('name')}")
                    if not run_command(test.get('run_bin'), PROJECT_DIR, ignore_errors=True):
                        logging.warning(f"Test Build/Run Failed: {test.get('name')}")
                        test['failed'] = True
                        commit_results['tests'].append(test)
                        continue
                logging.warning(f"Test Build/Run Failed: {test.get('name')}")
                test['failed'] = True
                commit_results['tests'].append(test)
                continue
            
            covered = get_covered_files(PROJECT_DIR)
            test['covered_files'] = covered
            commit_results['tests'].append(test)
            
        return commit_results

def prepare_for_energy_measurement():
    """
    Prepare the project for energy measurement.
    Build the project without coverage.
    
    :param rapl_pkg: Description
    :param test: Description
    :param commit: Description
    """
    print("\nPreparing project for energy measurement...")

    configure_openssl(PROJECT_DIR, coverage=False)
    build_openssl(PROJECT_DIR)
    
def run_phase_1_coverage(vuln, fix):
    """
    Run Phase 1 coverage analysis on a vulnerability and its fix commits.
    This function analyzes test coverage for changed files between a vulnerability
    commit and its corresponding fix commit. It processes both commits by running
    tests, extracting relevant test coverage, and measuring energy consumption
    using RAPL (Running Average Power Limit) for each successful test.
    Args:
        vuln (str): The git commit hash of the vulnerability commit.
        fix (str): The git commit hash of the fix commit.
    Returns:
        dict or None: A dictionary containing coverage analysis results with the structure:
            {
                "project": str,
                    "hash": str,
                    "failed": {"status": bool, "reason": str},
                    "tests": list
                    "hash": str,
                    "failed": {"status": bool, "reason": str},
                    "tests": list
        Returns None if:
        - Checking out the fix commit fails
        - No changed files are found in git diff
        - No successful tests exist in either commit
    """
    logging.info(f"--- Phase 1: Coverage {vuln[:8]} -> {fix[:8]} ---")
    coverage_results = {
        "project": REPO_NAME,
        "vuln_commit": {
            "hash": vuln,
            "failed":{
                "status": False,
                "reason": ""
            },
            "tests": []
        },
        "fix_commit": {
            "hash": fix,
            "failed": {
                "status": False,
                "reason": ""
            },
            "tests": []
        }
    }
    
    clean_repo(PROJECT_DIR)
    if not run_command(f"git checkout -f {fix}", PROJECT_DIR):
        logging.error(f"Failed to checkout fix commit: {fix}")
        return None
    
    git_changed_files= get_git_diff_files(PROJECT_DIR, fix)
    
    if not git_changed_files:
        logging.error("No target files found in git diff.")
        return None
    
    # FIX COMMIT
    coverage_results['fix_commit'] = process_commit(fix)
    if not coverage_results['fix_commit'].get('tests') or all(t.get('failed', True) for t in coverage_results['fix_commit']['tests']):
        logging.error("No successful tests in fix commit. Skipping vuln commit.")
        return None
    
    if coverage_results.get('failed', {}).get('status', False): 
        extract_test_covering_git_changes(coverage_results['fix_commit'].get('tests', []), git_changed_files)
    logging.info(f"Extracted tests covering changed files in pair ({vuln[:8]}, {fix[:8]}).")
    
    logging.info(f"Now computing energy for {fix[:8]}.")
    
    # extract RAPL package events
    rapl_pkg = detect_rapl()

    kept_tests = [t for t in coverage_results['fix_commit'].get('tests', []) if t.get('keep', True) and not t.get('failed', True)]

    prepare_for_energy_measurement()
    for test in kept_tests:
        measure_test(rapl_pkg, test, fix)

    # VULN COMMIT
    coverage_results['vuln_commit'] = process_commit(vuln)
    if not coverage_results['vuln_commit'].get('tests') or all(t.get('failed', True) for t in coverage_results['vuln_commit']['tests']):
        logging.error("No successful tests in vuln commit. Skipping processing.")
        return None

    logging.info(f"{vuln[:8]} completed.")

    if coverage_results.get('failed', {}).get('status', False): 
        extract_test_covering_git_changes(coverage_results['vuln_commit'].get('tests', []), git_changed_files)
    logging.info(f"Extracted tests covering changed files in pair ({vuln[:8]}, {fix[:8]}).")
    logging.info(f"Now computing energy for {vuln[:8]}.")
    
    kept_tests = [t for t in coverage_results['vuln_commit'].get('tests', []) if t.get('keep', True) and not t.get('failed', True)]
    
    prepare_for_energy_measurement()
    for test in kept_tests:
        measure_test(rapl_pkg, test, vuln)
    
def extract_test_covering_git_changes(coverage_results, target_files):  
    """
    Mark "keep" in tests that cover changed files. 
    
    :param coverage_results: Description
    :param target_files: Description
    """

    for target in target_files:
        for test in coverage_results.get('tests', []):
            if coverage_results.get('failed', {}).get('status', True):
                continue
        
            for test in coverage_results.get('tests', []):
                covered_files = test.get('covered_files', [])
                test['keep'] = target in covered_files


# ==========================================
# PHASE 2: ENERGY
# ==========================================
def detect_rapl(perf_bin="perf"):
    # --no-desc makes output easier to parse if supported; if not, fall back.
    cmd = [perf_bin, "list", "--no-desc"]
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError:
        out = subprocess.check_output([perf_bin, "list"], text=True, stderr=subprocess.STDOUT)

    events = set()
    for line in out.splitlines():
        # Grab all matches from the line (some lines may include multiple tokens)
        for m in ENERGY_RE.findall(line):
            # Normalize to the canonical perf selector form with trailing '/'
            if not m.endswith("/"):
                m += "/"
            events.add(m)

    return sorted(events)

def measure_test(pkg_event, test, commit):#, core_event):
    
    pb = ProgressBar(ITERATIONS)
    perf_dir = os.path.join(OUTPUT_DIR, REPO_NAME, "perf")
    os.makedirs(perf_dir, exist_ok=True)
    print(f"Running {ITERATIONS} iterations for energy measurement...")
    for iteration in range(ITERATIONS):
        pb.set(iteration)

        perf_out = os.path.join(perf_dir, f"{commit}_{test.get('name')}__{iteration}.csv")
        
        perf_cmd = f"perf stat -a -e {pkg_event},cycles,instructions -x, --output {perf_out} sh -c '{test['cmd']}'"
        res = subprocess.run(perf_cmd, cwd=PROJECT_DIR, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        
        if res.returncode != 0: 
            logging.error(f"[STD ERR] {test.get('name')}: {res.stderr}")
            if os.path.exists(perf_out):
                os.remove(perf_out)
            logging.error(f"Perf Measurement removed due to error.")
            return None
        
        logging.info(f"[COOL DOWN] {COOL_DOWN_TO_SEC} seconds...")
        time.sleep(COOL_DOWN_TO_SEC)
        
    return None

# ==========================================
# MAIN
# ==========================================
def main():
    download_csv_if_missing()

    pairs = []
    try:
        with open(INPUT_CSV, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get('project') == REPO_NAME and 'vuln_commit' in row and 'fix_commit' in row:
                    pairs.append((row['vuln_commit'], row['fix_commit']))
    except Exception as e:
        sys.exit(1)

    for i, (vuln, fix) in enumerate(pairs):
        print(f"\n[{i+1}/{len(pairs)}] Processing Pair: {vuln[:8]} -> {fix[:8]}")
        
        coverage_dict = run_phase_1_coverage(vuln, fix)
        if coverage_dict is None:
            print(f"\nSkipping Phase 2 due to Phase 1 failure for pair {vuln[:8]} -> {fix[:8]}")
            continue

        coverage_path = os.path.join(OUTPUT_DIR, f"{REPO_NAME}_{vuln[:8]}_{fix[:8]}_coverage.json")
        with open(coverage_path, "w") as f:
                json.dump(coverage_dict, f, indent=2)

if __name__ == "__main__":
    main()
