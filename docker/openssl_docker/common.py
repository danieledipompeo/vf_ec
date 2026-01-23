import os
import logging
import subprocess
import re
from pathlib import Path

class ProgressBar:
    def __init__(self, total, length=40, step=1):
        self.total = total
        self.length = length
        self.step = step

    def update(self, i):
        progress = (i + 1) / self.total
        filled = int(self.length * progress)
        bar = '█' * filled + '░' * (self.length - filled)
        print(f"\r[{bar}] {i+1}/{self.total}", end='', flush=True)

    def log(self, msg):
        print()
        print(msg)
        self.update(self.current)

    def set(self, i):
        self.current = i
        self.update(i)

GIST_CSV_URL = "https://gist.githubusercontent.com/waheed-sep/935cfc1ba42b2475d45336a4c779cbc8/raw/ea91568360d87979373a7eca38f289c9bf30d103/cwe_projects.csv"

def get_covered_files(cwd):
    """
    Scans the given directory for .gcda files and maps them to their corresponding .c source files.
    Returns a list of source file paths relative to the cwd.

    :param cwd: Description
    """
    covered = set()
    for root, dirs, files in os.walk(cwd):
        for file in files:
            if file.endswith(".gcda"):
                source_name = file.replace(".gcda", ".c")
                rel_dir = os.path.relpath(root, cwd)
                full_path = source_name if rel_dir == "." else os.path.join(rel_dir, source_name)
                covered.add(full_path)
    return list(covered)

def sh(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> tuple[str, int, str]:
    print("+", " ".join(cmd), flush=True)
    out = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env,
                         check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return out.stdout, out.returncode, out.stderr


class MakeHandler:
    logger = logging.getLogger("MakeHandler")
    handler = logging.FileHandler("make_handler.log")
    logger.addHandler(handler)
    
    GCDA_FOLDER = "coverage-per-test"
    
    @staticmethod
    def clean(cwd):
        cmd = ["make", "clean"]
        sh(cmd, cwd)
    
    @staticmethod   
    def build(cwd, n_proc=-1):
        cmd = ["make"]
        if n_proc == -1:
            import os
            nproc = os.cpu_count() or 1
            cmd.append(f"-j{nproc}")
        else:
            cmd.append(f"-j{n_proc}")
        
        _, errorcode, _ = sh(cmd, cwd)
        return errorcode == 0


    @staticmethod
    def test(cwd, target, output_dir="."):
        
        output_dir = os.path.join(output_dir, MakeHandler.GCDA_FOLDER)
        sh(["mkdir", "-p", output_dir])
        env_test = os.environ.copy()
        env_test["GCOV_PREFIX_STRIP"] = "3"
        env_test["GCOV_PREFIX"] = os.path.join(output_dir, target)
        
        cmd = ["make", "test", f"TESTS={target}", "HARNESS_JOBS=1"]
        try:
            _, errorcode, _ = sh(cmd, cwd, env=env_test)
            return errorcode == 0
        except Exception as e:
            MakeHandler.logger.error(f"Test '{target}' failed with exception: {e}")
            return False
        
    @staticmethod
    def coverage_file(root: Path, test_name: str) -> list[str]:
        """
        Retrieves a list of coverage file paths for a given test name.

        :param root: The root directory where coverage files are located.
        :param test_name: The name of the test for which coverage files are retrieved.
        :return: A list of relative coverage file paths.
        """
        gcdas = sorted((root / MakeHandler.GCDA_FOLDER / test_name).glob("**/*.gcda"))
        return [str(gcda.relative_to(root / MakeHandler.GCDA_FOLDER / test_name)).replace("gcda", "c") for gcda in gcdas]

class GitHandler:

    @staticmethod
    def get_git_diff_files(cwd, commit_hash):
        cmd = f"git diff-tree --no-commit-id --name-only -r {commit_hash}"
        result = subprocess.run(cmd, cwd=cwd, shell=True, stdout=subprocess.PIPE, text=True)
        return {f for f in result.stdout.strip().split('\n') if f}

    @staticmethod
    def clean_repo(cwd):
        sh(["git", "reset", "--hard"], cwd)
        sh(["git", "clean", "-fdx"], cwd)
        
    @staticmethod
    def checkout(cwd, commit_hash, force=True):
        return sh(["git", "checkout", "-f" if force else "", commit_hash], cwd)
    
    @staticmethod
    def clone_repo(cwd, repo_url):
        cmd = ['git', 'clone', repo_url]
        sh(cmd, cwd)

    

class EnergyHandler:

    logger = logging.getLogger("EnergyHandler")
    handler = logging.FileHandler("energy_handler.log")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)

    @staticmethod
    def detect_rapl(perf_bin="perf"):
        ENERGY_RE = re.compile(r'\bpower/energy-[^/\s]+/?\b')
        # --no-desc makes output easier to parse if supported; if not, fall back.
        cmd = [perf_bin, "list", "--no-desc"]
        try:
            out = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError:
            out = subprocess.check_output([perf_bin, "list"], text=True, stderr=subprocess.STDOUT)

        events = set()
        for line in out.splitlines():
            # Grab all matches from the line (some lines may include multiple tokens)
            for m in ENERGY_RE.findall(line):
                # Normalize to the canonical perf selector form with trailing '/'
                if not m.endswith("/"):
                    m += "/"
                events.add(m)
        EnergyHandler.logger.info(f"Detected RAPL energy events: {sorted(events)}")
        return sorted(events)

    @staticmethod
    def measure_test(pkg_event: list, test: dict, commit: str, output_dir: str, project_dir: str, iterations: int = 5, timeout_ms: int = 5000, cool_down_sec: float = 1.0):
        """
        Measures energy consumption for a given test command using perf.
        
        :param pkg_event: list of package events to monitor
        :type pkg_event: list
        :param test: Array containing test details
        :type test: dict
        :param commit: Hash of the commit being tested
        :type commit: str
        :param output_dir: Directory to store output files
        :type output_dir: str
        :param project_dir: Directory of the project being tested
        :type project_dir: str
        :param iterations: Number of iterations to run the test
        :type iterations: int
        :param timeout_ms: Timeout for each test iteration in milliseconds
        :type timeout_ms: int
        :param cool_down_sec: Cool down period between iterations in seconds
        :type cool_down_sec: float
        """
        import time

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
        
        pb = ProgressBar(iterations)
        output_dir = os.path.join(output_dir, "energy_measurements") 
        os.makedirs(output_dir, exist_ok=True)

        timeout_ms = test.get("timeout_ms", timeout_ms)  # e.g. 5s default, tune per test
        EnergyHandler.logger.info(f"Measuring energy for test '{test.get('name')}': "
              f"{iterations} iterations × {timeout_ms}ms timeout each")

        for iteration in range(iterations):
            pb.set(iteration)

            perf_out = os.path.join(output_dir, f"{commit}_{test.get('name')}__{iteration}.csv")
            # TODO : handle cmd through MakeHandler
            cmd = ["make", "test", f"TESTS={test.get('name')}", "HARNESS_JOBS=1"]
            wrapped_cmd = EnergyHandler._wrap_until_timeout(cmd, timeout_ms)

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
                cwd=project_dir, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.PIPE, 
                text=True)
            
            if res.returncode != 0: 
                EnergyHandler.logger.error(
                    f"\n[ERROR] Test '{test.get('name')}' failed during energy measurement.\n"
                    f"Return code: {res.returncode}\n"
                    f"Command: {wrapped_cmd}\n"
                    f"STDERR: {res.stderr.strip()}\n"
                    f"STDOUT: {res.stdout.strip()}"
                )
                if os.path.exists(perf_out):
                    EnergyHandler.logger.warning(f"Removing perf output file due to error: {perf_out}")
                    os.remove(perf_out)
                    
            EnergyHandler.logger.info(f"[COOL DOWN] {cool_down_sec} seconds...")
            time.sleep(cool_down_sec)

    @staticmethod
    def _wrap_until_timeout(test_cmd: list[str], timeout_ms: int) -> str:
        import shlex
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
                  {" ".join(test_cmd)}
                done
                """
            )
        )
        return wrapped
    