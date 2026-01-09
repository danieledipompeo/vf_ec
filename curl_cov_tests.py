# FIX_COMMIT for fix commit
# writer.writerow(["project", "fix_commit", "testfile", "sourcefile"])
# OUTPUT_CSV = os.path.join(RESULTS_DIR, "test_output.csv")
# LOG_FILE = os.path.join(RESULTS_DIR, "log", f"{PROJECT_NAME}_{VULN_COMMIT[:8]}.txt")


import os
import subprocess
import glob
import csv

# --- HARDCODED CONFIGURATION ---
PROJECT_NAME = "curl"
FIX_COMMIT = "380e132da3557235e4eb9aa593e79c62100156ea" # Updated to the commit you are debugging
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
            print(f"Critical Failure: {description}. Check logs.")
            # We don't exit here strictly to allow pipeline to attempt cleanup or logging, 
            # but usually build failure is fatal.
            exit(1)
        return False

def get_fix_files():
    """Returns list of .c files from git diff."""
    cmd = f"git show --name-only {FIX_COMMIT}"
    try:
        result = subprocess.check_output(cmd, cwd=PROJECT_PATH, shell=True, text=True)
        # We only care about filename, not full path for simple matching, 
        # but usually git returns path. We take basename to match get_touched logic.
        return [os.path.basename(f) for f in result.splitlines() if f.endswith('.c')]
    except subprocess.CalledProcessError:
        write_log(f"Failed to get files for commit {FIX_COMMIT}")
        return []

def normalize_gcda_name(filename):
    """
    Converts a gcda filename to a likely source filename, 
    handling curl's build prefixes.
    Ex: 'curl-tool_msgs.gcda' -> 'tool_msgs.c'
    """
    # 1. Remove extension
    if filename.endswith('.gcda'):
        name = filename[:-5]
    else:
        name = filename

    # 2. Known prefixes in Curl's build system
    prefixes = ["curl-", "libcurl_la-", "libcurltool_la-"]
    
    for prefix in prefixes:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break # Stop after stripping one valid prefix
            
    return name + ".c"

def get_touched_source_files():
    """Finds all .gcda files and maps them to clean .c filenames."""
    gcda_files = glob.glob(f"{PROJECT_PATH}/**/*.gcda", recursive=True)
    
    touched_sources = set()
    for g in gcda_files:
        base_name = os.path.basename(g)
        clean_name = normalize_gcda_name(base_name)
        touched_sources.add(clean_name)
        
    return touched_sources

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

    if not fixed_files:
        print("⚠️  No .c files found in this commit. Exiting.")
        return

    # Build steps
    # Note: Added -g -O0 to CFLAGS ensures coverage lines match source exactly
    run_cmd("./buildconf", PROJECT_PATH, "Buildconf")
    config_flags = (
        '--disable-ldap --without-ssl --disable-shared --enable-debug --enable-maintainer-mode '
        'CFLAGS="-fprofile-arcs -ftest-coverage -g -O0" LDFLAGS="-fprofile-arcs -ftest-coverage"'
    )
    run_cmd(f"./configure {config_flags}", PROJECT_PATH, "Configure")
    run_cmd("make -j4", PROJECT_PATH, "Make Main")
    
    # Build the test suite
    run_cmd("make", os.path.join(PROJECT_PATH, "tests"), "Make Tests")

    # Get list of all test IDs
    test_data_dir = os.path.join(PROJECT_PATH, "tests/data")
    test_files = sorted(glob.glob(os.path.join(test_data_dir, "test*")))
    # Extract just the numbers
    test_ids = [os.path.basename(t).replace("test", "") for t in test_files if os.path.basename(t).replace("test", "").isdigit()]

    print(f"🧪 Phase 2: Running {len(test_ids)} tests and checking intersection...")
    
    # Prepare CSV header
    file_exists = os.path.isfile(OUTPUT_CSV)
    with open(OUTPUT_CSV, "a", newline="") as csvfile:
        writer = csv.writer(csvfile)
        if not file_exists:
            writer.writerow(["project", "fix_commit", "testfile", "sourcefile"])

        for tid in test_ids:
            # 1. Clean previous coverage (Crucial to avoid data leaking between tests)
            subprocess.run(f"find . -name '*.gcda' -delete", cwd=PROJECT_PATH, shell=True)
            
            # 2. Run Test using runtests.pl
            # Note: runtests.pl typically passes even if the test logic fails, 
            # but we care if it RAN the code, not if it passed/failed logic.
            test_success = run_cmd(f"./runtests.pl {tid}", os.path.join(PROJECT_PATH, "tests"), f"Test {tid}", can_fail=True)
            
            # Even if test_success is False (test failed), we might still have coverage data.
            # So we proceed to check coverage regardless of test outcome.

            # 3. Check intersection
            touched_files = get_touched_source_files()
            intersection = [f for f in fixed_files if f in touched_files]

            if intersection:
                for source in intersection:
                    writer.writerow([PROJECT_NAME, FIX_COMMIT, tid, source])
                print(f"✅ Test {tid} touched: {intersection}")
                # Optional: break here if you only need ONE test that touches the code
                # break 

    print(f"🏁 Finished. Results in {OUTPUT_CSV}, Errors in {LOG_FILE}")

if __name__ == "__main__":
    main()


# import os
# import subprocess
# import glob
# import csv

# # --- HARDCODED CONFIGURATION ---
# PROJECT_NAME = "curl"
# FIX_COMMIT = "9069838b30fb3b48af0123e39f664cea683254a5"
# PROJECT_BASE_DIR = "ds_projects" # Relative to script dir
# RESULTS_DIR = "vfec_results"     # Relative to script dir

# # Derived Paths
# PROJECT_PATH = os.path.join(PROJECT_BASE_DIR, PROJECT_NAME)
# LOG_FILE = os.path.join(RESULTS_DIR, "log", f"{PROJECT_NAME}_{FIX_COMMIT[:8]}.txt")
# OUTPUT_CSV = os.path.join(RESULTS_DIR, "test_output.csv")

# def ensure_dirs():
#     """Create necessary results directories if they don't exist."""
#     os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
#     os.makedirs(os.path.dirname(OUTPUT_CSV), exist_ok=True)

# def write_log(message):
#     with open(LOG_FILE, "a") as f:
#         f.write(message + "\n")

# def run_cmd(command, cwd, description, can_fail=False):
#     """Executes command. Logs only on failure."""
#     try:
#         subprocess.run(
#             command, shell=True, cwd=cwd, check=True,
#             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
#         )
#         return True
#     except subprocess.CalledProcessError as e:
#         error_msg = f"ERROR in {description}:\nCmd: {command}\nStderr: {e.stderr}"
#         write_log(error_msg)
#         if not can_fail:
#             print(f"Critial Failure: {description}. Check logs.")
#             exit(1)
#         return False

# def get_fix_files():
#     """Returns list of .c files from git diff."""
#     cmd = f"git show --name-only {FIX_COMMIT}"
#     result = subprocess.check_output(cmd, cwd=PROJECT_PATH, shell=True, text=True)
#     return [os.path.basename(f) for f in result.splitlines() if f.endswith('.c')]

# def get_touched_source_files():
#     """Finds all .gcda files and maps them to .c filenames."""
#     gcda_files = glob.glob(f"{PROJECT_PATH}/**/*.gcda", recursive=True)
#     return set(os.path.basename(g).replace('.gcda', '.c') for g in gcda_files)

# def main():
#     ensure_dirs()
#     # Clear log for new run
#     with open(LOG_FILE, "w") as f: f.write(f"--- Log for {PROJECT_NAME} @ {FIX_COMMIT} ---\n")

#     print(f"🛠️  Phase 1: Building {PROJECT_NAME} with Coverage...")
#     run_cmd("git reset --hard", PROJECT_PATH, "Git Reset")
#     run_cmd("git clean -fdx", PROJECT_PATH, "Git Clean")
#     run_cmd(f"git checkout {FIX_COMMIT}", PROJECT_PATH, "Git Checkout")
    
#     fixed_files = get_fix_files()
#     print(f"🎯 Target files from fix: {fixed_files}")

#     # Build steps
#     run_cmd("./buildconf", PROJECT_PATH, "Buildconf")
#     config_flags = (
#         '--disable-ldap --without-ssl --disable-shared --enable-debug '
#         'CFLAGS="-fprofile-arcs -ftest-coverage" LDFLAGS="-fprofile-arcs -ftest-coverage"'
#     )
#     run_cmd(f"./configure {config_flags}", PROJECT_PATH, "Configure")
#     run_cmd("make -j4", PROJECT_PATH, "Make Main")
#     run_cmd("make", os.path.join(PROJECT_PATH, "tests"), "Make Tests")

#     # Get list of all test IDs
#     test_data_dir = os.path.join(PROJECT_PATH, "tests/data")
#     test_files = sorted(glob.glob(os.path.join(test_data_dir, "test*")))
#     test_ids = [os.path.basename(t).replace("test", "") for t in test_files if os.path.basename(t).replace("test", "").isdigit()]

#     print(f"🧪 Phase 2: Running {len(test_ids)} tests and checking intersection...")
    
#     # Prepare CSV header
#     file_exists = os.path.isfile(OUTPUT_CSV)
#     with open(OUTPUT_CSV, "a", newline="") as csvfile:
#         writer = csv.writer(csvfile)
#         if not file_exists:
#             writer.writerow(["project", "fix_commit", "testfile", "sourcefile"])

#         for tid in test_ids:
#             # 1. Clean previous coverage
#             subprocess.run(f"find . -name '*.gcda' -delete", cwd=PROJECT_PATH, shell=True)
            
#             # 2. Run Test
#             test_success = run_cmd(f"./runtests.pl {tid}", os.path.join(PROJECT_PATH, "tests"), f"Test {tid}", can_fail=True)
            
#             if not test_success:
#                 write_log(f"Test {tid} failed or was skipped by curl runner.")
#                 continue

#             # 3. Check intersection
#             touched_files = get_touched_source_files()
#             intersection = [f for f in fixed_files if f in touched_files]

#             if intersection:
#                 for source in intersection:
#                     writer.writerow([PROJECT_NAME, FIX_COMMIT, tid, source])
#                 print(f"✅ Test {tid} touched: {intersection}")

#     print(f"🏁 Finished. Results in {OUTPUT_CSV}, Errors in {LOG_FILE}")

# if __name__ == "__main__":
#     main()