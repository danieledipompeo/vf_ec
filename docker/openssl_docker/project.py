# create an abstract class for projects
from abc import ABC, abstractmethod
from glob import glob
import os
import logging
from pathlib import Path
import sys

from common import GitHandler, sh
# from config import Config

class ProjectFactory:
    
    @staticmethod
    def get_project(name: str, input_dir: str, output_dir: str) -> 'Project':
        if name.lower() == "openssl":
            return OpenSSLProject(output_dir, input_dir)
        elif name.lower() == "ffmpeg":
            return FfmpegProject(output_dir, input_dir)
        elif name.lower() == "vim":
            return VimProject(output_dir, input_dir)
        else:
            raise ValueError(f"Unknown project: {name}")


class Project(ABC):
    
    name = "generic_project"
    output_dir = "./output/generic_project"
    input_dir = "./input/generic_project"

    GCDA_FOLDER = "coverage-per-test"

    def _init(self, output_dir, input_dir, name, project_repo) -> None:
        super().__init__()
        self.logger = logging.getLogger(self.__class__.__name__)
        handler = logging.FileHandler(os.path.join(output_dir, "log",  "log.log"))
        self.logger.addHandler(handler)
        self.logger.setLevel(logging.DEBUG)
        
        self.name = name
        self.output_dir = os.path.join(output_dir, self.name)
        
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)
        
        self.input_dir = os.path.join(input_dir, self.name)

        if (not os.path.exists(self.input_dir)) or (not os.path.exists(os.path.join(self.input_dir, ".git"))):
            GitHandler.clone_repo(input_dir, project_repo)
        
    @abstractmethod
    def get_test(self):
        pass

    @abstractmethod
    def run_test(self, test_name: str) -> tuple[bool, Exception | None]:
        pass

    def _clean(self):
        sh(cmd=["make", "clean"], cwd=Path(self.input_dir))

    def _build(self, n_proc=-1):
        cmd = ["make"]
        if n_proc == -1:
            import os
            nproc = os.cpu_count() or 1
            cmd.append(f"-j{nproc}")
        else:
            cmd.append(f"-j{n_proc}")
        
        _, errorcode, _ = sh(cmd=cmd, cwd=Path(self.input_dir))
        return errorcode == 0

    def coverage_file(self, test_name: str) -> list[str]:
        """
        Retrieves a list of coverage file paths for a given test name.

        :param root: The root directory where coverage files are located.
        :param test_name: The name of the test for which coverage files are retrieved.
        :return: A list of relative coverage file paths.
        """
        gcdas = sorted((Path(self.output_dir) / Project.GCDA_FOLDER / test_name).glob("**/*.gcda"))
        return [str(gcda.relative_to(Path(self.output_dir) / Project.GCDA_FOLDER / test_name)).replace("gcda", "c") for gcda in gcdas]

    def _prepare_env_for_testing(self, test_name: str) -> dict:
        output_dir = os.path.join(self.output_dir, Project.GCDA_FOLDER)
        sh(["mkdir", "-p", output_dir])
        env_test = os.environ.copy()
        env_test["GCOV_PREFIX_STRIP"] = "3"
        env_test["GCOV_PREFIX"] = os.path.join(output_dir, test_name)
        return env_test

    def _run(self, cmd: list[str], test_name: str, env_test: dict, cwd: Path) -> tuple[bool, Exception | None]:
        try:
            _, errorcode, _ = sh(cmd, cwd=cwd, env=env_test)
            return errorcode == 0, None
        except Exception as e:
            # self.logger.error(f"Test '{test_name}' failed with exception: {e}")
            return False, e

    def build(self, coverage=False, n_proc=-1):
        if os.path.exists(os.path.join(self.input_dir, "Makefile")):
            self._clean()
        if not self._configure(cwd=Path(self.input_dir), coverage=coverage):
            self.logger.error("Configuration failed, cannot build.")
            return False
        return self._build(n_proc=n_proc)

    def _configure(self, cwd: Path, coverage=False):
        pass

class OpenSSLProject(Project):

    def __init__(self, output_dir, input_dir) -> None:
        super().__init__()

        self._init(output_dir, input_dir, "openssl", "https://github.com/openssl/openssl")

    def run_test(self, test_name: str) -> tuple[bool, Exception | None]:
        env_test = self._prepare_env_for_testing(test_name)
        
        cmd = ["make", "test", f"TESTS={test_name}", "HARNESS_JOBS=1"]
        return self._run(cmd, test_name, env_test, cwd=Path(self.input_dir))
        
    def get_test(self) -> list[str]:
        out, _, _ = sh(['make', 'list-tests'], cwd=Path(self.input_dir))
        return [line.strip().split()[0]
            for line in out.splitlines()
            if line.strip() and not line.lstrip().startswith(("make", "Files=", "Tests=", "Result:"))]
    
    def _configure(self, cwd: Path, coverage=False):
        config_args = ["./config", "no-asm", "-g", "-O0"] 
        if coverage:
            config_args.append("--coverage")
        
        _, errorcode, err = sh(config_args, cwd=cwd)
        if errorcode != 0:
            self.logger.warning(f"Trying fallback configuration due to exception: {err}")
            env_backup = os.environ.copy()
            env_backup['CFLAGS']="-O0 -g -fprofile-arcs -ftest-coverage"
            env_backup["LDFLAGS"]="--coverage"
            config_args = ["./Configure", "linux-x86_64", "no-asm"]
            _, errorcode, err = sh(config_args, cwd=cwd, env=env_backup)
            if errorcode !=0:
                self.logger.error(f"Fallback configuration also failed due to exception: {err}")
                return False
        
        self.logger.info("Configuration succeeded with standard ./config.")
        return True
    
class VimProject(Project):

    TEST_DIR = "src/testdir"

    def __init__(self, output_dir, input_dir):
        super().__init__()
        
        self._init(output_dir, input_dir, "vim", "https://github.com/vim/vim")

    def run_test(self, test_name: str) -> tuple[bool, Exception | None]:
        env_test = self._prepare_env_for_testing(test_name)
        
        cmd = ["make", test_name, "HARNESS_JOBS=1"]
        return self._run(cmd, test_name, env_test, 
                         cwd=Path(os.path.join(self.input_dir, "src", "testdir")))

    def get_test(self) -> list[str]:
        test_files = [Path(f).stem 
                      for f in glob.glob(f"inputs/vim/{VimProject.TEST_DIR}/test_*.vim") + 
                               glob.glob(f"inputs/vim/{VimProject.TEST_DIR}/test_*.in")]
        return test_files
        
    
    def _configure(self, cwd, coverage=False):
        # Vim configure
        # --with-features=huge: Enables most features (needed for many tests)
        # --enable-gui=no --without-x: Disables GUI (Critical for Docker)
        config_args = [
            "./configure",
            "--with-features=huge",
            "--enable-gui=no",
            "--without-x"
        ]
        
        env_test = os.environ.copy()
        env_test["CFLAGS"] = "-O0 "
        if coverage:
            env_test["CFLAGS"] += "--coverage "
            # Often helpful to link coverage too
            env_test["LDFLAGS"] = "--coverage "

        _, errorcode, err = sh(config_args, cwd=cwd, env=env_test)
        if errorcode != 0:
            self.logger.error(f"Configuration failed with error: {err}")
            return False
        
        self.logger.info("Configuration succeeded with standard ./config.")
        return True
    
class FfmpegProject(Project):

    def __init__(self, output_dir, input_dir):            
        super().__init__()
        
        self._init(output_dir, input_dir, "FFmpeg","https://github.com/FFmpeg/FFmpeg.git")
        
        samples_dir = os.path.join(output_dir, self.name, "fate_suite")
        if not os.path.exists(samples_dir) or not os.listdir(samples_dir):
            self.logger.info("FATE Samples not found. Downloading ...")
            try:
                # Create dir
                os.makedirs(samples_dir, exist_ok=True)
                # Standard FFmpeg FATE rsync command
                cmd = ["rsync", "-aL", "rsync://fate-suite.ffmpeg.org/fate-suite/", f"{samples_dir}/"]
                sh(cmd, input_dir)
                self.logger.info("FATE Samples download complete.")
            except Exception as e:
                self.logger.error(f"Failed to download FATE samples: {e}")
                sys.exit(1)


    def run_test(self, test_name: str) -> tuple[bool, Exception | None]:
        env_test = self._prepare_env_for_testing(test_name)
        
        cmd = ["make", test_name, "HARNESS_JOBS=1"]
        return self._run(cmd, test_name, env_test, cwd=Path(self.input_dir))

    def get_test(self) -> list[str]:
        out, _, _ = sh(['make', '-s', 'fate-list'], cwd=Path(self.input_dir))
        return [line.strip().split()[0]
            for line in out.splitlines()
            if line.strip() and not line.lstrip().startswith(("make", "Files=", "Tests=", "Result:", "GEN"))]
    
    def _configure(self, cwd, coverage=False):
        config_args = ["./configure", "--disable-asm", "--disable-doc"]
        if coverage:
            config_args.append("--extra-cflags=--coverage")
            config_args.append("--extra-ldflags=--coverage")
        _, errorcode, err = sh(config_args, cwd=cwd)
        if errorcode != 0:
            self.logger.error(f"Configuration failed with error: {err}")
            return False
        
        self.logger.info("Configuration succeeded with standard ./config.")
        return True