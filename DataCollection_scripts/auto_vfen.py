import os
import sys
import logging
import pandas as pd
import git  # GitPython
from git.exc import GitCommandError, BadName, BadObject # Import specific Git exceptions
import re
import requests
import time
from urllib.parse import urlparse

# --- Configuration (Relative Paths) ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECTS_LIST_PATH = os.path.join(BASE_DIR, 'selected_projects.csv')
PRIMEVUL_DIR = os.path.join(BASE_DIR, 'primevul_dataset')
RESULTS_DIR = os.path.join(BASE_DIR, 'vfec_results')
PROJECTS_SOURCE_DIR = os.path.join(BASE_DIR, 'ds_projects')

# Log file location
if not os.path.exists(RESULTS_DIR):
    os.makedirs(RESULTS_DIR)
LOG_FILE = os.path.join(RESULTS_DIR, 'run_log.txt')
OUTPUT_CSV = os.path.join(RESULTS_DIR, 'ds_snapshot.csv')

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

def load_target_projects():
    """Reads the selected projects from CSV (no header, col 1)."""
    if not os.path.exists(PROJECTS_LIST_PATH):
        logger.error(f"Selected projects file not found: {PROJECTS_LIST_PATH}")
        sys.exit(1)
    
    try:
        # Read CSV, assume no header, read first column only
        df = pd.read_csv(PROJECTS_LIST_PATH, header=None, usecols=[0])
        projects = df.iloc[:, 0].astype(str).str.strip().str.lower().dropna().unique().tolist()
        
        logger.info(f"Loaded {len(projects)} target projects from {PROJECTS_LIST_PATH}")
        return set(projects)
    except Exception as e:
        logger.error(f"Error loading selected projects: {e}")
        sys.exit(1)

def load_primevul_dataset():
    """Loads and combines all Primevul Excel files."""
    files = ['primevul_objects.xlsx', 'primevul_train.xlsx', 'primevul_valid.xlsx', 'primevul_test.xlsx']
    dfs = []
    
    for f in files:
        path = os.path.join(PRIMEVUL_DIR, f)
        if os.path.exists(path):
            try:
                df = pd.read_excel(path)
                df.columns = df.columns.astype(str).str.strip().str.lower()
                dfs.append(df)
                logger.info(f"Loaded {f}: {len(df)} rows.")
            except Exception as e:
                logger.error(f"Failed to read {f}: {e}")
        else:
            logger.warning(f"Dataset file not found: {path}")
    
    if not dfs:
        logger.error("No dataset files loaded.")
        sys.exit(1)
        
    combined_df = pd.concat(dfs, ignore_index=True)
    return combined_df

def get_cwe_online(cve_id):
    """Attempts to find CWE for a given CVE using the CIRCL API."""
    try:
        url = f"https://cve.circl.lu/api/cve/{cve_id}"
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            data = response.json()
            if "cwe" in data and data["cwe"] and data["cwe"] != "Unknown":
                return data["cwe"]
    except Exception as e:
        logger.warning(f"Could not fetch CWE online for {cve_id}: {e}")
    return None

def analyze_commit(repo, commit_hash):
    """
    Returns (files_set, lines_added, lines_deleted, parent_hash, commit_obj)
    Robustly handles missing commits and shallow clones.
    """
    try:
        # Try to find the commit object
        commit = repo.commit(commit_hash)
    except (ValueError, BadName, BadObject) as e:
        logger.warning(f"Commit {commit_hash} not found (Check if repo is shallow/outdated).")
        return None, 0, 0, None, None

    if not commit.parents:
        logger.warning(f"Commit {commit_hash} has no parents (Initial commit?).")
        return None, 0, 0, None, None

    parent = commit.parents[0]
    
    # Safe Stats Retrieval
    try:
        # commit.stats.files triggers a 'git diff' against the parent.
        # This will FAIL if the parent object is missing (common in shallow clones).
        stats = commit.stats.files 
        total_added = commit.stats.total['insertions']
        total_deleted = commit.stats.total['deletions']
        files_modified = set(stats.keys())
        
        return files_modified, total_added, total_deleted, parent.hexsha, commit

    except GitCommandError as e:
        logger.warning(f"Git diff failed for {commit_hash}. Parent {parent.hexsha[:8]} likely missing locally.")
        return None, 0, 0, None, None
    except Exception as e:
        logger.warning(f"Unexpected error analyzing {commit_hash}: {e}")
        return None, 0, 0, None, None

def process_pipeline():
    target_projects = load_target_projects()
    raw_data = load_primevul_dataset()
    
    if 'commit_id' not in raw_data.columns:
        logger.error(f"CRITICAL: 'commit_id' column not found. Available: {list(raw_data.columns)}")
        sys.exit(1)
    
    if 'project' not in raw_data.columns:
        logger.error("CRITICAL: 'project' column not found.")
        sys.exit(1)

    # Filter Dataset
    raw_data['project_normalized'] = raw_data['project'].astype(str).str.strip().str.lower()
    df_filtered = raw_data[raw_data['project_normalized'].isin(target_projects)].copy()
    logger.info(f"Filtered down to {len(df_filtered)} rows matching target projects.")

    # Deduplication
    df_unique = df_filtered.drop_duplicates(subset=['commit_id', 'commit_url'])
    logger.info(f"Unique entries to process: {len(df_unique)}")
    
    results = []

    for index, row in df_unique.iterrows():
        project = row['project']
        fix_commit_hash = row['commit_id']
        commit_url = row['commit_url']
        cve = row.get('cve', None)
        cwe = row.get('cwe', None)
        
        if pd.isna(cve): cve = None
        if pd.isna(cwe): cwe = None
        
        logger.info(f"Processing {project} | Fix: {str(fix_commit_hash)[:8]}...")

        # Locate Repository
        repo_path = os.path.join(PROJECTS_SOURCE_DIR, project)
        if not os.path.exists(repo_path):
            logger.warning(f"Repository directory not found for {project}. Skipping.")
            continue
            
        try:
            repo = git.Repo(repo_path)
        except git.InvalidGitRepositoryError:
            logger.warning(f"Invalid git repository at {repo_path}. Skipping.")
            continue

        # Analyze Fix Commit
        fix_files, fix_added, fix_deleted, parent_hash, fix_obj = analyze_commit(repo, fix_commit_hash)
        if fix_files is None:
            # Skip if analysis failed (missing commit/bad object)
            continue
        
        vuln_commit_hash = parent_hash
        
        # Analyze Vuln Commit
        vuln_files, vuln_added, vuln_deleted, _, _ = analyze_commit(repo, vuln_commit_hash)
        if vuln_files is None:
            # Log specific warning for vuln commit failure
            logger.warning(f"Could not analyze vuln commit {vuln_commit_hash}. Skipping.")
            continue

        # Intersection of files
        common_files = list(fix_files.intersection(vuln_files))
        
        # If no common files found, we can still record the commit, but file list is empty
        files_modified_str = ";".join(common_files)

        # CVE / CWE Enrichment
        if not cve and fix_obj:
            msg = fix_obj.message
            cve_match = re.search(r'(CVE-\d{4}-\d+)', msg, re.IGNORECASE)
            if cve_match:
                cve = cve_match.group(1).upper()
                logger.info(f"Found CVE in commit message: {cve}")

        if not cwe and cve:
            logger.info(f"Attempting online CWE lookup for {cve}...")
            cwe = get_cwe_online(cve)

        record = {
            'project': project,
            'commit_url': commit_url,
            'files_modified': files_modified_str,
            'vuln_commit': vuln_commit_hash,
            'vuln_lines_added': vuln_added,
            'vuln_lines_deleted': vuln_deleted,
            'fix_commit': fix_commit_hash,
            'fix_lines_added': fix_added,
            'fix_lines_deleted': fix_deleted,
            'cve': cve,
            'cwe': cwe
        }
        results.append(record)

    # Write Results
    if results:
        result_df = pd.DataFrame(results)
        cols = [
            'project', 'commit_url', 'files_modified', 
            'vuln_commit', 'vuln_lines_added', 'vuln_lines_deleted', 
            'fix_commit', 'fix_lines_added', 'fix_lines_deleted', 
            'cve', 'cwe'
        ]
        result_df = result_df[cols]
        result_df.to_csv(OUTPUT_CSV, index=False)
        logger.info(f"Successfully wrote {len(result_df)} records to {OUTPUT_CSV}")
    else:
        logger.warning("No records processed successfully.")

if __name__ == "__main__":
    process_pipeline()