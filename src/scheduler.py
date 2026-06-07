"""
Scheduler module for scheduling permit checks at configurable intervals.
Supports both time-based and interval-based scheduling with randomization.
"""

import logging
import random
from datetime import datetime, timedelta
import time

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

class Scheduler:
    """Schedules and manages permit checking jobs."""
    
    def __init__(self, app_config, permit_checker):
        """
        Initialize the scheduler.
        
        Args:
            app_config (dict): Application configuration.
            permit_checker (PermitChecker): Permit checker instance.
        """
        self.app_config = app_config
        self.permit_checker = permit_checker
        
        # Initialize the scheduler
        self.scheduler = BackgroundScheduler()
        
        # Get scheduler configuration
        scheduler_config = app_config.get('scheduler', {})
        self.scheduled_times = scheduler_config.get('scheduled_times', [])
        self.interval = scheduler_config.get('interval', 0)
        self.random_offset = scheduler_config.get('random_offset', 15)
        
        logger.info("Scheduler initialized")
    
    def start(self):
        """
        Start the scheduler with the configured jobs.
        """
        # Schedule based on configuration
        if self.scheduled_times:
            self._schedule_time_based_checks()
        
        if self.interval > 0:
            self._schedule_interval_based_checks()
        
        # If no scheduling is configured, run checks immediately and exit
        if not self.scheduled_times and self.interval <= 0:
            logger.warning("No scheduling configured. Running checks once.")
            self.permit_checker.check_all_permits()
            return
        
        # Start the scheduler
        self.scheduler.start()
        logger.info("Scheduler started")
        
        # Run initial check immediately
        self.permit_checker.check_all_permits()
        logger.info("Initial permit check completed")
    
    def stop(self):
        """
        Stop the scheduler.
        """
        if self.scheduler.running:
            self.scheduler.shutdown()
            logger.info("Scheduler stopped")
    
    def _schedule_time_based_checks(self):
        """
        Schedule permit checks at specific times of day with randomization.
        """
        for time_str in self.scheduled_times:
            try:
                # Parse the time string (format: HH:MM)
                hour, minute = map(int, time_str.split(':'))
                
                # Add random offset (in minutes) to avoid detection patterns
                if self.random_offset > 0:
                    minute_offset = random.randint(-self.random_offset, self.random_offset)
                    
                    # Calculate the new time with offset
                    check_time = datetime.now().replace(hour=hour, minute=minute, second=0) + timedelta(minutes=minute_offset)
                    check_hour = check_time.hour
                    check_minute = check_time.minute
                else:
                    check_hour = hour
                    check_minute = minute
                
                # Create a cron trigger
                trigger = CronTrigger(hour=check_hour, minute=check_minute)
                
                # Add the job to the scheduler
                self.scheduler.add_job(
                    self.permit_checker.check_all_permits,
                    trigger=trigger,
                    id=f"time_check_{hour}_{minute}"
                )
                
                logger.info(f"Scheduled check at {check_hour:02d}:{check_minute:02d}")
                
            except ValueError as e:
                logger.error(f"Invalid time format: {time_str}. Error: {e}")
    
    def _schedule_interval_based_checks(self):
        """
        Schedule permit checks at regular intervals with randomization.
        """
        # Convert interval from minutes to seconds
        interval_seconds = self.interval * 60
        
        # Add jitter to the interval
        if self.random_offset > 0:
            jitter_seconds = self.random_offset * 60
            # Apply jitter as a percentage of the interval
            jitter_factor = jitter_seconds / interval_seconds
            
            trigger = IntervalTrigger(
                seconds=interval_seconds,
                jitter=jitter_factor
            )
        else:
            trigger = IntervalTrigger(seconds=interval_seconds)
        
        # Add the job to the scheduler
        self.scheduler.add_job(
            self.permit_checker.check_all_permits,
            trigger=trigger,
            id="interval_check"
        )
        
        logger.info(f"Scheduled checks at {self.interval} minute intervals with {self.random_offset} minutes randomization")
