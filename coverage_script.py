# Takes vulnerable and fixed commit hashes.
# Creates the directory:
# curl_results/cov <vulnShort>...<fixShort>/
# Builds each commit with coverage flags.
# Runs make test.
# Generates LCOV coverage and perf energy reports.
# Stores all results under that directory.

# import os
# import subprocess
# import time
# import csv
# import json
# import statistics
# import math

# # matplotlib optional
# try:
#     import matplotlib.pyplot as plt
#     HAS_MPL = True
# except ImportError:
#     HAS_MPL = False

# # =============================
# # CONFIGURATION
# # =============================

# VULN_COMMIT = "e97679a360dda4ea6188b09a145f73a2a84acedd"
# FIX_COMMIT  = "d530e92f59ae9bb2d47066c3c460b25d2ffeb211"

# CURL_REPO = "curl"
# RESULTS_ROOT = "curl_results"
# WORKLOAD_SCRIPT = os.path.abspath("perf_workload.sh")

# RAPL_PKG = "/sys/class/powercap/intel-rapl:0/energy_uj"

# NUM_RUNS = 50   # workload repetitions


# # =============================
# # BASIC HELPERS
# # =============================

# def run(cmd, cwd=None, log=None, allow_fail=False):
#     print(f"\n>>> {cmd}")
#     if log:
#         log.write(f"\n>>> {cmd}\n")
#     result = subprocess.run(cmd, cwd=cwd, shell=True)
#     if not allow_fail and result.returncode != 0:
#         raise RuntimeError(f"Command failed: {cmd}")
#     return result.returncode


# def read_rapl_energy():
#     try:
#         v = subprocess.check_output(["sudo", "-n", "cat", RAPL_PKG])
#         return int(v.decode().strip())
#     except Exception:
#         return None


# def prepare_output_dir():
#     vuln_short = VULN_COMMIT[:8]
#     fix_short = FIX_COMMIT[:8]
#     dirname = f"cov_{vuln_short}...{fix_short}"

#     full = os.path.abspath(os.path.join(RESULTS_ROOT, dirname))

#     if os.path.exists(full):
#         subprocess.run(f"rm -rf '{full}'", shell=True)

#     os.makedirs(full, exist_ok=True)
#     return full


# # =============================
# # WORKLOAD PERFORMANCE + ENERGY
# # =============================

# def measure_workload_once(curl_bin, label, log_file):
#     """
#     Runs one workload with:
#       - RAPL before/after
#       - perf cycles,instructions
#     """
#     time.sleep(0.2)

#     e_start = read_rapl_energy()
#     t0 = time.time()

#     cmd = [
#         "perf", "stat",
#         "-x,", "-e", "cycles,instructions",
#         WORKLOAD_SCRIPT, curl_bin
#     ]

#     proc = subprocess.run(cmd, text=True, capture_output=True)

#     t1 = time.time()
#     e_end = read_rapl_energy()

#     if proc.returncode != 0:
#         raise RuntimeError(f"perf stat failed for {label}: {proc.stderr}")

#     # -------------------------------------------------------
#     # Robust parsing for hybrid CPU perf output
#     # Example actual output from your machine:
#     #   962781,,cpu_atom/cycles/,264232,100.00,,
#     #   <not counted>,,cpu_core/cycles/,0,0.00,,
#     #   730033,,cpu_atom/instructions/,264232,100.00,0.76,insn per cycle
#     #   <not counted>,,cpu_core/instructions/,0,0.00,,
#     # -------------------------------------------------------

#     cycles_total = 0
#     instr_total = 0
#     found_cycles = False
#     found_instr = False

#     for line in proc.stderr.splitlines():
#         parts = [p.strip() for p in line.split(",")]
#         if len(parts) < 3:
#             continue

#         value_str = parts[0]
#         event = parts[2]  # e.g., "cpu_atom/cycles/"

#         if value_str.startswith("<not counted>"):
#             continue

#         # Convert numeric value (remove commas and dots)
#         try:
#             v = int(value_str.replace(",", "").replace(".", ""))
#         except ValueError:
#             continue

#         # Normalize event name: remove trailing "/" and split
#         e = event.strip()
#         if e.endswith("/"):
#             e = e[:-1]
#         base_event = e.split("/")[-1]  # cycles or instructions

#         if "cycles" in base_event:
#             cycles_total += v
#             found_cycles = True

#         if "instructions" in base_event:
#             instr_total += v
#             found_instr = True

#     if not found_cycles or not found_instr:
#         raise RuntimeError(f"Failed to parse perf counters for {label}")

#     cycles = cycles_total
#     instructions = instr_total

#     # Energy computation
#     energy_j = None
#     if e_start is not None and e_end is not None:
#         delta = e_end - e_start
#         if delta < 0:
#             delta += 2**32
#         energy_j = delta / 1_000_000.0

#     time_sec = t1 - t0
#     ipc = instructions / cycles if cycles > 0 else float("nan")

#     if log_file:
#         log_file.write(
#             f"Run {label}: time={time_sec:.4f}s, energy={energy_j:.6f}J, "
#             f"cycles={cycles}, instr={instructions}, IPC={ipc:.4f}\n"
#         )

#     return {
#         "time_sec": time_sec,
#         "energy_j": energy_j,
#         "cycles": cycles,
#         "instructions": instructions,
#         "ipc": ipc,
#     }


# def measure_workload_multiple(curl_bin, label, outdir, log_file):
#     runs = []
#     for i in range(NUM_RUNS):
#         if log_file:
#             log_file.write(f"\n--- Workload run {i+1}/{NUM_RUNS} ({label}) ---\n")
#         m = measure_workload_once(curl_bin, label, log_file)
#         m["run_index"] = i + 1
#         runs.append(m)
#     return runs


# # =============================
# # COVERAGE + BUILD
# # =============================

# def build_and_test(commit, label, outdir, log_file, summary):
#     repo = os.path.abspath(CURL_REPO)

#     log_file.write(f"\n=== Processing {label} ({commit[:8]}) ===\n")
#     build_start = time.time()

#     run("git reset --hard", cwd=repo, log=log_file)
#     run("git clean -fdx", cwd=repo, log=log_file)
#     run(f"git checkout {commit}", cwd=repo, log=log_file)
#     run("git clean -fdx", cwd=repo, log=log_file)

#     run("./buildconf", cwd=repo, log=log_file)
#     run('CFLAGS="--coverage -O0 -g" LDFLAGS="--coverage" ./configure --enable-debug',
#         cwd=repo, log=log_file)
#     run("make -j$(nproc)", cwd=repo, log=log_file)

#     run("make test", cwd=repo, log=log_file, allow_fail=True)

#     # ---- Coverage ----
#     cov_info  = os.path.abspath(os.path.join(outdir, f"{label}-coverage.info"))
#     cov_clean = os.path.abspath(os.path.join(outdir, f"{label}-coverage.cleaned.info"))
#     html_dir  = os.path.abspath(os.path.join(outdir, f"{label}-coverage-html"))

#     run(f"lcov --capture --directory . --output-file \"{cov_info}\"",
#         cwd=repo, log=log_file)

#     run(
#         f"lcov --remove \"{cov_info}\" '/usr/*' '*/tests/*' "
#         f"--ignore-errors unused "
#         f"--output-file \"{cov_clean}\"",
#         cwd=repo, log=log_file
#     )

#     run(f"genhtml \"{cov_clean}\" --output-directory \"{html_dir}\"",
#         cwd=repo, log=log_file)

#     build_end = time.time()
#     log_file.write(f"\nBuild & test time ({label}): {build_end - build_start:.2f} sec\n")

#     # ---- Workload (50 repetitions) ----
#     curl_bin = os.path.abspath(os.path.join(repo, "src/curl"))
#     if not os.path.exists(curl_bin):
#         raise RuntimeError("curl binary not found.")

#     runs = measure_workload_multiple(curl_bin, label, outdir, log_file)
#     summary[label] = runs


# # =============================
# # STATISTICS + CSV + JSON + PLOT
# # =============================

# def compute_stats(runs):
#     def mean(xs): return statistics.mean(xs) if xs else float("nan")
#     def std(xs): return statistics.stdev(xs) if len(xs) > 1 else float("nan")

#     times = [r["time_sec"] for r in runs]
#     energies = [r["energy_j"] for r in runs if r["energy_j"] is not None]
#     cycles = [r["cycles"] for r in runs]
#     instrs = [r["instructions"] for r in runs]
#     ipcs = [r["ipc"] for r in runs if not math.isnan(r["ipc"])]

#     return {
#         "time_mean": mean(times),
#         "time_std": std(times),
#         "energy_mean": mean(energies),
#         "energy_std": std(energies),
#         "cycles_mean": mean(cycles),
#         "cycles_std": std(cycles),
#         "instr_mean": mean(instrs),
#         "instr_std": std(instrs),
#         "ipc_mean": mean(ipcs),
#         "ipc_std": std(ipcs),
#     }


# def write_summary_and_raw(outdir, summary):

#     vuln = summary["vuln"]
#     fixed = summary["fixed"]

#     S_v = compute_stats(vuln)
#     S_f = compute_stats(fixed)

#     # ----- summary.csv -----
#     summary_csv = os.path.join(outdir, "summary.csv")
#     with open(summary_csv, "w", newline="") as f:
#         w = csv.writer(f)
#         w.writerow(["Metric", "Vuln_mean", "Vuln_std", "Fixed_mean", "Fixed_std"])
#         w.writerow(["Time (s)", S_v["time_mean"], S_v["time_std"], S_f["time_mean"], S_f["time_std"]])
#         w.writerow(["Energy (J)", S_v["energy_mean"], S_v["energy_std"], S_f["energy_mean"], S_f["energy_std"]])
#         w.writerow(["Cycles", S_v["cycles_mean"], S_v["cycles_std"], S_f["cycles_mean"], S_f["cycles_std"]])
#         w.writerow(["Instructions", S_v["instr_mean"], S_v["instr_std"], S_f["instr_mean"], S_f["instr_std"]])
#         w.writerow(["IPC", S_v["ipc_mean"], S_v["ipc_std"], S_f["ipc_mean"], S_f["ipc_std"]])

#     # ----- raw_runs.csv -----
#     raw_csv = os.path.join(outdir, "raw_runs.csv")
#     with open(raw_csv, "w", newline="") as f:
#         w = csv.writer(f)
#         w.writerow(["label","run","time","energy","cycles","instructions","ipc"])
#         for label, runs in summary.items():
#             for r in runs:
#                 w.writerow([
#                     label,
#                     r["run_index"],
#                     r["time_sec"],
#                     r["energy_j"],
#                     r["cycles"],
#                     r["instructions"],
#                     r["ipc"],
#                 ])

#     # ----- raw_runs.json -----
#     raw_json = os.path.join(outdir, "raw_runs.json")
#     with open(raw_json, "w") as f:
#         json.dump(summary, f, indent=2)

#     # ----- Plot: Energy vs Time -----
#     if HAS_MPL:
#         vE = [r["energy_j"] for r in vuln]
#         vT = [r["time_sec"] for r in vuln]
#         fE = [r["energy_j"] for r in fixed]
#         fT = [r["time_sec"] for r in fixed]

#         plt.figure()
#         plt.scatter(vE, vT, label="vuln", marker="o")
#         plt.scatter(fE, fT, label="fixed", marker="x")
#         plt.xlabel("Energy (J)")
#         plt.ylabel("Time (s)")
#         plt.title("Workload Energy vs Time (vuln vs fixed)")
#         plt.legend()
#         plt.grid(True)
#         plot_file = os.path.join(outdir, "workload_energy_time.png")
#         plt.savefig(plot_file, dpi=150)
#         plt.close()

#     print("Summary + raw data + plots created.")


# # =============================
# # MAIN
# # =============================

# def main():
#     print("Requesting sudo...")
#     subprocess.run("sudo -v", shell=True)

#     outdir = prepare_output_dir()
#     print("Results in:", outdir)

#     log = open(os.path.join(outdir, "log.txt"), "w")
#     summary = {}

#     build_and_test(VULN_COMMIT, "vuln", outdir, log, summary)
#     build_and_test(FIX_COMMIT, "fixed", outdir, log, summary)

#     write_summary_and_raw(outdir, summary)

#     log.close()
#     print("\n=== DONE ===\n")


# if __name__ == "__main__":
#     main()


#!/usr/bin/env python3
import os
import subprocess
import time
import csv
import json
import statistics
import math

# matplotlib optional
try:
    import matplotlib.pyplot as plt
    HAS_MPL = True
except ImportError:
    HAS_MPL = False

# =============================
# CONFIGURATION
# =============================

VULN_COMMIT = "0583e87ada7a3cfb10904ae4ab61b339582c5bd3"
FIX_COMMIT  = "79b9d5f1a42578f807a6c94914bc65cbaa304b6d"

CURL_REPO = "curl"
RESULTS_ROOT = "curl_results"
WORKLOAD_SCRIPT = os.path.abspath("perf_workload.sh")

# RAPL PACKAGE
RAPL_PKG = "/sys/class/powercap/intel-rapl:0/energy_uj"

# Number of workload repetitions
NUM_RUNS = 50


# =============================
# BASIC HELPERS
# =============================

def run(cmd, cwd=None, log=None, allow_fail=False):
    print(f"\n>>> {cmd}")
    if log:
        log.write(f"\n>>> {cmd}\n")
    result = subprocess.run(cmd, cwd=cwd, shell=True)
    if not allow_fail and result.returncode != 0:
        raise RuntimeError(f"Command failed: {cmd}")
    return result.returncode


def read_rapl_energy():
    """Reads energy from RAPL (microjoules)."""
    try:
        v = subprocess.check_output(["sudo", "-n", "cat", RAPL_PKG])
        return int(v.decode().strip())
    except Exception:
        return None


def prepare_output_dir():
    """Creates a clean results directory for this pair of commits."""
    vuln_short = VULN_COMMIT[:8]
    fix_short = FIX_COMMIT[:8]
    dirname = f"cov_{vuln_short}...{fix_short}"
    full = os.path.abspath(os.path.join(RESULTS_ROOT, dirname))

    if os.path.exists(full):
        subprocess.run(f"rm -rf '{full}'", shell=True)

    os.makedirs(full, exist_ok=True)
    return full

# =============================
# WORKLOAD PERFORMANCE + ENERGY
# =============================

def measure_workload_once(curl_bin, label, log_file):
    """
    Runs one workload:
      - Measures time
      - Measures RAPL energy before/after
      - Captures perf stats (cycles, instructions)
    """
    time.sleep(0.2)

    # Energy before
    e_start = read_rapl_energy()
    t0 = time.time()

    # Run perf
    cmd = [
        "perf", "stat",
        "-x,", "-e", "cycles,instructions",
        WORKLOAD_SCRIPT, curl_bin
    ]

    proc = subprocess.run(cmd, text=True, capture_output=True)
    t1 = time.time()

    # Energy after
    e_end = read_rapl_energy()

    if proc.returncode != 0:
        raise RuntimeError(f"perf stat failed for {label}: {proc.stderr}")

    # ---------- Parse perf output ----------
    cycles_total = 0
    instr_total = 0
    found_cycles = False
    found_instr = False

    for line in proc.stderr.splitlines():
        parts = [p.strip() for p in line.split(",")]

        if len(parts) < 3:
            continue

        value_str = parts[0]
        event = parts[2]

        if value_str.startswith("<not counted>"):
            continue

        try:
            v = int(value_str.replace(",", "").replace(".", ""))
        except ValueError:
            continue

        e = event.strip()
        if e.endswith("/"):
            e = e[:-1]
        base_event = e.split("/")[-1]

        if "cycles" in base_event:
            cycles_total += v
            found_cycles = True

        if "instructions" in base_event:
            instr_total += v
            found_instr = True

    if not found_cycles or not found_instr:
        raise RuntimeError(f"Failed to parse perf counters for {label}")

    cycles = cycles_total
    instructions = instr_total

    # ----- Compute Energy -----
    energy_j = None
    if e_start is not None and e_end is not None:
        delta = e_end - e_start
        if delta < 0:
            delta += 2**32
        energy_j = delta / 1_000_000.0

    # Time
    time_sec = t1 - t0
    ipc = instructions / cycles if cycles > 0 else float("nan")

    if log_file:
        log_file.write(
            f"Run {label}: time={time_sec:.4f}s, "
            f"energy={energy_j:.6f}J, cycles={cycles}, "
            f"instr={instructions}, IPC={ipc:.4f}\n"
        )

    return {
        "time_sec": time_sec,
        "energy_j": energy_j,
        "cycles": cycles,
        "instructions": instructions,
        "ipc": ipc,
    }


def measure_workload_multiple(curl_bin, label, outdir, log_file):
    """Runs workload NUM_RUNS times."""
    runs = []
    for i in range(NUM_RUNS):
        if log_file:
            log_file.write(f"\n--- Workload run {i+1}/{NUM_RUNS} ({label}) ---\n")
        m = measure_workload_once(curl_bin, label, log_file)
        m["run_index"] = i + 1
        runs.append(m)
    return runs


# =============================
# BUILD + COVERAGE
# =============================

def build_and_test(commit, label, outdir, log_file, summary):
    repo = os.path.abspath(CURL_REPO)

    log_file.write(f"\n=== Processing {label} ({commit[:8]}) ===\n")
    build_start = time.time()

    # Clean + checkout
    run("git reset --hard", cwd=repo, log=log_file)
    run("git clean -fdx",  cwd=repo, log=log_file)
    run(f"git checkout {commit}", cwd=repo, log=log_file)
    run("git clean -fdx", cwd=repo, log=log_file)

    # Build with coverage flags
    run("./buildconf", cwd=repo, log=log_file)
    run(
        'CFLAGS="--coverage -O0 -g -fprofile-update=atomic" '
        'LDFLAGS="--coverage" ./configure --enable-debug --with-openssl',
        cwd=repo, log=log_file
    )

    run("make -j$(nproc)", cwd=repo, log=log_file)

    run("make test", cwd=repo, log=log_file, allow_fail=True)

    # ----- Coverage -----
    cov_info  = os.path.abspath(os.path.join(outdir, f"{label}-coverage.info"))
    cov_clean = os.path.abspath(os.path.join(outdir, f"{label}-coverage.cleaned.info"))
    html_dir  = os.path.abspath(os.path.join(outdir, f"{label}-coverage-html"))

    # Capture
    run(
        f"lcov --capture --ignore-errors negative "
        f"--directory . --output-file \"{cov_info}\"",
        cwd=repo, log=log_file
    )

    # Clean + remove test files
    run(
        f"lcov --remove \"{cov_info}\" '/usr/*' '*/tests/*' "
        f"--ignore-errors unused --output-file \"{cov_clean}\"",
        cwd=repo, log=log_file
    )

    # HTML report
    run(f"genhtml \"{cov_clean}\" --output-directory \"{html_dir}\"",
        cwd=repo, log=log_file)

    log_file.write(f"\nBuild & test time ({label}): {time.time() - build_start:.2f} sec\n")

    # ----- Workload (50 runs) -----
    curl_bin = os.path.abspath(os.path.join(repo, "src/curl"))
    if not os.path.exists(curl_bin):
        raise RuntimeError("curl binary not generated.")

    runs = measure_workload_multiple(curl_bin, label, outdir, log_file)
    summary[label] = runs

# =============================
# STATISTICS + CSV + JSON + PLOT
# =============================

def compute_stats(runs):
    """Compute mean and stdev for each metric."""
    def mean(xs): return statistics.mean(xs) if xs else float("nan")
    def std(xs): return statistics.stdev(xs) if len(xs) > 1 else float("nan")

    times  = [r["time_sec"] for r in runs]
    energies = [r["energy_j"] for r in runs if r["energy_j"] is not None]
    cycles = [r["cycles"] for r in runs]
    instrs = [r["instructions"] for r in runs]
    ipcs   = [r["ipc"] for r in runs if not math.isnan(r["ipc"])]

    return {
        "time_mean": mean(times),
        "time_std":  std(times),
        "energy_mean": mean(energies),
        "energy_std":  std(energies),
        "cycles_mean": mean(cycles),
        "cycles_std":  std(cycles),
        "instr_mean": mean(instrs),
        "instr_std":  std(instrs),
        "ipc_mean": mean(ipcs),
        "ipc_std":  std(ipcs),
    }


def write_summary_and_raw(outdir, summary):
    vuln  = summary["vuln"]
    fixed = summary["fixed"]

    S_v = compute_stats(vuln)
    S_f = compute_stats(fixed)

    # ========== SUMMARY CSV ==========
    summary_csv = os.path.join(outdir, "summary.csv")
    with open(summary_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Metric", "Vuln_mean", "Vuln_std", "Fixed_mean", "Fixed_std"])

        w.writerow(["Time (s)", S_v["time_mean"], S_v["time_std"], S_f["time_mean"], S_f["time_std"]])
        w.writerow(["Energy (J)", S_v["energy_mean"], S_v["energy_std"], S_f["energy_mean"], S_f["energy_std"]])
        w.writerow(["Cycles", S_v["cycles_mean"], S_v["cycles_std"], S_f["cycles_mean"], S_f["cycles_std"]])
        w.writerow(["Instructions", S_v["instr_mean"], S_v["instr_std"], S_f["instr_mean"], S_f["instr_std"]])
        w.writerow(["IPC", S_v["ipc_mean"], S_v["ipc_std"], S_f["ipc_mean"], S_f["ipc_std"]])

    # ========== RAW CSV ==========
    raw_csv = os.path.join(outdir, "raw_runs.csv")
    with open(raw_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["label","run","time","energy","cycles","instructions","ipc"])
        for label, runs in summary.items():
            for r in runs:
                w.writerow([
                    label,
                    r["run_index"],
                    r["time_sec"],
                    r["energy_j"],
                    r["cycles"],
                    r["instructions"],
                    r["ipc"],
                ])

    # ========== RAW JSON ==========
    raw_json = os.path.join(outdir, "raw_runs.json")
    with open(raw_json, "w") as f:
        json.dump(summary, f, indent=2)

    # ========== ENERGY vs TIME PLOT ==========
    if HAS_MPL:
        vE = [r["energy_j"] for r in vuln]
        vT = [r["time_sec"] for r in vuln]
        fE = [r["energy_j"] for r in fixed]
        fT = [r["time_sec"] for r in fixed]

        plt.figure()
        plt.scatter(vE, vT, label=f"vuln ({VULN_COMMIT[:8]})", marker="o")
        plt.scatter(fE, fT, label=f"fixed ({FIX_COMMIT[:8]})", marker="x")

        plt.xlabel("Energy (J)")
        plt.ylabel("Time (s)")
        plt.title(f"Energy vs Time\nvuln={VULN_COMMIT[:8]}  fixed={FIX_COMMIT[:8]}")
        plt.legend()
        plt.grid(True)

        plot_file = os.path.join(
            outdir,
            f"energy_time_{VULN_COMMIT[:8]}_{FIX_COMMIT[:8]}.png"
        )
        plt.savefig(plot_file, dpi=200)
        plt.close()

    print("Summary + raw data + plots created.")


# =============================
# MAIN
# =============================

def main():
    print("Requesting sudo...")
    subprocess.run("sudo -v", shell=True)

    outdir = prepare_output_dir()
    print("Results in:", outdir)

    log = open(os.path.join(outdir, "log.txt"), "w")
    summary = {}

    # vulnerable commit
    build_and_test(VULN_COMMIT, "vuln", outdir, log, summary)

    # fixed commit
    build_and_test(FIX_COMMIT, "fixed", outdir, log, summary)

    # write all output files
    write_summary_and_raw(outdir, summary)

    log.close()
    print("\n=== DONE ===\n")


if __name__ == "__main__":
    main()
