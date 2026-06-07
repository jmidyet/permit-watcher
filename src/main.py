#!/usr/bin/env python3
"""
Main entry point for the Permit Watcher application.
This module initializes the application, loads configuration,
and starts the permit checking scheduler.
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.config_loader import ConfigLoader
from src.permit_checker import PermitChecker
from src.scheduler import Scheduler
from src.notifier import Notifier

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(project_root / "permit_watcher.log")
    ]
)
logger = logging.getLogger(__name__)


def main():
    """Main function to run the permit watcher."""
    parser = argparse.ArgumentParser(description="recreation.gov permit watcher")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single check of all permits and exit (for cron/scheduled jobs).",
    )
    parser.add_argument(
        "--telegram-poll",
        action="store_true",
        help="Check Telegram for a manual-run command and reply (for cron).",
    )
    args = parser.parse_args()

    logger.info("Starting Permit Watcher...")

    # Load configuration
    config_path = project_root / "config"
    config_loader = ConfigLoader(config_path)
    app_config = config_loader.load_app_config()
    permits_config = config_loader.load_permits_config()

    # Set log level from config
    log_level = getattr(logging, app_config.get('app', {}).get('log_level', 'INFO'))
    logger.setLevel(log_level)

    # Telegram manual-run poll: check for a command, reply, and exit.
    if args.telegram_poll:
        from src.telegram_listener import poll_once
        poll_once(app_config, permits_config, project_root / "telegram_offset.json")
        return 0

    # Initialize components
    notifier = Notifier(app_config)
    state_path = project_root / app_config.get('app', {}).get('state_file', 'state.json')
    permit_checker = PermitChecker(app_config, permits_config, notifier, state_path=state_path)

    # One-shot mode: check once and exit (use with an external scheduler/cron).
    if args.once:
        logger.info("Running a single check (--once) and exiting.")
        permit_checker.check_all_permits()
        return 0

    # Initialize and start scheduler
    scheduler = Scheduler(app_config, permit_checker)

    try:
        logger.info("Permit Watcher is running. Press Ctrl+C to exit.")
        scheduler.start()
        
        # Keep the main thread alive
        while True:
            time.sleep(1)
            
    except KeyboardInterrupt:
        logger.info("Shutting down Permit Watcher...")
        scheduler.stop()
        logger.info("Permit Watcher has been stopped.")
    except Exception as e:
        logger.error(f"An error occurred: {e}", exc_info=True)
        scheduler.stop()
        return 1
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
