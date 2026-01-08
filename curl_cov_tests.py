import os
import subprocess
import glob
import csv

# --- HARDCODED CONFIGURATION ---
PROJECT_NAME = "curl"
FIX_COMMIT = "70b1900dd13d16f2e83f571407a614541d5ac9ba"
PROJECT_BASE_DIR = "ds_projects" # Relative to script dir
RESULTS_DIR = "vfec_results"     # Relative to script dir

# Derived Paths
PROJECT_PATH = os.path.join(PROJECT_BASE_DIR, PROJECT_NAME)
LOG_FILE = os.path.join(RESULTS_DIR, "log", f"{PROJECT_NAME}_{FIX_COMMIT[:8]}.txt")
OUTPUT_CSV = os.path.join(RESULTS_DIR, "test_output.csv")

def ensure_dirs():
    """Create necessary results directories if they don't exist."""
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    os.makedirs(os.path.dirname(OUTPUT_CSV), exist_ok=True)

def write_log(message):
    with open(LOG_FILE, "a") as f:
        f.write(message + "\n")

def run_cmd(command, cwd, description, can_fail=False):
    """Executes command. Logs only on failure."""
    try:
        subprocess.run(
            command, shell=True, cwd=cwd, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        return True
    except subprocess.CalledProcessError as e:
        error_msg = f"ERROR in {description}:\nCmd: {command}\nStderr: {e.stderr}"
        write_log(error_msg)
        if not can_fail:
            print(f"Critial Failure: {description}. Check logs.")
            exit(1)
        return False

def get_fix_files():
    """Returns list of .c files from git diff."""
    cmd = f"git show --name-only {FIX_COMMIT}"
    result = subprocess.check_output(cmd, cwd=PROJECT_PATH, shell=True, text=True)
    return [os.path.basename(f) for f in result.splitlines() if f.endswith('.c')]

def get_touched_source_files():
    """Finds all .gcda files and maps them to .c filenames."""
    gcda_files = glob.glob(f"{PROJECT_PATH}/**/*.gcda", recursive=True)
    return set(os.path.basename(g).replace('.gcda', '.c') for g in gcda_files)

def main():
    ensure_dirs()
    # Clear log for new run
    with open(LOG_FILE, "w") as f: f.write(f"--- Log for {PROJECT_NAME} @ {FIX_COMMIT} ---\n")

    print(f"🛠️  Phase 1: Building {PROJECT_NAME} with Coverage...")
    run_cmd("git reset --hard", PROJECT_PATH, "Git Reset")
    run_cmd("git clean -fdx", PROJECT_PATH, "Git Clean")
    run_cmd(f"git checkout {FIX_COMMIT}", PROJECT_PATH, "Git Checkout")
    
    fixed_files = get_fix_files()
    print(f"🎯 Target files from fix: {fixed_files}")

    # Build steps
    run_cmd("./buildconf", PROJECT_PATH, "Buildconf")
    config_flags = (
        '--disable-ldap --without-ssl --disable-shared --enable-debug '
        'CFLAGS="-fprofile-arcs -ftest-coverage" LDFLAGS="-fprofile-arcs -ftest-coverage"'
    )
    run_cmd(f"./configure {config_flags}", PROJECT_PATH, "Configure")
    run_cmd("make -j4", PROJECT_PATH, "Make Main")
    run_cmd("make", os.path.join(PROJECT_PATH, "tests"), "Make Tests")

    # Get list of all test IDs
    test_data_dir = os.path.join(PROJECT_PATH, "tests/data")
    test_files = sorted(glob.glob(os.path.join(test_data_dir, "test*")))
    test_ids = [os.path.basename(t).replace("test", "") for t in test_files if os.path.basename(t).replace("test", "").isdigit()]

    print(f"🧪 Phase 2: Running {len(test_ids)} tests and checking intersection...")
    
    # Prepare CSV header
    file_exists = os.path.isfile(OUTPUT_CSV)
    with open(OUTPUT_CSV, "a", newline="") as csvfile:
        writer = csv.writer(csvfile)
        if not file_exists:
            writer.writerow(["project", "fix_commit", "testfile", "sourcefile"])

        for tid in test_ids:
            # 1. Clean previous coverage
            subprocess.run(f"find . -name '*.gcda' -delete", cwd=PROJECT_PATH, shell=True)
            
            # 2. Run Test
            test_success = run_cmd(f"./runtests.pl {tid}", os.path.join(PROJECT_PATH, "tests"), f"Test {tid}", can_fail=True)
            
            if not test_success:
                write_log(f"Test {tid} failed or was skipped by curl runner.")
                continue

            # 3. Check intersection
            touched_files = get_touched_source_files()
            intersection = [f for f in fixed_files if f in touched_files]

            if intersection:
                for source in intersection:
                    writer.writerow([PROJECT_NAME, FIX_COMMIT, tid, source])
                print(f"✅ Test {tid} touched: {intersection}")

    print(f"🏁 Finished. Results in {OUTPUT_CSV}, Errors in {LOG_FILE}")

if __name__ == "__main__":
    main()