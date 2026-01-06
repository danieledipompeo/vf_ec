#!/usr/bin/env python3

import os
import subprocess
import csv
import re
from pathlib import Path

# =============================
# CONFIG
# =============================

CURL_REPO = Path("curl").resolve()
TESTS_DIR = CURL_REPO / "tests"
OUTPUT_CSV = Path("test_output.csv").resolve()
ALL_TESTS_FILE = Path("all_tests.txt").resolve()

# =============================
# HELPERS
# =============================

def run(cmd, cwd=None, allow_fail=False):
    print(f">>> {cmd}")
    p = subprocess.run(
        cmd,
        cwd=cwd,
        shell=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT
    )
    if p.returncode != 0 and not allow_fail:
        raise RuntimeError(p.stdout)
    return p.stdout

def build_curl():
    print("\n=== BUILDING CURL ===")
    run("./buildconf", cwd=CURL_REPO)
    run("./configure --enable-debug --with-openssl", cwd=CURL_REPO)
    run("make -j$(nproc)", cwd=CURL_REPO)

    curl_bin = CURL_REPO / "src" / "curl"
    if not curl_bin.exists():
        raise RuntimeError("src/curl was not built")

def extract_all_tests():
    print("\n=== DISCOVERING TESTS ===")
    out = run("./runtests.pl -l", cwd=TESTS_DIR)
    tests = []
    for line in out.splitlines():
        m = re.match(r"^(\d+)\s", line)
        if m:
            tests.append(m.group(1))
    ALL_TESTS_FILE.write_text("\n".join(tests))
    return tests

def run_single_test(test_id):
    cmd = f"./runtests.pl {test_id}"
    out = run(cmd, cwd=TESTS_DIR, allow_fail=True)

    status = "UNKNOWN"
    if "OK" in out:
        status = "PASS"
    elif "FAILED" in out or "TESTFAIL" in out:
        status = "FAIL"
    elif "SKIPPED" in out:
        status = "SKIPPED"
    elif "Killed" in out:
        status = "KILLED"

    return status, out.strip()

# =============================
# MAIN
# =============================

def main():
    build_curl()

    tests = extract_all_tests()
    print(f"\nRunning {len(tests)} tests...\n")

    with open(OUTPUT_CSV, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["test_id", "status", "output"])

        for i, tid in enumerate(tests, 1):
            print(f"[{i}/{len(tests)}] Running test {tid}")
            status, output = run_single_test(tid)
            writer.writerow([tid, status, output])

    print("\n=== TEST SUMMARY ===")
    with open(OUTPUT_CSV) as f:
        rows = list(csv.DictReader(f))
        total = len(rows)
        counts = {}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1

        print(f"Total tests: {total}")
        for k, v in counts.items():
            print(f"{k}: {v}")

    print(f"\nResults saved to: {OUTPUT_CSV}")

if __name__ == "__main__":
    main()
