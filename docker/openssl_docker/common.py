import os
import subprocess
import threading
import re
import shlex
from pathlib import Path

from logger import get_logger

logger = get_logger(__name__)

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

def sh(cmd: list[str] | str, 
       cwd: Path | None = None, 
       env: dict[str, str] | None = None, 
       use_shell: bool = True
       ) -> tuple[str, int, str]:
    """
    Execute a shell command and capture its output.
    
    Args:
        cmd: Command to execute. Can be a list of strings (command and arguments) or a single string.
        cwd: Working directory for the command execution. Defaults to None (current directory).
        env: Environment variables to use for the process. Defaults to None (inherits parent process env).
    
    Returns:
        A tuple containing:
            - stdout (str): Standard output from the command
            - returncode (int): Exit code of the process
            - stderr (str): Standard error output from the command
    
    Note:
        - If cmd is a list and contains shell special characters (|, ||, &&, ;, >, >>, <, $, `, (, )),
          the command will be executed with shell=True.
        - Command execution is printed to stdout with a prefix for debugging purposes.
        - Output streams are read concurrently using separate threads to avoid deadlocks.
        - stdin is set to DEVNULL, so no input can be provided to the command.
    """
    if isinstance(cmd, list):
        cmd_display = shlex.join(cmd)
        args: list[str] | str = cmd_display if use_shell else cmd
    else:
        cmd_display = cmd
        args = cmd
    logger.debug("+ %s%s %s", str(cwd) + "/" if cwd else "./", cmd_display, "(shell)" if use_shell else "")

    # args: list[str] | str = cmd
    # if isinstance(cmd, list):
        # shell_tokens = ("|", "||", "&&", ";", ">", ">>", "<", "$", "`", "(", ")")
        # if any(any(tok in part for tok in shell_tokens) for part in cmd):
            # use_shell = True
            # args = " ".join(cmd)

    process = subprocess.Popen(
        args,
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        shell=use_shell,
        bufsize=1,
    )

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    def _drain(stream, sink: list[str]):
        if stream is None:
            return
        for line in stream:
            sink.append(line)

    t_out = threading.Thread(target=_drain, args=(process.stdout, stdout_chunks))
    t_err = threading.Thread(target=_drain, args=(process.stderr, stderr_chunks))
    t_out.start()
    t_err.start()
    process.wait()
    t_out.join()
    t_err.join()

    return "".join(stdout_chunks), process.returncode, "".join(stderr_chunks)

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
    def clone_repo(cwd, repo_url, dest_path=""):
        cmd = ['git', 'clone', repo_url]
        # Append destination only when it carries a meaningful value.
        if dest_path and dest_path.strip() != "":
            cmd += [dest_path]
        if not os.path.exists(os.path.join(cwd, os.path.basename(repo_url).replace('.git',''))):
            sh(cmd, cwd)
    
    @staticmethod
    def get_age_of_commit(cwd, commit_hash):
        from datetime import datetime
        cmd = ["git", "show", "-s", "--format=%ct", commit_hash]
        out, code, err = sh(cmd, cwd)
        if code == 0:
            timestamp = int(out.strip())
            return datetime.fromtimestamp(timestamp).year
        return None

    @staticmethod
    def get_changed_lines(repo_dir: Path, commit: str) -> dict[str, set[int]]:
        """Return changed line numbers per file for the given commit."""
        cmd = ["git", "show", "--unified=0", "--format=", commit]
        result = subprocess.run(
            cmd,
            cwd=repo_dir,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=True,
        )

        changed: dict[str, set[int]] = {}
        current_file: str | None = None
        hunk_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

        for raw_line in result.stdout.splitlines():
            if raw_line.startswith("+++ b/"):
                file_name = raw_line[6:].strip()
                if file_name == "/dev/null":
                    current_file = None
                    continue
                current_file = GitHandler._normalize_repo_path(file_name, repo_dir)
                changed.setdefault(current_file, set())
                continue

            if current_file is None:
                continue

            match = hunk_re.match(raw_line)
            if not match:
                continue

            start_line = int(match.group(1))
            count = int(match.group(2) or "1")

            # Deletion-only hunks have a +<line>,0 range. Keep the insertion
            # anchor line so downstream matching can still reason about where
            # the change happened in the resulting file.
            if count <= 0:
                changed[current_file].add(start_line)
                continue

            for line_no in range(start_line, start_line + count):
                changed[current_file].add(line_no)

        return changed
    
    @staticmethod
    def _normalize_repo_path(path_value: str, repo_dir: Path) -> str:
        """Normalize path to repository-relative format when possible."""
        path_obj = Path(path_value)
        if path_obj.is_absolute():
            try:
                return str(path_obj.resolve().relative_to(repo_dir.resolve()))
            except ValueError:
                return str(path_obj)
        return str(path_obj)

class EnergyHandler:

    ITERATION_TIMEOUT_MS = 5000
    COOL_DOWN_SEC = 1.0
    ITERATIONS = 30
    output_dir = "energy_measurements"      

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
        logger.info(f"Detected RAPL energy events: {sorted(events)}")
        return sorted(events)

    # @staticmethod
    # def measure_test(#pkg_event: list, 
    #                  test: dict, commit: str, 
    #                  output_dir: str, project_dir: str, 
    #                  iterations: int = 5, timeout_ms: int = 5000, 
    #                  cool_down_sec: float = 1.0):
    @staticmethod
    def measure_test(test: str, cmd: list[str], output_filename: str, test_dir: str):
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
        
        pkg_event = EnergyHandler.detect_rapl() 

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
        
        # pb = ProgressBar(EnergyHandler.ITERATIONS)

        timeout_ms = EnergyHandler.ITERATION_TIMEOUT_MS  # e.g. 5s default, tune per test
        logger.info(f"Measuring energy for test '{test}': "
              f"{EnergyHandler.ITERATIONS} iterations × {timeout_ms}ms timeout each "
              f"cooling down between iterations for {EnergyHandler.COOL_DOWN_SEC}s, monitoring events: {perf_events}")

        for iteration in range(EnergyHandler.ITERATIONS):
            # pb.set(iteration)
            logger.debug(" --- Starting iteration %d for test '%s'", iteration + 1, test)

            energy_file = output_filename + f"__{iteration}.csv"
            iteration_count_file = output_filename + f"__{iteration}_count.txt"

            wrapped_script = EnergyHandler._wrap_until_timeout(cmd, timeout_ms, iteration_count_file)
            
            sample_freq_ms = 100

            # Build perf as argv list (safer than huge shell string)
            perf_argv = [
                "perf", "stat",
                "-a",
                "-e", f"{perf_events}",
                "-I", f"{sample_freq_ms}",
                "-x,", "--output", energy_file,
                "--",
            ]
            perf_argv += ["bash", "-lc", wrapped_script]

            out, rc, err = sh(perf_argv, cwd=Path(test_dir), use_shell=False)
            
            if rc != 0: 
                logger.error(
                    f"[ERROR] Test '{test}' failed during energy measurement.\n"
                    f"Return code: {rc}\n"
                    f"Script: {wrapped_script}\n"
                    f"STDERR: {err.strip()}\n"
                    f"STDOUT: {out.strip()}"
                )
                if os.path.exists(energy_file):
                    logger.warning(f"Removing perf output file due to error: {energy_file}")
                    os.remove(energy_file)
                    
            time.sleep(EnergyHandler.COOL_DOWN_SEC)

    @staticmethod
    def _wrap_until_timeout(test_cmd: list[str], timeout_ms: int, iteration_count_file: str | None = None) -> str:
        """
        Returns a bash script body that runs `test_cmd` repeatedly until timeout expires.
        - uses monotonic-ish wall clock via SECONDS (bash built-in, second resolution)
        - avoids killing a running iteration mid-command (it checks deadline BETWEEN iterations)
        - if iteration_count_file is provided, saves the loop iteration count to that file
        - enables shell tracing by default for easier debugging
        """
        # Use bash -lc so we can rely on bash features and keep quoting predictable
        # SECONDS is integer seconds since shell start; good enough for energy runs (>= 2-5s).
        timeout_s = max(1, int((timeout_ms + 999) / 1000))  # ceil to seconds
        cmd_quoted = " ".join(shlex.quote(part) for part in test_cmd)

        # Persist count even if a command fails under strict mode.
        count_trap = (
            f"trap 'printf %s\\\\n \"$iteration_count\" > {shlex.quote(iteration_count_file)}' EXIT"
            if iteration_count_file
            else ""
        )

        # Important:
        # - `set -Eeuo pipefail` makes failures stop the loop and propagate non-zero to perf
        # - you can change to `|| true` if you prefer "keep looping even if one iteration fails"
        script = f"""
        set -Eeuo pipefail
        iteration_count=0
        trap 'rc=$?; echo "[energy-wrapper] error rc=${{rc}} line=${{LINENO}} cmd=${{BASH_COMMAND}}" >&2; exit $rc' ERR
        {count_trap}
        PS4='+ [energy-wrapper:${{LINENO}}] '
        set -x
        end=$((SECONDS + {timeout_s}))
        while [ "$SECONDS" -lt "$end" ]; do
            {cmd_quoted}
            iteration_count=$((iteration_count + 1))
        done
        """
        return script
