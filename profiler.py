# Successfully collected the energy and performance measurement on first commit pair of FFmpeg and OpenSSl.

import os
import subprocess
import csv
import logging
import time
import math
import json
import sys

# ==========================================
# CONFIGURATION
# ==========================================
# UPDATE THIS TO YOUR ACTUAL OPENSSL CSV FILENAME
INPUT_CSV_NAME = "openssl_c9a826d2_f426625b_testCompile.csv"

# Target Duration per test (in seconds)
TARGET_DURATION_SEC = 3.0

# Batch size for writing to CSV (Save every N tests)
CSV_WRITE_INTERVAL = 50 

# ==========================================
# PATHS
# ==========================================
BASE_DIR = os.getcwd()
RESULTS_DIR = os.path.join(BASE_DIR, "vfec_results")
LOG_DIR = os.path.join(RESULTS_DIR, "log")
CACHE_DIR = os.path.join(RESULTS_DIR, "cache")

INPUT_CSV_PATH = os.path.join(RESULTS_DIR, INPUT_CSV_NAME)
OUTPUT_CSV_PATH = os.path.join(RESULTS_DIR, INPUT_CSV_NAME.replace("_testCompile", "_energyperf"))
LOG_FILE = os.path.join(LOG_DIR, "log_measure_energy.txt")
CHECKPOINT_FILE = os.path.join(CACHE_DIR, "measurements_checkpoint.json")

SAMPLES_DIR = os.path.join(BASE_DIR, "ds_projects", "fate-samples")
PROJECT_DIR_MAP = {
    "FFmpeg": os.path.join(BASE_DIR, "ds_projects", "FFmpeg"),
    "openssl": os.path.join(BASE_DIR, "ds_projects", "openssl")
}

# ==========================================
# LOGGING & SETUP
# ==========================================
for d in [RESULTS_DIR, LOG_DIR, CACHE_DIR]:
    if not os.path.exists(d): os.makedirs(d)

logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
console = logging.StreamHandler()
console.setLevel(logging.INFO)
logging.getLogger('').addHandler(console)

# ==========================================
# SYSTEM CHECKS & HELPERS
# ==========================================
def check_root():
    if os.geteuid() != 0:
        print("ERROR: This script must be run with sudo to access Energy (RAPL) counters.")
        sys.exit(1)

def detect_rapl_event_name():
    """Detects if the kernel uses 'power/energy-pkg/' or 'power/energy-pkg'."""
    cmd = "perf list"
    res = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, text=True)
    output = res.stdout
    
    events = []
    if "power/energy-pkg/" in output:
        events.append("power/energy-pkg/")
    elif "power/energy-pkg" in output:
        events.append("power/energy-pkg")
    else:
        logging.warning("RAPL energy-pkg event not found. Energy might be 0.")
        events.append("power/energy-pkg/") 

    if "power/energy-cores/" in output:
        events.append("power/energy-cores/")
    elif "power/energy-cores" in output:
        events.append("power/energy-cores")
    else:
        events.append("power/energy-cores/") 

    return events[0], events[1]

EVENT_PKG, EVENT_CORE = "power/energy-pkg/", "power/energy-cores/"
if os.geteuid() == 0:
    EVENT_PKG, EVENT_CORE = detect_rapl_event_name()
    logging.info(f"Using RAPL Events: {EVENT_PKG}, {EVENT_CORE}")

def save_checkpoint(data):
    try:
        with open(CHECKPOINT_FILE, 'w') as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logging.error(f"Failed to save checkpoint: {e}")

def load_checkpoint():
    if os.path.exists(CHECKPOINT_FILE):
        logging.info("Loading previous checkpoint...")
        with open(CHECKPOINT_FILE, 'r') as f:
            return json.load(f)
    return {}

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

def write_csv_from_cache(rows, results_cache):
    logging.info("Writing intermediate CSV dump...")
    
    fieldnames = [
        "project", "vuln_commit", "v_testname", 
        "v_energy_pkg", "v_energy_core", "v_cycles", "v_ipc",
        "fix_commit", "f_testname", "sourcefile", 
        "f_energy_pkg", "f_energy_core", "f_cycles", "f_ipc"
    ]
    
    try:
        with open(OUTPUT_CSV_PATH, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            
            for row in rows:
                out_row = {
                    "project": row['project'],
                    "vuln_commit": row['vuln_commit'],
                    "v_testname": row['v_testname'],
                    "fix_commit": row['fix_commit'],
                    "f_testname": row['f_testname'],
                    "sourcefile": row['sourcefile'],
                    "v_energy_pkg": "0", "v_energy_core": "0", "v_cycles": "0", "v_ipc": "0",
                    "f_energy_pkg": "0", "f_energy_core": "0", "f_cycles": "0", "f_ipc": "0"
                }
                
                # Fill Vuln Metrics
                vc = row['vuln_commit']
                vt = row['v_testname']
                if vt and vc in results_cache and vt in results_cache[vc]:
                    m = results_cache[vc][vt]
                    out_row["v_energy_pkg"] = f"{m['energy_pkg']:.4f}"
                    out_row["v_energy_core"] = f"{m['energy_core']:.4f}"
                    out_row["v_cycles"] = f"{m['cycles']:.0f}"
                    out_row["v_ipc"] = f"{m['ipc']:.4f}"

                # Fill Fix Metrics
                fc = row['fix_commit']
                ft = row['f_testname']
                if ft and fc in results_cache and ft in results_cache[fc]:
                    m = results_cache[fc][ft]
                    out_row["f_energy_pkg"] = f"{m['energy_pkg']:.4f}"
                    out_row["f_energy_core"] = f"{m['energy_core']:.4f}"
                    out_row["f_cycles"] = f"{m['cycles']:.0f}"
                    out_row["f_ipc"] = f"{m['ipc']:.4f}"
                
                writer.writerow(out_row)
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        logging.error(f"Failed to write CSV: {e}")

def get_test_command(project, test_name, cwd):
    if project == "FFmpeg":
        return f"make {test_name} SAMPLES={SAMPLES_DIR} -j1"
    elif project == "openssl":
        # Updated for Modern OpenSSL Tests
        return f"make test TESTS='{test_name}'"
    return None

def clean_and_checkout(project, commit_hash):
    cwd = PROJECT_DIR_MAP.get(project)
    if not cwd or not os.path.exists(cwd):
        logging.error(f"Project dir not found for {project}")
        return False

    logging.info(f"Checking out {project} @ {commit_hash}...")
    run_command("git reset --hard", cwd)
    run_command("git clean -fdx", cwd)
    
    if not run_command(f"git checkout -f {commit_hash}", cwd): return False

    logging.info("Building...")
    if project == "FFmpeg":
        run_command("./configure --disable-asm --disable-doc", cwd)
    elif project == "openssl":
        # Standard config for measuring (optimized build)
        run_command("./config", cwd) 

    if not run_command("make -j$(nproc)", cwd): 
        logging.error("Build Failed")
        return False
    
    return True

def measure_single_test(project, test_name, cwd):
    """Measures Duration, Calcs Iterations, Runs Perf."""
    cmd = get_test_command(project, test_name, cwd)
    
    # 1. Warmup & Timing Run
    start_time = time.time()
    if not run_command(cmd, cwd, ignore_errors=True):
        logging.error(f"Test {test_name} failed to run.")
        return None
    duration = time.time() - start_time
    
    if duration < 0.001: duration = 0.001
    
    # 2. Calculate Iterations for ~3 seconds
    iterations = math.ceil(TARGET_DURATION_SEC / duration)
    
    # 3. Perf Loop
    loop_cmd = f"for i in $(seq 1 {iterations}); do {cmd} >/dev/null 2>&1; done"
    
    # Detect Hybrid Architecture Events (Atom vs Core) by summing in Python later
    perf_cmd = f"perf stat -a -e {EVENT_PKG},{EVENT_CORE},cycles,instructions -x, sh -c '{loop_cmd}'"
    
    result = subprocess.run(perf_cmd, cwd=cwd, shell=True, stderr=subprocess.PIPE, text=True)
    
    metrics = {"energy_pkg": 0.0, "energy_core": 0.0, "cycles": 0, "instructions": 0, "ipc": 0.0}
    
    if result.returncode != 0:
        logging.error(f"Perf failed: {result.stderr}")
        return None

    # Parse Output (-x, format: value,unit,event,...)
    for line in result.stderr.split('\n'):
        parts = line.split(',')
        if len(parts) < 3: continue
        try:
            val_str = parts[0]
            if val_str == "<not supported>" or val_str == "": continue
            val = float(val_str)
            event = parts[2]
            
            if "energy-pkg" in event: metrics["energy_pkg"] = val
            elif "energy-cores" in event: metrics["energy_core"] = val
            
            # Hybrid Architecture Handling (Summing Core + Atom)
            elif "cycles" in event: metrics["cycles"] += val
            elif "instructions" in event: metrics["instructions"] += val
            
        except ValueError: continue

    # 4. Normalize
    if iterations > 0:
        final_metrics = {
            "energy_pkg": metrics["energy_pkg"] / iterations,
            "energy_core": metrics["energy_core"] / iterations,
            "cycles": metrics["cycles"] / iterations,
            "ipc": 0.0
        }
    else:
        return None
    
    if final_metrics["cycles"] > 0:
        final_metrics["ipc"] = metrics["instructions"] / metrics["cycles"]

    return final_metrics

# ==========================================
# MAIN LOGIC
# ==========================================
def main():
    check_root()

    if not os.path.exists(INPUT_CSV_PATH):
        print(f"Error: Input CSV {INPUT_CSV_PATH} not found.")
        return

    # 1. Read Input CSV
    with open(INPUT_CSV_PATH, 'r') as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if not rows:
        print("Input CSV is empty.")
        return

    # 2. Build Task List
    tasks = {} 
    
    for row in rows:
        if row['v_testname']:
            c = row['vuln_commit']
            if c not in tasks: tasks[c] = set()
            tasks[c].add(row['v_testname'])
            
        if row['f_testname']:
            c = row['fix_commit']
            if c not in tasks: tasks[c] = set()
            tasks[c].add(row['f_testname'])

    # 3. Load Cache
    results_cache = load_checkpoint()

    # 4. Run Measurements
    global_test_counter = 0

    for commit, test_set in tasks.items():
        project = rows[0]['project'] 
        cwd = PROJECT_DIR_MAP[project]
        
        if commit not in results_cache: results_cache[commit] = {}

        todos = [t for t in test_set if t not in results_cache[commit]]
        
        if not todos:
            print(f"Commit {commit}: All tests cached. Skipping.")
            continue
            
        print(f"Processing Commit: {commit} ({len(todos)} tests)")
        
        if clean_and_checkout(project, commit):
            for i, test in enumerate(todos):
                print(f"  Measuring [{i+1}/{len(todos)}]: {test}")
                metrics = measure_single_test(project, test, cwd)
                
                if metrics:
                    results_cache[commit][test] = metrics
                    save_checkpoint(results_cache) 
                    
                    global_test_counter += 1
                    if global_test_counter % CSV_WRITE_INTERVAL == 0:
                        print(f"  [Auto-Save] Writing CSV after {global_test_counter} tests...")
                        write_csv_from_cache(rows, results_cache)
                else:
                    logging.warning(f"Failed to measure {test} on {commit}")
        else:
            logging.error(f"Skipping commit {commit} due to build failure.")

    # 5. Final CSV Write
    print("Writing Final CSV...")
    write_csv_from_cache(rows, results_cache)
    print(f"Done. Measured data saved to: {OUTPUT_CSV_PATH}")

if __name__ == "__main__":
    main()

# Successfully collected the energy and performance measurement on first commit pair of FFmpeg.

# import os
# import subprocess
# import csv
# import logging
# import time
# import math
# import json
# import sys

# # ==========================================
# # CONFIGURATION
# # ==========================================
# # Input CSV from the previous step
# INPUT_CSV_NAME = "FFmpeg_89505d38_9ffa4949_testCompile.csv"

# # Target Duration per test (in seconds)
# TARGET_DURATION_SEC = 3.0

# # Batch size for writing to CSV (Save every N tests)
# CSV_WRITE_INTERVAL = 100

# # ==========================================
# # PATHS
# # ==========================================
# BASE_DIR = os.getcwd()
# RESULTS_DIR = os.path.join(BASE_DIR, "vfec_results")
# LOG_DIR = os.path.join(RESULTS_DIR, "log")
# CACHE_DIR = os.path.join(RESULTS_DIR, "cache")

# INPUT_CSV_PATH = os.path.join(RESULTS_DIR, INPUT_CSV_NAME)
# OUTPUT_CSV_PATH = os.path.join(RESULTS_DIR, INPUT_CSV_NAME.replace("_testCompile", "_energyperf"))
# LOG_FILE = os.path.join(LOG_DIR, "log_measure_energy.txt")
# CHECKPOINT_FILE = os.path.join(CACHE_DIR, "measurements_checkpoint.json")

# SAMPLES_DIR = os.path.join(BASE_DIR, "ds_projects", "fate-samples")
# PROJECT_DIR_MAP = {
#     "FFmpeg": os.path.join(BASE_DIR, "ds_projects", "FFmpeg"),
#     "openssl": os.path.join(BASE_DIR, "ds_projects", "openssl")
# }

# # ==========================================
# # LOGGING & SETUP
# # ==========================================
# for d in [RESULTS_DIR, LOG_DIR, CACHE_DIR]:
#     if not os.path.exists(d): os.makedirs(d)

# logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
# console = logging.StreamHandler()
# console.setLevel(logging.INFO)
# logging.getLogger('').addHandler(console)

# # ==========================================
# # SYSTEM CHECKS & HELPERS
# # ==========================================
# def check_root():
#     if os.geteuid() != 0:
#         print("ERROR: This script must be run with sudo to access Energy (RAPL) counters.")
#         sys.exit(1)

# def detect_rapl_event_name():
#     """Detects if the kernel uses 'power/energy-pkg/' or 'power/energy-pkg'."""
#     # Try with trailing slash first (common on newer kernels)
#     cmd = "perf list"
#     res = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, text=True)
#     output = res.stdout
    
#     events = []
#     if "power/energy-pkg/" in output:
#         events.append("power/energy-pkg/")
#     elif "power/energy-pkg" in output:
#         events.append("power/energy-pkg")
#     else:
#         logging.warning("RAPL energy-pkg event not found in 'perf list'. Energy might be 0.")
#         events.append("power/energy-pkg/") # Fallback

#     if "power/energy-cores/" in output:
#         events.append("power/energy-cores/")
#     elif "power/energy-cores" in output:
#         events.append("power/energy-cores")
#     else:
#         events.append("power/energy-cores/") # Fallback

#     return events[0], events[1]

# # Detect events once at startup
# EVENT_PKG, EVENT_CORE = "power/energy-pkg/", "power/energy-cores/"
# if os.geteuid() == 0:
#     EVENT_PKG, EVENT_CORE = detect_rapl_event_name()
#     logging.info(f"Using RAPL Events: {EVENT_PKG}, {EVENT_CORE}")

# def save_checkpoint(data):
#     try:
#         with open(CHECKPOINT_FILE, 'w') as f:
#             json.dump(data, f, indent=4)
#     except Exception as e:
#         logging.error(f"Failed to save checkpoint: {e}")

# def load_checkpoint():
#     if os.path.exists(CHECKPOINT_FILE):
#         logging.info("Loading previous checkpoint...")
#         with open(CHECKPOINT_FILE, 'r') as f:
#             return json.load(f)
#     return {}

# def run_command(command, cwd, ignore_errors=False):
#     try:
#         # logging.info(f"CMD: {command}")
#         result = subprocess.run(command, cwd=cwd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
#         if result.returncode != 0 and not ignore_errors:
#             logging.error(f"FAIL: {command}\nSTDERR: {result.stderr.strip()}")
#             return False
#         return True
#     except Exception as e:
#         logging.error(f"EXCEPTION: {e}")
#         return False

# def write_csv_from_cache(rows, results_cache):
#     """Writes the full CSV using the current data in cache."""
#     logging.info("Writing intermediate CSV dump...")
    
#     fieldnames = [
#         "project", "vuln_commit", "v_testname", 
#         "v_energy_pkg", "v_energy_core", "v_cycles", "v_ipc",
#         "fix_commit", "f_testname", "sourcefile", 
#         "f_energy_pkg", "f_energy_core", "f_cycles", "f_ipc"
#     ]
    
#     try:
#         with open(OUTPUT_CSV_PATH, 'w', newline='') as f:
#             writer = csv.DictWriter(f, fieldnames=fieldnames)
#             writer.writeheader()
            
#             for row in rows:
#                 out_row = {
#                     "project": row['project'],
#                     "vuln_commit": row['vuln_commit'],
#                     "v_testname": row['v_testname'],
#                     "fix_commit": row['fix_commit'],
#                     "f_testname": row['f_testname'],
#                     "sourcefile": row['sourcefile'],
#                     "v_energy_pkg": "0", "v_energy_core": "0", "v_cycles": "0", "v_ipc": "0",
#                     "f_energy_pkg": "0", "f_energy_core": "0", "f_cycles": "0", "f_ipc": "0"
#                 }
                
#                 # Fill Vuln Metrics
#                 vc = row['vuln_commit']
#                 vt = row['v_testname']
#                 if vt and vc in results_cache and vt in results_cache[vc]:
#                     m = results_cache[vc][vt]
#                     out_row["v_energy_pkg"] = f"{m['energy_pkg']:.4f}"
#                     out_row["v_energy_core"] = f"{m['energy_core']:.4f}"
#                     out_row["v_cycles"] = f"{m['cycles']:.0f}"
#                     out_row["v_ipc"] = f"{m['ipc']:.4f}"

#                 # Fill Fix Metrics
#                 fc = row['fix_commit']
#                 ft = row['f_testname']
#                 if ft and fc in results_cache and ft in results_cache[fc]:
#                     m = results_cache[fc][ft]
#                     out_row["f_energy_pkg"] = f"{m['energy_pkg']:.4f}"
#                     out_row["f_energy_core"] = f"{m['energy_core']:.4f}"
#                     out_row["f_cycles"] = f"{m['cycles']:.0f}"
#                     out_row["f_ipc"] = f"{m['ipc']:.4f}"
                
#                 writer.writerow(out_row)
#             f.flush()
#             os.fsync(f.fileno())
#     except Exception as e:
#         logging.error(f"Failed to write CSV: {e}")

# def get_test_command(project, test_name, cwd):
#     if project == "FFmpeg":
#         # -j1 is crucial for consistent energy per core
#         return f"make {test_name} SAMPLES={SAMPLES_DIR} -j1"
#     elif project == "openssl":
#         return f"env LD_LIBRARY_PATH=. ./apps/openssl enc -aes-256-cbc -salt -in input_3gb.bin -out output_test.enc -pass pass:12345"
#     return None

# def clean_and_checkout(project, commit_hash):
#     cwd = PROJECT_DIR_MAP.get(project)
#     if not cwd or not os.path.exists(cwd):
#         logging.error(f"Project dir not found for {project}")
#         return False

#     logging.info(f"Checking out {project} @ {commit_hash}...")
#     run_command("git reset --hard", cwd)
#     run_command("git clean -fdx", cwd)
    
#     if not run_command(f"git checkout -f {commit_hash}", cwd): return False

#     logging.info("Building...")
#     if project == "FFmpeg":
#         run_command("./configure --disable-asm --disable-doc", cwd)
#     elif project == "openssl":
#         run_command("./config -d", cwd)

#     if not run_command("make -j$(nproc)", cwd): 
#         logging.error("Build Failed")
#         return False
    
#     return True

# def measure_single_test(project, test_name, cwd):
#     """Measures Duration, Calcs Iterations, Runs Perf."""
#     cmd = get_test_command(project, test_name, cwd)
    
#     # 1. Warmup & Timing Run
#     start_time = time.time()
#     if not run_command(cmd, cwd, ignore_errors=True):
#         logging.error(f"Test {test_name} failed to run.")
#         return None
#     duration = time.time() - start_time
    
#     if duration < 0.001: duration = 0.001
    
#     # 2. Calculate Iterations for ~3 seconds
#     iterations = math.ceil(TARGET_DURATION_SEC / duration)
#     # logging.info(f"Test {test_name}: duration={duration:.4f}s. Running {iterations} iterations.")

#     # 3. Perf Loop
#     loop_cmd = f"for i in $(seq 1 {iterations}); do {cmd} >/dev/null 2>&1; done"
    
#     # CRITICAL FIX: Added -a (System Wide) to capture Energy
#     # RAPL counters are system-wide, they usually return 0 if -a is missing.
#     perf_cmd = f"perf stat -a -e {EVENT_PKG},{EVENT_CORE},cycles,instructions -x, sh -c '{loop_cmd}'"
    
#     # logging.info(f"Running Perf: {perf_cmd}")
#     result = subprocess.run(perf_cmd, cwd=cwd, shell=True, stderr=subprocess.PIPE, text=True)
    
#     metrics = {"energy_pkg": 0.0, "energy_core": 0.0, "cycles": 0, "instructions": 0, "ipc": 0.0}
    
#     if result.returncode != 0:
#         logging.error(f"Perf failed: {result.stderr}")
#         return None

#     # Parse Output (-x, format: value,unit,event,...)
#     for line in result.stderr.split('\n'):
#         parts = line.split(',')
#         if len(parts) < 3: continue
#         try:
#             val_str = parts[0]
#             if val_str == "<not supported>" or val_str == "": continue
#             val = float(val_str)
#             event = parts[2]
            
#             # Flexible matching for events
#             if "energy-pkg" in event: metrics["energy_pkg"] = val
#             elif "energy-cores" in event: metrics["energy_core"] = val
#             elif "cycles" in event: metrics["cycles"] = val
#             elif "instructions" in event: metrics["instructions"] = val
#         except ValueError: continue

#     # 4. Normalize
#     if iterations > 0:
#         final_metrics = {
#             "energy_pkg": metrics["energy_pkg"] / iterations,
#             "energy_core": metrics["energy_core"] / iterations,
#             "cycles": metrics["cycles"] / iterations,
#             "ipc": 0.0
#         }
#     else:
#         return None
    
#     if final_metrics["cycles"] > 0:
#         final_metrics["ipc"] = metrics["instructions"] / metrics["cycles"]

#     return final_metrics

# # ==========================================
# # MAIN LOGIC
# # ==========================================
# def main():
#     check_root()

#     if not os.path.exists(INPUT_CSV_PATH):
#         print(f"Error: Input CSV {INPUT_CSV_PATH} not found.")
#         return

#     # 1. Read Input CSV to get list of Tests
#     with open(INPUT_CSV_PATH, 'r') as f:
#         reader = csv.DictReader(f)
#         rows = list(reader)

#     if not rows:
#         print("Input CSV is empty.")
#         return

#     # 2. Build Task List (Unique tests per commit)
#     tasks = {} # { commit: {test_name, ...} }
    
#     for row in rows:
#         if row['v_testname']:
#             c = row['vuln_commit']
#             if c not in tasks: tasks[c] = set()
#             tasks[c].add(row['v_testname'])
            
#         if row['f_testname']:
#             c = row['fix_commit']
#             if c not in tasks: tasks[c] = set()
#             tasks[c].add(row['f_testname'])

#     # 3. Load Cache
#     results_cache = load_checkpoint()

#     # 4. Run Measurements
#     global_test_counter = 0

#     for commit, test_set in tasks.items():
#         project = rows[0]['project'] 
#         cwd = PROJECT_DIR_MAP[project]
        
#         # Ensure commit key exists
#         if commit not in results_cache: results_cache[commit] = {}

#         # Filter already done
#         todos = [t for t in test_set if t not in results_cache[commit]]
        
#         if not todos:
#             print(f"Commit {commit}: All tests cached. Skipping.")
#             continue
            
#         print(f"Processing Commit: {commit} ({len(todos)} tests)")
        
#         if clean_and_checkout(project, commit):
#             for i, test in enumerate(todos):
#                 print(f"  Measuring [{i+1}/{len(todos)}]: {test}")
#                 metrics = measure_single_test(project, test, cwd)
                
#                 if metrics:
#                     results_cache[commit][test] = metrics
#                     save_checkpoint(results_cache) # Save JSON Checkpoint
                    
#                     # PERIODIC CSV WRITE (Every 100 tests)
#                     global_test_counter += 1
#                     if global_test_counter % CSV_WRITE_INTERVAL == 0:
#                         print(f"  [Auto-Save] Writing CSV after {global_test_counter} tests...")
#                         write_csv_from_cache(rows, results_cache)
#                 else:
#                     logging.warning(f"Failed to measure {test} on {commit}")
#         else:
#             logging.error(f"Skipping commit {commit} due to build failure.")

#     # 5. Final CSV Write
#     print("Writing Final CSV...")
#     write_csv_from_cache(rows, results_cache)
#     print(f"Done. Measured data saved to: {OUTPUT_CSV_PATH}")

# if __name__ == "__main__":
#     main()