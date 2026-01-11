# Script to perform test compilation and find touched file(s) in both vuln and fix commit

import os
import subprocess
import csv
import logging
import json
import time

# ==========================================
# CONFIGURATION
# ==========================================
REPO_NAME = "FFmpeg"       
VULN_COMMIT = "89505d38de989bddd579ce3b841f1c011f1d7bf2"     
FIX_COMMIT = "9ffa49496d1aae4cbbb387aac28a9e061a6ab0a6"      

TEST_LIMIT = None  # Set to integer (e.g., 50) for testing, or None for all.

# ==========================================
# PATHS
# ==========================================
BASE_DIR = os.getcwd()
PROJECT_DIR = os.path.join(BASE_DIR, "ds_projects", REPO_NAME)
RESULTS_DIR = os.path.join(BASE_DIR, "vfec_results")
LOG_DIR = os.path.join(RESULTS_DIR, "log")
CACHE_DIR = os.path.join(RESULTS_DIR, "cache") # New directory for checkpoints

# File Paths
OUTPUT_CSV = os.path.join(RESULTS_DIR, f"{REPO_NAME}_{VULN_COMMIT[:8]}_{FIX_COMMIT[:8]}_testCompile.csv")
LOG_FILE = os.path.join(LOG_DIR, f"log_{REPO_NAME}_{VULN_COMMIT[:8]}_{FIX_COMMIT[:8]}.txt")
VULN_CHECKPOINT = os.path.join(CACHE_DIR, f"checkpoint_{VULN_COMMIT[:8]}.json")

SAMPLES_DIR = os.path.join(BASE_DIR, "ds_projects", "fate-samples")
WORKLOAD_FILE_OPENSSL = "input_3gb.bin"

# ==========================================
# SETUP
# ==========================================
for d in [RESULTS_DIR, LOG_DIR, CACHE_DIR]:
    if not os.path.exists(d): os.makedirs(d)

logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
console = logging.StreamHandler()
console.setLevel(logging.INFO)
logging.getLogger('').addHandler(console)

# ==========================================
# HELPERS
# ==========================================
def run_command(command, cwd, ignore_errors=False):
    try:
        result = subprocess.run(command, cwd=cwd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        if result.returncode != 0 and not ignore_errors:
            logging.error(f"FAIL: {command}\nSTDERR: {result.stderr.strip()}")
            return False
        return True
    except Exception as e:
        logging.error(f"EXCEPTION: {e}")
        return False

def save_checkpoint(filepath, data):
    """Saves dictionary to JSON immediately."""
    try:
        with open(filepath, 'w') as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logging.error(f"Failed to save checkpoint: {e}")

def load_checkpoint(filepath):
    """Loads dictionary from JSON if exists."""
    if os.path.exists(filepath):
        with open(filepath, 'r') as f:
            return json.load(f)
    return {}

def clean_repo(cwd):
    run_command("git reset --hard", cwd)
    run_command("git clean -fdx", cwd)

def reset_coverage_counters(cwd):
    subprocess.run("find . -name '*.gcda' -delete", cwd=cwd, shell=True)

def get_covered_files(cwd):
    covered = set()
    for root, dirs, files in os.walk(cwd):
        for file in files:
            if file.endswith(".gcda"):
                source_name = file.replace(".gcda", ".c")
                rel_dir = os.path.relpath(root, cwd)
                full_path = source_name if rel_dir == "." else os.path.join(rel_dir, source_name)
                covered.add(full_path)
    return list(covered) # Convert to list for JSON serialization

def get_git_diff_files(cwd, commit_hash):
    cmd = f"git diff-tree --no-commit-id --name-only -r {commit_hash}"
    result = subprocess.run(cmd, cwd=cwd, shell=True, stdout=subprocess.PIPE, text=True)
    return {f for f in result.stdout.strip().split('\n') if f}

def get_test_suite(cwd):
    if REPO_NAME.lower() == "ffmpeg":
        logging.info("Fetching FATE tests...")
        res = subprocess.run("make fate-list", cwd=cwd, shell=True, stdout=subprocess.PIPE, text=True)
        tests = [l.strip() for l in res.stdout.split('\n') if l.strip().startswith("fate-")]
        if TEST_LIMIT: tests = tests[:TEST_LIMIT]
        return [{"name": t, "cmd": f"make {t} SAMPLES={SAMPLES_DIR} -j$(nproc)"} for t in tests]
    elif REPO_NAME.lower() == "openssl":
        return [{"name": "standard_workload", "cmd": f"env LD_LIBRARY_PATH=. ./apps/openssl enc -aes-256-cbc -salt -in {WORKLOAD_FILE_OPENSSL} -out output_test.enc -pass pass:12345"}]
    return []

# ==========================================
# PHASE 1: VULN COMMIT (Build Cache)
# ==========================================
def run_vuln_phase(target_files):
    logging.info(f"=== Phase 1: Vuln Commit {VULN_COMMIT} ===")
    
    # 1. Check if we already have a full checkpoint
    cached_data = load_checkpoint(VULN_CHECKPOINT)
    if cached_data.get("status") == "COMPLETE":
        logging.info("Vuln phase already completed (Loaded from Checkpoint).")
        return cached_data["results"]

    # 2. Build
    clean_repo(PROJECT_DIR)
    if not run_command(f"git checkout -f {VULN_COMMIT}", PROJECT_DIR): return None
    if REPO_NAME.lower() == "ffmpeg":
        run_command("./configure --disable-asm --disable-doc --extra-cflags='--coverage' --extra-ldflags='--coverage'", PROJECT_DIR)
    elif REPO_NAME.lower() == "openssl":
        run_command("./config -d --coverage", PROJECT_DIR)
    
    if not run_command("make -j$(nproc)", PROJECT_DIR): return None

    # 3. Run Tests
    suite = get_test_suite(PROJECT_DIR)
    results = cached_data.get("results", {}) # Resume if partial

    print(f"Running {len(suite)} tests for Vuln Commit...")
    
    for i, test in enumerate(suite):
        t_name = test['name']
        
        # Skip if already done
        if t_name in results: continue
        
        if i % 5 == 0: print(f"  [Vuln] Test {i}/{len(suite)}: {t_name}")
        
        reset_coverage_counters(PROJECT_DIR)
        run_command(test['cmd'], PROJECT_DIR, ignore_errors=True)
        
        # Capture coverage
        covered_list = get_covered_files(PROJECT_DIR)
        
        # OPTIMIZATION: Only save if it touched a Target File
        # We save memory/disk by filtering early, but we save exact matches.
        relevant_files = [f for f in covered_list if f in target_files]
        
        if relevant_files:
            results[t_name] = relevant_files
            # Update Checkpoint immediately
            save_checkpoint(VULN_CHECKPOINT, {"status": "IN_PROGRESS", "results": results})

    # Mark complete
    save_checkpoint(VULN_CHECKPOINT, {"status": "COMPLETE", "results": results})
    return results

# ==========================================
# PHASE 2: FIX COMMIT (Compare & Write)
# ==========================================
def run_fix_phase(vuln_results, target_files):
    logging.info(f"=== Phase 2: Fix Commit {FIX_COMMIT} ===")
    
    # 1. Build
    clean_repo(PROJECT_DIR)
    if not run_command(f"git checkout -f {FIX_COMMIT}", PROJECT_DIR): return
    if REPO_NAME.lower() == "ffmpeg":
        run_command("./configure --disable-asm --disable-doc --extra-cflags='--coverage' --extra-ldflags='--coverage'", PROJECT_DIR)
    elif REPO_NAME.lower() == "openssl":
        run_command("./config -d --coverage", PROJECT_DIR)
    if not run_command("make -j$(nproc)", PROJECT_DIR): return

    # 2. Prepare CSV
    headers = ["project", "vuln_commit", "v_testname", "fix_commit", "f_testname", "sourcefile"]
    file_exists = os.path.isfile(OUTPUT_CSV)
    
    # Open file ONCE in append mode
    f_csv = open(OUTPUT_CSV, mode='a', newline='')
    writer = csv.writer(f_csv)
    if not file_exists:
        writer.writerow(headers)
        f_csv.flush()

    # 3. Run Tests
    suite = get_test_suite(PROJECT_DIR)
    print(f"Running {len(suite)} tests for Fix Commit...")

    processed_tests = set()

    for i, test in enumerate(suite):
        t_name = test['name']
        processed_tests.add(t_name)
        
        if i % 5 == 0: print(f"  [Fix] Test {i}/{len(suite)}: {t_name}")

        reset_coverage_counters(PROJECT_DIR)
        run_command(test['cmd'], PROJECT_DIR, ignore_errors=True)
        
        covered_list = get_covered_files(PROJECT_DIR)
        
        # CHECK MATCHES IMMEDIATELY
        for target in target_files:
            v_covered = (t_name in vuln_results) and (target in vuln_results[t_name])
            f_covered = target in covered_list

            if v_covered or f_covered:
                v_entry = t_name if v_covered else ""
                f_entry = t_name if f_covered else ""
                
                # Write Row Immediately
                writer.writerow([REPO_NAME, VULN_COMMIT, v_entry, FIX_COMMIT, f_entry, target])
                f_csv.flush()
                os.fsync(f_csv.fileno()) # Force write to disk

    # 4. Handle Tests that existed in Vuln but were deleted/missing in Fix
    # (Rare, but we must check vuln_results for tests not in processed_tests)
    for t_name, covered_files in vuln_results.items():
        if t_name not in processed_tests:
            for target in target_files:
                if target in covered_files:
                    # It was covered in Vuln, but test didn't run in Fix
                    writer.writerow([REPO_NAME, VULN_COMMIT, t_name, FIX_COMMIT, "", target])
                    f_csv.flush()
                    os.fsync(f_csv.fileno())

    f_csv.close()
    print(f"Done. Final CSV: {OUTPUT_CSV}")

# ==========================================
# MAIN
# ==========================================
def main():
    if not os.path.exists(PROJECT_DIR): return

    # 1. Get Targets
    target_files = get_git_diff_files(PROJECT_DIR, FIX_COMMIT)
    print(f"Target Files: {target_files}")
    if not target_files: return

    # 2. Run Vuln (Saves to Checkpoint JSON)
    vuln_results = run_vuln_phase(target_files)
    if vuln_results is None: return

    # 3. Run Fix (Reads Checkpoint, Writes to Final CSV)
    run_fix_phase(vuln_results, target_files)

if __name__ == "__main__":
    main()