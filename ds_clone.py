import os
import sys
import pandas as pd
import subprocess
import logging

# --- Configuration ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECTS_LIST_PATH = os.path.join(BASE_DIR, 'selected_projects.csv')
PROJECTS_SOURCE_DIR = os.path.join(BASE_DIR, 'ds_projects')
LOG_FILE = os.path.join(BASE_DIR, 'clone_log.txt')

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, mode='w'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger()

def setup_directory():
    if not os.path.exists(PROJECTS_SOURCE_DIR):
        os.makedirs(PROJECTS_SOURCE_DIR)
        logger.info(f"Created source directory: {PROJECTS_SOURCE_DIR}")

def load_projects():
    """Reads project name (col 0) and URL (col 1) from CSV."""
    if not os.path.exists(PROJECTS_LIST_PATH):
        logger.error(f"File not found: {PROJECTS_LIST_PATH}")
        sys.exit(1)
        
    try:
        # Read CSV without header. 
        # Col 0 = Name, Col 1 = URL
        df = pd.read_csv(PROJECTS_LIST_PATH, header=None, usecols=[0, 1])
        df.columns = ['name', 'url']
        
        # Clean data
        df['name'] = df['name'].astype(str).str.strip()
        df['url'] = df['url'].astype(str).str.strip()
        
        # --- FIX: Ensure URL ends with .git ---
        # This makes it robust even if your CSV misses it
        df['url'] = df['url'].apply(lambda x: x if x.endswith('.git') else x + '.git')
        
        return df
    except Exception as e:
        logger.error(f"Error reading CSV: {e}")
        sys.exit(1)

def run_git_cmd(cmd, cwd, description):
    """Runs a git command via subprocess."""
    try:
        subprocess.run(
            cmd, 
            cwd=cwd, 
            check=True, 
            stdout=subprocess.DEVNULL, 
            stderr=subprocess.PIPE
        )
        return True
    except subprocess.CalledProcessError as e:
        # Capture stderr for specific checks
        error_msg = e.stderr.decode('utf-8', errors='ignore').strip()
        return error_msg

def process_repos():
    setup_directory()
    df = load_projects()
    logger.info(f"Loaded {len(df)} projects to process.")
    
    success_count = 0
    fail_count = 0
    
    for index, row in df.iterrows():
        name = row['name']
        url = row['url']
        target_path = os.path.join(PROJECTS_SOURCE_DIR, name)
        
        logger.info(f"[{index+1}/{len(df)}] Processing: {name}")

        # 1. CASE: Repo does not exist -> CLONE
        if not os.path.exists(target_path):
            logger.info(f"  -> Cloning fresh from {url}...")
            result = run_git_cmd(['git', 'clone', url, name], PROJECTS_SOURCE_DIR, "Cloning")
            
            if result is True:
                logger.info("  -> Clone successful.")
                success_count += 1
            else:
                logger.error(f"  -> Clone FAILED: {result}")
                fail_count += 1
                
        # 2. CASE: Repo exists -> REPAIR/UPDATE
        else:
            logger.info(f"  -> Directory exists. Attempting update/unshallow...")
            
            # Try to unshallow (fix missing history)
            # This fails if the repo is already complete (which is good), so we check the error.
            res_unshallow = run_git_cmd(['git', 'fetch', '--unshallow'], target_path, "Unshallowing")
            
            if res_unshallow is True:
                logger.info("  -> Successfully unshallowed (Full history restored).")
                success_count += 1
            else:
                # If unshallow failed, it might mean it's ALREADY full. 
                # Check for "shallow" keyword in error, or just try a normal fetch.
                if "complete" in str(res_unshallow) or "not shallow" in str(res_unshallow):
                    # It was already full, just update it
                    logger.info("  -> Repo is already full depth. Fetching updates...")
                    run_git_cmd(['git', 'fetch', '--all'], target_path, "Fetching")
                    success_count += 1
                else:
                    # Genuine error (e.g., remote URL changed, permissions, corruption)
                    logger.warning(f"  -> Could not update/unshallow: {res_unshallow}")
                    logger.warning(f"  -> Recommendation: Delete {target_path} and re-run to clone fresh.")
                    fail_count += 1

    logger.info("---")
    logger.info(f"Batch completed. Success: {success_count}, Failed/Skipped: {fail_count}")

if __name__ == "__main__":
    process_repos()