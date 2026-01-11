import os
import sys
import logging
import pandas as pd
import subprocess
import shutil
import re
import csv
from datetime import datetime

# --- Configuration ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DS_SNAPSHOT_PATH = os.path.join(BASE_DIR, 'vfec_results', 'ds_snapshot.csv')
PROJECTS_SOURCE_DIR = os.path.join(BASE_DIR, 'ds_projects')
RESULTS_DIR = os.path.join(BASE_DIR, 'vfec_results')
OUTPUT_CSV = os.path.join(RESULTS_DIR, 'tc_results.csv')
LOG_FILE = os.path.join(RESULTS_DIR, 'logs/tc_verifier_log.txt')      # Summary Log (Info/Errors)
FULL_LOG_FILE = os.path.join(RESULTS_DIR, 'logs/log_tc_results.txt')   # Complete Build/Test Output

# --- Logging Setup ---
if not os.path.exists(RESULTS_DIR):
    os.makedirs(RESULTS_DIR)

# 1. Summary Logger (Console + Summary File)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, mode='w'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger()

# 2. Full Log Helper
# We write directly to file to avoid formatting overhead for massive build logs
def append_full_log(header, content):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(FULL_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n{'='*80}\n")
        f.write(f"[{timestamp}] {header}\n")
        f.write(f"{'='*80}\n")
        f.write(f"{content}\n")
        f.write("-" * 80 + "\n")

# Initialize Full Log File
with open(FULL_LOG_FILE, "w", encoding="utf-8") as f:
    f.write(f"VFEC Build & Test Full Log - Started {datetime.now()}\n")

def run_cmd(cmd, cwd, timeout=600):
    """Runs a shell command and returns stdout, stderr, and return code."""
    try:
        # Run with timeout to prevent hanging on interactive prompts
        result = subprocess.run(
            cmd, 
            cwd=cwd, 
            shell=True, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE, 
            timeout=timeout,
            universal_newlines=True,
            errors='ignore'
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Command timed out"
    except Exception as e:
        return -1, "", str(e)

def clean_repo(repo_path):
    """Hard resets the repo to ensure clean state."""
    run_cmd("git reset --hard", repo_path)
    run_cmd("git clean -fdx", repo_path)

def checkout_commit(repo_path, commit_hash):
    clean_repo(repo_path)
    code, out, err = run_cmd(f"git checkout {commit_hash}", repo_path)
    if code != 0:
        logger.error(f"Failed to checkout {commit_hash}")
        append_full_log(f"CHECKOUT FAILED: {commit_hash}", f"STDOUT:\n{out}\nSTDERR:\n{err}")
        return False
    return True

def detect_and_build(repo_path, project_name, stage):
    """
    Attempts to detect build system and build with Coverage Flags.
    Returns: (success_bool, build_log, exec_dir)
    """
    # Environment variables for Coverage
    env_flags = "CFLAGS='-fprofile-arcs -ftest-coverage -O0' CXXFLAGS='-fprofile-arcs -ftest-coverage -O0' LDFLAGS='-fprofile-arcs -ftest-coverage'"
    
    full_log_output = ""
    success = False
    exec_dir = repo_path

    # 1. CMake
    if os.path.exists(os.path.join(repo_path, "CMakeLists.txt")):
        logger.info("Detected CMake.")
        build_dir = os.path.join(repo_path, "build_vfec")
        if not os.path.exists(build_dir):
            os.makedirs(build_dir)
        exec_dir = build_dir
        
        # Configure
        cmd_conf = f"cmake -DCMAKE_BUILD_TYPE=Debug -DCMAKE_C_FLAGS='-fprofile-arcs -ftest-coverage' -DCMAKE_CXX_FLAGS='-fprofile-arcs -ftest-coverage' .."
        c, o, e = run_cmd(cmd_conf, build_dir)
        full_log_output += f"\n--- CMake Conf ---\nSTDOUT:\n{o}\nSTDERR:\n{e}"
        
        if c == 0:
            # Build
            cmd_build = "make -j$(nproc)"
            c2, o2, e2 = run_cmd(cmd_build, build_dir)
            full_log_output += f"\n--- CMake Build ---\nSTDOUT:\n{o2}\nSTDERR:\n{e2}"
            if c2 == 0: success = True
        
    # 2. Autogen/Configure
    elif os.path.exists(os.path.join(repo_path, "configure")) or os.path.exists(os.path.join(repo_path, "autogen.sh")):
        logger.info("Detected Autotools.")
        
        # Run autogen if needed
        if not os.path.exists(os.path.join(repo_path, "configure")):
            c, o, e = run_cmd("./autogen.sh", repo_path)
            full_log_output += f"\n--- Autogen ---\nSTDOUT:\n{o}\nSTDERR:\n{e}"
        
        # Configure
        cmd_conf = f"{env_flags} ./configure --disable-shared --enable-static"
        c, o, e = run_cmd(cmd_conf, repo_path)
        full_log_output += f"\n--- Configure ---\nSTDOUT:\n{o}\nSTDERR:\n{e}"
        
        if c == 0:
            cmd_build = f"{env_flags} make -j$(nproc)"
            c2, o2, e2 = run_cmd(cmd_build, repo_path)
            full_log_output += f"\n--- Make ---\nSTDOUT:\n{o2}\nSTDERR:\n{e2}"
            if c2 == 0: success = True

    # 3. Simple Makefile
    elif os.path.exists(os.path.join(repo_path, "Makefile")):
        logger.info("Detected Makefile.")
        cmd_build = f"{env_flags} make -j$(nproc)"
        c, o, e = run_cmd(cmd_build, repo_path)
        full_log_output += f"\n--- Make ---\nSTDOUT:\n{o}\nSTDERR:\n{e}"
        if c == 0: success = True

    else:
        full_log_output = "No recognized build system (CMake, configure, Makefile) found."
        success = False

    # WRITE LOG
    append_full_log(f"BUILD LOG: {project_name} ({stage})", full_log_output)
    
    return success, full_log_output, exec_dir

def parse_test_output(output_str):
    """
    Parses generic test outputs (CTest, Automake, GTest) to find test names.
    Returns: list of dicts [{'name': 'testA', 'status': 'PASS'}, ...]
    """
    tests = []
    
    # Pattern 1: CTest / GTest (" 1/5 Test #1: TestName ... Passed")
    # Regex: Look for "Test #N: Name ... Status"
    ctest_pattern = re.compile(r"Test\s+#\d+:\s+(\S+)\s+\.+\s+(Passed|Failed)", re.IGNORECASE)
    
    # Pattern 2: Automake ("PASS: test-name")
    automake_pattern = re.compile(r"^(PASS|FAIL|XFAIL|XPASS|SKIP|ERROR):\s+(.+)$", re.MULTILINE)

    # Apply CTest
    for match in ctest_pattern.finditer(output_str):
        name = match.group(1)
        status = match.group(2).upper() # PASSED/FAILED
        tests.append({'name': name, 'status': status})

    # If CTest found nothing, try Automake
    if not tests:
        for match in automake_pattern.finditer(output_str):
            status = match.group(1) # PASS/FAIL
            name = match.group(2).strip()
            # Normalize status
            if "PASS" in status: status = "PASSED"
            else: status = "FAILED"
            tests.append({'name': name, 'status': status})
            
    return tests

def run_tests(cwd, project_name, stage):
    """
    Attempts to run tests via standard commands (ctest, make check, make test).
    """
    full_log = ""
    tests_found = []
    
    # Priority 1: CTest (Standard for CMake)
    # -V gives verbose output with test names
    c, o, e = run_cmd("ctest -V", cwd, timeout=300)
    full_log += f"\n--- CTest Attempt ---\nSTDOUT:\n{o}\nSTDERR:\n{e}"
    
    if c == 0 or "Test project" in o:
        tests_found = parse_test_output(o)
        
    # Priority 2: Make Check / Make Test (Only if CTest didn't find anything)
    if not tests_found:
        for target in ["check", "test"]:
            c, o, e = run_cmd(f"make {target} -k", cwd, timeout=300) # -k keeps going after fail
            full_log += f"\n--- Make {target} Attempt ---\nSTDOUT:\n{o}\nSTDERR:\n{e}"
            if "No rule to make target" not in e:
                parsed = parse_test_output(o)
                if parsed:
                    tests_found = parsed
                    break

    append_full_log(f"TEST RUN LOG: {project_name} ({stage})", full_log)
    return tests_found, full_log

def process_commit_stage(repo_path, commit_hash, stage_name, project_name):
    """
    Orchestrates Checkout -> Build -> Test for a single commit.
    Returns: (build_success, total_tc, passed_tc, test_list_of_dicts)
    """
    logger.info(f"  > Processing {stage_name} commit: {commit_hash[:8]}")
    
    # 1. Checkout
    if not checkout_commit(repo_path, commit_hash):
        return False, 0, 0, [], "Checkout Failed"

    # 2. Build
    build_success, build_log, exec_dir = detect_and_build(repo_path, project_name, stage_name)
    if not build_success:
        logger.warning(f"    Build Failed for {stage_name}. See {FULL_LOG_FILE} for details.")
        return False, 0, 0, [], "Build Failed"

    # 3. Run Tests
    logger.info(f"    Build Success. Running Tests in {exec_dir}...")
    tests, test_log = run_tests(exec_dir, project_name, stage_name)
    
    total = len(tests)
    passed = sum(1 for t in tests if t['status'] == 'PASSED')
    
    logger.info(f"    Tests: {passed}/{total} Passed.")
    
    return True, total, passed, tests, "Success"

def main():
    if not os.path.exists(DS_SNAPSHOT_PATH):
        logger.error(f"Snapshot file not found: {DS_SNAPSHOT_PATH}")
        sys.exit(1)

    df = pd.read_csv(DS_SNAPSHOT_PATH)
    
    # Prepare Output CSV Header
    fieldnames = [
        'project', 
        'vuln_commit', 'vuln_total_tc', 'vuln_passed_tc', 'vuln_tn', 'vuln_sf',
        'fix_commit', 'fix_total_tc', 'fix_passed_tc', 'fix_tn', 'fix_sf'
    ]
    
    # Open CSV for writing
    with open(OUTPUT_CSV, 'w', newline='') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        
        for index, row in df.iterrows():
            project = row['project']
            vuln_hash = row['vuln_commit']
            fix_hash = row['fix_commit']
            
            repo_path = os.path.join(PROJECTS_SOURCE_DIR, project)
            if not os.path.exists(repo_path):
                logger.warning(f"Repo {project} not found. Skipping.")
                continue

            logger.info(f"[{index+1}/{len(df)}] Analyzing {project}...")
            append_full_log(f"START PROJECT: {project}", f"Vuln: {vuln_hash} | Fix: {fix_hash}")

            # --- PROCESS VULN COMMIT ---
            v_build, v_tot, v_pass, v_tests, v_msg = process_commit_stage(repo_path, vuln_hash, "VULN", project)
            
            # --- PROCESS FIX COMMIT ---
            f_build, f_tot, f_pass, f_tests, f_msg = process_commit_stage(repo_path, fix_hash, "FIX", project)

            # --- ORGANIZE DATA FOR CSV ---
            # Scenario A: Build Failed -> One row recording failure
            if not v_build or not f_build:
                writer.writerow({
                    'project': project,
                    'vuln_commit': vuln_hash, 'vuln_total_tc': 0, 'vuln_passed_tc': 0, 
                    'vuln_tn': v_msg if not v_build else "See detailed rows", 'vuln_sf': "",
                    'fix_commit': fix_hash, 'fix_total_tc': 0, 'fix_passed_tc': 0, 
                    'fix_tn': f_msg if not f_build else "See detailed rows", 'fix_sf': ""
                })
                continue
            
            # Scenario B: Build Success -> Map tests
            max_rows = max(len(v_tests), len(f_tests), 1) # At least 1 row
            
            for i in range(max_rows):
                # Get Vuln Test Data
                v_data = v_tests[i] if i < len(v_tests) else None
                v_name = v_data['name'] if v_data else ""
                v_sf = "Pending_Coverage_Analysis" if v_data else ""
                
                # Get Fix Test Data
                f_data = f_tests[i] if i < len(f_tests) else None
                f_name = f_data['name'] if f_data else ""
                f_sf = "Pending_Coverage_Analysis" if f_data else "" 

                writer.writerow({
                    'project': project,
                    'vuln_commit': vuln_hash,
                    'vuln_total_tc': v_tot,
                    'vuln_passed_tc': v_pass,
                    'vuln_tn': v_name,
                    'vuln_sf': v_sf,
                    'fix_commit': fix_hash,
                    'fix_total_tc': f_tot,
                    'fix_passed_tc': f_pass,
                    'fix_tn': f_name,
                    'fix_sf': f_sf
                })
            
            # Cleanup
            clean_repo(repo_path)
            
    logger.info(f"Verification complete. Results saved to {OUTPUT_CSV}")

if __name__ == "__main__":
    main()