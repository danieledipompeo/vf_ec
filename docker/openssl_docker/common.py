import os
import subprocess
import threading
import re
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
        cmd_display = " ".join(cmd)
    else:
        cmd_display = cmd
    logger.debug("+ %s%s %s", str(cwd) + "/" if cwd else "./", cmd_display, "(shell)" if use_shell else "")

    # args: list[str] | str = cmd
    # if isinstance(cmd, list):
        # shell_tokens = ("|", "||", "&&", ";", ">", ">>", "<", "$", "`", "(", ")")
        # if any(any(tok in part for tok in shell_tokens) for part in cmd):
            # use_shell = True
            # args = " ".join(cmd)

    process = subprocess.Popen(
        cmd_display,
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
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
        cmd = ['git', 'clone', repo_url, dest_path]
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

class EnergyHandler:

    ITERATION_TIMEOUT_MS = 5000
    COOL_DOWN_SEC = 1.0
    ITERATIONS = 5
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
        
        pb = ProgressBar(EnergyHandler.ITERATIONS)

        timeout_ms = EnergyHandler.ITERATION_TIMEOUT_MS  # e.g. 5s default, tune per test
        logger.info(f"Measuring energy for test '{test}': "
              f"{EnergyHandler.ITERATIONS} iterations × {timeout_ms}ms timeout each")

        for iteration in range(EnergyHandler.ITERATIONS):
            pb.set(iteration)

            energy_file = output_filename + f"__{iteration}.csv"

            # cmd = ["make", "test", f"TESTS={test.get('name')}", "HARNESS_JOBS=1"]
            wrapped_cmd = EnergyHandler._wrap_until_timeout(cmd, timeout_ms)

            # Build perf as argv list (safer than huge shell string)
            perf_argv = [
                "perf", "stat",
                "-a",
                "-e", f"{perf_events}",
                "-x,", "--output", energy_file,
                "--",
            ]
            # wrapped_cmd already includes "bash -lc '<script>'" so we run via sh -c? Not needed.
            # But since wrapped_cmd is a single string, we can still do: ["sh","-c", wrapped_cmd]
            perf_argv += ["sh", "-c", wrapped_cmd]

            out, rc, err = sh(perf_argv, cwd=Path(test_dir), use_shell=True)
            #res = subprocess.run(
            #    perf_argv, 
            #    cwd=test_dir, 
            #    stdout=subprocess.PIPE, 
            #    stderr=subprocess.PIPE, 
            #    text=True)
            
            if rc != 0: 
                logger.error(
                    f"[ERROR] Test '{test}' failed during energy measurement.\n"
                    f"Return code: {rc}\n"
                    f"Command: {wrapped_cmd}\n"
                    f"STDERR: {err.strip()}\n"
                    f"STDOUT: {out.strip()}"
                )
                if os.path.exists(energy_file):
                    logger.warning(f"Removing perf output file due to error: {energy_file}")
                    os.remove(energy_file)
                    
            logger.info(f"[COOL DOWN] {EnergyHandler.COOL_DOWN_SEC} seconds...")
            time.sleep(EnergyHandler.COOL_DOWN_SEC)

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
