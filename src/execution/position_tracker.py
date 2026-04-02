"""
Position Tracker

Monitors open positions, floating P&L, and exposure for the bot's trades.
Only tracks positions with the bot's magic number.
"""

import logging
from dataclasses import dataclass
from typing import Optional

import MetaTrader5 as mt5

logger = logging.getLogger(__name__)


@dataclass
class Position:
    ticket: int
    symbol: str
    direction: str  # "buy" or "sell"
    volume: float
    open_price: float
    current_price: float
    sl: float
    tp: float
    profit: float
    time_open: int  # Unix timestamp


@dataclass
class AccountState:
    balance: float
    equity: float
    margin: float
    free_margin: float
    margin_level: float
    profit: float


class PositionTracker:
    """Tracks bot positions and account state via MT5."""

    def __init__(self, magic_number: int = 123456):
        self.magic = magic_number

    def get_positions(self, symbol: Optional[str] = None) -> list[Position]:
        """Get all open positions for the bot, optionally filtered by symbol."""
        if symbol:
            raw = mt5.positions_get(symbol=symbol)
        else:
            raw = mt5.positions_get()

        if raw is None:
            return []

        positions = []
        for p in raw:
            if p.magic != self.magic:
                continue
            positions.append(Position(
                ticket=p.ticket,
                symbol=p.symbol,
                direction="buy" if p.type == mt5.ORDER_TYPE_BUY else "sell",
                volume=p.volume,
                open_price=p.price_open,
                current_price=p.price_current,
                sl=p.sl,
                tp=p.tp,
                profit=p.profit,
                time_open=p.time,
            ))
        return positions

    def get_position_for_symbol(self, symbol: str) -> Optional[Position]:
        """Get the bot's position for a specific symbol (None if flat)."""
        positions = self.get_positions(symbol)
        return positions[0] if positions else None

    def get_total_exposure(self) -> dict[str, float]:
        """Get net lot exposure per symbol. Positive = long, negative = short."""
        exposure: dict[str, float] = {}
        for pos in self.get_positions():
            sign = 1.0 if pos.direction == "buy" else -1.0
            exposure[pos.symbol] = exposure.get(pos.symbol, 0.0) + sign * pos.volume
        return exposure

    def get_floating_pnl(self) -> dict[str, float]:
        """Get floating P&L per symbol."""
        pnl: dict[str, float] = {}
        for pos in self.get_positions():
            pnl[pos.symbol] = pnl.get(pos.symbol, 0.0) + pos.profit
        return pnl

    def get_total_floating_pnl(self) -> float:
        """Get total floating P&L across all bot positions."""
        return sum(self.get_floating_pnl().values())

    def get_account_state(self) -> AccountState:
        """Get current account state from MT5."""
        info = mt5.account_info()
        if info is None:
            logger.error("Failed to get account info")
            return AccountState(0, 0, 0, 0, 0, 0)
        return AccountState(
            balance=info.balance,
            equity=info.equity,
            margin=info.margin,
            free_margin=info.margin_free,
            margin_level=info.margin_level if info.margin_level else 0,
            profit=info.profit,
        )

    def has_position(self, symbol: str) -> bool:
        """Check if bot has any open position for symbol."""
        return self.get_position_for_symbol(symbol) is not None

    def count_positions(self) -> int:
        """Count total open bot positions."""
        return len(self.get_positions())
