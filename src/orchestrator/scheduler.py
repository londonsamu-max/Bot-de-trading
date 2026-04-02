"""
Trading Scheduler

Controls which instruments are active based on market hours.
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class TradingScheduler:
    """
    Determines which instruments can be traded at the current time.

    Each instrument has configured trading hours (UTC).
    """

    def __init__(self, instruments_config: dict):
        """
        Args:
            instruments_config: Dict from symbols.yaml with trading_hours_utc per instrument.
        """
        self.instruments = instruments_config

    def get_active_instruments(self) -> list[str]:
        """Return list of instrument names currently within trading hours."""
        now = datetime.utcnow()
        active = []

        for name, config in self.instruments.items():
            hours_str = config.get("trading_hours_utc", "00:00-23:59")
            if self._is_within_hours(now, hours_str):
                active.append(name)

        return active

    def is_active(self, instrument: str) -> bool:
        """Check if a specific instrument is within trading hours."""
        config = self.instruments.get(instrument)
        if not config:
            return False
        hours_str = config.get("trading_hours_utc", "00:00-23:59")
        return self._is_within_hours(datetime.utcnow(), hours_str)

    def time_until_next_session(self) -> float:
        """Seconds until any instrument becomes active. Returns 0 if already active."""
        if self.get_active_instruments():
            return 0.0

        # Simple: wait 60 seconds and check again
        return 60.0

    @staticmethod
    def _is_within_hours(now: datetime, hours_str: str) -> bool:
        """
        Check if current time is within trading hours.

        Format: "HH:MM-HH:MM" in UTC.
        Handles overnight ranges (e.g. "22:00-06:00").
        """
        try:
            start_str, end_str = hours_str.split("-")
            start_h, start_m = map(int, start_str.split(":"))
            end_h, end_m = map(int, end_str.split(":"))

            current_minutes = now.hour * 60 + now.minute
            start_minutes = start_h * 60 + start_m
            end_minutes = end_h * 60 + end_m

            if start_minutes <= end_minutes:
                return start_minutes <= current_minutes <= end_minutes
            else:
                # Overnight range
                return current_minutes >= start_minutes or current_minutes <= end_minutes

        except (ValueError, AttributeError):
            logger.error(f"Invalid trading hours format: {hours_str}")
            return False

    @staticmethod
    def is_weekend() -> bool:
        """Check if today is Saturday or Sunday (markets closed)."""
        return datetime.utcnow().weekday() >= 5
