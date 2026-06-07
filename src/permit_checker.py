"""
Permit Checker module for checking availability of permits on recreation.gov.

Talks to the public recreation.gov availability API, which returns a full
month of availability per request:

    GET /api/permits/{permit_id}/availability/month
        ?start_date=YYYY-MM-01T00:00:00.000Z&commercial_acct=false

Response shape (trimmed):

    {"payload": {"availability": {
        "<division_id>": {
            "division_id": "<division_id>",
            "date_availability": {
                "2026-07-01T00:00:00Z": {"total": 275, "remaining": 85, ...},
                ...
            }
        }, ...
    }}}

Implements rate limiting and user-agent randomization to be a polite client.
"""

import json
import logging
import random
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from tenacity import retry, wait_exponential, stop_after_attempt

logger = logging.getLogger(__name__)

# A reasonable, current desktop UA used when randomization is disabled or
# fake-useragent is unavailable.
_FALLBACK_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class PermitChecker:
    """Checks for permit availability on recreation.gov."""

    def __init__(self, app_config, permits_config, notifier, state_path=None):
        """
        Initialize the PermitChecker.

        Args:
            app_config (dict): Application configuration.
            permits_config (dict): Permits configuration.
            notifier (Notifier): Notifier instance for sending notifications.
            state_path (Path or str, optional): File used to persist the
                "already notified" map across runs (so a `--once` cron job
                doesn't re-alert about the same opening every invocation).
        """
        self.app_config = app_config
        self.permits_config = permits_config
        self.notifier = notifier
        self.state_path = Path(state_path) if state_path else None

        # Request configuration
        self.base_url = app_config.get('app', {}).get(
            'base_url', 'https://www.recreation.gov/api'
        ).rstrip('/')
        self.randomize_user_agent = app_config.get('app', {}).get('randomize_user_agent', True)

        # Request delay settings
        request_config = app_config.get('request', {})
        self.min_delay = request_config.get('delay', {}).get('min', 10)
        self.max_delay = request_config.get('delay', {}).get('max', 30)
        self.jitter = request_config.get('jitter', 0.2)
        self.timeout = request_config.get('timeout', 30)

        # Map of "permit/division/date" -> last-notified datetime, used with the
        # cooldown to avoid duplicate alerts. Loaded from disk if available.
        self.found_availabilities = self._load_state()

        # Reuse a session for connection pooling.
        self.session = requests.Session()

        # Optional random user agents. fake-useragent reaches out to the
        # network on first use and can fail; degrade gracefully if so.
        self._user_agent_gen = None
        if self.randomize_user_agent:
            try:
                from fake_useragent import UserAgent
                self._user_agent_gen = UserAgent()
            except Exception as e:  # noqa: BLE001 - any failure -> fallback UA
                logger.warning(f"fake-useragent unavailable, using a static UA: {e}")
                self.randomize_user_agent = False

        logger.info("PermitChecker initialized")

    def check_all_permits(self):
        """Check all configured permits for availability."""
        logger.info("Checking all permits for availability...")

        permits = self.permits_config.get('permits', {})
        # Support both the categorized layout (trails/rivers/...) and a flat list.
        if isinstance(permits, list):
            groups = [permits]
        else:
            groups = permits.values()

        first = True
        for group in groups:
            for permit in group or []:
                if not first:
                    self._add_random_delay()
                first = False
                self._check_permit(permit)

        logger.info("Completed checking all permits.")
        self._save_state()

    def _load_state(self):
        """Load the already-notified map from disk; return {} if unavailable."""
        if not self.state_path or not self.state_path.exists():
            return {}
        try:
            raw = json.loads(self.state_path.read_text())
            return {k: datetime.fromisoformat(v) for k, v in raw.items()}
        except (ValueError, OSError) as e:
            logger.warning(f"Could not read state file {self.state_path}: {e}")
            return {}

    def _save_state(self):
        """Persist the already-notified map to disk, if a path is configured."""
        if not self.state_path:
            return
        try:
            serializable = {k: v.isoformat() for k, v in self.found_availabilities.items()}
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(serializable, indent=2))
        except OSError as e:
            logger.warning(f"Could not write state file {self.state_path}: {e}")

    def _check_permit(self, permit):
        """
        Check a single permit across its configured date window.

        Args:
            permit (dict): Permit configuration.
        """
        permit_id = permit.get('id')
        permit_name = permit.get('name', permit_id)

        if not permit_id:
            logger.warning(f"Skipping permit with no id: {permit_name}")
            return

        try:
            start_date, end_date = self._resolve_window(permit)
        except (TypeError, ValueError) as e:
            logger.error(f"Invalid dates for permit {permit_name}: {e}")
            return

        logger.info(
            f"Checking permit: {permit_name} (ID: {permit_id}) "
            f"{start_date.date()} -> {end_date.date()}"
        )

        # Optional division (entry/launch point) filter. Recreation.gov divisions
        # are identified by numeric ids; accept ints or strings.
        wanted_divisions = {
            str(d) for d in (permit.get('divisions') or [])
        }

        # remaining must be at least this many to count as available.
        min_remaining = int(permit.get('min_remaining', 1))

        # Recreation.gov exposes two different availability APIs. River/standard
        # permits use the "month" endpoint; Inyo and SEKI wilderness permits use
        # the "inyo" availabilityv2 endpoint with a different response shape.
        api_style = permit.get('api', 'month')

        # division_id -> {date(): remaining}
        availability = {}
        months = self._months_in_range(start_date, end_date)
        for i, month_start in enumerate(months):
            if i > 0:
                self._add_random_delay()
            data = self._fetch_month(permit_id, month_start, api_style)
            self._merge_month(availability, data, start_date, end_date, api_style)

        if not availability:
            logger.info(f"No availability data returned for {permit_name}.")
            return

        # A single open entry/launch date is all that's needed: one permit covers
        # the whole trip. So alert on every open date in each watched division.
        found_any = False
        for division_id, dates in availability.items():
            if wanted_divisions and division_id not in wanted_divisions:
                continue

            for d in sorted(dates):
                if dates[d] >= min_remaining:
                    found_any = True
                    self._maybe_notify(permit, division_id, d, dates[d])

        if not found_any:
            logger.info(f"No availability found for {permit_name} in the requested window.")

    def _resolve_window(self, permit):
        """Return (start_date, end_date) applying configured date flexibility."""
        dates = permit.get('dates', {})
        start_date = datetime.strptime(dates['start'], "%Y-%m-%d")
        end_date = datetime.strptime(dates['end'], "%Y-%m-%d")

        flex = self.permits_config.get('search_options', {}).get('date_flexibility', {})
        if flex.get('enabled', False):
            start_date -= timedelta(days=int(flex.get('days_before', 0)))
            end_date += timedelta(days=int(flex.get('days_after', 0)))

        if end_date < start_date:
            raise ValueError("end date precedes start date")
        return start_date, end_date

    @staticmethod
    def _months_in_range(start_date, end_date):
        """First-of-month datetimes covering [start_date, end_date]."""
        months = []
        year, month = start_date.year, start_date.month
        while (year, month) <= (end_date.year, end_date.month):
            months.append(datetime(year, month, 1))
            month += 1
            if month > 12:
                month, year = 1, year + 1
        return months

    @retry(
        wait=wait_exponential(multiplier=1, min=4, max=60),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    def _fetch_month(self, permit_id, month_start, api_style='month'):
        """
        Fetch one month of availability. Retries on transient errors.

        Args:
            permit_id (str): The recreation.gov permit id.
            month_start (datetime): First day of the month to fetch.
            api_style (str): "month" (standard /availability/month) or
                "inyo" (/permitinyo/{id}/availabilityv2).

        Returns:
            dict: Parsed JSON payload, or None on a non-retryable failure
                  (e.g. a disabled/unknown permit).
        """
        if api_style == 'inyo':
            url = f"{self.base_url}/permitinyo/{permit_id}/availabilityv2"
            params = {
                'start_date': month_start.strftime("%Y-%m-%d"),
                'commercial_acct': 'false',
            }
        else:
            url = f"{self.base_url}/permits/{permit_id}/availability/month"
            params = {
                'start_date': month_start.strftime("%Y-%m-01T00:00:00.000Z"),
                'commercial_acct': 'false',
            }

        try:
            response = self.session.get(
                url, headers=self._get_request_headers(), params=params, timeout=self.timeout
            )
        except requests.RequestException as e:
            logger.error(f"Request error for permit {permit_id}: {e}")
            raise

        if response.status_code == 200:
            return response.json()

        # 429 / 5xx are transient: raise so tenacity backs off and retries.
        if response.status_code == 429 or response.status_code >= 500:
            logger.warning(
                f"Transient {response.status_code} for permit {permit_id}; will retry."
            )
            response.raise_for_status()

        # 4xx (other than 429): permit disabled, bad id, etc. Don't retry.
        logger.warning(
            f"Request for permit {permit_id} failed with "
            f"{response.status_code}: {response.text[:200]}"
        )
        return None

    @classmethod
    def _merge_month(cls, availability, data, start_date, end_date, api_style='month'):
        """Merge a month payload into the division->{date: remaining} map."""
        if not data:
            return
        payload = data.get('payload', data)
        if api_style == 'inyo':
            cls._merge_inyo(availability, payload, start_date, end_date)
        else:
            cls._merge_standard(availability, payload, start_date, end_date)

    @staticmethod
    def _merge_standard(availability, payload, start_date, end_date):
        """Parse the standard /availability/month shape."""
        for division_id, division in (payload.get('availability', {}) or {}).items():
            date_map = division.get('date_availability', {}) or {}
            bucket = availability.setdefault(str(division_id), {})
            for date_key, info in date_map.items():
                try:
                    d = datetime.strptime(date_key[:10], "%Y-%m-%d").date()
                except ValueError:
                    continue
                if start_date.date() <= d <= end_date.date():
                    bucket[d] = int(info.get('remaining', 0))

    @staticmethod
    def _merge_inyo(availability, payload, start_date, end_date):
        """Parse the Inyo/SEKI /permitinyo availabilityv2 shape.

        payload is keyed by date string, then division id:
            {"2026-07-01": {"495": {"quota_usage_by_member_daily":
                {"total": 15, "remaining": 3}, ...}}}
        """
        for date_key, divisions in (payload or {}).items():
            try:
                d = datetime.strptime(date_key[:10], "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue
            if not (start_date.date() <= d <= end_date.date()):
                continue
            if not isinstance(divisions, dict):
                continue
            for division_id, info in divisions.items():
                if not isinstance(info, dict):
                    continue
                quota = info.get('quota_usage_by_member_daily', {}) or {}
                availability.setdefault(str(division_id), {})[d] = int(quota.get('remaining', 0))

    def _maybe_notify(self, permit, division_id, date, remaining):
        """Send a notification for an open entry/launch date, respecting cooldown."""
        permit_id = permit.get('id')
        permit_name = permit.get('name', permit_id)
        people = permit.get('people', 1)

        when = date.isoformat()
        permit_key = f"{permit_id}_{division_id}_{when}"
        cooldown_hours = self.app_config.get('notification', {}).get('cooldown', 24)
        last = self.found_availabilities.get(permit_key)
        if last and (datetime.now() - last).total_seconds() < cooldown_hours * 3600:
            logger.info(f"Skipping notification for {permit_name} ({when}); within cooldown.")
            return

        logger.info(f"AVAILABLE: {permit_name} division {division_id} {when} (remaining {remaining})")
        self.notifier.send_notification(
            subject=f"Permit Available: {permit_name}",
            message=(
                f"{permit_name}\n"
                f"Dates: {when}\n"
                f"Entry/division: {division_id}\n"
                f"Spots remaining: {remaining} (party size {people})\n"
                f"Book now: https://www.recreation.gov/permits/{permit_id}"
            ),
        )
        self.found_availabilities[permit_key] = datetime.now()

    def _get_request_headers(self):
        """Request headers, with an optional random user agent."""
        if self.randomize_user_agent and self._user_agent_gen is not None:
            try:
                user_agent = self._user_agent_gen.random
            except Exception:  # noqa: BLE001
                user_agent = _FALLBACK_USER_AGENT
        else:
            user_agent = _FALLBACK_USER_AGENT

        return {
            'User-Agent': user_agent,
            'Accept': 'application/json',
            'Connection': 'keep-alive',
            'Referer': 'https://www.recreation.gov/',
            'DNT': '1',
            'Accept-Language': 'en-US,en;q=0.9',
        }

    def _add_random_delay(self):
        """Sleep a randomized, jittered interval between requests."""
        base_delay = random.uniform(self.min_delay, self.max_delay)
        jitter_amount = base_delay * self.jitter
        delay = max(0.0, base_delay + random.uniform(-jitter_amount, jitter_amount))
        logger.debug(f"Sleeping {delay:.1f}s between requests")
        time.sleep(delay)
