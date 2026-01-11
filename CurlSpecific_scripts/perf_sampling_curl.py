#!/usr/bin/env python3
"""
perf-based FUNCTION SAMPLING + RAPL ENERGY measurement on a C/C++ project (example: curl).

Key behavior (FIXED):
- Even if `make test` fails (non-zero exit), we STILL:
  - parse and save energy/time from `perf stat` (if present)
  - parse and save sampling data from `perf record` (if perf.data exists)
- We record `test_exit_code` and `test_status` in the CSV so you can filter later.

Paths (relative to this script's directory):
- Projects root:   ./ds_projects
- Results root:    ./vfec_results
- Logs:            ./vfec_results/logs
- perf data:       ./vfec_results/perfdata
"""

from __future__ import annotations

import csv
import re
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ----------------------------
# Config (edit if needed)
# ----------------------------
PROJECT_NAME = "curl"

# Best-effort build steps for curl (some snapshots may not have buildconf/configure)
BUILD_COMMANDS = [
    "./buildconf",
    "./configure --enable-debug --with-openssl",
    "make -j$(nproc)",
    "make -C tests servers",
]

# You said: rely on tests in projects, no custom workload scripts
TEST_COMMAND = "make test"

# perf sampling
PERF_FREQ_HZ = 99
PERF_CALLGRAPH = True

# If tests are very long, set timeout seconds. None = no timeout.
TEST_TIMEOUT_SEC: Optional[int] = None

# RAPL energy events to try (your system supports these)
CANDIDATE_ENERGY_EVENTS = [
    "power/energy-pkg/",
    "power/energy-cores/",
    "power/energy-ram/",
    "power/energy-psys/",
]


# ----------------------------
# Data models
# ----------------------------
@dataclass
class SampleRow:
    project: str
    percent: float
    symbol: str
    dso: str
    samples_rank: int


# ----------------------------
# Helpers
# ----------------------------
def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def have_tool(tool: str) -> bool:
    return subprocess.call(
        ["bash", "-lc", f"command -v {shlex.quote(tool)} >/dev/null 2>&1"]
    ) == 0


def run_cmd_stream(cmd: str, cwd: Path, log_fp, timeout: Optional[int] = None) -> int:
    """Run command and stream stdout/stderr to log file, return exit code."""
    log_fp.write(f"\n$ {cmd}\n")
    log_fp.flush()

    p = subprocess.Popen(
        ["bash", "-lc", cmd],
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert p.stdout is not None
        for line in p.stdout:
            log_fp.write(line)
        log_fp.flush()
        return p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        log_fp.write(f"\n[ERROR] Timeout expired for command: {cmd}\n")
        log_fp.flush()
        return 124


def run_cmd_capture(cmd: str, cwd: Path, timeout: Optional[int] = None) -> Tuple[int, str]:
    """Run command and capture combined output (stdout+stderr)."""
    try:
        out = subprocess.check_output(
            ["bash", "-lc", cmd],
            cwd=str(cwd),
            text=True,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        return 0, out
    except subprocess.CalledProcessError as e:
        return e.returncode, e.output
    except subprocess.TimeoutExpired:
        return 124, "[ERROR] Timeout expired\n"


def list_supported_perf_events() -> str:
    rc, out = run_cmd_capture("perf list", cwd=Path.cwd(), timeout=30)
    return out if rc == 0 else ""


def filter_supported_energy_events(candidate: List[str]) -> List[str]:
    """
    Best-effort: check `perf list` output for each event string.
    If perf list fails or shows nothing, return candidates and let perf stat decide.
    """
    text = list_supported_perf_events()
    if not text.strip():
        return candidate[:]

    supported = [ev for ev in candidate if ev in text]
    return supported if supported else candidate[:]


def parse_perf_stat_energy(stat_output: str) -> Dict[str, float]:
    """
    Parse perf stat output for:
      - energy events (Joules)
      - elapsed time (seconds)

    Typical lines:
      2,306.32 Joules power/energy-pkg/
      277.955355190 seconds time elapsed
    """
    results: Dict[str, float] = {}

    # Match: <number> Joules power/energy-<domain>/
    energy_re = re.compile(
        r"^\s*([\d\.,]+)\s+Joules\s+power/energy-([a-z]+)\/\s*(?:#.*)?\s*$",
        re.IGNORECASE,
    )
    time_re = re.compile(
        r"^\s*([\d\.,]+)\s+seconds\s+time\s+elapsed\s*$", re.IGNORECASE
    )

    for line in stat_output.splitlines():
        line = line.strip()

        m = energy_re.match(line)
        if m:
            raw_val, domain = m.group(1), m.group(2).lower()
            val = float(raw_val.replace(",", ""))
            results[f"energy-{domain}-j"] = val
            continue

        t = time_re.match(line)
        if t:
            raw_val = t.group(1)
            results["time-elapsed-s"] = float(raw_val.replace(",", ""))
            continue

    return results


def parse_perf_report_samples(report_text: str, project: str) -> List[SampleRow]:
    """
    Parse `perf report --stdio --no-children --sort symbol,dso` output.

    Common line shape:
      12.34%  <symbol>  <dso>
    """
    rows: List[SampleRow] = []
    line_re = re.compile(r"^\s*(\d+\.\d+)%\s+(.*?)\s+(\S+)\s*$")

    rank = 0
    for line in report_text.splitlines():
        m = line_re.match(line)
        if not m:
            continue
        percent_str, symbol, dso = m.group(1), m.group(2), m.group(3)

        # Skip obvious headers/noise
        if symbol.lower().startswith(("children", "overhead", "samples")):
            continue

        try:
            percent = float(percent_str)
        except ValueError:
            continue

        rank += 1
        rows.append(
            SampleRow(
                project=project,
                percent=percent,
                symbol=symbol.strip(),
                dso=dso.strip(),
                samples_rank=rank,
            )
        )

    return rows


# ----------------------------
# Main
# ----------------------------
def main() -> int:
    script_dir = Path(__file__).resolve().parent

    projects_root = script_dir / "ds_projects"
    results_root = script_dir / "vfec_results"
    logs_dir = results_root / "logs"
    perfdata_dir = results_root / "perfdata"

    ensure_dir(results_root)
    ensure_dir(logs_dir)
    ensure_dir(perfdata_dir)

    proj_dir = projects_root / PROJECT_NAME
    if not proj_dir.exists():
        print(f"[ERROR] Project directory not found: {proj_dir}")
        return 2

    if not have_tool("perf") or not have_tool("make"):
        print("[ERROR] Missing required tools in PATH: perf and/or make")
        return 3

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    log_path = logs_dir / f"{PROJECT_NAME}_{ts}.log"
    perf_data_path = perfdata_dir / f"{PROJECT_NAME}_{ts}.data"

    energy_csv = results_root / f"{PROJECT_NAME}_energy_{ts}.csv"
    samples_csv = results_root / f"{PROJECT_NAME}_samples_{ts}.csv"

    # Decide energy events (best-effort)
    energy_events = filter_supported_energy_events(CANDIDATE_ENERGY_EVENTS)
    energy_events_str = ",".join(energy_events)

    # We will keep these so we can write status consistently
    perf_stat_exit_code: Optional[int] = None
    perf_stat_status: str = "UNKNOWN"

    perf_record_exit_code: Optional[int] = None
    perf_record_status: str = "UNKNOWN"

    # Store parsed texts to use after log file closes
    stat_out = ""
    report_text = ""

    with open(log_path, "w", encoding="utf-8") as log_fp:
        log_fp.write(f"Project: {PROJECT_NAME}\n")
        log_fp.write(f"Project dir: {proj_dir}\n")
        log_fp.write(f"Timestamp: {ts}\n")
        log_fp.write(f"Test command: {TEST_COMMAND}\n")
        log_fp.write(f"Energy events requested: {energy_events_str}\n")
        log_fp.write(f"Sampling freq: {PERF_FREQ_HZ} Hz\n")
        log_fp.write(f"Callgraph: {PERF_CALLGRAPH}\n")

        # ---------------- BUILD ----------------
        log_fp.write("\n=== BUILD PHASE ===\n")
        for cmd in BUILD_COMMANDS:
            if cmd.strip() == "./buildconf" and not (proj_dir / "buildconf").exists():
                log_fp.write("\n[INFO] ./buildconf not found; skipping.\n")
                continue

            rc = run_cmd_stream(cmd, cwd=proj_dir, log_fp=log_fp)
            if rc != 0:
                log_fp.write(f"\n[WARN] Command failed (rc={rc}): {cmd}\n")
                log_fp.write("[WARN] Continuing; some repos may still proceed.\n")

        # ---------------- ENERGY (perf stat) ----------------
        log_fp.write("\n=== ENERGY PHASE (perf stat) ===\n")
        perf_stat_cmd = f"perf stat -e {shlex.quote(energy_events_str)} -- {TEST_COMMAND}"

        rc, stat_out = run_cmd_capture(perf_stat_cmd, cwd=proj_dir, timeout=TEST_TIMEOUT_SEC)
        perf_stat_exit_code = rc
        perf_stat_status = "OK" if rc == 0 else "FAILED"

        log_fp.write(f"\n$ {perf_stat_cmd}\n")
        log_fp.write(stat_out)

        energy_metrics = parse_perf_stat_energy(stat_out)
        parsed_any_energy = any(k.startswith("energy-") for k in energy_metrics.keys())
        parsed_any_time = "time-elapsed-s" in energy_metrics

        if rc != 0:
            log_fp.write(f"\n[WARN] Wrapped test command returned non-zero exit code (rc={rc}).\n")
            if not (parsed_any_energy or parsed_any_time):
                log_fp.write("[ERROR] perf stat produced no parsable energy/time; aborting.\n")
                return 4
            log_fp.write("[WARN] Energy/time were parsed and will be saved, but run is marked FAILED.\n")

        # Write energy CSV (single row)
        with open(energy_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                "project",
                "timestamp",
                "energy_events_requested",
                "test_exit_code",
                "test_status",
                "time_elapsed_s",
                "energy_pkg_j",
                "energy_cores_j",
                "energy_ram_j",
                "energy_psys_j",
                "log_file",
            ])
            w.writerow([
                PROJECT_NAME,
                ts,
                energy_events_str,
                perf_stat_exit_code,
                perf_stat_status,
                energy_metrics.get("time-elapsed-s", ""),
                energy_metrics.get("energy-pkg-j", ""),
                energy_metrics.get("energy-cores-j", ""),
                energy_metrics.get("energy-ram-j", ""),
                energy_metrics.get("energy-psys-j", ""),
                log_path.name,
            ])

        # ---------------- SAMPLING (perf record) ----------------
        log_fp.write("\n=== SAMPLING PHASE (perf record) ===\n")
        callgraph_flag = "-g" if PERF_CALLGRAPH else ""
        perf_record_cmd = (
            f"perf record -F {PERF_FREQ_HZ} {callgraph_flag} "
            f"-o {shlex.quote(str(perf_data_path))} -- {TEST_COMMAND}"
        )

        rc = run_cmd_stream(perf_record_cmd, cwd=proj_dir, log_fp=log_fp, timeout=TEST_TIMEOUT_SEC)
        perf_record_exit_code = rc
        perf_record_status = "OK" if rc == 0 else "FAILED"

        if rc != 0:
            log_fp.write(f"\n[WARN] perf record wrapped test command ended with rc={rc}.\n")

        # If perf.data doesn't exist, we cannot report samples
        if not perf_data_path.exists() or perf_data_path.stat().st_size == 0:
            log_fp.write("\n[ERROR] perf.data was not created (or is empty). Cannot generate samples report.\n")
            # Still exit gracefully since energy was saved
            print(f"[WARN] Energy CSV saved:   {energy_csv}")
            print(f"[WARN] Log saved:         {log_path}")
            print("[ERROR] No perf.data -> cannot produce samples CSV.")
            return 5

        # ---------------- PERF REPORT ----------------
        log_fp.write("\n=== PERF REPORT PHASE ===\n")
        perf_report_cmd = (
            f"perf report --stdio --no-children --percent-limit 0 "
            f"--sort symbol,dso -i {shlex.quote(str(perf_data_path))}"
        )

        rc, report_text = run_cmd_capture(perf_report_cmd, cwd=proj_dir, timeout=60)
        log_fp.write(f"\n$ {perf_report_cmd}\n")
        log_fp.write(report_text[:20000])
        if len(report_text) > 20000:
            log_fp.write("\n[INFO] perf report output truncated in log.\n")

        if rc != 0:
            log_fp.write(f"\n[ERROR] perf report failed (rc={rc}).\n")
            # Still exit gracefully since energy was saved and perf.data exists
            print(f"[WARN] Energy CSV saved:   {energy_csv}")
            print(f"[WARN] Log saved:         {log_path}")
            print("[ERROR] perf report failed -> cannot produce samples CSV.")
            return 6

    # Parse & write samples CSV (outside log context)
    sample_rows = parse_perf_report_samples(report_text, project=PROJECT_NAME)
    with open(samples_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "project",
            "timestamp",
            "test_exit_code",
            "test_status",
            "samples_rank",
            "percent_samples",
            "function",
            "binary_or_library(dso)",
            "perf_data_file",
            "log_file",
        ])
        for r in sample_rows:
            w.writerow([
                r.project,
                ts,
                perf_record_exit_code,
                perf_record_status,
                r.samples_rank,
                f"{r.percent:.4f}",
                r.symbol,
                r.dso,
                perf_data_path.name,
                log_path.name,
            ])

    print(f"[OK] Energy CSV saved:   {energy_csv}")
    print(f"[OK] Samples CSV saved:  {samples_csv}")
    print(f"[OK] Log saved:          {log_path}")
    print(f"[OK] perf.data saved:    {perf_data_path}")

    print("\nNotes (simple):")
    print("- If tests fail, CSVs still get created (marked FAILED). You can filter later.")
    print("- Energy is for the whole test run. Samples-per-function come from perf profiling.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
