
from pathlib import Path
import sys
import yaml
import os

from common import logging

class Config:
    TEST_LIMIT = 10

    
    
    def __init__(self, config_file: str | Path):
        
        self.config_file = config_file
        
        if not os.path.exists(self.config_file):
            sys.exit(1)
        try:
            self.config = self._read_config()
        except Exception as e:
            sys.exit(1)

        # Ensure configuration is loaded
        if self.config is None:
            sys.exit(1)

        # Prepare directories first so other classes can rely on them
        self._prepare_directories()

        # Logging setup (after directories are ready)
        self.logger = logging.getLogger(self.__class__.__name__)
        log_dir = self.config.get("paths", {}).get("log_dir", ".")
        os.makedirs(log_dir, exist_ok=True)
        handler = logging.FileHandler(os.path.join(log_dir, "config.log"))
        self.logger.addHandler(handler)
        self.logger.setLevel(logging.DEBUG)

        # Proceed with dataset setup
        self._download_dataset()

        
    def _prepare_directories(self):
        if self.config:
            paths = self.config.get("paths", {})
            for path_name, path_value in paths.items():
                if path_value and not os.path.exists(path_value):
                    os.makedirs(path_value, exist_ok=True)
                    self.logger.info(f"Created directory: {path_value}")
            
            
    def _download_dataset(self):
        if self.config:
            if os.path.exists(self.config.get("dataset").get("csv_file")):
                self.logger.info(f"Dataset CSV found at {self.config.get('dataset').get('csv_file')}")
                return
            else:
                import urllib.request
                url = self.config.get("dataset").get("csv_url")
                try:
                    urllib.request.urlretrieve(url, self.config.get("dataset").get("csv_file"))
                    self.logger.info("Download complete.")
                except Exception as e:
                    self.logger.error(f"Error downloading CSV: {e}")
                    sys.exit(1)
          
    def _read_config(self):
        with open(self.config_file, 'r') as f:
            return yaml.safe_load(f)
        
    def get(self, key: str):
        if self.config is None:
            self.logger.error("Configuration not loaded.")
            raise ValueError("Configuration not loaded.")
        return self.config.get(key, {})