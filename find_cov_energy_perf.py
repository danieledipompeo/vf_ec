import pandas as pd
import os
import subprocess
import time
import statistics
import shutil
import sys
import logging
from datetime import datetime

# ================= CONFIGURATION =================
# Root directory is assumed to be: /home/mwk/UCD/vf_ec/

TC_RESULTS_PATH = os.path.join("vfec_results", "tc_results.csv")
SNAPSHOT_PATH = os.path.join("vfec_results", "ds_snapshot.csv")
PROJECTS_ROOT = "ds_projects"
OUTPUT_CSV = os.path.join("vfec_results", "cov_energy_perf.csv")
COVERAGE_ROOT = os.path.join("vfec_results", "coverage")
LOG_DIR = os.path.join("vfec_results", "logs")

# Build Flags (Optimized for Coverage vs Performance)
COV_CFLAGS = "-fprofile-arcs -ftest-coverage -g -O0"
COV_LDFLAGS = "-lgcov --coverage"
INVALID_CWES = {"NVD-CWE-Other", "NVD-CWE-noinfo", "", float("nan")}

# Energy: Minimum duration to get valid RAPL reading (0.1s)
MIN_DURATION_SEC = 0.1 

# ================= LOGGING =================
if not os.path.exists(LOG_DIR): os.makedirs(LOG_DIR)
log_file = os.path.join(LOG_DIR, f"pipeline_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger()

# ================= HELPER FUNCTIONS =================

def run_cmd(cmd, cwd, ignore_errors=False, timeout=1200):
    try:
        if not os.path.exists(cwd): return False
        
        # logger.debug(f"Exec: {cmd}") # Uncomment for verbose debug
        result = subprocess.run(
            cmd, cwd=cwd, shell=True, 
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=timeout
        )
        
        if result.returncode != 0 and not ignore_errors:
            logger.error(f"Cmd Failed in {cwd}: {cmd}")
            logger.error(f"Stderr: {result.stderr.strip()[:300]}")
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.error(f"Timeout ({timeout}s): {cmd}")
        return False
    except Exception as e:
        logger.error(f"Exec Error: {e}")
        return False

def find_executable(repo_path, script_name):
    """
    Locates the test script/binary.
    1. Checks direct path.
    2. Searches in subfolders (tests, test, regtest).
    """
    script_name = script_name.split()[0] # Remove arguments
    
    # Check 1: Is it an absolute path or relative to root?
    if os.path.exists(os.path.join(repo_path, script_name)):
        return script_name
    
    # Check 2: Recursive search (depth 2)
    for root, dirs, files in os.walk(repo_path):
        if script_name in files:
            rel_path = os.path.relpath(os.path.join(root, script_name), repo_path)
            logger.info(f"Found executable at: {rel_path}")
            return rel_path
        # Stop digging too deep
        if root.count(os.sep) - repo_path.count(os.sep) >= 2:
            del dirs[:]
            
    logger.warning(f"Could not find executable: {script_name}")
    return script_name # Return original hoping for the best

def checkout_commit(repo_path, commit_hash):
    logger.info(f"Checking out {commit_hash[:8]}...")
    run_cmd("git clean -fdx", repo_path, ignore_errors=True)
    run_cmd("git reset --hard", repo_path, ignore_errors=True)
    return run_cmd(f"git checkout {commit_hash}", repo_path)

def build_project(repo_path, mode='clean'):
    logger.info(f"Building {os.path.basename(repo_path)} (Mode: {mode})")
    
    has_cmake = os.path.exists(os.path.join(repo_path, "CMakeLists.txt"))
    has_configure = os.path.exists(os.path.join(repo_path, "configure"))
    has_makefile = os.path.exists(os.path.join(repo_path, "Makefile"))

    cflags = COV_CFLAGS if mode == 'coverage' else "-O2"
    ldflags = COV_LDFLAGS if mode == 'coverage' else ""

    try:
        if has_cmake:
            build_dir = os.path.join(repo_path, "build_vfec")
            if os.path.exists(build_dir): shutil.rmtree(build_dir)
            os.makedirs(build_dir)
            cmd = f"cmake .. -DCMAKE_C_FLAGS='{cflags}' -DCMAKE_EXE_LINKER_FLAGS='{ldflags}'"
            if not run_cmd(cmd, build_dir): return False
            return run_cmd(f"make -j{os.cpu_count()}", build_dir)
        elif has_configure:
            run_cmd("make distclean", repo_path, ignore_errors=True)
            if not run_cmd(f"./configure CFLAGS='{cflags}' LDFLAGS='{ldflags}'", repo_path): return False
            return run_cmd(f"make -j{os.cpu_count()}", repo_path)
        elif has_makefile:
            run_cmd("make clean", repo_path, ignore_errors=True)
            return run_cmd(f"make -j{os.cpu_count()} CFLAGS='{cflags}' LDFLAGS='{ldflags}'", repo_path)
        return False
    except Exception:
        return False

def parse_lcov_info(info_path):
    if not os.path.exists(info_path): return ""
    touched = set()
    with open(info_path, 'r', errors='ignore') as f:
        for line in f:
            if line.startswith("SF:"):
                # Clean path
                raw_path = line.strip().split(":", 1)[1]
                # We only want the filename to avoid long path issues
                touched.add(os.path.basename(raw_path))
    return ";".join(sorted(list(touched)))

def get_coverage_run(repo_path, test_cmd_raw, output_path):
    # Locate real path
    parts = test_cmd_raw.split()
    exe = find_executable(repo_path, parts[0])
    args = " ".join(parts[1:])
    full_cmd = f"./{exe} {args}" if not exe.startswith("/") and not exe.startswith(".") else f"{exe} {args}"

    logger.info(f"Coverage Run: {full_cmd}")
    run_cmd(f"{full_cmd} || true", repo_path)
    
    info_file = output_path + ".info"
    # Note: We look in . (root) AND ./build_vfec because cmake hides gcda files there
    cmd = f"lcov --capture --directory . --directory build_vfec --output-file {info_file} --rc lcov_branch_coverage=1 --ignore-errors gcov"
    run_cmd(cmd, repo_path, ignore_errors=True)
    
    return parse_lcov_info(info_file)

def measure_energy_smart(repo_path, test_cmd_raw):
    """
    Runs test. If too fast (<0.1s), runs in a loop (Batch Mode).
    Returns averaged metrics per single run.
    """
    # 1. Locate Binary
    parts = test_cmd_raw.split()
    exe = find_executable(repo_path, parts[0])
    args = " ".join(parts[1:])
    # Ensure ./ prefix if needed
    if not exe.startswith("/") and not exe.startswith("."):
        exe = "./" + exe
    
    base_cmd = f"{exe} {args}"
    perf_out = os.path.abspath(os.path.join(repo_path, "perf_temp.txt"))

    # 2. DRY RUN (Check Duration)
    start_t = time.time()
    run_cmd(f"{base_cmd} > /dev/null 2>&1", repo_path, ignore_errors=True)
    duration = time.time() - start_t

    # 3. DETERMINE LOOP COUNT
    loop_count = 1
    if duration < MIN_DURATION_SEC:
        # If it takes 0.0004s, we need ~250 runs to hit 0.1s. Let's aim for 0.5s safety.
        # Avoid division by zero
        safe_dur = max(duration, 0.000001)
        loop_count = int(0.5 / safe_dur)
        # Cap at 2000 to prevent timeouts
        loop_count = min(loop_count, 2000)
        logger.info(f"    -> Test too fast ({duration:.5f}s). Enabling Batch Mode: {loop_count} loops.")

    # 4. CONSTRUCT PERF COMMAND
    # We use a shell loop to run the command N times inside ONE perf session
    if loop_count > 1:
        # sh -c "for i in $(seq N); do cmd; done"
        final_cmd = f"sh -c 'for i in $(seq {loop_count}); do {base_cmd} > /dev/null; done'"
    else:
        final_cmd = base_cmd

    # 5. RUN PERF (3 Repetitions of the batch)
    measurements = []
    for _ in range(3):
        cmd = f"sudo perf stat -e power/energy-pkg/,cycles,instructions,task-clock -x, -o {perf_out} -- {final_cmd}"
        if run_cmd(cmd, repo_path, ignore_errors=True):
            if os.path.exists(perf_out):
                m = {"energy": 0.0, "cycles": 0, "instructions": 0, "time": 0.0}
                with open(perf_out, 'r') as f:
                    for line in f:
                        p = line.strip().split(',')
                        if len(p) < 3 or "<not supported>" in p[0] or not p[0]: continue
                        try:
                            val = float(p[0])
                            if "power/energy-pkg" in p[2]: m["energy"] = val
                            elif "cycles" in p[2]: m["cycles"] = int(val)
                            elif "instructions" in p[2]: m["instructions"] = int(val)
                            elif "task-clock" in p[2]: m["time"] = val / 1000.0
                        except: pass
                
                # NORMALIZE: Divide by loop_count to get "Per Run" metrics
                if loop_count > 1:
                    m["energy"] /= loop_count
                    m["cycles"] = int(m["cycles"] / loop_count)
                    m["instructions"] = int(m["instructions"] / loop_count)
                    m["time"] /= loop_count

                m["ipc"] = m["instructions"]/m["cycles"] if m["cycles"] > 0 else 0
                measurements.append(m)

    if not measurements: return None

    # Median
    med = {}
    for k in measurements[0].keys():
        med[k] = statistics.median([x[k] for x in measurements])
    return med

# ================= MAIN =================

def main():
    logger.info("=== Starting Auto-VFEC Pipeline v5 (Smart Search & Loop) ===")
    
    try:
        df_tc = pd.read_csv(TC_RESULTS_PATH)
        df_snap = pd.read_csv(SNAPSHOT_PATH)
    except Exception as e:
        logger.critical(f"Load Error: {e}")
        return

    df_snap_unique = df_snap[['fix_commit', 'cve', 'cwe']].drop_duplicates(subset=['fix_commit'])
    df_merged = pd.merge(df_tc, df_snap_unique, on='fix_commit', how='left')
    
    mask = (df_merged['vuln_sf'] == "Pending_Coverage_Analysis") & \
           (df_merged['cve'].notna()) & \
           (~df_merged['cwe'].isin(INVALID_CWES))
    
    df_queue = df_merged[mask].copy()
    logger.info(f"Queue Size: {len(df_queue)}")

    grouped = df_queue.groupby(['project', 'vuln_commit', 'fix_commit'])

    for (proj, v_hash, f_hash), group in grouped:
        repo = os.path.join(PROJECTS_ROOT, proj)
        logger.info(f"--- Group: {proj} (Tests: {len(group)}) ---")
        
        execution_data = {}

        # 1. VULN PHASE
        if checkout_commit(repo, v_hash):
            if build_project(repo, 'coverage'):
                for _, row in group.iterrows():
                    tn = row['vuln_tn']
                    out = os.path.join(COVERAGE_ROOT, proj, f"cov_{v_hash[:8]}_{tn.replace('/','_').replace(' ','_')}")
                    if not os.path.exists(os.path.dirname(out)): os.makedirs(os.path.dirname(out))
                    
                    if tn not in execution_data: execution_data[tn] = {}
                    execution_data[tn]['v_sf'] = get_coverage_run(repo, tn, out)
            
            if build_project(repo, 'clean'):
                for _, row in group.iterrows():
                    tn = row['vuln_tn']
                    res = measure_energy_smart(repo, tn)
                    if res: execution_data[tn]['v_eng'] = res

        # 2. FIX PHASE
        if checkout_commit(repo, f_hash):
            if build_project(repo, 'coverage'):
                for _, row in group.iterrows():
                    tn = row['fix_tn']
                    v_tn = row['vuln_tn']
                    
                    out = os.path.join(COVERAGE_ROOT, proj, f"cov_{f_hash[:8]}_{tn.replace('/','_').replace(' ','_')}")
                    if not os.path.exists(os.path.dirname(out)): os.makedirs(os.path.dirname(out))
                    
                    if v_tn in execution_data:
                        execution_data[v_tn]['f_sf'] = get_coverage_run(repo, tn, out)

            if build_project(repo, 'clean'):
                for _, row in group.iterrows():
                    tn = row['fix_tn']
                    v_tn = row['vuln_tn']
                    
                    if v_tn in execution_data:
                        res = measure_energy_smart(repo, tn)
                        if res: execution_data[v_tn]['f_eng'] = res

        # 3. SAVE PHASE
        for _, row in group.iterrows():
            key = row['vuln_tn']
            data = execution_data.get(key, {})
            
            v_eng = data.get('v_eng')
            f_eng = data.get('f_eng')
            
            if v_eng and f_eng:
                rec = {
                    'project': proj,
                    'vuln_commit': v_hash,
                    'vuln_tn': row['vuln_tn'],
                    'vuln_sf': data.get('v_sf', ''),
                    'vuln_energy_joule': v_eng['energy'],
                    'vuln_time_sec': v_eng['time'],
                    'vuln_cycles': v_eng['cycles'],
                    'vuln_instructions': v_eng['instructions'],
                    'vuln_ipc': v_eng['ipc'],
                    
                    'fix_commit': f_hash,
                    'fix_tn': row['fix_tn'],
                    'fix_sf': data.get('f_sf', ''),
                    'fix_energy_joule': f_eng['energy'],
                    'fix_time_sec': f_eng['time'],
                    'fix_cycles': f_eng['cycles'],
                    'fix_instructions': f_eng['instructions'],
                    'fix_ipc': f_eng['ipc']
                }
                
                pd.DataFrame([rec]).to_csv(OUTPUT_CSV, mode='a', header=not os.path.exists(OUTPUT_CSV), index=False)
                logger.info(f"Saved: {key}")
            else:
                logger.warning(f"Incomplete data for {key}, skipping.")

if __name__ == "__main__":
    main()