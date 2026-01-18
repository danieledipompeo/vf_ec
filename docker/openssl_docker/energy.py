
import os
import re
import shlex
import subprocess
import time
import logging
from common import ProgressBar
from common import OUTPUT_DIR, REPO_NAME
from common import PROJECT_DIR
from common import ITERATIONS, DEFAULT_TIMEOUT_MS

# ==========================================
# PHASE 2: ENERGY
# ==========================================

COOL_DOWN_TO_SEC = 1.0

def _wrap_until_timeout(test_cmd: str, timeout_ms: int) -> str:
    """
    Returns a bash command that runs `test_cmd` repeatedly until timeout expires.
    - uses monotonic-ish wall clock via SECONDS (bash built-in, second resolution)
    - avoids killing a running iteration mid-command (it checks deadline BETWEEN iterations)
    """
    # Use bash -lc so we can rely on bash features and keep quoting predictable
    # SECONDS is integer seconds since shell start; good enough for energy runs (>= 2-5s).
    timeout_s = max(1, int((timeout_ms + 999) / 1000))  # ceil to seconds

    # Important:
    # - `set -e` makes failures stop the loop and propagate non-zero to perf (you want this)
    # - you can change to `|| true` if you prefer "keep looping even if one iteration fails"
    wrapped = (
        "bash -lc "
        + shlex.quote(
            f"""
            set -e
            end=$((SECONDS + {timeout_s}))
            while [ $SECONDS -lt $end ]; do
              {test_cmd}
            done
            """
        )
    )
    return wrapped

def measure_test(pkg_event, test, commit):
    """
    Measures energy consumption of a test using `perf stat -e <events>`.
    Runs the test command repeatedly until timeout expires, for a number of iterations.
    Saves perf output CSV files in OUTPUT_DIR/REPO_NAME/perf/
    Returns None on success, or an error message string on failure.
    """
    
    # Accept a list from detect_rapl() or a single event string.
    if isinstance(pkg_event, (list, tuple, set)):
        events = [str(e).strip() for e in pkg_event if str(e).strip()]
    elif pkg_event:
        events = [str(pkg_event).strip()]
    else:
        events = []

    if not events:
        events = ["power/energy-pkg/"]

    perf_events = ",".join(events + ["cycles", "instructions"])
    
    pb = ProgressBar(ITERATIONS)
    perf_dir = os.path.join(OUTPUT_DIR, REPO_NAME, "perf")
    os.makedirs(perf_dir, exist_ok=True)

    timeout_ms = test.get("timeout_ms", DEFAULT_TIMEOUT_MS)  # e.g. 5s default, tune per test
    print(f"\nMeasuring energy for test '{test.get('name')}': "
          f"{ITERATIONS} iterations × {timeout_ms}ms timeout each")

    for iteration in range(ITERATIONS):
        pb.set(iteration)

        perf_out = os.path.join(perf_dir, f"{commit}_{test.get('name')}__{iteration}.csv")

        wrapped_cmd = _wrap_until_timeout(test["cmd"], timeout_ms)

        # Build perf as argv list (safer than huge shell string)
        perf_argv = [
            "perf", "stat",
            "-a",
            "-e", f"{perf_events}",
            "-x,", "--output", perf_out,
            "--",
        ]
        # wrapped_cmd already includes "bash -lc '<script>'" so we run via sh -c? Not needed.
        # But since wrapped_cmd is a single string, we can still do: ["sh","-c", wrapped_cmd]
        perf_argv += ["sh", "-c", wrapped_cmd]

        res = subprocess.run(
            perf_argv, 
            cwd=PROJECT_DIR, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE, 
            text=True)
        
        if res.returncode != 0: 
            print(f"\n[ERROR] Test {test.get('name')} failed during energy measurement. {res.stderr.strip()}")
            logging.error(f"[STD ERR] {test.get('name')}: {res.stderr}")
            if os.path.exists(perf_out):
                os.remove(perf_out)
            logging.error(f"Perf Measurement removed due to error.")
            return None
        
        logging.info(f"[COOL DOWN] {COOL_DOWN_TO_SEC} seconds...")
        time.sleep(COOL_DOWN_TO_SEC)
        
    return None