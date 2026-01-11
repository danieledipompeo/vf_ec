import pandas as pd
import subprocess
import os
import shutil
import sys
import re

# =================CONFIGURATION=================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Input: Your "Modern Only" list
DS_SNAPSHOT_PATH = os.path.join(SCRIPT_DIR, 'vfec_results/ds_snapshot.csv')

# Output
RESULTS_DIR = os.path.join(SCRIPT_DIR, 'vfec_results')
OUTPUT_CSV_PATH = os.path.join(RESULTS_DIR, 'filtered_tests_granular.csv')
DS_PROJECTS_DIR = os.path.join(SCRIPT_DIR, 'ds_projects')

# Ensure directories exist
os.makedirs(os.path.join(RESULTS_DIR, 'logs_granular'), exist_ok=True)

def run_cmd(cmd, cwd, log_file, env=None, ignore_errors=False):
    """Runs a command and logs it."""
    with open(log_file, 'a') as f:
        # Don't log every single test run to avoid massive log files, 
        # unless it's a build command
        if 'ctest' not in cmd[0]: 
            f.write(f"\n>>> CMD: {' '.join(cmd)}\n")
        
        run_env = os.environ.copy()
        if env: run_env.update(env)
        try:
            subprocess.run(cmd, cwd=cwd, env=run_env, stdout=f, stderr=f, check=True)
        except subprocess.CalledProcessError as e:
            if not ignore_errors: raise e

def get_available_tests(build_dir, log_file):
    """Asks CTest for the list of available test names."""
    try:
        # -N lists tests without running them
        cmd = ['ctest', '-N']
        result = subprocess.check_output(cmd, cwd=build_dir, stderr=subprocess.STDOUT).decode('utf-8')
        
        # Output format is usually: "Test #1: TestName"
        test_names = []
        for line in result.splitlines():
            match = re.search(r'Test #\d+:\s+(.+)', line)
            if match:
                test_names.append(match.group(1))
        return test_names
    except Exception as e:
        with open(log_file, 'a') as f: f.write(f"[WARN] Could not list tests: {e}\n")
        return []

def reset_coverage_data(repo_path):
    """Deletes all .gcda (execution counts) and .gcov (reports) files."""
    for root, dirs, files in os.walk(repo_path):
        for file in files:
            if file.endswith('.gcda') or file.endswith('.gcov'):
                try:
                    os.remove(os.path.join(root, file))
                except: pass

def collect_covered_files(repo_path):
    """Runs gcov and returns a Set of filenames that were executed."""
    covered_files = set()
    
    # 1. Run GCOV on all found .gcda files
    for root, dirs, files in os.walk(repo_path):
        for file in files:
            if file.endswith('.gcda'):
                try:
                    source_guess = file.replace('.gcda', '.c')
                    # We send output to DEVNULL to keep logs clean during the loop
                    subprocess.run(['gcov', source_guess], cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except: pass

    # 2. Parse the resulting .gcov files
    for root, dirs, files in os.walk(repo_path):
        for file in files:
            if file.endswith('.gcov'):
                try:
                    path = os.path.join(root, file)
                    with open(path, 'r', errors='replace') as gcov_f:
                        # Check for any line with a non-zero count
                        if re.search(r'^\s*[1-9]\d*:', gcov_f.read(), re.MULTILINE):
                            clean_name = file.replace('.gcov', '')
                            covered_files.add(clean_name)
                except: pass
    
    return covered_files

def main():
    # 1. Setup CSV
    if not os.path.exists(OUTPUT_CSV_PATH):
        # Header matches your requirement
        pd.DataFrame(columns=['project', 'fix_commit', 'testfile', 'source_file']).to_csv(OUTPUT_CSV_PATH, index=False)

    if not os.path.exists(DS_SNAPSHOT_PATH):
        print("Error: Input CSV not found.")
        return

    df = pd.read_csv(DS_SNAPSHOT_PATH)
    total_commits = len(df)
    print(f"Loaded {total_commits} commits. Starting Granular Analysis...")

    # 2. Iterate Commits
    for index, row in df.iterrows():
        project = row['project']
        fix_commit = row['fix_commit']
        repo_path = os.path.join(DS_PROJECTS_DIR, project)
        log_file = os.path.join(RESULTS_DIR, 'logs_granular', f'{project}_{fix_commit[:8]}.txt')

        print(f"[{index+1}/{total_commits}] {project} @ {fix_commit[:8]}...", end='', flush=True)

        if not os.path.exists(repo_path):
            print(" [SKIP] Repo missing.")
            continue

        try:
            # --- PHASE 1: PREPARE & BUILD ---
            # Clean
            subprocess.run(['git', 'checkout', '-f', fix_commit], cwd=repo_path, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
            subprocess.run(['git', 'clean', '-fdx'], cwd=repo_path, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)

            # Build Directory
            build_dir = os.path.join(repo_path, 'build_vfec')
            if os.path.exists(build_dir): shutil.rmtree(build_dir)
            os.makedirs(build_dir)

            # CMake Configure
            cmake_cmd = [
                'cmake', '..',
                '-DCMAKE_BUILD_TYPE=Debug',
                '-DCMAKE_C_FLAGS=-fprofile-arcs -ftest-coverage -g -O0',
                '-DCMAKE_CXX_FLAGS=-fprofile-arcs -ftest-coverage -g -O0',
                '-DCMAKE_EXE_LINKER_FLAGS=-fprofile-arcs -ftest-coverage'
            ]
            run_cmd(cmake_cmd, build_dir, log_file)
            
            # Compile
            run_cmd(['cmake', '--build', '.', '--parallel', '4'], build_dir, log_file)

            # Get Git Diff Files (The Target)
            diff_cmd = ['git', 'diff-tree', '--no-commit-id', '--name-only', '-r', fix_commit]
            diff_out = subprocess.check_output(diff_cmd, cwd=repo_path).decode().splitlines()
            diff_files = {os.path.basename(f) for f in diff_out} # Set for faster lookup
            
            if not diff_files:
                print(" -> No diff files found. Skipping.")
                continue

            # --- PHASE 2: TEST IDENTIFICATION ---
            test_list = get_available_tests(build_dir, log_file)
            num_tests = len(test_list)
            
            if num_tests == 0:
                print(" -> No tests found.")
                continue

            print(f" Scanning {num_tests} tests...", end='', flush=True)

            # --- PHASE 3: SEQUENTIAL EXECUTION ---
            matches_found_count = 0
            
            for i, test_name in enumerate(test_list):
                # 1. Reset counters (Crucial for granular mapping)
                reset_coverage_data(repo_path)
                
                # 2. Run ONE test
                # -R regex matches the exact test name
                subprocess.run(['ctest', '-R', f'^{re.escape(test_name)}$'], cwd=build_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                
                # 3. Collect Coverage
                covered_files = collect_covered_files(repo_path)
                
                # 4. Check Intersection
                intersection = diff_files.intersection(covered_files)
                
                if intersection:
                    matches_found_count += 1
                    # Append matching rows immediately to CSV
                    rows = []
                    for src in intersection:
                        rows.append({
                            'project': project,
                            'fix_commit': fix_commit,
                            'testfile': test_name, # THE SPECIFIC TEST NAME
                            'source_file': src
                        })
                    pd.DataFrame(rows).to_csv(OUTPUT_CSV_PATH, mode='a', header=False, index=False)

            print(f" -> Found {matches_found_count} relevant tests.")

        except Exception as e:
            print(" -> FAILED.")
            with open(log_file, 'a') as f: f.write(f"\n[ERROR] {e}\n")

if __name__ == "__main__":
    main()