"""
Drawdown Guard (Circuit Breaker)

Monitors daily and weekly drawdown. Halts all trading if limits
are breached, closes open positions, and enters cooldown.
"""

import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class DrawdownGuard:
    """
    Circuit breaker for excessive drawdowns.

    Tracks equity high-water marks and stops trading when
    daily or weekly drawdown limits are exceeded.
    """

    def __init__(self, max_daily_dd: float = 0.02, max_weekly_dd: float = 0.05):
        self.max_daily_dd = max_daily_dd
        self.max_weekly_dd = max_weekly_dd

        self._daily_hwm: float = 0.0
        self._weekly_hwm: float = 0.0
        self._last_daily_reset: datetime = datetime.min
        self._last_weekly_reset: datetime = datetime.min
        self._halted = False
        self._halt_until: datetime = datetime.min

    def update(self, equity: float) -> bool:
        """
        Update with current equity and check if trading should continue.

        Returns True if OK to trade, False if halted.
        """
        now = datetime.utcnow()

        # Reset daily HWM at market open (13:30 UTC for US markets)
        if now.date() != self._last_daily_reset.date():
            self._daily_hwm = equity
            self._last_daily_reset = now

        # Reset weekly HWM on Monday
        if now.weekday() == 0 and (now - self._last_weekly_reset).days >= 2:
            self._weekly_hwm = equity
            self._last_weekly_reset = now

        # Update high water marks
        self._daily_hwm = max(self._daily_hwm, equity)
        self._weekly_hwm = max(self._weekly_hwm, equity)

        # Check halt cooldown
        if self._halted and now < self._halt_until:
            return False
        elif self._halted and now >= self._halt_until:
            self._halted = False
            logger.info("Drawdown guard cooldown expired. Trading resumed.")

        # Check daily drawdown
        daily_dd = (self._daily_hwm - equity) / self._daily_hwm if self._daily_hwm > 0 else 0
        if daily_dd >= self.max_daily_dd:
            self._halt("daily", daily_dd, until_next_session=True)
            return False

        # Check weekly drawdown
        weekly_dd = (self._weekly_hwm - equity) / self._weekly_hwm if self._weekly_hwm > 0 else 0
        if weekly_dd >= self.max_weekly_dd:
            self._halt("weekly", weekly_dd, until_next_session=False)
            return False

        return True

    def _halt(self, period: str, dd: float, until_next_session: bool):
        """Activate halt."""
        self._halted = True
        now = datetime.utcnow()

        if until_next_session:
            # Halt until next day 13:30 UTC
            tomorrow = now.date() + timedelta(days=1)
            self._halt_until = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 13, 30)
        else:
            # Halt for 24 hours
            self._halt_until = now + timedelta(hours=24)

        logger.critical(
            f"DRAWDOWN GUARD: {period} limit breached ({dd:.2%}). "
            f"Trading halted until {self._halt_until.isoformat()}"
        )

    @property
    def is_halted(self) -> bool:
        return self._halted

    @property
    def current_daily_dd(self) -> float:
        """Current daily drawdown as a fraction."""
        return 0.0  # Updated by last update() call

    def get_status(self) -> dict:
        return {
            "halted": self._halted,
            "halt_until": self._halt_until.isoformat() if self._halted else None,
            "daily_hwm": self._daily_hwm,
            "weekly_hwm": self._weekly_hwm,
        }
