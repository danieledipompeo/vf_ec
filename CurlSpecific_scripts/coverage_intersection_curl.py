import os
import subprocess
import re
import csv

# --- Path Configuration ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_PATH = os.path.join(BASE_DIR, "curl")
RESULTS_BASE = os.path.join(BASE_DIR, "curl_results")

# Master Output Files
MASTER_CSV_PATH = os.path.join(RESULTS_BASE, "curl_cov_intersection.csv")
CLEAN_CSV_PATH = os.path.join(RESULTS_BASE, "clean_curl_cov_intersection.csv")

# List of commit pairs (vuln, fix)
COMMIT_PAIRS = [
    ("da0db499fd1fed3ab061d8c03d25c06164c9f429", "192c4f788d48f82c03e9cef40013f34370e90737"),
    ("e97679a360dda4ea6188b09a145f73a2a84acedd","d530e92f59ae9bb2d47066c3c460b25d2ffeb211"),
    ("9d8dad1a9d79d60e021f0c4e0f66bf5d51fb3c4e","81d135d67155c5295b1033679c606165d4e28f3f"),
    ("81d135d67155c5295b1033679c606165d4e28f3f","f3a24d7916b9173c69a3e0ee790102993833d6c5"),
    ("0b4ccc97f26316476d4c2abbd429952bf61b6375","ba1dbd78e5f1ed67c1b8d37ac89d90e5e330b628"),
    ("82f3ba3806a34fe94dcf9e5c9b88deda6679ca1b","facb0e4662415b5f28163e853dc6742ac5fafb3d"),
    ("769647e714b8da41bdb72720bf02dce56033e02e","13c9a9ded3ae744a1e11cbc14e9146d9fa427040"),
    ("9b5e12a5491d2e6b68e0c88ca56f3a9ef9fba400","0b664ba968437715819bfe4c7ada5679d16ebbc3"),
    ("184ffc0bdfcab17eb96d1dd64f9c0cecc139e3e9","7214288898f5625a6cc196e22a74232eada7861c"),
    ("c79b2ca03d94d996c23cee13859735cc278838c1","9b5e12a5491d2e6b68e0c88ca56f3a9ef9fba400"),
    ("9cb1059f92286a6eb5d28c477fdd3f26aed1d554","75dc096e01ef1e21b6c57690d99371dedb2c0b80"),
    ("19ebc282172ff204648f350c6e716197d5b4d221","57d299a499155d4b327e341c6024e293b0418243"),
    ("bbb71507b7bab52002f9b1e0880bed6a32834511","39ce47f219b09c380b81f89fe54ac586c8db6bde"),
    # Add more pairs here: ("vuln_hash", "fix_hash"),
]

def get_file_content_at_commit(repo_path, commit_hash, filename):
    """Retrieves file content at a specific commit."""
    cmd = ["git", "-C", repo_path, "show", f"{commit_hash}:{filename}"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return {i + 1: line for i, line in enumerate(result.stdout.splitlines())} if result.returncode == 0 else {}

def get_diff_mapping(repo_path, v_hash, f_hash):
    """Parses git diff -U0 to map lines between vuln and fix commits."""
    cmd = ["git", "-C", repo_path, "diff", "-U0", f"{v_hash}..{f_hash}"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    mapping = []
    current_file = None
    hunk_regex = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')

    for line in result.stdout.splitlines():
        if line.startswith('--- a/'):
            current_file = line[6:].strip()
        elif line.startswith('@@') and current_file:
            match = hunk_regex.match(line)
            if match:
                v_start = int(match.group(1))
                v_len = int(match.group(2)) if match.group(2) else 1
                f_start = int(match.group(3))
                f_len = int(match.group(4)) if match.group(4) else 1
                
                max_range = max(v_len, f_len)
                for i in range(max_range):
                    v_curr = v_start + i if i < v_len else None
                    f_curr = f_start + i if i < f_len else None
                    mapping.append({"file": current_file, "v_lnum": v_curr, "f_lnum": f_curr})
    return mapping

def parse_lcov_info(file_path):
    """Parses LCOV .info file to extract executable lines and hits."""
    coverage = {}
    if not os.path.exists(file_path):
        return coverage
    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
        current_sf = None
        for line in f:
            line = line.strip()
            if line.startswith("SF:"):
                full_path = line[3:]
                current_sf = os.path.relpath(full_path, PROJECT_PATH) if PROJECT_PATH in full_path else full_path
                coverage[current_sf] = {}
            elif line.startswith("DA:") and current_sf:
                parts = line[3:].split(',')
                coverage[current_sf][int(parts[0])] = int(parts[1])
    return coverage

def main():
    all_rows = []
    summary_stats = []

    for v_hash, f_hash in COMMIT_PAIRS:
        v_short, f_short = v_hash[:8], f_hash[:8]
        pair_str = f"{v_short}...{f_short}"
        target_dir = os.path.join(RESULTS_BASE, f"cov_{pair_str}")
        v_info = os.path.join(target_dir, "vuln-coverage.info")
        f_info = os.path.join(target_dir, "fixed-coverage.info")

        print(f"Processing Pair: {pair_str}")
        diff_map = get_diff_mapping(PROJECT_PATH, v_hash, f_hash)
        v_cov = parse_lcov_info(v_info)
        f_cov = parse_lcov_info(f_info)

        files_to_read = list(set(m['file'] for m in diff_map))
        v_contents = {f: get_file_content_at_commit(PROJECT_PATH, v_hash, f) for f in files_to_read}
        f_contents = {f: get_file_content_at_commit(PROJECT_PATH, f_hash, f) for f in files_to_read}

        v_total, v_hit, f_total, f_hit = 0, 0, 0, 0

        for item in diff_map:
            fname, v_lnum, f_lnum = item['file'], item['v_lnum'], item['f_lnum']
            
            # Executable check: if it's in DA records, it's executable
            is_v_executable = v_lnum in v_cov.get(fname, {}) if v_lnum else False
            is_f_executable = f_lnum in f_cov.get(fname, {}) if f_lnum else False
            
            vh = v_cov.get(fname, {}).get(v_lnum, 0) if v_lnum else "N/A"
            fh = f_cov.get(fname, {}).get(f_lnum, 0) if f_lnum else "N/A"

            if v_lnum:
                v_total += 1
                if isinstance(vh, int) and vh > 0: v_hit += 1
            if f_lnum:
                f_total += 1
                if isinstance(fh, int) and fh > 0: f_hit += 1

            all_rows.append({
                "commit_pair": pair_str,
                "file": fname,
                "v_line_number": v_lnum if v_lnum else "N/A",
                "vuln_snippet": v_contents.get(fname, {}).get(v_lnum, "[ADDED]").strip() if v_lnum else "[ADDED]",
                "vuln_hits": vh,
                "f_line_number": f_lnum if f_lnum else "N/A",
                "fix_snippet": f_contents.get(fname, {}).get(f_lnum, "[DELETED]").strip() if f_lnum else "[DELETED]",
                "fix_hits": fh,
                "is_executable": is_v_executable or is_f_executable
            })

        summary_stats.append({
            "Pair": pair_str, "V_Lines": v_total, "V_Hits": v_hit,
            "V_Cov%": f"{(v_hit/v_total)*100:.2f}%" if v_total > 0 else "0%",
            "F_Lines": f_total, "F_Hits": f_hit,
            "F_Cov%": f"{(f_hit/f_total)*100:.2f}%" if f_total > 0 else "0%"
        })

    # 1. Export MASTER CSV (Everything)
    # 2. Export CLEAN CSV (Only Executable Rows)
    headers = ["commit_pair", "file", "v_line_number", "vuln_snippet", "vuln_hits", "f_line_number", "fix_snippet", "fix_hits"]
    
    for path, data_filter in [(MASTER_CSV_PATH, lambda x: True), (CLEAN_CSV_PATH, lambda x: x['is_executable'])]:
        rows_to_write = [ {k: v for k, v in r.items() if k != 'is_executable'} for r in all_rows if data_filter(r) ]
        
        with open(path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            writer.writeheader()
            writer.writerows(rows_to_write)
            
            # Summary Section
            writer.writerow({h: "" for h in headers})
            writer.writerow({"commit_pair": "--- SUMMARY SECTION ---"})
            sum_headers = ["Pair", "V_Lines", "V_Hits", "V_Cov%", "F_Lines", "F_Hits", "F_Cov%"]
            writer.writerow({headers[i]: sum_headers[i] for i in range(len(sum_headers))})
            for stat in summary_stats:
                writer.writerow({headers[i]: stat[sum_headers[i]] for i in range(len(sum_headers))})

    print(f"Done! Files generated:\n1. {MASTER_CSV_PATH}\n2. {CLEAN_CSV_PATH}")

if __name__ == "__main__":
    main()