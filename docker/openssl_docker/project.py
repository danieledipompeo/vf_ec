# create an abstract class for projects
from abc import ABC, abstractmethod
from glob import glob
import os
from pathlib import Path
import sys

from common import GitHandler, EnergyHandler, sh
from logger import get_logger

logger = get_logger(__name__)

CMAKE_BUILD_DIR = "cmake_build"

class ProjectFactory:
    
    @staticmethod
    def get_project(name: str, input_dir: str, output_dir: str) -> 'Project':
        if name.lower() == "openssl":
            return OpenSSLProject(output_dir, input_dir)
        # elif name.lower() == "ffmpeg":
            # return FfmpegProject(output_dir, input_dir)
        elif name.lower() == "vim":
            return VimProject(output_dir, input_dir)
        # elif name.lower() == "php-src":
        #     return PhpSrcProject(output_dir, input_dir) 
        # elif name.lower() == 'libraw':
            # return LibRawProject(output_dir, input_dir)
        # elif name.lower() == "libvncserver":
        #     return LibVNCServerProject(output_dir, input_dir)
        elif name.lower() == "libarchive":
            return LibarchiveProject(output_dir, input_dir)
        elif name.lower() == "curl":
            return CurlProject(output_dir, input_dir)
        else:
            raise ValueError(f"Unknown project: {name}")


class Project(ABC):
    
    name = "generic_project"
    output_dir = "./output/generic_project"
    input_dir = "./input/generic_project"
    build_dir = "./input/generic_project/tests"

    GCDA_FOLDER = "coverage-per-test"

    def _init(self, output_dir: str, input_dir: str, name: str, project_repo: str) -> None:
        super().__init__()
        self.logger = logger
        
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

    @abstractmethod
    def get_test_cmd(self, test_name: str, coverage=True) -> list[str]:
        pass

    def run_test(self, test_name: str, coverage=True) -> tuple[bool, dict]:
        # env_test = self._prepare_env_for_testing(test_name)
        cmd = self.get_test_cmd(test_name, coverage=coverage) #= ["make", test_name, "HARNESS_JOBS=1"]
        return self._run(cmd)#, env_test)

    @abstractmethod
    def compute_energy(self, test_name: str, commit: str):
        pass 

    def _clean(self):
        sh(cmd=["make", "clean"], cwd=Path(self.input_dir))

    def _build(self, n_proc=-1, coverage=False) -> bool:
        cmd = ["make"]
        if n_proc == -1:
            import os
            nproc = os.cpu_count() or 1
            cmd.append(f"-j{nproc}")
        else:
            cmd.append(f"-j{n_proc}")
        
        _, errorcode, _ = sh(cmd=cmd, cwd=Path(self.input_dir))
        return errorcode == 0

    def _resolve_source_path(self, reported_file: str, objdir: Path) -> Path:
        p = Path(reported_file)
        if p.is_absolute():
            return p
        return (objdir / p).resolve()

    def _extract_covered_file(self, stdout: str, obj_dir: Path) -> str | None:
        for line in stdout.splitlines():
            if line.startswith("File"):
                source_file = line.split("File ",1)[-1].replace("'", "")
                real_path = self._resolve_source_path(source_file, obj_dir)
                return str(real_path)

    def coverage_file(self, test_name: str) -> list[str]:
        building_dir = Path(self.build_dir)
        gco_files = building_dir.rglob("*.gcda")
        covered = []
        for file in gco_files:
            obj_dir = file.parent
            stdout, code, stderr = sh(["gcov", "-n", "-o", str(obj_dir), str(file)], cwd=building_dir)
            if code != 0:
                self.logger.error(f"gcov failed for {file} with error: {stderr}")
                continue
            
            covered_file = self._extract_covered_file(stdout, obj_dir)
            if covered_file:
                covered_file = str(Path(covered_file).relative_to(self.input_dir))
                covered.append(covered_file)
                
        return covered

    def _run(self, cmd: list[str]) -> tuple[bool, dict]:
        stdout, errorcode, stderr = sh(cmd, cwd=Path(self.build_dir))
        if errorcode != 0:
            self.logger.error(f"Test output:\n{stdout}\n{stderr}")
        return errorcode == 0, {"stdout": stdout, "stderr": stderr, "errorcode": errorcode}

    def build(self, coverage=False, n_proc=1) -> bool:
        if os.path.exists(os.path.join(self.input_dir, "Makefile")):
            self._clean()
        if not self._configure(cwd=Path(self.input_dir), coverage=coverage):
            self.logger.error("Configuration failed, cannot build.")
            return False
        return self._build(n_proc=n_proc, coverage=coverage)

    def _configure(self, cwd: Path, coverage=False) -> bool | None:
        pass


class CurlProject(Project):
    
    def __init__(self, output_dir, input_dir) -> None:
        super().__init__()
        
        self._init(output_dir, input_dir, "curl", "https://github.com/curl/curl")
        # self.build_dir = os.path.join(self.input_dir, "tests")
        # if os.path.exists(os.path.join(self.input_dir, "CMakeLists.txt")):  
            # self.build_dir = os.path.join(self.input_dir, CMAKE_BUILD_DIR)
    
    def coverage_file(self, test_name: str) -> list[str]:
        building_dir = Path(self.input_dir)
        if os.path.exists(os.path.join(self.input_dir, CMAKE_BUILD_DIR)):
            building_dir = Path(self.build_dir)

        gco_files = building_dir.rglob("*.gcda")
        covered = []
        for file in gco_files:
            obj_dir = file.parent # Folder containing the .gcda|gcno|o files
            stdout, code, stderr = sh(["gcov", "-n", "-o", str(obj_dir), str(file)], cwd=building_dir)
            if code != 0:
                self.logger.error(f"gcov failed for {file} with error: {stderr}")
                continue
            
            covered_file = self._extract_covered_file(stdout, obj_dir)
            if covered_file:
                covered_file = str(Path(covered_file).relative_to(self.input_dir))
                covered.append(covered_file)
                
        return covered
    
    def _configure(self, cwd: Path, coverage=False) -> bool:
        if os.path.exists(os.path.join(cwd, "CMakeLists.txt")):
            self.logger.info("Running CMake configuration for curl.")
            cmd = ["cmake", "-S", ".", "-B", CMAKE_BUILD_DIR, 
                   "-DCMAKE_BUILD_TYPE=Debug", 
                   "-DENABLE_CURL_MANUAL=OFF", 
                   "-DENABLE_TESTS=ON", 
                   "-DENABLE_CURL_DEBUG=ON", 
                   f"-DENABLE_CODE_COVERAGE={'ON' if coverage else 'OFF'}"]
        else:
            cmd = ["./buildconf"]
            self.logger.info("Running legacy Autotools configuration for curl.")
            _, errorcode, _ = sh(cmd, cwd=cwd)
            if errorcode != 0:
                self.logger.error("Autotools buildconf failed.")
                return False
            
            cmd =["./configure",
                  "--enable-debug",
                  "--disable-manual",
                  "--enable-http",
                  "--with-ssl",
                  "--with-zlib"
                 ]
            if  coverage:
                cmd += [
                  "--disable-shared",
                  "CFLAGS='-O0 -g --coverage -fprofile-arcs -ftest-coverage'",
                  "LDFLAGS='--coverage'"]

        _, errorcode, _ = sh(cmd, cwd=cwd)
        return errorcode == 0
    
    def _build(self, n_proc=-1, coverage=False):
        if os.path.exists(os.path.join(self.input_dir, CMAKE_BUILD_DIR)):
            cmd = ["cmake", "--build", CMAKE_BUILD_DIR]
            self.build_dir = os.path.join(self.input_dir, CMAKE_BUILD_DIR)
            _, errorcode, _ = sh(cmd=cmd, cwd=Path(self.input_dir))
            return errorcode == 0

        self.build_dir = os.path.join(self.input_dir, "tests")
        cmd = ["make"]
            
        if n_proc == -1:
            nproc = os.cpu_count() or 1
            cmd.append(f"-j{nproc}")
        else:
            cmd.append(f"-j{n_proc}")
        
        _, errorcode, _ = sh(cmd=cmd, cwd=Path(self.input_dir))
        if errorcode != 0:
            return False
        
        self.logger.debug("Building tests for curl.") 
        _, errorcode, _ = sh(cmd=["make", "-C", "tests"], cwd=Path(self.input_dir))
        return errorcode == 0
    
    def get_test_cmd(self, test_name: str, coverage=True) -> list[str]:
        if os.path.exists(os.path.join(self.input_dir, CMAKE_BUILD_DIR)):
            cmd = ["ctest", "-R", f"^{test_name}$", "--output-on-failure"]
        else:
            cmd = ["./runtests.pl", f"{test_name.replace('test', '')}"]
            
        return cmd

    def get_test(self) -> list[str]:
        tests = []
        # CMake logic
        build_dir = os.path.join(self.input_dir, CMAKE_BUILD_DIR)
        if os.path.exists(build_dir):
            stdout, returncode, stderr = sh(["ctest", "-N"], cwd=Path(build_dir))
            for line in stdout.splitlines():
                if "Test #" in line:
                    parts = line.split(":")
                    if len(parts) >= 2:
                        tests.append(parts[1].strip())
        # Autotools logic
        else:
            test_dir = Path(self.input_dir) / "tests" 
            test_data_dir = test_dir / "data"
            tests = list(test_data_dir.rglob("test*"))
            tests = [t.name for t in tests]
        
        return tests
    
    def compute_energy(self, test_name: str, commit: str):
        os.makedirs(os.path.join(self.output_dir, "energy_measurements"), exist_ok=True)
        cmd = self.get_test_cmd(test_name, coverage=False)
        out_filename = os.path.join(self.output_dir, "energy_measurements", f"{commit}__{test_name}_energy")
                                    
        EnergyHandler.measure_test(test_name, cmd=cmd,
                                   output_filename=out_filename,
                                   test_dir=self.build_dir)
        
class LibarchiveProject(Project):
    
    def __init__(self, output_dir, input_dir) -> None:
        super().__init__()
        
        self._init(output_dir, input_dir, "libarchive", "https://github.com/libarchive/libarchive")  
        self.build_dir = os.path.join(self.input_dir, CMAKE_BUILD_DIR)

    def compute_energy(self, test_name: str, commit: str):
        os.makedirs(os.path.join(self.output_dir, "energy_measurements"), exist_ok=True)
        cmd = self.get_test_cmd(test_name, coverage=False)
        out_filename = os.path.join(self.output_dir, "energy_measurements", f"{commit}__{test_name}_energy")
                                    
        EnergyHandler.measure_test(test_name, cmd=cmd,
                                   output_filename=out_filename,
                                   test_dir=self.build_dir)

    def _run(self, cmd: list[str]) -> tuple[bool, dict]:
        stdout, errorcode, stderr = sh(cmd, cwd=Path(self.build_dir))
        return errorcode == 0, {"stdout": stdout, "stderr": stderr, "errorcode": errorcode}
    
    def get_test_cmd(self, test_name: str, coverage=True) -> list[str]:
        return ["ctest", "-R", f"^{test_name}$", "--output-on-failure"]
    
    def get_test(self) -> list[str]:
        stdout, code, stderr = sh(["ctest", "-N"], cwd=Path(self.build_dir))
        
        if code != 0:
            self.logger.error(f"Failed to get test list: {stderr}")
            return []
        
        tests = [str(line).split(":")[1].strip() for line in stdout.splitlines() if "Test #" in line]
        return tests

    def _configure(self, cwd: Path, coverage=False) -> bool:
        cmd = ["cmake", "-B", CMAKE_BUILD_DIR, "-S", ".",
               "-DCMAKE_BUILD_TYPE=Debug",
               "-DENABLE_COVERAGE=OFF",
               "-DENABLE_TEST=ON"]

        dcmake_c_flags = '-DCMAKE_C_FLAGS="-g -O0 -w"'
        if coverage:
            cmd.append('-DCMAKE_EXE_LINKER_FLAGS="-fprofile-arcs -ftest-coverage"')
            dcmake_c_flags = '-DCMAKE_C_FLAGS="-g -O0 -w -fprofile-arcs -ftest-coverage"'

        cmd.append(dcmake_c_flags)

        _, errorcode, _ = sh(cmd, cwd=cwd)
        return errorcode == 0
    
    def _build(self, n_proc=-1, coverage=False):
        import os
        if os.path.exists(os.path.join(self.input_dir, CMAKE_BUILD_DIR)):
            cmd = ["cmake", "--build", CMAKE_BUILD_DIR]
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


class OpenSSLProject(Project):
    
    CFLAG_COVERAGE="-fPIC -DOPENSSL_PIC -DOPENSSL_THREADS -D_REENTRANT -DDSO_DLFCN -DHAVE_DLFCN_H -m64 -DL_ENDIAN -DTERMIO -O0 -Wall -DMD32_REG_T=int --coverage"
    SHARED_LDFLAGS="-m64 --coverage"
    EX_LDL="-ldl --coverage"
    
    CFLAG = [f'CFLAG="{CFLAG_COVERAGE}"']
    LDFLAGS = [f'LDFLAGS="{SHARED_LDFLAGS}"']
    EX_LIBS = [f'EX_LIBS="{EX_LDL}"']
    
    def __init__(self, output_dir, input_dir) -> None:
        super().__init__()

        self._init(output_dir, input_dir, "openssl", "https://github.com/openssl/openssl") 
        self.test_dir = self.input_dir
        self.build_dir = self.input_dir

    def get_test_cmd(self, test_name: str, coverage=False) -> list[str]:
        cmd = ["make", "test"]
        if coverage:
            cmd += OpenSSLProject.CFLAG
            cmd += OpenSSLProject.LDFLAGS
            cmd += OpenSSLProject.EX_LIBS
        
        cmd += ["TESTS=" + test_name, "HARNESS_JOBS=1"]
        return cmd
        
    def get_test(self) -> list[str]:
        if os.path.exists(os.path.join(self.test_dir, "test", "recipes")):
            out, _, _ = sh(['make', 'list-tests'], cwd=Path(self.build_dir))
            tests = [line.strip() for line in out.splitlines() if line.strip()]
        else:
            tests = glob(os.path.join(self.test_dir, "test", "*.c"))    
            tests = [os.path.splitext(os.path.basename(t))[0] for t in tests] 
        
        return tests 
    
    def _build(self, n_proc=1, coverage=False):
        cmd = ["make"]
        
        if coverage:
            cmd += OpenSSLProject.CFLAG
            cmd += OpenSSLProject.LDFLAGS
            cmd += OpenSSLProject.EX_LIBS
        
        if n_proc == -1:
            cmd.append(f"-j")
        else:
            cmd.append(f"-j{n_proc}")
        _, errorcode, _ = sh(cmd=cmd, cwd=Path(self.input_dir))
        return errorcode == 0
    
    def _configure(self, cwd: Path, coverage=False) -> bool:
        # Use shared libraries with proper configuration for linking
        config_args = ["./Configure", "linux-x86_64", "shared", "no-asm"]
        _, errorcode, err = sh(config_args, cwd=cwd)
        
        if errorcode != 0:
            self.logger.error(f"Configuration failed with error: {err}")
            return False
        
        self.logger.info("Configuration succeeded.")
        return True

    def compute_energy(self, test_name: str, commit: str):
        os.makedirs(os.path.join(self.output_dir, "energy_measurements"), exist_ok=True)
        cmd = self.get_test_cmd(test_name, coverage=False)
        out_filename = os.path.join(self.output_dir, "energy_measurements", f"{commit}__{test_name}_energy")
        
        EnergyHandler.measure_test(test_name, cmd=cmd,
                                   output_filename=out_filename,
                                   test_dir=self.build_dir)
    
class VimProject(Project):

    TEST_DIR = "src/testdir"
    SOURCE_DIR = "src"

    def __init__(self, output_dir, input_dir):
        super().__init__()
        
        self._init(output_dir, input_dir, "vim", "https://github.com/vim/vim")
        self.test_dir = os.path.join(self.input_dir, VimProject.TEST_DIR)
        self.input_dir = os.path.join(self.input_dir, VimProject.SOURCE_DIR)
        self.build_dir = self.input_dir

    # FIX: vim coverage file extraction 
    def coverage_file(self, test_name: str) -> list[str]:
        building_dir = Path(self.build_dir)
        gco_files = building_dir.rglob("*.gcda")
        covered = []
        for file in gco_files:
            obj_dir = file.parent
            stdout, code, stderr = sh(["gcov", "-n", "-o", str(obj_dir), str(file)], cwd=building_dir)
            if code != 0:
                self.logger.error(f"gcov failed for {file} with error: {stderr}")
                continue
            
            covered_file = self._extract_covered_file(stdout, obj_dir)
            if covered_file:
                covered_file = str(Path(VimProject.SOURCE_DIR) / Path(covered_file).relative_to(obj_dir))
                covered.append(covered_file)
                
        return covered

    def get_test_cmd(self, test_name: str, coverage=True) -> list[str]:
        cmd = ["make", test_name, "HARNESS_JOBS=1", "LINES=24", "COLUMNS=80"]
        # Wrap the command with script to create a pseudo-terminal with proper size
        # script -q -e -c "command" /dev/null creates a PTY that vim can query
        # set rows and cols to 24x80 to avoid test failures due to terminal size
        cmd_str = '"stty rows 24 cols 80;'
        cmd_str += " ".join(cmd)
        cmd_str += '"'
        return ["script", "-q", "-f", "-e", "-c", f"{cmd_str}", "/dev/null"]

    def get_test(self) -> list[str]:
        test_dir = Path(self.test_dir)

        test_names = glob(f"{test_dir}/test_*.vim") 
        test_names += glob(f"{test_dir}/test_*.in")

        test_names = [t.replace(f"{test_dir}/", "") for t in test_names]
        test_names = [t.replace(".in", "") for t in test_names]
        test_names = [t.replace(".vim", "") for t in test_names]
        return test_names
        
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
        
        _, errorcode, err = sh(config_args, cwd=cwd)
        if errorcode != 0:
            self.logger.error(f"Configuration failed with error: {err}")
            return False
        
        self.logger.info("Configuration succeeded with standard ./config.")
        return True
    
    def _build(self, n_proc=1, coverage=False):
        cmd = ["make"]

        if coverage:
            cmd += ['PROFILE_CFLAGS="-g -O0 -fprofile-arcs -ftest-coverage -DWE_ARE_PROFILING -DUSE_GCOV_FLUSH"', 'LDFLAGS="--coverage"']

        if n_proc == -1:
            cmd.append(f"-j")
        else:
            cmd.append(f"-j{n_proc}")
        
        _, errorcode, _ = sh(cmd=cmd, cwd=Path(self.input_dir))
        return errorcode == 0

    def compute_energy(self, test_name: str, commit: str):
        os.makedirs(os.path.join(self.output_dir, "energy_measurements"), exist_ok=True)
        cmd = self.get_test_cmd(test_name, coverage=False)
        out_filename = os.path.join(self.output_dir, "energy_measurements", f"{commit}__{test_name}_energy")

        EnergyHandler.measure_test(test_name, cmd=cmd,
                                   output_filename=out_filename,
                                   test_dir=self.test_dir)
    
#class FfmpegProject(Project):
#
#    def __init__(self, output_dir, input_dir):            
#        super().__init__()
#        
#        self._init(output_dir, input_dir, "FFmpeg","https://github.com/FFmpeg/FFmpeg.git")
#        
#        samples_dir = os.path.join(output_dir, self.name, "fate_suite")
#        if not os.path.exists(samples_dir) or not os.listdir(samples_dir):
#            self.logger.info("FATE Samples not found. Downloading ...")
#            try:
#                # Create dir
#                os.makedirs(samples_dir, exist_ok=True)
#                # Standard FFmpeg FATE rsync command
#                cmd = ["rsync", "-aL", "rsync://fate-suite.ffmpeg.org/fate-suite/", f"{samples_dir}/"]
#                sh(cmd, input_dir)
#                self.logger.info("FATE Samples download complete.")
#            except Exception as e:
#                self.logger.error(f"Failed to download FATE samples: {e}")
#                sys.exit(1)
#
#    def get_test_cmd(self, test_name: str, coverage=False) -> list[str]:
#        return ["make", test_name, "HARNESS_JOBS=1"]
#
#    def get_test(self) -> list[str]:
#        out, _, _ = sh(['make', '-s', 'fate-list'], cwd=Path(self.input_dir))
#        return [line.strip().split()[0]
#            for line in out.splitlines()
#            if line.strip() and not line.lstrip().startswith(("make", "Files=", "Tests=", "Result:", "GEN"))]
#    
#    def _configure(self, cwd, coverage=False) -> bool:
#        config_args = ["./configure", "--disable-asm", "--disable-doc"]
#        if coverage:
#            config_args.append("--extra-cflags=--coverage")
#            config_args.append("--extra-ldflags=--coverage")
#        _, errorcode, err = sh(config_args, cwd=cwd)
#        if errorcode != 0:
#            self.logger.error(f"Configuration failed with error: {err}")
#            return False
#        
#        self.logger.info("Configuration succeeded with standard ./config.")
#        return True
#    
#    def compute_energy(self, test_name: str, commit: str):
#        os.makedirs(os.path.join(self.output_dir, "energy_measurements"), exist_ok=True)
#        cmd = self.get_test_cmd(test_name, coverage=False)
#        out_filename = os.path.join(self.output_dir, "energy_measurements", f"{commit}__{test_name}_energy")
#
#        EnergyHandler.measure_test(test_name, cmd=cmd,
#                                   output_filename=out_filename,
#                                   test_dir=self.build_dir) 
    
# class LibRawProject(Project):
    
#     raw_sample: str
#     def __init__(self, output_dir, input_dir):            
#         super().__init__()
        
#         self._init(output_dir, input_dir, "libraw","https://github.com/LibRaw/LibRaw.git")
#         self.raw_sample = os.path.join(self.input_dir, "sample.cr2")

#     def get_test_cmd(self, test_name: str) -> list[str]:
#         return ["make", test_name, "HARNESS_JOBS=1"]
    
#     def get_test(self):
#         return sh(['raw-identify', self.raw_sample], cwd=Path('./bin'))
    
# class LibVNCServerProject(Project):
# 
    # def __init__(self, output_dir, input_dir) -> None:
        # super().__init__()
        # 
        # self._init(output_dir, input_dir, "libvncserver", "https://github.com/LibVNC/libvncserver")
        # 
        # sh(["git", "submodule", "update", "--init", "--recursive"], cwd=Path(self.input_dir))
        # self.test_dir = os.path.join(self.input_dir, CMAKE_BUILD_DIR)
    # 
    # def get_test_cmd(self, test_name: str) -> list[str]:
        # return ["ctest", "-R", f"^{test_name}$", "--output-on-failure"]
    # 
    # def _run(self, cmd: list[str], env_test: dict, cwd: Path) -> tuple[bool, Exception | None]:
        # try:
            # _, errorcode, _ = sh(cmd, cwd=Path(self.test_dir), env=env_test)
            # return errorcode == 0, None
        # except Exception as e:
            # self.logger.error(f"Test '{test_name}' failed with exception: {e}")
            # return False, e
        # 
    # def get_test(self) -> list[str]:
        # tests = []
# 
        # CMake logic
        # build_dir = os.path.join(self.input_dir, CMAKE_BUILD_DIR)
        # if os.path.exists(build_dir):
            # stdout, returncode, stderr = sh(["ctest", "-N"], cwd=Path(build_dir))
            # for line in stdout.splitlines():
                # if "Test #" in line:
                    # parts = line.split(":")
                    # if len(parts) >= 2:
                        # tests.append(parts[1].strip())
        # Autotools logic
        # else:
            # TODO fix during the debugging session
            # pass
        # return tests
    # 
    # def _build(self, n_proc=-1):
        # import os
        # if os.path.exists(os.path.join(self.input_dir, CMAKE_BUILD_DIR)):
            # cmd = ["cmake", "--build", CMAKE_BUILD_DIR]
        # else:
            # cmd = ["make"]
            # 
        # if n_proc == -1:
            # import os
            # nproc = os.cpu_count() or 1
            # cmd.append(f"-j{nproc}")
        # else:
            # cmd.append(f"-j{n_proc}")
        # 
        # _, errorcode, _ = sh(cmd=cmd, cwd=Path(self.input_dir))
        # return errorcode == 0
        # 
    # def _configure(self, cwd: Path, coverage=False) -> bool:
        # flags = "-O0"
        # libs = ""
        # 
        # if coverage:
            # flags += " --coverage"
            # libs = "-lgcov" 
        # 
        # [NEW] Hybrid build logic. LibVNCServer switched from Autotools to CMake over time.
        # if os.path.exists(os.path.join(cwd, "CMakeLists.txt")):
            # logger.info("Detected CMake build system.")
            # cmake_cmd = [
                # 'cmake', '-B', CMAKE_BUILD_DIR, '-S', '.',
                # f'-DCMAKE_C_FLAGS="{flags}"',
                # f'-DCMAKE_EXE_LINKER_FLAGS="{libs}"',
                # '-DBUILD_TESTS=ON'
            # ]
            # stdout, returncode, stderr = sh(cmake_cmd, cwd=Path(cwd))
        # else:
            # logger.info("Detected Legacy Autotools build system.")
            # if not os.path.exists(os.path.join(cwd, "configure")):
                # sh(["autogen.sh"], cwd=Path(cwd))
            # 
            # env_test = os.environ.copy()
            # env_test["CFLAGS"] = flags
            # env_test["LDFLAGS"] = flags
            # env_test["LIBS"] = libs
                        # 
            # full_cmd = ['./configure', '--enable-static']
            # _, returncode, _ = sh(full_cmd, cwd=Path(cwd), env=env_test)
        # return returncode == 0


# class PhpSrcProject(Project):

#     def __init__(self, output_dir, input_dir) -> None:
#         super().__init__()
#         
#         self._init(output_dir, input_dir, "php-src", "https://github.com/php/php-src")

#     def get_test_cmd(self, test_name: str) -> list[str]:
#         return ["make", "test", f"TESTS={test_name}", "HARNESS_JOBS=1"]
#         
#     def _configure(self, cwd: Path, coverage=False) -> bool:
#         # PHP requires buildconf to generate the configure script first
#         _, returncode, err = sh(["./buildconf", "--force"], cwd=cwd)
#         if returncode != 0:
#             logger.error("buildconf failed")
#             return False

#         # Basic PHP configuration
#         #config_args = [
#         #    "./configure",
#         #    "--disable-all", # Minimal build for speed
#         #    "--enable-cli",  # Essential for running tests
#         #    "--disable-cgi",
#         #    "--disable-fpm",
#         #    "--disable-phpdbg",
#         #    "--without-pear",
#         #    "--enable-filter", 
#         #    "--enable-json",
#         #    "--enable-tokenizer"
#         #]

#         config_args = [
#             "./configure",
#             "--prefix=/usr/local/php", 
#             "--enable-cli",
#             "CFLAGS=-fprofile-arcs -ftest-coverage",
#             "CXXFLAGS=-fprofile-arcs -ftest-coverage"
#         ]

#         # env_test = os.environ.copy()
#         # env_test["CFLAGS"] = "-O0 -g -w"
#         # if coverage:
#         #     env_test["CFLAGS"] += " --coverage"
#         #     env_test["LDFLAGS"] = "--coverage"

#         _, errorcode, _ = sh(cmd=config_args, cwd=cwd)
#         return errorcode == 0

#     def get_test(self,cwd):
#         tests = []
#         # PHP tests are .phpt files found in tests/, Zend/, ext/
#         # We walk the directory to find them.
#         tests = glob(os.path.join(cwd, "**/*.phpt"), recursive=True)
#         tests = [os.path.splitext(os.path.basename(t))[0] for t in tests]
#         #for root, dirs, files in os.walk(cwd):
#         #    for f in files:
#         #        if f.endswith(".phpt"):
#         #            t_name = f[:-5] # remove .phpt
#         #            rel_path = os.path.relpath(os.path.join(root, f), cwd)

#         #            # Command to run a single test via make
#         #            # NO_INTERACTION=1 prevents it from asking to send reports to PHP.net
#         #            # TESTS=path points to the specific file
#         #            cmd = f"NO_INTERACTION=1 make test TESTS='{rel_path}'"

#         #            tests.append({
#         #                "name": t_name,
#         #                "cmd": cmd,
#         #                "type": "phpt"
#         #            })

#         return tests