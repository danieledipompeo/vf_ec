# create an abstract class for projects
from abc import ABC, abstractmethod
from glob import glob
import glob as glob_module
import os
import logging
from pathlib import Path
import sys

from common import GitHandler, EnergyHandler, sh
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
        elif name.lower() == "php-src":
            return PhpSrcProject(output_dir, input_dir) 
        elif name.lower() == 'libraw':
            return LibRawProject(output_dir, input_dir)
        elif name.lower() == "libvncserver":
            return LibVNCServerProject(output_dir, input_dir)

        else:
            raise ValueError(f"Unknown project: {name}")


class Project(ABC):
    
    name = "generic_project"
    output_dir = "./output/generic_project"
    input_dir = "./input/generic_project"

    GCDA_FOLDER = "coverage-per-test"

    def _init(self, output_dir: str, input_dir: str, name: str, project_repo: str) -> None:
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

        if not os.path.exists(os.path.join(self.input_dir, ".git")):
            GitHandler.clone_repo(input_dir, project_repo)
        
    @abstractmethod
    def get_test(self):
        pass

    def run_test(self, test_name: str) -> tuple[bool, Exception | None]:
        env_test = self._prepare_env_for_testing(test_name)
        cmd = self.get_test_cmd(test_name) #= ["make", test_name, "HARNESS_JOBS=1"]
        return self._run(cmd, test_name, env_test, cwd=Path(self.input_dir))

    @abstractmethod
    def get_test_cmd(self, test_name: str) -> list[str]:
        pass

    def compute_energy(self, test_name: str):
        print("COMPUTE ENERGY DISABLED FOR TESTING")
        # os.makedirs(os.path.join(self.output_dir, "energy_measurements"), exist_ok=True)
        # EnergyHandler.measure_test(test_name, cmd=self.get_test_cmd(test_name),
                                #    output_filename=os.path.join(self.output_dir, "energy_measurements", f"{test_name}_energy"),
                                #    project_dir=self.input_dir)
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
        gcda_path = Path(self.output_dir) / Project.GCDA_FOLDER / test_name
        gcdas = [f.split(".dir/")[-1] for f in glob(str(gcda_path / "**/*.gcda"), recursive=True) if ".dir/" in f]
        #gcdas = sorted((Path(self.output_dir) / Project.GCDA_FOLDER / test_name).glob("**/*.gcda"))
        gcdas = [g.replace('.c.gcda', '.c') for g in gcdas]
        gcdas = [g.replace('.gcda', '.c') for g in gcdas]
        return gcdas

    def _prepare_env_for_testing(self, test_name: str) -> dict:
        output_dir = os.path.join(self.output_dir, Project.GCDA_FOLDER)
        sh(["mkdir", "-p", output_dir])
        env_test = os.environ.copy()
        env_test["GCOV_PREFIX_STRIP"] = "3"
        env_test["GCOV_PREFIX"] = os.path.join(output_dir, test_name)
        return env_test

    def _run(self, cmd: list[str], test_name: str, env_test: dict, cwd: Path) -> tuple[bool, Exception | None]:
        try:
            stdout, errorcode, stderr = sh(cmd, cwd=cwd, env=env_test)
            if errorcode != 0:
                print(f"Test output:\n{stdout}\n{stderr}")
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

    def _configure(self, cwd: Path, coverage=False) -> bool | None:
        pass

class LibarchiveProject(Project):
    
    def __init__(self, output_dir, input_dir) -> None:
        super().__init__()
        
        self._init(output_dir, input_dir, "libarchive", "https://github.com/libarchive/libarchive")  

    def get_test_cmd(self, test_name: str) -> list[str]:
        return ["cmake", "--build", "cmake_build", "-R", test_name, "--output-on-failure"]
    
    def get_test(self) -> list[str]:
        stdout, code, stderr = sh(["cmake", "-N"], cwd=Path(self.input_dir) / "cmake_build")
        
        if code != 0:
            self.logger.error(f"Failed to get test list: {stderr}")
            return []
        
        tests = [str(line).split(":")[1] for line in stdout.splitlines() if "Test #" in line]
        return tests

    def _configure(self, cwd: Path, coverage=False) -> bool:
        cmd = ["cmake", "-B", "cmake_build", "-S", ".",
               "-DCMAKE_BUILD_TYPE=Debug",
               "-DENABLE_COVERAGE=OFF",
               "-DENABLE_TEST=ON"]

        cmd .append("-DCMAKE_C_FLAGS=-g -O0 -Wno-error=unused-variable" + (" --coverage" if coverage else ""))
        if coverage:
            cmd.append("-DCMAKE_EXE_LINKER_FLAGS=--coverage")

        _, errorcode, _ = sh(cmd, cwd=cwd)
        return errorcode == 0
    
    def _build(self, n_proc=-1):
        import os
        if os.path.exists(os.path.join(self.input_dir, "cmake_build")):
            cmd = ["cmake", "--build", "cmake_build"]
        else:
            cmd = ["make"]
            
        if n_proc == -1:
            import os
            nproc = os.cpu_count() or 1
            cmd.append(f"-j{nproc}")
        else:
            cmd.append(f"-j{n_proc}")
        
        _, errorcode, _ = sh(cmd=cmd, cwd=Path(self.input_dir))
        return errorcode == 0
        
    

class LibVNCServerProject(Project):

    def __init__(self, output_dir, input_dir) -> None:
        super().__init__()
        
        self._init(output_dir, input_dir, "libvncserver", "https://github.com/LibVNC/libvncserver")
        
        sh(["git", "submodule", "update", "--init", "--recursive"], cwd=Path(self.input_dir))
    
    def get_test_cmd(self, test_name: str) -> list[str]:
        return ["ctest", "-R", f"^{test_name}$", "--output-on-failure"]
    
    def _run(self, cmd: list[str], test_name: str, env_test: dict, cwd: Path) -> tuple[bool, Exception | None]:
        try:
            _, errorcode, _ = sh(cmd, cwd=cwd / 'cmake_build', env=env_test)
            return errorcode == 0, None
        except Exception as e:
            # self.logger.error(f"Test '{test_name}' failed with exception: {e}")
            return False, e
        
    def get_test(self) -> list[str]:
        tests = []

        # CMake logic
        build_dir = os.path.join(self.input_dir, "cmake_build")
        if os.path.exists(build_dir):
            stdout, returncode, stderr = sh(["ctest", "-N"], cwd=Path(build_dir))
            for line in stdout.splitlines():
                if "Test #" in line:
                    parts = line.split(":")
                    if len(parts) >= 2:
                        tests.append(parts[1].strip())
        # Autotools logic
        else:
            # TODO fix during the debugging session
            pass
        return tests
    
    def _build(self, n_proc=-1):
        import os
        if os.path.exists(os.path.join(self.input_dir, "cmake_build")):
            cmd = ["cmake", "--build", "cmake_build"]
        else:
            cmd = ["make"]
            
        if n_proc == -1:
            import os
            nproc = os.cpu_count() or 1
            cmd.append(f"-j{nproc}")
        else:
            cmd.append(f"-j{n_proc}")
        
        _, errorcode, _ = sh(cmd=cmd, cwd=Path(self.input_dir))
        return errorcode == 0
        
        
    def _configure(self, cwd: Path, coverage=False) -> bool:
        flags = "-O0"
        libs = ""
        
        if coverage:
            flags += " --coverage"
            libs = "-lgcov" 
        
        # [NEW] Hybrid build logic. LibVNCServer switched from Autotools to CMake over time.
        if os.path.exists(os.path.join(cwd, "CMakeLists.txt")):
            logging.info("Detected CMake build system.")
            cmake_cmd = [
                'cmake', '-B', 'cmake_build', '-S', '.',
                f'-DCMAKE_C_FLAGS="{flags}"',
                f'-DCMAKE_EXE_LINKER_FLAGS="{libs}"',
                '-DBUILD_TESTS=ON'
            ]
            stdout, returncode, stderr = sh(cmake_cmd, cwd=Path(cwd))
        else:
            logging.info("Detected Legacy Autotools build system.")
            if not os.path.exists(os.path.join(cwd, "configure")):
                sh(["autogen.sh"], cwd=Path(cwd))
            
            env_test = os.environ.copy()
            env_test["CFLAGS"] = flags
            env_test["LDFLAGS"] = flags
            env_test["LIBS"] = libs
                        
            full_cmd = ['./configure', '--enable-static']
            _, returncode, _ = sh(full_cmd, cwd=Path(cwd), env=env_test)
        return returncode == 0

class PhpSrcProject(Project):

    def __init__(self, output_dir, input_dir) -> None:
        super().__init__()
        
        self._init(output_dir, input_dir, "php-src", "https://github.com/php/php-src")

    def get_test_cmd(self, test_name: str) -> list[str]:
        return ["make", "test", f"TESTS={test_name}", "HARNESS_JOBS=1"]
        
    def _configure(self, cwd: Path, coverage=False) -> bool:
        # PHP requires buildconf to generate the configure script first
        _, returncode, err = sh(["./buildconf", "--force"], cwd=cwd)
        if returncode != 0:
            logging.error("buildconf failed")
            return False

        # Basic PHP configuration
        #config_args = [
        #    "./configure",
        #    "--disable-all", # Minimal build for speed
        #    "--enable-cli",  # Essential for running tests
        #    "--disable-cgi",
        #    "--disable-fpm",
        #    "--disable-phpdbg",
        #    "--without-pear",
        #    "--enable-filter", 
        #    "--enable-json",
        #    "--enable-tokenizer"
        #]

        config_args = [
            "./configure",
            "--prefix=/usr/local/php", 
            "--enable-cli",
            "CFLAGS=-fprofile-arcs -ftest-coverage",
            "CXXFLAGS=-fprofile-arcs -ftest-coverage"
        ]

        # env_test = os.environ.copy()
        # env_test["CFLAGS"] = "-O0 -g -w"
        # if coverage:
        #     env_test["CFLAGS"] += " --coverage"
        #     env_test["LDFLAGS"] = "--coverage"

        _, errorcode, _ = sh(cmd=config_args, cwd=cwd)
        return errorcode == 0

    def get_test(self,cwd):
        tests = []
        # PHP tests are .phpt files found in tests/, Zend/, ext/
        # We walk the directory to find them.
        tests = glob(os.path.join(cwd, "**/*.phpt"), recursive=True)
        tests = [os.path.splitext(os.path.basename(t))[0] for t in tests]
        #for root, dirs, files in os.walk(cwd):
        #    for f in files:
        #        if f.endswith(".phpt"):
        #            t_name = f[:-5] # remove .phpt
        #            rel_path = os.path.relpath(os.path.join(root, f), cwd)

        #            # Command to run a single test via make
        #            # NO_INTERACTION=1 prevents it from asking to send reports to PHP.net
        #            # TESTS=path points to the specific file
        #            cmd = f"NO_INTERACTION=1 make test TESTS='{rel_path}'"

        #            tests.append({
        #                "name": t_name,
        #                "cmd": cmd,
        #                "type": "phpt"
        #            })

        return tests

class OpenSSLProject(Project):

    def __init__(self, output_dir, input_dir) -> None:
        super().__init__()

        self._init(output_dir, input_dir, "openssl", "https://github.com/openssl/openssl") 

    def get_test_cmd(self, test_name: str) -> list[str]:
        return ["make", "test", f"TESTS={test_name}", "HARNESS_JOBS=1"]
        
    def get_test(self) -> list[str]:
        out, _, _ = sh(['make', 'list-tests'], cwd=Path(self.input_dir))
        return [line.strip().split()[0]
            for line in out.splitlines()
            if line.strip() and not line.lstrip().startswith(("make", "Files=", "Tests=", "Result:"))]
    
    def _get_configure_cmd(self, coverage=True) -> str:
        return "./config no-asm -g -O0 --coverage"
        
    
    def _configure(self, cwd: Path, coverage=False) -> bool:
        config_args = ["./config", "--debug", "no-shared"]
        # config_args = ["./config", "no-asm", "-g", "-O0"] 
        if coverage:
            config_args.append("--coverage")
        
        _, errorcode, err = sh(config_args, cwd=cwd)
        #if errorcode != 0:
        #    self.logger.warning(f"Trying fallback configuration due to exception: {err}")
        #    env_backup = os.environ.copy()
        #    env_backup['CFLAGS']="-O0 -g -fprofile-arcs -ftest-coverage"
        #    env_backup["LDFLAGS"]="--coverage"
        #    config_args = ["./Configure", "linux-x86_64", "no-asm"]
        #    _, errorcode, err = sh(config_args, cwd=cwd, env=env_backup)
        #    if errorcode !=0:
        #        self.logger.error(f"Fallback configuration also failed due to exception: {err}")
        #        return False
        
        self.logger.info(f"Configuration succeeded: {errorcode == 0}.")
        return errorcode == 0
    
class VimProject(Project):

    TEST_DIR = "src/testdir"

    def __init__(self, output_dir, input_dir):
        super().__init__()
        
        self._init(output_dir, input_dir, "vim", "https://github.com/vim/vim")

    def _prepare_env_for_testing(self, test_name: str) -> dict:
        output_dir = os.path.join(self.output_dir, Project.GCDA_FOLDER)
        sh(["mkdir", "-p", output_dir])
        env_test = os.environ.copy()
        env_test["GCOV_PREFIX_STRIP"] = "3"
        env_test["GCOV_PREFIX"] = os.path.join(output_dir, test_name)
        env_test["LINES"] = "24"
        env_test["COLUMNS"] = "80"
        return env_test

    def get_test_cmd(self, test_name: str) -> list[str]:
        return ["make", test_name, "HARNESS_JOBS=1", "LINES=24", "COLUMNS=80"]

    def get_test(self) -> list[str]:
        test_dir = Path(self.input_dir) / VimProject.TEST_DIR

        test_names = glob(f"{test_dir}/test_*.vim") 
        test_names += glob(f"{test_dir}/test_*.in")

        test_names = [t.replace(f"{test_dir}/", "") for t in test_names]
        test_names = [t.replace(".in", "") for t in test_names]
        test_names = [t.replace(".vim", "") for t in test_names]
        return test_names
        
    def run_test(self, test_name: str) -> tuple[bool, Exception | None]:
        env_test = self._prepare_env_for_testing(test_name)
        cmd = self.get_test_cmd(test_name)
        # Wrap the command with script to create a pseudo-terminal with proper size
        # script -q -e -c "command" /dev/null creates a PTY that vim can query
        # set rows and cols to 24x80 to avoid test failures due to terminal size
        cmd_str = '"stty rows 24 cols 80;'
        cmd_str += " ".join(cmd)
        cmd_str += '"'
        wrapped_cmd = ["script", "-q", "-e", "-c", f"{cmd_str}", "/dev/null"]
        return self._run(wrapped_cmd, test_name, env_test, cwd=Path(self.input_dir) / "src")
    
    def _run(self, cmd: list[str], test_name: str, env_test: dict, cwd: Path) -> tuple[bool, Exception | None]:
        try:
            stdout, errorcode, stderr = sh(cmd, cwd=cwd, env=env_test)
            if errorcode != 0:
                print(f"Test output:\n{stdout}\n{stderr}")
            return errorcode == 0, None
        except Exception as e:
            return False, e
    
    def _configure(self, cwd, coverage=False) -> bool:
        # Vim configure
        # --with-features=huge: Enables most features (needed for many tests)
        # --enable-gui=no --without-x: Disables GUI (Critical for Docker)
        config_args = [
            "./configure",
            "--with-features=huge",
            "--enable-gui=no",
            "--without-x"
        ]
        
        # env_test = os.environ.copy()
        # env_test["CFLAGS"] = "-O0 "
        # if coverage:
        #     env_test["CFLAGS"] += "--coverage "
        #     # Often helpful to link coverage too
        #     env_test["LDFLAGS"] = "--coverage "

        _, errorcode, err = sh(config_args, cwd=cwd)
        if errorcode != 0:
            self.logger.error(f"Configuration failed with error: {err}")
            return False
        
        self.logger.info("Configuration succeeded with standard ./config.")
        return True
    
    def _build(self, n_proc=1):
        cmd = ["make"]
        cmd_args = ['PROFILE_CFLAGS="-g -O0 -fprofile-arcs -ftest-coverage -DWE_ARE_PROFILING -DUSE_GCOV_FLUSH"', 'LDFLAGS="--coverage"']
        cmd += cmd_args
        if n_proc == -1:
            import os
            nproc = os.cpu_count() or 1
            cmd.append(f"-j{nproc}")
        else:
            cmd.append(f"-j{n_proc}")
        
        _, errorcode, _ = sh(cmd=cmd, cwd=Path(self.input_dir))
        return errorcode == 0
    
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

    def get_test_cmd(self, test_name: str) -> list[str]:
        return ["make", test_name, "HARNESS_JOBS=1"]

    def get_test(self) -> list[str]:
        out, _, _ = sh(['make', '-s', 'fate-list'], cwd=Path(self.input_dir))
        return [line.strip().split()[0]
            for line in out.splitlines()
            if line.strip() and not line.lstrip().startswith(("make", "Files=", "Tests=", "Result:", "GEN"))]
    
    def _configure(self, cwd, coverage=False) -> bool:
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
    
class LibRawProject(Project):
    
    raw_sample: str
    def __init__(self, output_dir, input_dir):            
        super().__init__()
        
        self._init(output_dir, input_dir, "libraw","https://github.com/LibRaw/LibRaw.git")
        self.raw_sample = os.path.join(self.input_dir, "sample.cr2")

    def get_test_cmd(self, test_name: str) -> list[str]:
        return ["make", test_name, "HARNESS_JOBS=1"]
    
    def get_test(self):
        return sh(['raw-identify', self.raw_sample], cwd=Path('./bin'))