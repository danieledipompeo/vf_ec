# create an abstract class for projects
from abc import ABC, abstractmethod
from glob import glob
import json
import os
from pathlib import Path
import re

from common import GitHandler, EnergyHandler, sh
from logger import get_logger
from extract_coverage_data import generate_gcovr_json, parse_gcovr_covered_lines

logger = get_logger(__name__)

CMAKE_BUILD_DIR = "cmake_build"

class ProjectFactory:
    """Factory for creating project instances based on project name."""
    
    @staticmethod
    def get_project(name: str, input_dir: str, output_dir: str) -> 'Project':
        """
        Create and return a project instance.
        
        Args:
            name: Project name (case-insensitive). Supported: openssl, vim, libarchive, 
                  curl, libxml2, imagemagick.
            input_dir: Root directory for input/source code.
            output_dir: Root directory for output/results.
        
        Returns:
            A Project subclass instance.
        
        Raises:
            ValueError: If project name is not recognized.
        """
        projects = {
            "openssl": OpenSSLProject,
            "vim": VimProject,
            "libarchive": LibarchiveProject,
            "jasper": JasperProject,
            "curl": CurlProject,
            "libxml2": LibXML2Project,
            "imagemagick": ImageMagickProject,
            "tcpdump": TcpDumpProject,
            "qemu": QEMUProject,
            "ffmpeg": FFmpegProject,
        }
        project_cls = projects.get(name.lower())
        if project_cls is None:
            raise ValueError(f"Unknown project: {name}")
        return project_cls(output_dir, input_dir)


class Project(ABC):
    """Abstract base class for managing project builds, tests, and energy measurements.
    
    Handles common functionality for cloning, building, testing, and measuring energy consumption
    across multiple open-source projects with different build systems (CMake, Autotools, Make).
    Subclasses customize behavior through template methods and hooks.
    """
    
    GCDA_FOLDER = "coverage-per-test"

    def __init__(self, output_dir: str, input_dir: str, name: str, project_repo: str) -> None:
        super().__init__()
        self.logger = logger
        self.name = name
        
        self.output_dir = Path(output_dir) / self.name
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        self.input_dir = Path(input_dir) / self.name
        if not (self.input_dir / ".git").exists():
            GitHandler.clone_repo(input_dir, project_repo)
        
        self.build_dir = self.input_dir / "tests"
        
    @abstractmethod
    def get_test(self):
        pass

    @abstractmethod
    def get_test_cmd(self, test_name: str, coverage=True) -> list[str]:
        pass

    def run_test(self, test_name: str, coverage=True) -> tuple[bool, dict]:
        cmd = self.get_test_cmd(test_name, coverage=coverage)
        return self._run(cmd)

    def compute_energy(self, test_name: str, commit: str):
        """Default implementation for energy measurement."""
        energy_dir = self.output_dir / "energy_measurements"
        energy_dir.mkdir(parents=True, exist_ok=True)
        cmd = self.get_test_cmd(test_name, coverage=False)
        out_filename = str(energy_dir / f"{commit}__{test_name}_energy")
        test_dir = self._get_test_dir_for_energy()
        
        EnergyHandler.measure_test(test_name, cmd=cmd,
                                   output_filename=out_filename,
                                   test_dir=str(test_dir))
    
    def _get_test_dir_for_energy(self) -> Path:
        """Override to customize test directory for energy measurement."""
        return self.build_dir 

    def _clean(self):
        sh(cmd=["make", "clean"], cwd=self.input_dir)

    def _build(self, n_proc=-1, coverage=False) -> bool:
        cmd = ["make"]
        if n_proc == -1:
            nproc = os.cpu_count() or 1
            cmd.append(f"-j{nproc}")
        else:
            cmd.append(f"-j{n_proc}")
        
        _, errorcode, _ = sh(cmd=cmd, cwd=self.input_dir)
        return errorcode == 0

    def _resolve_source_path(self, reported_file: str, objdir: Path) -> Path:
        reported = reported_file.strip().strip("'\"")
        p = Path(reported)
        if p.is_absolute():
            return p

        # Most projects: gcov reports paths relative to object dir.
        objdir_candidate = (objdir / p).resolve()
        if objdir_candidate.exists():
            return objdir_candidate

        # Some builds report paths relative to source root.
        source_candidate = (self.input_dir / p).resolve()
        if source_candidate.exists():
            return source_candidate

        # QEMU/legacy gcov can emit absolute-like paths without a leading '/'.
        # Example: app/inputs/qemu/..., which would otherwise duplicate objdir.
        abs_like = Path("/" + reported.lstrip("/"))
        if abs_like.exists():
            return abs_like.resolve()

        # Fallback keeps previous behavior for unknown layouts.
        return objdir_candidate

    def _extract_covered_file(self, stdout: str, obj_dir: Path) -> list[str] | None:
        covered_files = []
        for line in stdout.splitlines():
            if line.startswith("File"):
                source_file = line.split("File ",1)[-1].replace("'", "")
                real_path = self._resolve_source_path(source_file, obj_dir)
                covered_files.append(str(real_path))
        return covered_files if covered_files else None

    # def coverage_file(self, test_name: str) -> list[str]:
    def coverage_file(self, test_name: str) -> dict[str, set[int]]:
        return self._process_coverage_files(self.build_dir, test_name) 
    
    #def _process_coverage_files(self, building_dir: Path) -> list[str]:
    #    """Process .gcda files and return list of covered files."""
    #    gco_files = list(building_dir.rglob("*.gcda"))
    #    self.logger.debug(f"Found {len(gco_files)} .gcda files in {building_dir}")
    #    covered = []
    #    for file in gco_files:
    #        obj_dir = file.parent
    #        stdout, code, stderr = sh(["gcov", "-n", "-o", str(obj_dir), str(file)], cwd=building_dir)
    #        if code != 0:
    #            self.logger.error(f"gcov failed for {file} with error: {stderr}")
    #            continue
    #        
    #        covered_files = self._extract_covered_file(stdout, obj_dir)
    #        if covered_files:
    #            for covered_file in covered_files:
    #                covered_path = Path(covered_file)
    #                try:
    #                    covered.append(str(covered_path.relative_to(self.input_dir)))
    #                except ValueError:
    #                    self.logger.debug(
    #                        "Skipping generated coverage file outside source tree: %s",
    #                        covered_path,
    #                    )
    #            
    #    return covered
    
    def _process_coverage_files(self, building_dir: Path, test_name: str) -> dict[str, set[int]]:
        """
        Process .gcda files using gcovr JSON output to get covered lines per file.
        Return a dictionary mapping source file paths (relative to input_dir) to sets of covered line numbers.
        """
        
        gcovr_json_path = (building_dir / f"{test_name}.json").resolve()
        gcda_dirs_out, _, _ = sh(
            ["find", ".", "-type", "f", "-name", "*.gcda", "-exec", "dirname", "{}", "+"],
            cwd=building_dir,
            use_shell=False,
        )
        gcda_folders = sorted({line.strip() for line in gcda_dirs_out.splitlines() if line.strip()})
        try:
            generate_gcovr_json(building_dir, gcovr_json_path, gcda_folders)
        except RuntimeError as err:
            print("Warning: gcovr generation failed, continuing with lcov only.")
            print(err)
            gcovr_json_path = None
        
        covered = {
            file_name: covered_lines
            for file_name, covered_lines in (
                parse_gcovr_covered_lines(gcovr_json_path, self.input_dir) if gcovr_json_path else {}
            ).items()
            if str(file_name).strip() and covered_lines
        }
        
        return covered
         

    def _run(self, cmd: list[str]) -> tuple[bool, dict]:
        stdout, errorcode, stderr = sh(cmd, cwd=Path(self.build_dir))
        if errorcode != 0:
            self.logger.error(f"Test output:\n{stdout}\n{stderr}")
        return errorcode == 0, {"stdout": stdout, "stderr": stderr, "errorcode": errorcode}

    def build(self, coverage=False, n_proc=1) -> bool:
        if (self.input_dir / "Makefile").exists():
            self._clean()
        if not self._configure(cwd=self.input_dir, coverage=coverage):
            self.logger.error("Configuration failed, cannot build.")
            return False
        return self._build(n_proc=n_proc, coverage=coverage)

    @abstractmethod
    def _configure(self, cwd: Path, coverage=False) -> bool | None:
        pass
    
    def _get_ctest_tests(self, build_dir: Path) -> list[str]:
        """Extract test names from CTest."""
        stdout, code, stderr = sh(["ctest", "-N"], cwd=build_dir)
        if code != 0:
            self.logger.error(f"Failed to get test list: {stderr}")
            return []
        return [str(line).split(":")[1].strip() for line in stdout.splitlines() if "Test #" in line]
    
    def _has_cmake_build(self) -> bool:
        """Check if project uses CMake build system."""
        return (self.input_dir / CMAKE_BUILD_DIR).exists()
    
    def _build_cmake(self, n_proc: int = -1) -> bool:
        """Build project using CMake."""
        cmd = ["cmake", "--build", CMAKE_BUILD_DIR]
        if n_proc == -1:
            nproc = os.cpu_count() or 1
            cmd.append(f"-j{nproc}")
        elif n_proc > 1:
            cmd.append(f"-j{n_proc}")
        
        _, errorcode, _ = sh(cmd=cmd, cwd=self.input_dir)
        return errorcode == 0



class ImageMagickProject(Project):
    """ImageMagick project using Autotools build system.
    
    Tests are defined as .tap (IM7+) or .sh (IM6) scripts in tests/ directory.
    Coverage files are processed from the input directory.
    """
    
    def __init__(self, output_dir, input_dir) -> None:
        super().__init__(output_dir, input_dir, "ImageMagick", "https://github.com/ImageMagick/ImageMagick")
        self.build_dir = self.input_dir
        
    # def coverage_file(self, test_name: str) -> list[str]:
        # return self._process_coverage_files(self.input_dir)
    
    def _get_test_dir_for_energy(self) -> Path:
        return self.input_dir
    
    def get_test_cmd(self, test_name: str, coverage=True) -> list[str]:
        tests_dir = self.input_dir / "tests"
        for ext in (".tap", ".sh"):
            candidate = tests_dir / f"{test_name}{ext}"
            if candidate.exists():
                return ['make', 'check', f"TESTS={candidate}"]
        return []
    
    def get_test(self) -> list[str]:
        # Autotools: each test is a .tap (IM7) or .sh (IM6) script in tests/.
        # The stem of the filename is the test name — exactly what make check
        # reports and what you pass to run individually.
        # Also picks up drawtest and wandtest (standalone C binaries with .tap wrappers).
        tests_dir = Path(self.input_dir) / "tests"
        # .tap is IM7+, .sh is IM6 — try tap first, fall back to sh
        scripts = sorted(tests_dir.glob("*.tap"))
        if not scripts:
            scripts = sorted(tests_dir.glob("*.sh"))
        return [s.stem for s in scripts]
    
    def _configure(self, cwd: Path, coverage=False) -> bool | None:
        cmd = ["./configure"]
        if coverage:
            cmd.append("--enable-gcov")
            
        _, errorcode, _ = sh(cmd, cwd=cwd)
        return errorcode == 0
        
    def _build(self, n_proc=-1, coverage=False):
        cmd = ["make"]
        if n_proc == -1:
            nproc = os.cpu_count() or 1
            cmd.append(f"-j{nproc}")
        else:
            cmd.append(f"-j{n_proc}")
        
        _, errorcode, _ = sh(cmd=cmd, cwd=self.input_dir)
        return errorcode == 0

    def _run(self, cmd: list[str]) -> tuple[bool, dict]:
        stdout, errorcode, stderr = sh(cmd, cwd=self.input_dir)
        if errorcode != 0:
            self.logger.error(f"Test output:\n{stdout}\n{stderr}")
        return errorcode == 0, {"stdout": stdout, "stderr": stderr, "errorcode": errorcode}
    
class LibXML2Project(Project):
    """LibXML2 project supporting both CMake and Autotools build systems.
    
    Automatically detects and switches between CMake (newer builds) and Autotools (legacy).
    Tests are discovered from CTest or extracted from runtest.c sources.
    """
    
    def __init__(self, output_dir, input_dir) -> None:
        super().__init__(output_dir, input_dir, "libxml2", "https://gitlab.gnome.org/GNOME/libxml2")
        self.build_dir = self.input_dir
        
    def get_test_cmd(self, test_name: str, coverage=True) -> list[str]:
        if (self.input_dir / CMAKE_BUILD_DIR).exists():
            self.logger.info("Using CMake logic to get test command for libxml2.")
            return ["ctest", "-R", f"^{test_name}$", "--output-on-failure"]
        else:
            self.logger.info(f"Using Autotools logic to get test {test_name} command for libxml2.")
            # Check if runtest binary exists; if not, try via make check
            runtest_bin = self.input_dir / "runtest"
            if not runtest_bin.exists():
                self.logger.warning(f"runtest binary not found at {runtest_bin}, will use 'make check'")
                return ["make", "check", f"TESTS={test_name}"]
            return ["./runtest", test_name]
    
    def get_test(self) -> list[str]:
        if self._has_cmake_build():
            self.logger.info("Using CMake logic to extract test names for libxml2.")
            return self._get_ctest_tests(self.input_dir / CMAKE_BUILD_DIR)
        else:
            self.logger.info("Using Autotools logic to extract test names for libxml2.")
            runtest_c = self.input_dir / "runtest.c"
            source = runtest_c.read_text(encoding="utf-8", errors="replace")

            array_match = re.search(
                r"testDesc\w*\s+testDescriptions\[\]\s*=\s*\{(.+?)\}\s*;",
                source,
                re.DOTALL,
            )
            if array_match:
                tests = re.findall(r'\{\s*"([^"]+)"', array_match.group(1))

            return tests
    
    def _get_test_dir_for_energy(self) -> Path:
        if self._has_cmake_build():
            return self.input_dir / CMAKE_BUILD_DIR
        return self.input_dir
        
    def _run(self, cmd: list[str]) -> tuple[bool, dict]:
        build_dir = self.build_dir
        if (self.input_dir / CMAKE_BUILD_DIR).exists():
            build_dir = self.input_dir / CMAKE_BUILD_DIR
        
        stdout, errorcode, stderr = sh(cmd, cwd=build_dir)
        if errorcode != 0:
            self.logger.error(f"Test output:\n{stdout}\n{stderr}")
        return errorcode == 0, {"stdout": stdout, "stderr": stderr, "errorcode": errorcode}
    
    def _build(self, n_proc=-1, coverage=False) -> bool:
        if (self.input_dir / "CMakeLists.txt").exists():
            return self._build_cmake(n_proc)
        else:
            if not super()._build(n_proc=n_proc, coverage=coverage):
                return False
            self.logger.debug("Building tests for libxml2.")
            cmd = ["make", "check"]
            _, errorcode, _ = sh(cmd=cmd, cwd=self.input_dir)
            return errorcode == 0

        

    def _configure(self, cwd: Path, coverage=False) -> bool | None:
        if (cwd / "CMakeLists.txt").exists():
            self.logger.info(f"Running CMake configuration for {self.name}.")
            cmd = ["cmake", "-S", ".", "-B", CMAKE_BUILD_DIR, 
                   "-DCMAKE_BUILD_TYPE=Debug", 
                   "-DENABLE_CURL_MANUAL=OFF", 
                   "-DENABLE_TESTS=ON", 
                   "-DENABLE_CURL_DEBUG=ON"
                   ]
            if coverage:
                cmd.append("-DCMAKE_C_FLAGS=--coverage")
        else:
            self.logger.info("Running legacy Autotools configuration for libxml2.")
            if not (cwd / "configure").exists():
                _, rc, _ = sh(["./autogen.sh"], cwd=cwd)
                if rc != 0:
                    # autogen.sh may fail on newer autotools due to obsolete macros,
                    # but configure might already exist from a prior successful run
                    self.logger.warning("Autotools autogen.sh failed, checking if configure exists.")
                    if not (cwd / "configure").exists():
                        self.logger.error("Autotools autogen.sh failed and configure script not found.")
                        return False
                    self.logger.info("Using existing configure script despite autogen.sh failure.")
            cmd=['./configure']
            if coverage:
                cmd.insert(0, 'CFLAGS=--coverage')
                cmd.insert(1, 'LDFLAGS=--coverage')

        _, errorcode, _ = sh(cmd, cwd=cwd)
        return errorcode == 0
        

class CurlProject(Project):
    """Curl project supporting both CMake and Autotools build systems.
    
    Hybrid build support with buildconf for Autotools and CMake for modern builds.
    Tests discovered via CTest for CMake builds or runtests.pl for Autotools.
    Coverage files processed from build or input directory depending on build system.
    """
    
    def __init__(self, output_dir, input_dir) -> None:
        super().__init__(output_dir, input_dir, "curl", "https://github.com/curl/curl")
    
    def coverage_file(self, test_name: str) -> dict[str, set[int]] | None:
        self.build_dir = self.input_dir if not self._has_cmake_build() else self.build_dir
        return super().coverage_file(test_name)
    
    def _configure(self, cwd: Path, coverage=False) -> bool:
        if (cwd / "CMakeLists.txt").exists():
            self.logger.info("Running CMake configuration for curl.")
            cmd = ["cmake", "-S", ".", "-B", CMAKE_BUILD_DIR, 
                   "-DCMAKE_BUILD_TYPE=Debug", 
                   "-DENABLE_MANUAL=OFF", 
                   "-DBUILD_TESTING=ON", 
                   "-DENABLE_DEBUG=ON"
                ]
            if coverage:
                cmd += [
                    "-DCMAKE_C_FLAGS=--coverage",
                    "-DCMAKE_EXE_LINKER_FLAGS=--coverage"
                ]
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
                  "CFLAGS=-O0 -g --coverage -fprofile-arcs -ftest-coverage",
                  "LDFLAGS=--coverage"]

        _, errorcode, _ = sh(cmd, cwd=cwd)
        return errorcode == 0
    
    def _build(self, n_proc=-1, coverage=False):
        if self._has_cmake_build():
            self.build_dir = self.input_dir / CMAKE_BUILD_DIR
            return self._build_cmake(n_proc)

        self.build_dir = self.input_dir / "tests"
        cmd = ["make"]
            
        if n_proc == -1:
            nproc = os.cpu_count() or 1
            cmd.append(f"-j{nproc}")
        else:
            cmd.append(f"-j{n_proc}")
        
        _, errorcode, _ = sh(cmd=cmd, cwd=self.input_dir)
        if errorcode != 0:
            return False
        
        self.logger.debug("Building tests for curl.") 
        _, errorcode, _ = sh(cmd=["make", "-C", "tests"], cwd=self.input_dir)
        return errorcode == 0
    
    def get_test_cmd(self, test_name: str, coverage=True) -> list[str]:
        if (self.input_dir / CMAKE_BUILD_DIR).exists():
            cmd = ["ctest", "-R", f"^{test_name}$", "--output-on-failure"]
        else:
            cmd = ["./runtests.pl", f"{test_name.replace('test', '')}"]
            
        return cmd

    def _run(self, cmd: list[str]) -> tuple[bool, dict]:
        self.build_dir = self.input_dir / "tests" if not self._has_cmake_build() else self.input_dir / CMAKE_BUILD_DIR
        return super()._run(cmd)

    def get_test(self) -> list[str]:
        build_dir = self.input_dir / CMAKE_BUILD_DIR
        if build_dir.exists():
            return self._get_ctest_tests(build_dir)
        else:
            tests = []
            test_dir = self.input_dir / "tests" 
            test_data_dir = test_dir / "data"
            tests = list(test_data_dir.rglob("test*"))
            tests = [t.name for t in tests]
        
        return tests
    
    def _get_test_dir_for_energy(self) -> Path:
        """Override to customize test directory for energy measurement."""
        return self.input_dir / "tests" if not self._has_cmake_build() else self.input_dir / CMAKE_BUILD_DIR

        
class LibarchiveProject(Project):
    """Libarchive project using CMake build system.
    
    Pure CMake-based project with consistent test discovery via CTest.
    All tests run in the cmake_build directory.
    """
    
    def __init__(self, output_dir, input_dir) -> None:
        super().__init__(output_dir, input_dir, "libarchive", "https://github.com/libarchive/libarchive")  
        self.build_dir = self.input_dir / CMAKE_BUILD_DIR

    def _run(self, cmd: list[str]) -> tuple[bool, dict]:
        stdout, errorcode, stderr = sh(cmd, cwd=self.build_dir)
        return errorcode == 0, {"stdout": stdout, "stderr": stderr, "errorcode": errorcode}
    
    def get_test_cmd(self, test_name: str, coverage=True) -> list[str]:
        return ["ctest", "-R", f"^{test_name}$", "--output-on-failure"]
    
    def get_test(self) -> list[str]:
        return self._get_ctest_tests(self.build_dir)

    def _configure(self, cwd: Path, coverage=False) -> bool:
        cmd = ["cmake", "-B", CMAKE_BUILD_DIR, "-S", ".",
               "-DCMAKE_BUILD_TYPE=Debug",
               "-DENABLE_COVERAGE=OFF",
               "-DENABLE_TEST=ON"]

        dcmake_c_flags = "-DCMAKE_C_FLAGS=-g -O0 -w"
        if coverage:
            cmd.append("-DCMAKE_EXE_LINKER_FLAGS=-fprofile-arcs -ftest-coverage")
            dcmake_c_flags = "-DCMAKE_C_FLAGS=-g -O0 -w -fprofile-arcs -ftest-coverage"

        cmd.append(dcmake_c_flags)

        _, errorcode, _ = sh(cmd, cwd=cwd, use_shell=False)
        return errorcode == 0
    
    def _build(self, n_proc=-1, coverage=False):
        if self._has_cmake_build():
            return self._build_cmake(n_proc)
        return super()._build(n_proc=n_proc, coverage=coverage)

    def coverage_file(self, test_name: str) -> dict[str, set[int]]:
        return self._process_coverage_files(self.input_dir, test_name)

class JasperProject(Project):
    """Jasper project using CMake with CTest-based test discovery.

    The upstream project defines its regression tests through CTest in the
    out-of-tree build directory. Coverage is enabled by passing compiler and
    linker coverage flags during CMake configuration.
    """

    def __init__(self, output_dir, input_dir) -> None:
        super().__init__(output_dir, input_dir, "jasper", "https://github.com/jasper-software/jasper.git")
        # Jasper rejects in-source builds, and pipeline cleanup runs under
        # project.output_dir. Keep build artifacts outside both source and
        # output trees so .gcno files are preserved between test runs.
        workspace_root = self.output_dir.parent.parent
        self.build_dir = workspace_root / "build" / self.name / CMAKE_BUILD_DIR
        self.build_dir.mkdir(parents=True, exist_ok=True)

    def get_test_cmd(self, test_name: str, coverage=True) -> list[str]:
        return ["ctest", "-R", f"^{test_name}$", "--output-on-failure"]

    def _cmake_cache_flag(self, name: str, default: bool = False) -> bool:
        cache = self.build_dir / "CMakeCache.txt"
        if not cache.exists():
            return default

        for line in cache.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.startswith(f"{name}:"):
                continue
            try:
                value = line.split("=", 1)[1].strip().upper()
            except IndexError:
                return default
            return value in {"1", "ON", "TRUE", "YES"}

        return default

    def get_test(self) -> list[str]:
        tests = self._get_ctest_tests(self.build_dir)

        # Some Jasper tests require JPEG codec support. If libjpeg is not
        # available in the current environment, these tests fail even though
        # source test data paths are valid.
        jpeg_enabled = (
            self._cmake_cache_flag("JAS_HAVE_LIBJPEG", default=False)
            and self._cmake_cache_flag("JAS_INCLUDE_JPG_CODEC", default=False)
            and self._cmake_cache_flag("JAS_ENABLE_JPG_CODEC", default=False)
        )

        if not jpeg_enabled:
            jpeg_tests = {"run_test_2"}
            filtered = [t for t in tests if t not in jpeg_tests]
            dropped = sorted(set(tests) - set(filtered))
            if dropped:
                self.logger.warning(
                    "Skipping JPEG-dependent Jasper tests because JPEG codec is unavailable: %s",
                    ", ".join(dropped),
                )
            tests = filtered

        return tests

    def _process_coverage_files(self, building_dir: Path) -> list[str]:
        """Collect coverage files for Jasper from its out-of-source build dir.

        Jasper often leaves stale .gcda files when binaries are rebuilt. We
        skip those artifacts instead of failing the whole coverage pass.
        """
        gco_files = list(building_dir.rglob("*.gcda"))
        self.logger.debug(f"Found {len(gco_files)} .gcda files in {building_dir}")
        covered: list[str] = []

        for gcda in gco_files:
            obj_dir = gcda.parent
            gcno = gcda.with_suffix(".gcno")
            if not gcno.exists():
                self.logger.warning(f"Skipping {gcda}: missing matching {gcno.name}")
                continue

            gcov_target = str(gcda.with_suffix(""))
            stdout, code, stderr = sh(
                ["gcov", "-n", "-o", str(obj_dir), gcov_target],
                cwd=obj_dir,
            )

            if code != 0:
                stderr_l = stderr.lower()
                if "stamp mismatch" in stderr_l or "cannot open notes file" in stderr_l:
                    # Remove stale runtime data so later passes are cleaner.
                    try:
                        gcda.unlink(missing_ok=True)
                    except OSError:
                        pass
                    self.logger.warning(f"Skipping stale coverage artifact {gcda}: {stderr.strip()}")
                else:
                    self.logger.error(f"gcov failed for {gcda} with error: {stderr}")
                continue

            covered_files = self._extract_covered_file(stdout, obj_dir)
            if covered_files:
                for covered_file in covered_files:
                    try:
                        covered.append(str(Path(covered_file).relative_to(self.input_dir)))
                    except ValueError:
                        covered.append(str(Path(covered_file)))

        # Keep deterministic output and avoid duplicates.
        return sorted(set(covered))

    def _configure(self, cwd: Path, coverage=False) -> bool:
        cmd = [
            "cmake", "-S", str(self.input_dir), "-B", str(self.build_dir),
            "-DCMAKE_BUILD_TYPE=Debug",
            "-DJAS_ENABLE_DOC=OFF",
            "-DJAS_ENABLE_OPENGL=OFF",
            "-DJAS_ENABLE_LIBHEIF=OFF",
            "-DJAS_ENABLE_LIBJPEG=ON",
            "-DJAS_INCLUDE_JPG_CODEC=ON",
            "-DJAS_ENABLE_JPG_CODEC=ON",
            "-DJAS_ENABLE_SHARED=OFF",
            "-DJAS_ENABLE_PROGRAMS=ON",
            "-DJAS_ENABLE_CONFORMANCE_TESTS=OFF",
        ]

        if coverage:
            cmd += [
                '-DCMAKE_C_FLAGS="-g -O0 --coverage"',
                '-DCMAKE_EXE_LINKER_FLAGS="--coverage"',
                '-DCMAKE_STATIC_LINKER_FLAGS=""',
            ]

        _, errorcode, _ = sh(cmd, cwd=cwd)
        return errorcode == 0

    def _build(self, n_proc=-1, coverage=False):
        cmd = ["cmake", "--build", str(self.build_dir)]
        if n_proc == -1:
            nproc = os.cpu_count() or 1
            cmd.append(f"-j{nproc}")
        elif n_proc > 1:
            cmd.append(f"-j{n_proc}")

        _, errorcode, _ = sh(cmd=cmd, cwd=self.input_dir)
        return errorcode == 0


class OpenSSLProject(Project):
    """OpenSSL project using custom Make-based build system.
    
    Uses ./Configure script for configuration with custom make targets for testing.
    Test names and coverage flags are project-specific constants.
    """
    
    CFLAG_COVERAGE="-fPIC -DOPENSSL_PIC -DOPENSSL_THREADS -D_REENTRANT -DDSO_DLFCN -DHAVE_DLFCN_H -m64 -DL_ENDIAN -DTERMIO -O0 -Wall -DMD32_REG_T=int --coverage"
    SHARED_LDFLAGS="-m64 --coverage"
    EX_LDL="-ldl --coverage"
    
    def __init__(self, output_dir, input_dir) -> None:
        super().__init__(output_dir, input_dir, "openssl", "https://github.com/openssl/openssl") 
        self.test_dir = self.input_dir
        self.build_dir = self.input_dir

    def get_test_cmd(self, test_name: str, coverage=False) -> list[str]:
        cmd = ["make", "test"]

        cmd += ["TESTS=" + test_name, "HARNESS_JOBS=1"]
        return cmd
        
    def get_test(self) -> list[str]:
        if (self.test_dir / "test" / "recipes").exists():
            out, _, _ = sh(['make', 'list-tests'], cwd=self.build_dir)
            tests = [line.strip() for line in out.splitlines() if line.strip()]
        else:
            tests = glob(str(self.test_dir / "test" / "*.c"))    
            tests = [os.path.splitext(os.path.basename(t))[0] for t in tests] 
        
        return tests 
    
    def _build(self, n_proc=1, coverage=False):
        cmd = ["make"]
        
        if n_proc == -1:
            cmd.append(f"-j")
        else:
            cmd.append(f"-j{n_proc}")
        _, errorcode, _ = sh(cmd=cmd, cwd=self.input_dir)
        return errorcode == 0
    
    def _configure(self, cwd: Path, coverage=False) -> bool:
        config_args = ["./Configure"]
        if coverage:
            # OpenSSL Configure expects compiler flags as Configure arguments,
            # not as trailing positional args after the target.
            config_args += [
                "-fPIC",
                "-DOPENSSL_PIC",
                "-DOPENSSL_THREADS",
                "-D_REENTRANT",
                "-DDSO_DLFCN",
                "-DHAVE_DLFCN_H",
                "-m64",
                "-DL_ENDIAN",
                "-DTERMIO",
                "-O0",
                "-Wall",
                "-DMD32_REG_T=int",
                "-fprofile-arcs",
                "-ftest-coverage",
            ]

        config_args += ["linux-x86_64", "no-shared", "no-asm"]

        _, errorcode, err = sh(config_args, cwd=cwd)
        
        if errorcode != 0:
            self.logger.error(f"Configuration failed with error: {err}")
            return False
        
        self.logger.info("Configuration succeeded.")
        return True


    
class VimProject(Project):
    """Vim project using Autotools build system with special test harness.
    
    Tests are .vim or .in scripts in src/testdir/. Runs tests in pseudo-terminal
    via script command to handle interactive test execution. Coverage files use
    special path handling relative to SOURCE_DIR.
    """

    TEST_DIR = "src/testdir"
    SOURCE_DIR = "src"

    def __init__(self, output_dir, input_dir):
        super().__init__(output_dir, input_dir, "vim", "https://github.com/vim/vim")
        self.source_dir = self.input_dir / VimProject.SOURCE_DIR
        self.test_dir = self.input_dir / VimProject.TEST_DIR
        self.build_dir = self.test_dir
        
    def _get_test_dir_for_energy(self) -> Path:
        return self.test_dir
    
    def get_test_cmd(self, test_name: str, coverage=True) -> list[str]:
        cmd = ["make", test_name, "HARNESS_JOBS=1", "LINES=24", "COLUMNS=80"]
        cmd_str = "stty rows 24 cols 80;" + " ".join(cmd)
        return ["script", "-q", "-f", "-e", "-c", cmd_str, "/dev/null"]
    
    def _run(self, cmd: list[str]) -> tuple[bool, dict]:
        stdout, errorcode, stderr = sh(cmd, cwd=Path(self.build_dir), use_shell=False)
        if errorcode != 0:
            self.logger.error(f"Test output:\n{stdout}\n{stderr}")
        return errorcode == 0, {"stdout": stdout, "stderr": stderr, "errorcode": errorcode}

    def get_test(self) -> list[str]:
        skipped_tests = ["test_filechanged", 
                        #  "test_cursor_func",
                        #  "test_cursorline", 
                         "test_terminal",
                         "test_recover",
                         "test_functions",
                         "test_buffer"]
        test_names = glob(str(self.test_dir / "test_*.vim")) 
        test_names += glob(str(self.test_dir / "test_*.in"))

        # Clean up test names by removing directory path and file extensions
        test_names = [t.replace(f"{self.test_dir}/", "") for t in test_names]
        test_names = [t.replace(".in", "").replace(".vim", "") for t in test_names]
        
        # Filter out skipped tests
        test_names = [t for t in test_names if not any(skipped in t for skipped in skipped_tests)]
        
        return test_names
        
    def _configure(self, cwd: Path, coverage=False) -> bool:
        config_args = [
            "./configure",
            "--with-features=huge",
            "--enable-gui=no",
            "--without-x"
        ]

        env = None
        if coverage:
            env = os.environ.copy()
            env['CFLAGS'] = "-g -O0 -fprofile-arcs -ftest-coverage"
            env['LDFLAGS'] = "-fprofile-arcs -ftest-coverage -lgcov"
        
        _, errorcode, err = sh(config_args, cwd=self.source_dir)
        if errorcode != 0:
            self.logger.error(f"Configuration failed with error: {err}")
            return False
        
        self.logger.info("Configuration succeeded with standard ./config.")
        return True
    
    def _build(self, n_proc=1, coverage=False):
        cmd = ["make"]

        env = None
        if coverage:
            cmd += [
                "PROFILE_CFLAGS=-g -O0 -fprofile-arcs -ftest-coverage -DWE_ARE_PROFILING -DUSE_GCOV_FLUSH",
                "LDFLAGS=-fprofile-arcs -ftest-coverage -lgcov"
            ]

        if n_proc == -1:
            cmd.append(f"-j")
        else:
            cmd.append(f"-j{n_proc}")
        
        _, errorcode, _ = sh(cmd=cmd, cwd=self.source_dir)
        return errorcode == 0

    def coverage_file(self, test_name: str) -> dict[str, set[int]]:
        return self._process_coverage_files(self.input_dir, test_name)
    
class TcpDumpProject(Project):
    """TcpDump project using Autotools build system.

    Simple tests are listed in tests/TESTLIST (one per line, first field is the
    name) and run individually via ``tests/TESTrun.sh <name>``.  Complex tests
    are the *.sh scripts in tests/ (excluding the TESTrun.sh / TESTonce
    harness files that begin with "TEST").

    Coverage is enabled by forwarding ``CFLAGS`` and ``LDFLAGS`` containing
    ``--coverage`` to ``./configure`` at configuration time.
    """

    def __init__(self, output_dir, input_dir) -> None:
        super().__init__(output_dir, input_dir, "tcpdump",
                         "https://github.com/the-tcpdump-group/tcpdump")
        # Tests are executed from inside the tests/ sub-directory
        self.build_dir = self.input_dir

    # ------------------------------------------------------------------
    # Test discovery
    # ------------------------------------------------------------------

    def get_test(self) -> list[str]:
        tests: list[str] = []

        # Simple tests: each non-comment, non-blank line in TESTLIST has the
        # test name as its first whitespace-separated field.
        testlist = self.input_dir / "tests" / "TESTLIST"
        if testlist.exists():
            for line in testlist.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                name = line.split()[0]
                tests.append(name)

        # Complex tests: *.sh scripts excluding the harness files (TEST*.sh).
        for sh_script in sorted((self.input_dir / "tests").glob("*.sh")):
            if sh_script.name.startswith("TEST"):
                continue
            tests.append(sh_script.stem)

        return tests

    # ------------------------------------------------------------------
    # Running tests
    # ------------------------------------------------------------------

    def get_test_cmd(self, test_name: str, coverage=True) -> list[str]:
        # TESTrun.sh accepts an optional single argument to run one test.
        return ["./TESTrun.sh", test_name]

    def _run(self, cmd: list[str]) -> tuple[bool, dict]:
        stdout, errorcode, stderr = sh(cmd, cwd=self.input_dir / "tests")
        if errorcode != 0:
            self.logger.error(f"Test output:\n{stdout}\n{stderr}")
        return errorcode == 0, {"stdout": stdout, "stderr": stderr, "errorcode": errorcode}

    def _get_test_dir_for_energy(self) -> Path:
        return self.input_dir / "tests"

    # ------------------------------------------------------------------
    # Build system
    # ------------------------------------------------------------------

    def _configure(self, cwd: Path, coverage=False) -> bool:
        cmd = ["./configure"]
        env = None
        if coverage:
            env = os.environ.copy()
            env["CFLAGS"] = "-g -O0 -fprofile-arcs -ftest-coverage"
            env["LDFLAGS"] = "-fprofile-arcs -ftest-coverage"
        _, errorcode, _ = sh(cmd, cwd=cwd, env=env)
        return errorcode == 0


# TODO: check why it doesn't create energy data
class QEMUProject(Project):
    """QEMU project using configure+Meson with per-test coverage support.

    Build is done in-tree so generated sources, test binaries, and coverage
    artifacts stay alongside the checked-out revision. Test discovery uses
    Meson introspection when available, and falls back to legacy make rules.
    """

    def __init__(self, output_dir, input_dir) -> None:
        super().__init__(output_dir, input_dir, "qemu", "https://gitlab.com/qemu-project/qemu.git")
        self.build_dir = self.input_dir

    def _meson_executable(self) -> str | None:
        """Return path to meson if available in the build venv, else None."""
        local_meson = self.build_dir / "pyvenv" / "bin" / "meson"
        if local_meson.exists():
            return str(local_meson)
        # Do not fall back to a bare 'meson' that may not be installed;
        # old QEMU commits pre-date Meson and rely on plain make.
        return None

    def _run_configure_with_fallback(self, configure_script: Path, options: list[str]) -> tuple[bool, str]:
        """Run configure, dropping only options explicitly reported as unknown."""
        pending = list(options)
        unknown_patterns = [
            re.compile(r"unknown option\s+(--[^\s]+)", re.IGNORECASE),
            re.compile(r"unrecognized option\s+['\"]?(--[^\s'\"]+)['\"]?", re.IGNORECASE),
            re.compile(r"option\s+['\"]?(--[^\s'\"]+)['\"]?\s+not recognized", re.IGNORECASE),
        ]

        for _ in range(len(options) + 1):
            cmd = [str(configure_script)] + pending
            stdout, errorcode, stderr = sh(cmd, cwd=self.build_dir)
            combined = f"{stdout}\n{stderr}"
            if errorcode == 0:
                self.logger.debug("QEMU configure options selected: %s", " ".join(pending))
                return True, combined

            match = None
            for pattern in unknown_patterns:
                match = pattern.search(combined)
                if match:
                    break
            if not match:
                self.logger.error("QEMU configure failed without unknown-option hint:\n%s", combined)
                return False, combined

            unknown = match.group(1).strip("'\".,:;)")
            new_pending = [o for o in pending if o.split("=", 1)[0] != unknown]
            if len(new_pending) == len(pending):
                self.logger.error("QEMU configure reported unknown option %s, but it was not in pending options", unknown)
                return False, combined
            self.logger.warning("QEMU configure does not support %s; retrying without it", unknown)
            pending = new_pending

        return False, ""

    def _python_candidates(self) -> list[str]:
        candidates = [
            "/usr/bin/python3",
            "/usr/local/bin/python2.7"
        ]
        return [p for p in candidates if Path(p).exists()]

    def _is_python_config_error(self, configure_output: str) -> bool:
        out = configure_output.lower()
        return "python" in out and (
            "is required" in out or "cannot use" in out or "not found" in out or "unsupported" in out
        )

    def run_test(self, test_name: str, coverage=True) -> tuple[bool, dict]:
        # QEMU's own target clears prior *.gcda state and is the recommended
        # way to prepare for single-test coverage runs.
        if coverage:
            sh(["make", "clean-gcda"], cwd=self.build_dir)
        # For make-based builds the individual test binaries are not compiled
        # during the main build step; build the specific binary on demand.
        if self._meson_executable() is None:
            test_bin = self.build_dir / "tests" / test_name
            if not test_bin.exists():
                out, rc, err = sh(["make", f"tests/{test_name}"], cwd=self.build_dir)
                if rc != 0:
                    return False, {"errorcode": rc, "stdout": out, "stderr": err}

        return super().run_test(test_name, coverage=coverage)

    def get_test_cmd(self, test_name: str, coverage=True) -> list[str]:
        meson = self._meson_executable()
        if meson is not None:
            return [meson, "test", "--no-rebuild", "--print-errorlogs", test_name]
        # Old make-based build: execute the test via make target.
        return ["make", str(Path("tests") / test_name)]

    def _get_make_unit_tests(self) -> list[str]:
        """Parse tests/Makefile from source tree to extract unit-test binary names."""
        makefile = self.input_dir / "tests" / "Makefile"
        if not makefile.exists():
            return []
        tests: list[str] = []
        for line in makefile.read_text(encoding="utf-8", errors="replace").splitlines():
            # matches: check-unit-y = tests/check-qdict$(EXESUF)
            #          check-unit-y += tests/test-coroutine$(EXESUF)
            m = re.match(r'check-unit-[\w$()]+\s*[+:]?=\s*tests/([^$(\s]+)', line)
            if m:
                tests.append(m.group(1))
        return tests

    def get_test(self) -> list[str]:
        meson = self._meson_executable()
        if meson is not None:
            stdout, code, _ = sh([meson, "introspect", "--tests"], cwd=self.build_dir)
            if code == 0:
                try:
                    tests_data = json.loads(stdout)
                    tests = [entry.get("name", "").strip() for entry in tests_data if isinstance(entry, dict)]
                    tests = [t for t in tests if t]
                    if tests:
                        return sorted(set(tests))
                except json.JSONDecodeError:
                    self.logger.warning("Failed to parse Meson introspection output for QEMU tests.")

        # Old make-based build: discover individual unit-test binaries from
        # tests/Makefile in the source tree. Binaries are compiled on-demand
        # at test-run time, so no existence check is applied here.
        self.logger.info("Using tests/Makefile for individual QEMU unit-test discovery.")
        unit_tests = self._get_make_unit_tests()
        if unit_tests:
            return sorted(set(unit_tests))

        # Last resort: coarse suite targets via make check-help.
        self.logger.info("Falling back to make check-help for QEMU test discovery.")
        out, code, _ = sh(["make", "check-help"], cwd=self.build_dir)
        if code != 0:
            return []
        suites: list[str] = []
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith("check-"):
                continue
            suites.append(line.split()[0])
        return sorted(set(suites))

    def _configure(self, cwd: Path, coverage=False) -> bool:
        configure_script = self.input_dir / "configure"

        # Keep Meson/configure state clean between commits in in-tree mode.
        # Stale build dirs can make configure fail before option fallback helps.
        sh(["make", "distclean"], cwd=self.input_dir)
        sh(["rm", "-rf", "build"], cwd=self.input_dir)

        base_options = [
            "--disable-werror",
            "--disable-docs",
            "--disable-tools",
            "--disable-guest-agent",
            "--disable-xen",
            "--disable-xen-pci-passthrough",
            "--disable-gtk",
            "--disable-sdl",
            "--disable-opengl",
            "--disable-cocoa",
            "--disable-spice",
            "--enable-debug",
            "--target-list=x86_64-softmmu",
            "--extra-cflags=-Wno-error=nested-externs",
        ]

        if coverage:
            base_options.append("--enable-gcov")

        last_output = ""
        for python_exec in self._python_candidates():
            options = list(base_options)
            options.append(f"--python={python_exec}")
            ok, output = self._run_configure_with_fallback(configure_script, options)
            if ok:
                return True
            last_output = output
            if not self._is_python_config_error(output):
                return False
            self.logger.warning("QEMU configure rejected Python interpreter %s; trying next candidate", python_exec)

        # Fallback: if no explicit interpreter works, let configure pick from PATH.
        ok, output = self._run_configure_with_fallback(configure_script, list(base_options))
        if ok:
            return True
        if output:
            self.logger.error("QEMU configure failed after Python fallback attempts:\n%s", output)
        elif last_output:
            self.logger.error("QEMU configure failed after Python fallback attempts:\n%s", last_output)
        return False
    
    def _get_test_dir_for_energy(self) -> Path:
        """Override to customize test directory for energy measurement."""
        return self.build_dir


class FFmpegProject(Project):
    """FFmpeg project using its own configure script with the FATE test suite.

    Configuration uses ./configure with --toolchain=gcov for coverage instrumentation.
    Tests are FATE tests discovered via 'make fate-list' (requires a configured build).
    Individual tests are run via 'make <test-name>' from the source/build directory.
    Coverage files (.gcda) are collected from the in-source tree after each test run.
    """

    def __init__(self, output_dir, input_dir) -> None:
        super().__init__(output_dir, input_dir, "FFmpeg", "https://github.com/FFmpeg/FFmpeg.git")
        # FFmpeg performs in-source builds; tests run from the top-level source dir.
        self.build_dir = self.input_dir

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _configure(self, cwd: Path, coverage=False) -> bool:
        cmd = [
            "./configure",
            "--disable-doc",
            "--disable-htmlpages",
            "--disable-manpages",
            "--disable-podpages",
            "--disable-txtpages",
            "--disable-optimizations",
            "--disable-stripping",
            "--enable-debug=3",
            "--disable-x86asm"
        ]
        if coverage:
            # --toolchain=gcov adds -fprofile-arcs -ftest-coverage to CFLAGS/LDFLAGS
            cmd.append("--toolchain=gcov")

        _, errorcode, _ = sh(cmd, cwd=cwd)
        return errorcode == 0

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def _build(self, n_proc=-1, coverage=False) -> bool:
        # Build main executable using parent class implementation
        if not super()._build(n_proc=n_proc, coverage=coverage):
            return False

        # Build the FATE helper binaries (videogen, audiogen, …)
        _, errorcode, _ = sh(["make", "testprogs"], cwd=self.input_dir)
        return errorcode == 0

    # ------------------------------------------------------------------
    # Test discovery
    # ------------------------------------------------------------------

    def get_test(self) -> list[str]:
        """Return all FATE test names via 'make fate-list' (needs a configured build)."""
        stdout, code, _ = sh(["make", "fate-list"], cwd=self.input_dir)
        if code != 0:
            self.logger.error("'make fate-list' failed; has the project been built?")
            return []
        return [line.strip() for line in stdout.splitlines() if line.strip().startswith("fate-")]

    # ------------------------------------------------------------------
    # Running tests
    # ------------------------------------------------------------------

    def get_test_cmd(self, test_name: str, coverage=True) -> list[str]:
        """Return the make command to run a single FATE test."""
        return ["make", test_name]

    def _run(self, cmd: list[str]) -> tuple[bool, dict]:
        stdout, errorcode, stderr = sh(cmd, cwd=self.input_dir)
        if errorcode != 0:
            self.logger.error(f"Test failed:\n{stdout}\n{stderr}")
        return errorcode == 0, {"stdout": stdout, "stderr": stderr, "errorcode": errorcode}

    def _get_test_dir_for_energy(self) -> Path:
        return self.input_dir
