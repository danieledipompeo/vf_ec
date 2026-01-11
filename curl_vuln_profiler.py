# previously vuln_test_output.csv renamed to vuln_testcov.csv
# Computes the energy and performance measurements for vuln_commit of curl

import os
import subprocess
import csv
import multiprocessing
import statistics
import time
import shutil

# --- CONFIGURATION ---
PROJECT_NAME = "curl"
PROJECT_BASE_DIR = "ds_projects"
RESULTS_DIR = "vfec_results"
CSV_FILE = os.path.join(RESULTS_DIR, "vuln_testcov.csv")

# Profiling Settings
TARGET_DURATION_SEC = 3.0   # Inner Loop duration (Resolution)
OUTER_LOOP_COUNT = 5       # Outer Loop repetitions (Stability)
CPU_CORES = multiprocessing.cpu_count()

# Derived Paths
PROJECT_PATH = os.path.join(PROJECT_BASE_DIR, PROJECT_NAME)
LOG_DIR = os.path.join(RESULTS_DIR, "log")

# --- PERMISSION HANDLING ---
SUDO_UID = int(os.environ.get('SUDO_UID', os.getuid()))
SUDO_GID = int(os.environ.get('SUDO_GID', os.getgid()))

def fix_ownership(filepath):
    """Changes file ownership from root back to the original user."""
    try:
        if os.path.exists(filepath):
            os.chown(filepath, SUDO_UID, SUDO_GID)
    except Exception as e:
        pass

def ensure_dirs():
    os.makedirs(LOG_DIR, exist_ok=True)
    fix_ownership(LOG_DIR)

def get_log_file(commit_hash):
    filename = os.path.join(LOG_DIR, f"curl_log_vuln_profiler_{commit_hash[:8]}.txt")
    return filename

def write_log(message, log_file):
    with open(log_file, "a") as f:
        f.write(message + "\n")
    fix_ownership(log_file)
    if "Error" in message or "Phase" in message or "Median" in message:
        print(message)

def run_cmd(command, cwd, log_file, can_fail=False):
    try:
        result = subprocess.run(
            command, shell=True, cwd=cwd, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        error_msg = f"ERROR executing: {command}\nStderr: {e.stderr}"
        write_log(error_msg, log_file)
        if not can_fail:
            print(f"❌ Critical Failure. See log: {log_file}")
            exit(1)
        return None

def build_commit_clean(commit_hash, log_file):
    write_log(f"🛠️  Phase 1: Building {commit_hash[:8]} (Clean Release Build)...", log_file)
    
    # Clean up any root-owned files first
    run_cmd("git reset --hard", PROJECT_PATH, log_file)
    run_cmd("git clean -fdx", PROJECT_PATH, log_file)
    
    run_cmd(f"git checkout {commit_hash}", PROJECT_PATH, log_file)
    
    run_cmd("./buildconf", PROJECT_PATH, log_file)
    config_cmd = (
        "./configure --disable-shared --disable-ldap --without-ssl "
        "--enable-maintainer-mode --enable-symbol-hiding"
    )
    run_cmd(config_cmd, PROJECT_PATH, log_file)
    run_cmd(f"make -j{CPU_CORES}", PROJECT_PATH, log_file)
    run_cmd("make", os.path.join(PROJECT_PATH, "tests"), log_file)

def calibrate_loops(test_id, log_file):
    write_log(f"⚖️  Calibrating test {test_id}...", log_file)
    
    cmd = f"./runtests.pl -q {test_id}"
    test_dir = os.path.join(PROJECT_PATH, "tests")
    
    start_time = time.time()
    run_cmd(cmd, test_dir, log_file, can_fail=True)
    end_time = time.time()
    
    duration = end_time - start_time
    if duration <= 0: duration = 0.001 
    
    n_loops = int(TARGET_DURATION_SEC / duration)
    if n_loops < 1: n_loops = 1
    
    write_log(f"   -> Single Run: {duration:.4f}s. Target Loops (N): {n_loops}", log_file)
    return n_loops

def parse_perf_output(output):
    results = {
        "energy_pkg": 0.0, "energy_core": 0.0, "instructions": 0, "cycles": 0
    }
    for line in output.splitlines():
        parts = line.split(',')
        if len(parts) < 2: continue
        val_str = parts[0]
        event = parts[2] if len(parts) > 2 else ""
        if "<not supported>" in line or val_str.strip() == "": continue
        try:
            val = float(val_str)
            if "energy-pkg" in event: results["energy_pkg"] = val
            elif "energy-cores" in event: results["energy_core"] = val
            elif "instructions" in event: results["instructions"] = int(val)
            elif "cycles" in event: results["cycles"] = int(val)
        except ValueError: continue
    return results

def profile_test(test_id, n_loops, log_file):
    perf_cmd = (
        "perf stat -x, -a -e "
        "power/energy-pkg/,power/energy-cores/,cycles,instructions "
        f"sh -c 'for i in $(seq 1 {n_loops}); do ./runtests.pl -q {test_id} > /dev/null 2>&1; done'"
    )
    test_dir = os.path.join(PROJECT_PATH, "tests")
    
    measurements = {
        "energy_pkg": [], "energy_core": [], "instructions": [], "cycles": []
    }
    
    write_log(f"⚡ Profiling Test {test_id} (Loops: {n_loops}, Repetitions: {OUTER_LOOP_COUNT})...", log_file)
    
    for i in range(OUTER_LOOP_COUNT):
        proc = subprocess.run(
            perf_cmd, shell=True, cwd=test_dir, 
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        data = parse_perf_output(proc.stderr)
        
        if n_loops > 0:
            measurements["energy_pkg"].append(data["energy_pkg"] / n_loops)
            measurements["energy_core"].append(data["energy_core"] / n_loops)
            measurements["instructions"].append(data["instructions"] / n_loops)
            measurements["cycles"].append(data["cycles"] / n_loops)

    final_stats = {}
    for key, values in measurements.items():
        if values:
            final_stats[key] = statistics.median(values)
        else:
            final_stats[key] = 0
            
    write_log(f"   -> Result (Median): {final_stats}", log_file)
    return final_stats

def main():
    ensure_dirs()
    
    if not os.path.exists(CSV_FILE):
        print("❌ CSV file not found.")
        return

    rows = []
    with open(CSV_FILE, "r") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames

    new_cols = ["energy_pkg", "energy_core", "instructions", "cycles"]
    for col in new_cols:
        if col not in fieldnames: fieldnames.append(col)

    commit_map = {}
    for idx, row in enumerate(rows):
        if not row.get("energy_pkg") or row.get("energy_pkg") == "":
            c = row["vuln_commit"]
            if c not in commit_map: commit_map[c] = []
            commit_map[c].append(idx)

    print(f"📋 Found {len(commit_map)} commits to profile.")
    print(f"🔒 Running as Root, but writing files as User ID: {SUDO_UID}")

    for commit, row_indices in commit_map.items():
        log_file = get_log_file(commit)
        write_log(f"--- Starting Profiling for Commit {commit} ---", log_file)
        
        build_commit_clean(commit, log_file)
        
        test_cache = {} 
        
        for idx in row_indices:
            test_id = rows[idx]["testfile"]
            
            # Profile if not already cached
            if test_id not in test_cache:
                n_loops = calibrate_loops(test_id, log_file)
                stats = profile_test(test_id, n_loops, log_file)
                test_cache[test_id] = stats
            
            # Update the row
            stats = test_cache[test_id]
            rows[idx]["energy_pkg"] = f"{stats['energy_pkg']:.6f}"
            rows[idx]["energy_core"] = f"{stats['energy_core']:.6f}"
            rows[idx]["instructions"] = int(stats['instructions'])
            rows[idx]["cycles"] = int(stats['cycles'])

            # --- REAL-TIME SAVE BLOCK (MOVED INSIDE LOOP) ---
            try:
                temp_file = CSV_FILE + ".tmp"
                with open(temp_file, "w", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(rows)
                
                fix_ownership(temp_file)
                shutil.move(temp_file, CSV_FILE)
                fix_ownership(CSV_FILE)
                print(f"💾 Checkpoint: Saved test {test_id} for commit {commit[:8]}")
            except Exception as e:
                print(f"⚠️ Warning: Could not save checkpoint: {e}")
            # ------------------------------------------------

    print(f"🏁 Profiling Complete. Results updated in {CSV_FILE}")

if __name__ == "__main__":
    main()