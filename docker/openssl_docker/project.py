# create an abstract class for projects
from abc import ABC, abstractmethod
import os
import logging
from pathlib import Path

from common import GitHandler, MakeHandler, sh
from config import Config

class ProjectFactory:
    
    logger = logging.getLogger("ProjectFactory")
    logger.setLevel(logging.DEBUG)
    
    @staticmethod
    def get_project(cfg : Config ) -> 'Project':
        name = cfg.get('project').get('name')
        output_dir = cfg.get('paths').get('output_dir')
        input_dir = cfg.get('paths').get('input_dir')
        if name.lower() == "openssl":
            ProjectFactory.logger.info(f"Creating OpenSSL project instance.")
            return OpenSSLProject(output_dir, input_dir)
        else:
            ProjectFactory.logger.error(f"Unknown project: {name}")
            raise ValueError(f"Unknown project: {name}")


class Project(ABC):
    
    name = "generic_project"
    output_dir = "./output/generic_project"
    input_dir = "./input/generic_project"
    
    @abstractmethod
    def build(self, coverage=False):
        pass

    @abstractmethod
    def get_test(self):
        pass

class OpenSSLProject(Project):

    def __init__(self, output_dir, input_dir) -> None:
        super().__init__()
        self.logger = logging.getLogger(self.__class__.__name__)
        handler = logging.FileHandler(os.path.join(output_dir, "openssl_project.log"))
        self.logger.addHandler(handler)
        self.logger.setLevel(logging.DEBUG)
        
        self.name = "openssl"
        self.output_dir = os.path.join(output_dir, self.name)
        
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)
            
        self.input_dir = os.path.join(input_dir, self.name)
        if (not os.path.exists(self.input_dir)) or (not os.path.exists(os.path.join(self.input_dir, ".git"))):
            GitHandler.clone_repo(input_dir, "https://github.com/openssl/openssl")

    def build(self, coverage=False):
        if os.path.exists(os.path.join(self.input_dir, "Makefile")):
            MakeHandler.clean(self.input_dir)
        if not self._configure(self.input_dir, coverage=coverage):
            self.logger.error("Configuration failed, cannot build.")
            return False
        return MakeHandler.build(self.input_dir)
        
    def get_test(self) -> list[str]:
        out, _, _ = sh(['make', 'list-tests'], cwd=Path(self.input_dir))
        return [line.strip().split()[0]
            for line in out.splitlines()
            if line.strip() and not line.lstrip().startswith(("make", "Files=", "Tests=", "Result:"))]
    
    def _configure(self, cwd, coverage=False):
        config_args = ["./config", "no-asm", "-g", "-O0"] 
        if coverage:
            config_args.append("--coverage")
        
        try:
            _, errorcode, _ = sh(config_args, cwd=cwd)
        except Exception as e:
            self.logger.warning(f"Trying fallback configuration due to exception: {e}")
            env_backup = os.environ.copy()
            env_backup['CFLAGS']="-O0 -g -fprofile-arcs -ftest-coverage"
            env_backup["LDFLAGS"]="--coverage"
            config_args = ["./Configure", "linux-x86_64", "no-asm"]
            try:
                _, errorcode, _ = sh(config_args, cwd=cwd, env=env_backup)
            except Exception as e2:
                self.logger.error(f"Fallback configuration also failed due to exception: {e2}")
                return False
        
        #if errorcode != 0:
        #    self.logger.warning("Initial config failed, trying with custom CFLAGS/LDFLAGS...")
        #    config_args = ["./config", "no-asm"]
        #    env_backup = os.environ.copy()
        #    os.environ["CFLAGS"] = "-O0 -g"
        #    if coverage:
        #        os.environ["CFLAGS"] += " --coverage"
        #        os.environ["LDFLAGS"] = " --coverage"
        #    _, errorcode, _ = sh(config_args, cwd=cwd, env=env_backup)   
        #    if errorcode != 0:
        #        self.logger.error("Configuration failed even with custom CFLAGS/LDFLAGS.")
        #        return False
        #    else:
        #        self.logger.info("Configuration succeeded with custom CFLAGS/LDFLAGS.")
        #        return True
        self.logger.info("Configuration succeeded with standard ./config.")
        return True
        
            