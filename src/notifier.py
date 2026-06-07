"""
Notifier module for sending notifications when permit availability is found.
Supports email and SMS notifications.
"""

import logging
import smtplib
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

class Notifier:
    """Sends notifications when permit availability is found."""
    
    def __init__(self, app_config):
        """
        Initialize the notifier.
        
        Args:
            app_config (dict): Application configuration.
        """
        self.app_config = app_config
        self.notification_config = app_config.get('notification', {})

        # Telegram settings (token + chat id come from environment variables)
        self.telegram_enabled = self.notification_config.get('telegram', False)

        # Email settings
        self.email_enabled = self.notification_config.get('email', False)
        self.email_settings = self.notification_config.get('email_settings', {})

        # SMS settings
        self.sms_enabled = self.notification_config.get('sms', False)
        self.sms_settings = self.notification_config.get('sms_settings', {})

        logger.info("Notifier initialized")
    
    def send_notification(self, subject, message):
        """
        Send a notification using all enabled notification methods.
        
        Args:
            subject (str): Subject or title of the notification.
            message (str): Content of the notification.
        """
        logger.info(f"Sending notification: {subject}")
        
        # Log the message
        logger.info(f"Notification message: {message}")

        # Send Telegram message if enabled
        if self.telegram_enabled:
            self._send_telegram(subject, message)

        # Send email if enabled
        if self.email_enabled:
            self._send_email(subject, message)

        # Send SMS if enabled
        if self.sms_enabled:
            self._send_sms(message)
    
    def _send_telegram(self, subject, message):
        """
        Send a Telegram message via the Bot API.

        Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables.
        """
        try:
            token = os.environ.get('TELEGRAM_BOT_TOKEN')
            chat_id = os.environ.get('TELEGRAM_CHAT_ID')

            if not (token and chat_id):
                logger.error(
                    "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID environment variables"
                )
                return

            text = f"\U0001F3D5 {subject}\n\n{message}"
            response = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={'chat_id': chat_id, 'text': text, 'disable_web_page_preview': False},
                timeout=15,
            )

            if response.status_code == 200:
                logger.info("Telegram notification sent")
            else:
                logger.error(
                    f"Telegram send failed {response.status_code}: {response.text[:200]}"
                )

        except Exception as e:
            logger.error(f"Failed to send Telegram notification: {e}", exc_info=True)

    def _send_email(self, subject, message):
        """
        Send an email notification.
        
        Args:
            subject (str): Email subject.
            message (str): Email message body.
        """
        try:
            # Get email settings
            smtp_server = self.email_settings.get('smtp_server')
            smtp_port = self.email_settings.get('smtp_port')
            from_address = self.email_settings.get('from_address')
            to_addresses = self.email_settings.get('to_addresses', [])
            
            # Get credentials from environment variables
            username = os.environ.get('SMTP_USERNAME')
            password = os.environ.get('SMTP_PASSWORD')
            
            if not (smtp_server and smtp_port and from_address and to_addresses):
                logger.error("Missing required email settings")
                return
            
            if not (username and password):
                logger.error("Missing SMTP credentials in environment variables")
                return
            
            # Create message
            msg = MIMEMultipart()
            msg['From'] = from_address
            msg['To'] = ', '.join(to_addresses)
            msg['Subject'] = subject
            
            # Add timestamp to the message
            full_message = f"{message}\n\nSent at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            msg.attach(MIMEText(full_message, 'plain'))
            
            # Send the message
            with smtplib.SMTP(smtp_server, smtp_port) as server:
                server.starttls()
                server.login(username, password)
                server.send_message(msg)
            
            logger.info(f"Email notification sent to {to_addresses}")
            
        except Exception as e:
            logger.error(f"Failed to send email notification: {e}", exc_info=True)
    
    def _send_sms(self, message):
        """
        Send an SMS notification using Twilio.
        
        Args:
            message (str): SMS message body.
        """
        try:
            # Check if Twilio is installed
            try:
                from twilio.rest import Client
            except ImportError:
                logger.error("Twilio is not installed. Install with 'pip install twilio'")
                return
            
            # Get Twilio credentials from environment variables
            account_sid = os.environ.get('TWILIO_ACCOUNT_SID')
            auth_token = os.environ.get('TWILIO_AUTH_TOKEN')
            from_number = os.environ.get('TWILIO_PHONE_NUMBER')
            to_numbers = self.sms_settings.get('to_numbers', [])
            
            if not (account_sid and auth_token and from_number and to_numbers):
                logger.error("Missing required Twilio settings")
                return
            
            # Create Twilio client
            client = Client(account_sid, auth_token)
            
            # Truncate message if too long for SMS
            max_sms_length = 160
            if len(message) > max_sms_length:
                message = message[:max_sms_length - 3] + "..."
            
            # Send SMS to each number
            for to_number in to_numbers:
                client.messages.create(
                    body=message,
                    from_=from_number,
                    to=to_number
                )
            
            logger.info(f"SMS notification sent to {to_numbers}")
            
        except Exception as e:
            logger.error(f"Failed to send SMS notification: {e}", exc_info=True)
