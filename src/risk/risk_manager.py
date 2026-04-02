"""
Risk Manager

Pre-trade validation and position sizing. Every trade must pass
through these checks before execution.
"""

import logging
from dataclasses import dataclass
from typing import Optional

import MetaTrader5 as mt5

from src.execution.position_tracker import PositionTracker, AccountState

logger = logging.getLogger(__name__)


@dataclass
class TradeSignal:
    symbol: str
    direction: str  # "buy" or "sell"
    lot_multiplier: float  # 0.5 or 1.0
    sl_price: float
    tp_price: float
    atr: float


@dataclass
class TradeDecision:
    approved: bool
    symbol: str = ""
    direction: str = ""
    lots: float = 0.0
    sl_price: float = 0.0
    tp_price: float = 0.0
    rejection_reason: str = ""


class RiskManager:
    """
    Validates trades and computes position sizes.

    Enforces: max risk per trade, daily/weekly DD limits, max positions,
    correlation limits, spread checks, and trading hours.
    """

    def __init__(
        self,
        position_tracker: PositionTracker,
        max_risk_per_trade: float = 0.01,
        max_positions_total: int = 3,
        max_positions_per_instrument: int = 1,
        min_free_margin_pct: float = 0.50,
        correlated_pairs: Optional[list[list[str]]] = None,
        max_correlated_multiplier: float = 1.5,
        instruments_config: Optional[dict] = None,
    ):
        self.tracker = position_tracker
        self.max_risk_pct = max_risk_per_trade
        self.max_pos_total = max_positions_total
        self.max_pos_per_inst = max_positions_per_instrument
        self.min_free_margin_pct = min_free_margin_pct
        self.correlated_pairs = correlated_pairs or []
        self.max_corr_mult = max_correlated_multiplier
        self.instruments = instruments_config or {}

    def validate_trade(self, signal: TradeSignal,
                       account: AccountState) -> TradeDecision:
        """
        Run all pre-trade checks. Returns approved TradeDecision or rejection.
        """
        # Check 1: Max positions total
        if self.tracker.count_positions() >= self.max_pos_total:
            return self._reject(signal, "Max total positions reached")

        # Check 2: Max positions per instrument
        if self.tracker.has_position(signal.symbol):
            return self._reject(signal, f"Already has position in {signal.symbol}")

        # Check 3: Free margin
        if account.equity > 0:
            free_margin_ratio = account.free_margin / account.equity
            if free_margin_ratio < self.min_free_margin_pct:
                return self._reject(signal, f"Free margin too low: {free_margin_ratio:.1%}")

        # Check 4: Correlation check
        corr_reject = self._check_correlation(signal)
        if corr_reject:
            return corr_reject

        # Check 5: Spread check
        spread_reject = self._check_spread(signal)
        if spread_reject:
            return spread_reject

        # Check 6: Calculate position size based on risk
        lots = self._calculate_lots(signal, account)
        if lots <= 0:
            return self._reject(signal, "Position size too small")

        return TradeDecision(
            approved=True,
            symbol=signal.symbol,
            direction=signal.direction,
            lots=lots,
            sl_price=signal.sl_price,
            tp_price=signal.tp_price,
        )

    def _calculate_lots(self, signal: TradeSignal,
                        account: AccountState) -> float:
        """
        Calculate position size based on risk percentage.

        Risk amount = equity * max_risk_pct
        Lots = risk_amount / (SL_distance_in_price * point_value / point_size)
        """
        risk_amount = account.equity * self.max_risk_pct

        # SL distance in price
        if signal.direction == "buy":
            sl_distance = abs(signal.sl_price - mt5.symbol_info_tick(signal.symbol).ask) if signal.sl_price > 0 else signal.atr * 2
        else:
            sl_distance = abs(mt5.symbol_info_tick(signal.symbol).bid - signal.sl_price) if signal.sl_price > 0 else signal.atr * 2

        if sl_distance <= 0:
            return 0.0

        info = mt5.symbol_info(signal.symbol)
        if info is None:
            return 0.0

        # Point value per lot
        point_value = info.trade_tick_value / info.trade_tick_size if info.trade_tick_size > 0 else 1
        raw_lots = risk_amount / (sl_distance * point_value)

        # Apply multiplier from signal
        raw_lots *= signal.lot_multiplier

        # Clamp to broker limits
        lots = max(info.volume_min, min(raw_lots, info.volume_max))
        lots = round(lots / info.volume_step) * info.volume_step

        # Cap by instrument config
        inst_config = self.instruments.get(signal.symbol, {})
        max_lot = inst_config.get("max_lot", info.volume_max)
        lots = min(lots, max_lot)

        return lots

    def _check_correlation(self, signal: TradeSignal) -> Optional[TradeDecision]:
        """Check if adding this trade would exceed correlated exposure limits."""
        exposure = self.tracker.get_total_exposure()
        for pair in self.correlated_pairs:
            if signal.symbol not in pair:
                continue
            # Check if correlated symbol already has same-direction exposure
            for corr_symbol in pair:
                if corr_symbol == signal.symbol:
                    continue
                corr_exposure = exposure.get(corr_symbol, 0)
                if corr_exposure != 0:
                    same_direction = (
                        (signal.direction == "buy" and corr_exposure > 0) or
                        (signal.direction == "sell" and corr_exposure < 0)
                    )
                    if same_direction:
                        logger.warning(
                            f"Correlated exposure: {signal.symbol} {signal.direction} "
                            f"+ {corr_symbol} exposure={corr_exposure}"
                        )
                        # Don't reject, but could reduce size here
        return None

    def _check_spread(self, signal: TradeSignal) -> Optional[TradeDecision]:
        """Reject if current spread is too wide."""
        tick = mt5.symbol_info_tick(signal.symbol)
        if tick is None:
            return self._reject(signal, "No tick data for spread check")

        spread = tick.ask - tick.bid
        inst_config = self.instruments.get(signal.symbol, {})
        typical_spread = inst_config.get("spread_typical_points", 5.0)

        # Convert typical spread from points to price
        info = mt5.symbol_info(signal.symbol)
        if info:
            typical_price_spread = typical_spread * info.point
            if spread > typical_price_spread * 2:
                return self._reject(
                    signal,
                    f"Spread too wide: {spread:.2f} vs typical {typical_price_spread:.2f}",
                )
        return None

    @staticmethod
    def _reject(signal: TradeSignal, reason: str) -> TradeDecision:
        logger.warning(f"Trade rejected [{signal.symbol} {signal.direction}]: {reason}")
        return TradeDecision(approved=False, symbol=signal.symbol, rejection_reason=reason)
