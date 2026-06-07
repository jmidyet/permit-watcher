"""
Configuration loader for the Permit Watcher application.
Loads and validates configuration from YAML files.
"""

import os
import yaml
import logging
from pathlib import Path
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

class ConfigLoader:
    """Loads and validates configuration from YAML files."""
    
    def __init__(self, config_dir):
        """
        Initialize the ConfigLoader.
        
        Args:
            config_dir (Path or str): Path to the directory containing config files.
        """
        self.config_dir = Path(config_dir)
        
        # Load environment variables from .env file if it exists
        env_file = self.config_dir.parent / ".env"
        if env_file.exists():
            load_dotenv(env_file)
            logger.debug(f"Loaded environment variables from {env_file}")
        else:
            logger.warning(f".env file not found at {env_file}. Using system environment variables only.")
    
    def load_app_config(self):
        """
        Load the application configuration.
        
        Returns:
            dict: The application configuration.
        """
        config_file = self.config_dir / "config.yaml"
        return self._load_yaml(config_file)
    
    def load_permits_config(self):
        """
        Load the permits configuration.
        
        Returns:
            dict: The permits configuration.
        """
        permits_file = self.config_dir / "permits.yaml"
        return self._load_yaml(permits_file)
    
    def _load_yaml(self, file_path):
        """
        Load a YAML file and process environment variables.
        
        Args:
            file_path (Path): Path to the YAML file.
            
        Returns:
            dict: The loaded and processed configuration.
        """
        try:
            if not file_path.exists():
                logger.error(f"Configuration file not found: {file_path}")
                return {}
            
            with open(file_path, 'r') as file:
                config = yaml.safe_load(file)
            
            # Process environment variables in the config
            config = self._process_env_vars(config)
            
            return config
        
        except yaml.YAMLError as e:
            logger.error(f"Error parsing YAML file {file_path}: {e}")
            return {}
        except Exception as e:
            logger.error(f"Error loading configuration from {file_path}: {e}")
            return {}
    
    def _process_env_vars(self, config):
        """
        Process environment variables in the configuration.
        
        Args:
            config: Configuration object (dict, list, or scalar).
            
        Returns:
            The processed configuration.
        """
        if isinstance(config, dict):
            return {k: self._process_env_vars(v) for k, v in config.items()}
        elif isinstance(config, list):
            return [self._process_env_vars(v) for v in config]
        elif isinstance(config, str) and config.startswith("${") and config.endswith("}"):
            # Extract environment variable name
            env_var = config[2:-1].strip()
            value = os.environ.get(env_var)
            if value is None:
                logger.warning(f"Environment variable {env_var} not found.")
                return config
            return value
        else:
            return config
