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
from datetime import date as Date
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


def _date_iso(value):
    if isinstance(value, Date):
        return value.isoformat()
    return str(value)[:10]


def format_permit_date(value):
    """Format a permit date for alert messages, e.g. Friday, 8/14/26."""
    if isinstance(value, Date):
        d = value
    else:
        d = datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    return f"{d.strftime('%A')}, {d.month}/{d.day}/{d.strftime('%y')}"


def format_permit_dates(values, new_dates=None):
    """Return formatted dates, marking dates present in new_dates as NEW."""
    new_date_set = {_date_iso(d) for d in (new_dates or [])}
    return [
        f"{format_permit_date(value)}{' (NEW)' if _date_iso(value) in new_date_set else ''}"
        for value in values
    ]


def _date_bullets(values, new_dates=None):
    return "\n".join(f"- {d}" for d in format_permit_dates(values, new_dates))


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

        # Map of "permitid_divisionid" -> state for current and already-alerted
        # dates. Loaded from disk if available.
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
        """Load the already-notified map from disk; return {} if unavailable.

        Maps "permitid_divisionid" -> {
            "current_dates": [...],
            "alerted_dates": [...],
            "notified_at": iso,
            "checked_at": iso,
        }.

        Older state files used {"dates": [...], "notified_at": iso}; those are
        normalized lazily when each segment is checked.
        """
        if not self.state_path or not self.state_path.exists():
            return {}
        try:
            data = json.loads(self.state_path.read_text())
            if not isinstance(data, dict):
                return {}
            # Keep only entries in the current schema; drop legacy/foreign ones.
            return {k: v for k, v in data.items() if isinstance(v, dict)}
        except (ValueError, OSError) as e:
            logger.warning(f"Could not read state file {self.state_path}: {e}")
            return {}

    def _save_state(self):
        """Persist the already-notified map to disk, if a path is configured."""
        if not self.state_path:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(self.found_availabilities, indent=2))
        except OSError as e:
            logger.warning(f"Could not write state file {self.state_path}: {e}")

    @staticmethod
    def _state_dates(entry, field):
        values = entry.get(field, [])
        if not isinstance(values, list):
            return []
        return [_date_iso(value) for value in values]

    def _check_permit(self, permit):
        """Check one permit and alert (on newly-appeared dates) per open segment."""
        segments = self._open_segments(permit)
        if segments is None:
            return
        permit_name = permit.get('name', permit.get('id'))
        found_any = False
        for division_id, open_dates in segments:
            if open_dates:
                found_any = True
            self._maybe_notify_segment(permit, division_id, open_dates)
        if not found_any:
            logger.info(f"No availability found for {permit_name} in the requested window.")

    def _open_segments(self, permit):
        """
        Fetch availability for one permit and return a list of
        (division_id, open_dates) for each watched division (open_dates may be
        empty). Returns None if the permit is misconfigured or its window is
        invalid/entirely in the past.
        """
        permit_id = permit.get('id')
        permit_name = permit.get('name', permit_id)

        if not permit_id:
            logger.warning(f"Skipping permit with no id: {permit_name}")
            return None

        try:
            start_date, end_date = self._resolve_window(permit)
        except (TypeError, ValueError) as e:
            logger.error(f"Invalid/elapsed window for permit {permit_name}: {e}")
            return None

        logger.info(
            f"Checking permit: {permit_name} (ID: {permit_id}) "
            f"{start_date.date()} -> {end_date.date()}"
        )

        # Optional division (entry/launch point) filter. Recreation.gov divisions
        # are identified by numeric ids; accept ints or strings.
        wanted_divisions = {str(d) for d in (permit.get('divisions') or [])}

        # Recreation.gov exposes two different availability APIs. River/standard
        # permits use the "month" endpoint; Inyo and SEKI wilderness permits use
        # the "inyo" availabilityv2 endpoint with a different response shape.
        api_style = permit.get('api', 'month')

        # How many open slots count as "available". The APIs count different
        # things: the month API's `remaining` is launches/permits (one covers the
        # whole group, so >=1 is enough), while the inyo API's `remaining` is a
        # per-person quota, so a party needs at least `people` person-slots.
        # Override either with an explicit min_remaining on the permit.
        if permit.get('min_remaining') is not None:
            min_remaining = int(permit['min_remaining'])
        elif api_style == 'inyo':
            min_remaining = int(permit.get('people', 1))
        else:
            min_remaining = 1

        # division_id -> {date(): remaining}
        availability = {}
        for i, month_start in enumerate(self._months_in_range(start_date, end_date)):
            if i > 0:
                self._add_random_delay()
            data = self._fetch_month(permit_id, month_start, api_style)
            self._merge_month(availability, data, start_date, end_date, api_style)

        segments = []
        for division_id, dates in availability.items():
            if wanted_divisions and division_id not in wanted_divisions:
                continue
            open_dates = sorted(d for d, rem in dates.items() if rem >= min_remaining)
            segments.append((division_id, open_dates))
        return segments

    def collect_open_segments(self):
        """
        Return [(permit_name, segment_label, [iso dates])] for every currently
        open segment across all permits. Used for on-demand summaries; does not
        touch notification state.
        """
        results = []
        permits = self.permits_config.get('permits', {})
        groups = [permits] if isinstance(permits, list) else permits.values()

        first = True
        for group in groups:
            for permit in group or []:
                if not first:
                    self._add_random_delay()
                first = False
                segments = self._open_segments(permit)
                if not segments:
                    continue
                names = permit.get('division_names', {})
                for division_id, open_dates in segments:
                    if open_dates:
                        results.append((
                            permit.get('name', permit.get('id')),
                            names.get(str(division_id), str(division_id)),
                            [d.isoformat() for d in open_dates],
                        ))
        return results

    def _resolve_window(self, permit):
        """Return (start_date, end_date) applying configured date flexibility."""
        dates = permit.get('dates', {})
        start_date = datetime.strptime(dates['start'], "%Y-%m-%d")
        end_date = datetime.strptime(dates['end'], "%Y-%m-%d")

        flex = self.permits_config.get('search_options', {}).get('date_flexibility', {})
        if flex.get('enabled', False):
            start_date -= timedelta(days=int(flex.get('days_before', 0)))
            end_date += timedelta(days=int(flex.get('days_after', 0)))

        # Ignore dates in the past or too soon to act on — recreation.gov also
        # reports stale availability for elapsed dates. min_lead_days adds the
        # buffer you need to actually plan/travel (default 2 = ~48 hours).
        lead_days = int(self.permits_config.get('search_options', {}).get('min_lead_days', 2))
        earliest = (datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                    + timedelta(days=lead_days))
        if start_date < earliest:
            start_date = earliest

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

    def _maybe_notify_segment(self, permit, division_id, open_dates):
        """
        Send one message per segment listing all open dates, but only when a
        date appears that we haven't already alerted on (so a frequent cron run
        doesn't re-send the same opening every time).

        Args:
            permit (dict): Permit configuration.
            division_id (str): The division/segment id.
            open_dates (list[date]): Open dates within the window, ascending.
        """
        permit_id = permit.get('id')
        permit_name = permit.get('name', permit_id)
        seg = permit.get('division_names', {}).get(str(division_id), str(division_id))
        key = f"{permit_id}_{division_id}"

        current = [d.isoformat() for d in open_dates]
        prior_entry = self.found_availabilities.get(key, {})
        if not isinstance(prior_entry, dict):
            prior_entry = {}

        # Backward compatibility: old state used "dates" for both current and
        # already-notified dates.
        alerted_dates = set(self._state_dates(prior_entry, 'alerted_dates'))
        if not alerted_dates:
            alerted_dates.update(self._state_dates(prior_entry, 'dates'))

        new_dates = [d for d in current if d not in alerted_dates]
        now = datetime.now().isoformat()

        # Always remember the latest open set. Keep alerted_dates even when a
        # segment temporarily goes empty so API flicker/reopenings do not repeat
        # the same date until state.json is intentionally wiped.
        alerted_dates.update(new_dates)
        self.found_availabilities[key] = {
            'current_dates': current,
            'alerted_dates': sorted(alerted_dates),
            'notified_at': now if new_dates else prior_entry.get('notified_at'),
            'checked_at': now,
        }
        self._save_state()

        if not open_dates:
            logger.info(f"{permit_name} / {seg}: no open dates; keeping alert history.")
            return

        if not new_dates:
            logger.info(
                f"{permit_name} / {seg}: {len(current)} open date(s), none new since "
                f"last alert; skipping."
            )
            return

        people = permit.get('people', 1)
        logger.info(
            f"AVAILABLE: {permit_name} / {seg}: {len(new_dates)} new of "
            f"{len(current)} open date(s) -> notifying"
        )
        self.notifier.send_notification(
            subject=f"Permit Available: {permit_name} - {seg}",
            message=(
                f"{permit_name}\n"
                f"Segment: {seg}\n"
                f"New date(s) [{len(new_dates)}]:\n"
                f"{_date_bullets(new_dates)}\n"
                f"All currently open [{len(current)}]:\n"
                f"{_date_bullets(current, new_dates)}\n"
                f"Party size: {people}\n"
                f"Book now: https://www.recreation.gov/permits/{permit_id}"
            ),
        )

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
