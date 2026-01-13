import os
import subprocess
import csv
import logging
import json
import time
import math
import sys

# ==========================================
# CONFIGURATION
# ==========================================
REPO_NAME = "FFmpeg"
TARGET_DURATION_SEC = 3.0
CSV_WRITE_INTERVAL = 50
TEST_LIMIT = None  # Set to integer (e.g., 5) for debugging, None for full run

# ==========================================
# PATHS
# ==========================================
BASE_DIR = "/app"
INPUT_CSV = os.path.join(BASE_DIR, "input_ffmpeg.csv")

# Input Directories (Mounted from Host)
PROJECT_DIR = os.path.join(BASE_DIR, "ds_projects", REPO_NAME)
SAMPLES_DIR = os.path.join(BASE_DIR, "ds_projects", "fate-samples")

# Output Directory (Mounted from Host)
# All CSVs, Logs, and JSONs will go here so they persist on your laptop.
RESULTS_DIR = os.path.join(BASE_DIR, "vfec_results")
LOG_DIR = os.path.join(RESULTS_DIR, "log")
CACHE_DIR = os.path.join(LOG_DIR, "cache") # As requested: log/cache

# Setup Directories
for d in [RESULTS_DIR, LOG_DIR, CACHE_DIR]:
    if not os.path.exists(d): os.makedirs(d)

# Logging Setup
LOG_FILE = os.path.join(LOG_DIR, "pipeline_execution.log")
logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
console = logging.StreamHandler()
console.setLevel(logging.INFO)
logging.getLogger('').addHandler(console)

# ==========================================
# SHARED HELPERS
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

def save_json(filepath, data):
    try:
        with open(filepath, 'w') as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logging.error(f"Failed to save JSON: {e}")

def load_json(filepath):
    if os.path.exists(filepath):
        with open(filepath, 'r') as f:
            return json.load(f)
    return {}

def clean_repo(cwd):
    """Resets the repo to clean state."""
    # WARNING: This wipes untracked files in the mounted directory!
    run_command("git reset --hard", cwd)
    run_command("git clean -fdx", cwd)

# ==========================================
# PHASE 1: COVERAGE
# ==========================================
def get_git_diff_files(cwd, commit_hash):
    cmd = f"git diff-tree --no-commit-id --name-only -r {commit_hash}"
    result = subprocess.run(cmd, cwd=cwd, shell=True, stdout=subprocess.PIPE, text=True)
    return {f for f in result.stdout.strip().split('\n') if f}

def get_fate_tests(cwd):
    if not os.path.exists(SAMPLES_DIR) or not os.listdir(SAMPLES_DIR):
        logging.warning(f"SAMPLES_DIR seems empty at {SAMPLES_DIR}. Tests may fail.")

    logging.info("Fetching FATE tests...")
    res = subprocess.run("make fate-list", cwd=cwd, shell=True, stdout=subprocess.PIPE, text=True)
    tests = [l.strip() for l in res.stdout.split('\n') if l.strip().startswith("fate-")]
    if TEST_LIMIT: tests = tests[:TEST_LIMIT]
    # -j$(nproc) for fast coverage compilation/run
    return [{"name": t, "cmd": f"make {t} SAMPLES={SAMPLES_DIR} -j$(nproc)"} for t in tests]

def get_covered_files(cwd):
    covered = set()
    for root, dirs, files in os.walk(cwd):
        for file in files:
            if file.endswith(".gcda"):
                source_name = file.replace(".gcda", ".c")
                rel_dir = os.path.relpath(root, cwd)
                full_path = source_name if rel_dir == "." else os.path.join(rel_dir, source_name)
                covered.add(full_path)
    return list(covered)

def run_phase_1_coverage(vuln_commit, fix_commit, output_csv_path, checkpoint_path):
    logging.info(f"--- Phase 1: Coverage for Pair {vuln_commit} -> {fix_commit} ---")
    
    clean_repo(PROJECT_DIR)
    run_command(f"git checkout -f {fix_commit}", PROJECT_DIR)
    target_files = get_git_diff_files(PROJECT_DIR, fix_commit)
    logging.info(f"Target Files: {target_files}")
    
    if not target_files:
        logging.error("No target files found. Skipping pair.")
        return False

    cached_data = load_json(checkpoint_path)
    vuln_results = cached_data.get("results", {})

    # A. VULN COMMIT
    if cached_data.get("status") != "COMPLETE":
        logging.info(f"Building Vuln {vuln_commit} (Coverage)...")
        clean_repo(PROJECT_DIR)
        run_command(f"git checkout -f {vuln_commit}", PROJECT_DIR)
        run_command("./configure --disable-asm --disable-doc --extra-cflags='--coverage' --extra-ldflags='--coverage'", PROJECT_DIR)
        run_command("make -j$(nproc)", PROJECT_DIR)
        
        suite = get_fate_tests(PROJECT_DIR)
        print(f"Running {len(suite)} tests for Vuln Commit...")

        for i, test in enumerate(suite):
            t_name = test['name']
            if t_name in vuln_results: continue
            
            if i % 10 == 0: print(f"  [P1-Vuln] {i}/{len(suite)}: {t_name}")
            
            run_command("find . -name '*.gcda' -delete", PROJECT_DIR)
            run_command(test['cmd'], PROJECT_DIR, ignore_errors=True)
            
            covered = get_covered_files(PROJECT_DIR)
            relevant = [f for f in covered if f in target_files]
            
            if relevant:
                vuln_results[t_name] = relevant
                save_json(checkpoint_path, {"status": "IN_PROGRESS", "results": vuln_results})
        
        save_json(checkpoint_path, {"status": "COMPLETE", "results": vuln_results})
    else:
        logging.info("Vuln phase loaded from checkpoint.")

    # B. FIX COMMIT
    logging.info(f"Building Fix {fix_commit} (Coverage)...")
    clean_repo(PROJECT_DIR)
    run_command(f"git checkout -f {fix_commit}", PROJECT_DIR)
    run_command("./configure --disable-asm --disable-doc --extra-cflags='--coverage' --extra-ldflags='--coverage'", PROJECT_DIR)
    run_command("make -j$(nproc)", PROJECT_DIR)

    f_csv = open(output_csv_path, mode='w', newline='')
    writer = csv.writer(f_csv)
    writer.writerow(["project", "vuln_commit", "v_testname", "fix_commit", "f_testname", "sourcefile"])
    
    suite = get_fate_tests(PROJECT_DIR)
    processed_tests = set()

    print(f"Running {len(suite)} tests for Fix Commit...")
    for i, test in enumerate(suite):
        t_name = test['name']
        processed_tests.add(t_name)
        
        if i % 10 == 0: print(f"  [P1-Fix] {i}/{len(suite)}: {t_name}")

        run_command("find . -name '*.gcda' -delete", PROJECT_DIR)
        run_command(test['cmd'], PROJECT_DIR, ignore_errors=True)
        
        covered = get_covered_files(PROJECT_DIR)
        
        for target in target_files:
            v_covered = (t_name in vuln_results) and (target in vuln_results[t_name])
            f_covered = target in covered

            if v_covered or f_covered:
                v_entry = t_name if v_covered else ""
                f_entry = t_name if f_covered else ""
                writer.writerow([REPO_NAME, vuln_commit, v_entry, fix_commit, f_entry, target])
                f_csv.flush()

    for t_name, covered_files in vuln_results.items():
        if t_name not in processed_tests:
            for target in target_files:
                if target in covered_files:
                    writer.writerow([REPO_NAME, vuln_commit, t_name, fix_commit, "", target])

    f_csv.close()
    return True

# ==========================================
# PHASE 2: ENERGY
# ==========================================
def detect_rapl():
    cmd = "perf list"
    res = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, text=True)
    out = res.stdout
    pkg = "power/energy-pkg/" if "power/energy-pkg/" in out else "power/energy-pkg"
    core = "power/energy-cores/" if "power/energy-cores/" in out else "power/energy-cores"
    return pkg, core

def measure_test(test_name, pkg_event, core_event):
    cmd = f"make {test_name} SAMPLES={SAMPLES_DIR} -j1"
    
    start = time.time()
    if not run_command(cmd, PROJECT_DIR, ignore_errors=True): return None
    duration = max(time.time() - start, 0.001)
    
    iterations = math.ceil(TARGET_DURATION_SEC / duration)
    
    loop_cmd = f"for i in $(seq 1 {iterations}); do {cmd} >/dev/null 2>&1; done"
    perf_cmd = f"perf stat -a -e {pkg_event},{core_event},cycles,instructions -x, sh -c '{loop_cmd}'"
    
    res = subprocess.run(perf_cmd, cwd=PROJECT_DIR, shell=True, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0: return None

    metrics = {"energy_pkg": 0.0, "energy_core": 0.0, "cycles": 0, "instructions": 0}
    for line in res.stderr.split('\n'):
        parts = line.split(',')
        if len(parts) < 3: continue
        try:
            val = float(parts[0])
            evt = parts[2]
            if "energy-pkg" in evt: metrics["energy_pkg"] = val
            elif "energy-cores" in evt: metrics["energy_core"] = val
            elif "cycles" in evt: metrics["cycles"] = val
            elif "instructions" in evt: metrics["instructions"] = val
        except ValueError: continue

    if iterations > 0:
        return {k: v / iterations for k, v in metrics.items()}
    return None

def write_energy_csv(output_path, rows, cache):
    fieldnames = [
        "project", "vuln_commit", "v_testname", "v_energy_pkg", "v_energy_core", "v_cycles", "v_ipc",
        "fix_commit", "f_testname", "sourcefile", "f_energy_pkg", "f_energy_core", "f_cycles", "f_ipc"
    ]
    with open(output_path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            out = row.copy()
            # Default 0
            for k in ["v_energy_pkg", "v_energy_core", "v_cycles", "v_ipc", "f_energy_pkg", "f_energy_core", "f_cycles", "f_ipc"]: out[k] = 0
            
            vc, vt = row['vuln_commit'], row['v_testname']
            if vt and vc in cache and vt in cache[vc]:
                m = cache[vc][vt]
                out["v_energy_pkg"] = f"{m['energy_pkg']:.4f}"
                out["v_energy_core"] = f"{m['energy_core']:.4f}"
                out["v_cycles"] = f"{m['cycles']:.0f}"
                out["v_ipc"] = f"{m['instructions']/m['cycles']:.4f}" if m['cycles'] > 0 else "0"

            fc, ft = row['fix_commit'], row['f_testname']
            if ft and fc in cache and ft in cache[fc]:
                m = cache[fc][ft]
                out["f_energy_pkg"] = f"{m['energy_pkg']:.4f}"
                out["f_energy_core"] = f"{m['energy_core']:.4f}"
                out["f_cycles"] = f"{m['cycles']:.0f}"
                out["f_ipc"] = f"{m['instructions']/m['cycles']:.4f}" if m['cycles'] > 0 else "0"
            
            writer.writerow(out)
            f.flush()
            os.fsync(f.fileno())

def run_phase_2_energy(input_csv_path, output_csv_path, checkpoint_path):
    logging.info(f"--- Phase 2: Energy Measurement for {input_csv_path} ---")
    
    if os.geteuid() != 0:
        logging.error("Phase 2 requires root (sudo).")
        return False

    if not os.path.exists(input_csv_path):
        logging.error(f"Input CSV {input_csv_path} not found.")
        return False

    EVENT_PKG, EVENT_CORE = detect_rapl()
    
    with open(input_csv_path, 'r') as f:
        rows = list(csv.DictReader(f))

    tasks = {}
    for row in rows:
        if row['v_testname']: tasks.setdefault(row['vuln_commit'], set()).add(row['v_testname'])
        if row['f_testname']: tasks.setdefault(row['fix_commit'], set()).add(row['f_testname'])

    cache = load_json(checkpoint_path)
    counter = 0

    for commit, test_set in tasks.items():
        if commit not in cache: cache[commit] = {}
        todos = [t for t in test_set if t not in cache[commit]]
        
        if not todos: continue
        
        logging.info(f"Building {commit} for Perf (No Coverage)...")
        clean_repo(PROJECT_DIR)
        run_command(f"git checkout -f {commit}", PROJECT_DIR)
        run_command("./configure --disable-asm --disable-doc", PROJECT_DIR)
        run_command("make -j$(nproc)", PROJECT_DIR)

        for i, test in enumerate(todos):
            print(f"  [P2-Measure] {commit[:8]} - {test} ({i+1}/{len(todos)})")
            metrics = measure_test(test, EVENT_PKG, EVENT_CORE)
            if metrics:
                cache[commit][test] = metrics
                save_json(checkpoint_path, cache)
                counter += 1
                if counter % CSV_WRITE_INTERVAL == 0:
                    write_energy_csv(output_csv_path, rows, cache)

    write_energy_csv(output_csv_path, rows, cache)
    return True

# ==========================================
# MAIN LOOP
# ==========================================
def main():
    if not os.path.exists(INPUT_CSV):
        print(f"ERROR: Input CSV file not found at {INPUT_CSV}")
        print("Please ensure it was copied into the container.")
        sys.exit(1)

    print(f"Reading commit pairs from {INPUT_CSV}...")
    
    pairs = []
    with open(INPUT_CSV, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if 'vuln_commit' in row and 'fix_commit' in row:
                pairs.append((row['vuln_commit'], row['fix_commit']))

    if not pairs:
        print("No pairs found in CSV.")
        sys.exit(1)

    print(f"Found {len(pairs)} pairs to process.")

    for i, (vuln, fix) in enumerate(pairs):
        print(f"\n==================================================")
        print(f"PROCESSING PAIR {i+1}/{len(pairs)}: {vuln[:8]} -> {fix[:8]}")
        print(f"==================================================\n")

        # Define Output Filenames (Inside /app/vfec_results so they are exposed to host)
        p1_csv = os.path.join(RESULTS_DIR, f"FFmpeg_{vuln[:8]}_{fix[:8]}_testCompile.csv")
        p2_csv = os.path.join(RESULTS_DIR, f"FFmpeg_{vuln[:8]}_{fix[:8]}_energyperf.csv")
        
        p1_cache = os.path.join(CACHE_DIR, f"ckpt_cov_{vuln[:8]}.json")
        p2_cache = os.path.join(CACHE_DIR, f"ckpt_eng_{vuln[:8]}_{fix[:8]}.json")

        success_p1 = run_phase_1_coverage(vuln, fix, p1_csv, p1_cache)
        
        if success_p1:
            print("Phase 1 complete. Starting Phase 2...")
            run_phase_2_energy(p1_csv, p2_csv, p2_cache)
            print(f"Pair Complete. Final Result: {p2_csv}")
        else:
            print("Phase 1 failed. Skipping Phase 2 for this pair.")

if __name__ == "__main__":
    main()