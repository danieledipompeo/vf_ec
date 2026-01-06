import pandas as pd
import subprocess
import os
import shutil
import sys
import re

# =================CONFIGURATION=================
# Base paths relative to this script
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Corrected paths (No "../")
DS_SNAPSHOT_PATH = os.path.join(SCRIPT_DIR, 'vfec_results/ds_snapshot.csv')
DS_PROJECTS_DIR = os.path.join(SCRIPT_DIR, 'ds_projects')
RESULTS_DIR = os.path.join(SCRIPT_DIR, 'vfec_results')
OUTPUT_CSV_PATH = os.path.join(RESULTS_DIR, 'filtered_tests.csv')

# Ensure directories exist
os.makedirs(os.path.join(RESULTS_DIR, 'logs'), exist_ok=True)

def run_cmd(cmd, cwd, log_file, env=None, ignore_errors=False):
    """Runs a command and writes output to log."""
    with open(log_file, 'a') as f:
        f.write(f"\n>>> CMD: {' '.join(cmd)}\n")
        f.flush()
        run_env = os.environ.copy()
        if env:
            run_env.update(env)
        
        try:
            subprocess.run(cmd, cwd=cwd, env=run_env, stdout=f, stderr=f, check=True)
        except subprocess.CalledProcessError as e:
            if not ignore_errors:
                raise e

def build_with_autotools(repo_path, log_file, env, project_name):
    """Generic ./configure && make logic"""
    # 1. Generate configure if missing
    if os.path.exists(os.path.join(repo_path, 'buildconf')):
        run_cmd(['./buildconf'], repo_path, log_file, env)
    elif os.path.exists(os.path.join(repo_path, 'autogen.sh')):
        run_cmd(['./autogen.sh'], repo_path, log_file, env)
    else:
        try:
            run_cmd(['autoreconf', '-fi'], repo_path, log_file, env)
        except:
            pass

    # 2. Configure
    # Special flags for CURL, generic for others
    if project_name == 'curl':
        conf_cmd = ['./configure', '--disable-shared', '--with-openssl', '--disable-threaded-resolver']
    else:
        # Standard flags for most C projects to enable coverage
        conf_cmd = ['./configure', '--disable-shared']
        
    run_cmd(conf_cmd, repo_path, log_file, env)

    # 3. Make
    run_cmd(['make', '-j4'], repo_path, log_file, env)

def build_with_cmake(repo_path, log_file, env):
    """Generic CMake logic"""
    build_dir = os.path.join(repo_path, 'build_temp')
    if os.path.exists(build_dir):
        shutil.rmtree(build_dir)
    os.makedirs(build_dir)
    
    # Configure
    cmd = ['cmake', '..', '-DCMAKE_BUILD_TYPE=Debug']
    run_cmd(cmd, build_dir, log_file, env)
    
    # Build
    run_cmd(['cmake', '--build', '.'], build_dir, log_file, env)

def collect_coverage(repo_path, log_file):
    """Finds all .gcda files and runs gcov"""
    covered_files = set()
    with open(log_file, 'a') as f:
        f.write("\n>>> Collecting Coverage...\n")
        for root, dirs, files in os.walk(repo_path):
            for file in files:
                if file.endswith('.gcda'):
                    try:
                        source_guess = file.replace('.gcda', '.c')
                        subprocess.run(['gcov', source_guess], cwd=root, stdout=f, stderr=f)
                    except:
                        pass

        # Parse .gcov files
        for root, dirs, files in os.walk(repo_path):
            for file in files:
                if file.endswith('.gcov'):
                    try:
                        path = os.path.join(root, file)
                        with open(path, 'r', errors='replace') as gcov_f:
                            if re.search(r'^\s*[1-9]\d*:', gcov_f.read(), re.MULTILINE):
                                covered_files.add(file.replace('.gcov', ''))
                    except:
                        pass
    return covered_files

def main():
    if not os.path.exists(OUTPUT_CSV_PATH):
        pd.DataFrame(columns=['project', 'fix_commit', 'testfile', 'source_file']).to_csv(OUTPUT_CSV_PATH, index=False)

    df = pd.read_csv(DS_SNAPSHOT_PATH)
    
    # --- CHANGE: No more filtering, run everything ---
    # df_filtered = df[df['project'] == 'curl'].head(1) 
    df_filtered = df
    
    total = len(df_filtered)
    print(f"Starting pipeline for {total} commits...")

    # COMMON ENV VARS
    env = {
        'CFLAGS': '-fprofile-arcs -ftest-coverage -g -O0',
        'LDFLAGS': '-fprofile-arcs -ftest-coverage'
    }

    for index, row in df_filtered.iterrows():
        project = row['project']
        fix_commit = row['fix_commit']
        repo_path = os.path.join(DS_PROJECTS_DIR, project)
        log_file = os.path.join(RESULTS_DIR, 'logs', f'{project}_{fix_commit[:8]}.txt')

        print(f"[{index+1}/{total}] {project} @ {fix_commit[:8]}...", end='', flush=True)

        if not os.path.exists(repo_path):
            print(" [SKIP] Repo not found.")
            continue

        try:
            # 1. Clean
            subprocess.run(['git', 'checkout', '-f', fix_commit], cwd=repo_path, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
            subprocess.run(['git', 'clean', '-fdx'], cwd=repo_path, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)

            # 2. Build & Test
            try:
                # Try Autotools first
                build_with_autotools(repo_path, log_file, env, project)
                
                # Determine test target
                test_target = 'test-nonflaky' if project == 'curl' else 'test'
                run_cmd(['make', test_target], repo_path, log_file, env)
                
            except Exception as e:
                # Fallback to CMake
                with open(log_file, 'a') as f: f.write(f"\n[WARN] Autotools failed. Trying CMake... {e}\n")
                subprocess.run(['git', 'clean', '-fdx'], cwd=repo_path, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
                build_with_cmake(repo_path, log_file, env)
                # Note: CMake generic testing usually involves 'ctest', but we skip execution complexity for now
                # to avoid breaking flow. Coverage might be lower on fallback.

            # 3. Coverage
            covered = collect_coverage(repo_path, log_file)
            
            # 4. Git Diff & Match
            diff_cmd = ['git', 'diff-tree', '--no-commit-id', '--name-only', '-r', fix_commit]
            diff_out = subprocess.check_output(diff_cmd, cwd=repo_path).decode().splitlines()
            diff_files = [os.path.basename(f) for f in diff_out]

            matches = set(diff_files).intersection(covered)
            if matches:
                print(f" -> MATCH! ({len(matches)} files)")
                with open(OUTPUT_CSV_PATH, 'a') as csv:
                    for m in matches:
                        csv.write(f"{project},{fix_commit},aggregated_tests,{m}\n")
            else:
                print(f" -> Done. (Covered: {len(covered)}, Diff: {len(diff_files)}, Overlap: 0)")

        except Exception as e:
            print(f" -> FAILED. See log.")
            # We catch the error so the loop continues to the next project!

if __name__ == "__main__":
    main()