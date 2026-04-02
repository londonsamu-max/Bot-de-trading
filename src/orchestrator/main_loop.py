"""
Main Trading Loop (Orchestrator)

The heartbeat of the trading bot. Runs every tick_interval:
1. Check if within trading hours
2. Check drawdown guard
3. Fetch SpotGamma data
4. Fetch price data from MT5
5. Build features -> observation
6. Query PPO agent for action
7. Validate through risk manager
8. Execute order via MT5
9. Manage open positions (trailing stops, breakeven)
10. Log metrics
"""

import logging
import signal
import time
from typing import Optional

import numpy as np

from src.data.spotgamma_client import SpotGammaClient
from src.data.mt5_market_data import MT5MarketData
from src.data.feature_engine import FeatureEngine
from src.data.state_builder import StateBuilder
from src.agent.ppo_agent import PPOAgent
from src.execution.mt5_connector import MT5Connector
from src.execution.order_manager import OrderManager
from src.execution.position_tracker import PositionTracker
from src.risk.risk_manager import RiskManager, TradeSignal
from src.risk.drawdown_guard import DrawdownGuard
from src.orchestrator.scheduler import TradingScheduler

logger = logging.getLogger(__name__)


class TradingOrchestrator:
    """
    Main trading loop that wires all components together.

    One PPO agent per instrument (NAS100, US30, XAUUSD).
    Runs on a configurable interval (default 5 minutes).
    """

    def __init__(
        self,
        mt5_connector: MT5Connector,
        spotgamma_client: SpotGammaClient,
        agents: dict[str, PPOAgent],
        instruments_config: dict,
        risk_config: dict,
        tick_interval: int = 300,
        mode: str = "paper",
    ):
        self.mt5 = mt5_connector
        self.sg_client = spotgamma_client
        self.agents = agents  # {instrument_name: PPOAgent}
        self.instruments = instruments_config
        self.tick_interval = tick_interval
        self.mode = mode

        # Components
        self.market_data = MT5MarketData()
        self.feature_engines: dict[str, FeatureEngine] = {
            name: FeatureEngine() for name in instruments_config
        }
        self.state_builder = StateBuilder()
        self.order_manager = OrderManager(
            magic_number=mt5_connector.magic_number,
        )
        self.position_tracker = PositionTracker(
            magic_number=mt5_connector.magic_number,
        )
        self.risk_manager = RiskManager(
            position_tracker=self.position_tracker,
            max_risk_per_trade=risk_config.get("max_risk_per_trade_pct", 0.01),
            max_positions_total=risk_config.get("max_positions_total", 3),
            max_positions_per_instrument=risk_config.get("max_positions_per_instrument", 1),
            min_free_margin_pct=risk_config.get("min_free_margin_pct", 0.50),
            correlated_pairs=risk_config.get("correlated_pairs", []),
            instruments_config=instruments_config,
        )
        self.drawdown_guard = DrawdownGuard(
            max_daily_dd=risk_config.get("max_daily_drawdown_pct", 0.02),
            max_weekly_dd=risk_config.get("max_weekly_drawdown_pct", 0.05),
        )
        self.scheduler = TradingScheduler(instruments_config)

        self._running = False
        self._setup_signal_handlers()

    def _setup_signal_handlers(self):
        """Graceful shutdown on SIGINT/SIGTERM."""
        signal.signal(signal.SIGINT, self._shutdown_handler)
        signal.signal(signal.SIGTERM, self._shutdown_handler)

    def _shutdown_handler(self, signum, frame):
        logger.info(f"Shutdown signal received ({signum}). Stopping...")
        self._running = False

    def run(self):
        """Main trading loop."""
        logger.info(f"Starting trading bot in {self.mode} mode")
        logger.info(f"Instruments: {list(self.instruments.keys())}")
        logger.info(f"Tick interval: {self.tick_interval}s")

        if not self.mt5.connect():
            logger.error("Failed to connect to MT5. Exiting.")
            return

        # Discover symbols
        symbol_map = {}
        for name, config in self.instruments.items():
            symbol = self.mt5.discover_symbol(
                config["mt5_symbol"],
                config.get("mt5_alternatives", []),
            )
            if symbol:
                symbol_map[name] = symbol
            else:
                logger.error(f"Could not find symbol for {name}. Skipping.")

        # Authenticate SpotGamma
        if not self.sg_client.authenticate():
            logger.warning("SpotGamma auth failed. Running without gamma data.")

        self._running = True
        while self._running:
            loop_start = time.time()

            try:
                self._tick(symbol_map)
            except Exception as e:
                logger.error(f"Loop error: {e}", exc_info=True)

            # Sleep until next tick
            elapsed = time.time() - loop_start
            sleep_time = max(0, self.tick_interval - elapsed)
            if sleep_time > 0 and self._running:
                time.sleep(sleep_time)

        # Shutdown
        logger.info("Shutting down...")
        if self.mode == "live":
            logger.info("Closing all positions...")
            self.order_manager.close_all()
        self.mt5.disconnect()
        logger.info("Bot stopped.")

    def _tick(self, symbol_map: dict[str, str]):
        """Single iteration of the trading loop."""
        # Skip weekends
        if self.scheduler.is_weekend():
            return

        # Get active instruments
        active = self.scheduler.get_active_instruments()
        if not active:
            return

        # Check account & drawdown
        account = self.position_tracker.get_account_state()
        if not self.drawdown_guard.update(account.equity):
            logger.warning("Drawdown guard active. Closing all positions.")
            if self.mode == "live":
                self.order_manager.close_all()
            return

        # Process each active instrument
        for inst_name in active:
            if inst_name not in symbol_map:
                continue
            if inst_name not in self.agents:
                continue

            mt5_symbol = symbol_map[inst_name]
            gamma_ticker = self.instruments[inst_name].get("gamma_ticker", "")

            try:
                self._process_instrument(
                    inst_name, mt5_symbol, gamma_ticker, account,
                )
            except Exception as e:
                logger.error(f"Error processing {inst_name}: {e}", exc_info=True)

    def _process_instrument(self, inst_name: str, mt5_symbol: str,
                            gamma_ticker: str, account):
        """Process one instrument: data -> features -> PPO -> risk -> execute."""
        config = self.instruments[inst_name]

        # 1. Fetch price data
        price_data = self.market_data.get_multi_timeframe(mt5_symbol)
        if not price_data:
            logger.warning(f"No price data for {mt5_symbol}")
            return

        current_price_info = self.market_data.get_current_price(mt5_symbol)
        if not current_price_info:
            return
        current_price = current_price_info["bid"]

        # 2. Fetch gamma data
        gamma = self.sg_client.get_snapshot(gamma_ticker)

        # 3. Build position state
        position = self.position_tracker.get_position_for_symbol(mt5_symbol)
        position_state = {
            "direction": 0,
            "size_normalized": 0.0,
            "unrealized_pnl_normalized": 0.0,
            "bars_in_position": 0,
            "distance_to_sl": 0.0,
        }
        if position:
            pos_dir = 1 if position.direction == "buy" else -1
            position_state["direction"] = pos_dir
            position_state["size_normalized"] = position.volume / config.get("base_lot", 0.1)
            position_state["unrealized_pnl_normalized"] = position.profit / max(account.equity, 1)

        # 4. Compute features
        features = self.feature_engines[inst_name].compute(
            gamma=gamma,
            price_data=price_data,
            current_price=current_price,
            position_state=position_state,
        )

        # 5. Build observation
        obs = self.state_builder.build(features)

        # 6. Get PPO action
        action, info = self.agents[inst_name].predict(obs, deterministic=True)
        logger.info(f"[{inst_name}] Action: {info['action_name']} | Price: {current_price}")

        # 7. Translate action to trade signal
        if action == 0:  # Hold
            self._manage_existing_position(inst_name, mt5_symbol, price_data, gamma)
            return

        # Determine direction and size
        if action in (1, 2):
            direction = "buy"
            lot_mult = 0.5 if action == 1 else 1.0
        else:
            direction = "sell"
            lot_mult = 0.5 if action == 3 else 1.0

        # Skip if already in same direction
        if position:
            current_dir = "buy" if position.direction == "buy" else "sell"
            if current_dir == direction:
                self._manage_existing_position(inst_name, mt5_symbol, price_data, gamma)
                return
            # Close existing before reversing
            if self.mode == "live":
                self.order_manager.close_position(position.ticket)
            else:
                logger.info(f"[PAPER] Would close {inst_name} position {position.ticket}")

        # Calculate SL/TP
        m5_data = price_data.get("M5")
        atr = self._compute_atr(m5_data) if m5_data is not None else 1.0
        sl_mult = config.get("sl_atr_multiple", 2.0)
        tp_mult = config.get("tp_atr_multiple", 3.0)

        if direction == "buy":
            sl_price = current_price - atr * sl_mult
            tp_price = current_price + atr * tp_mult
        else:
            sl_price = current_price + atr * sl_mult
            tp_price = current_price - atr * tp_mult

        # 8. Risk validation
        signal = TradeSignal(
            symbol=mt5_symbol,
            direction=direction,
            lot_multiplier=lot_mult,
            sl_price=sl_price,
            tp_price=tp_price,
            atr=atr,
        )
        decision = self.risk_manager.validate_trade(signal, account)

        if not decision.approved:
            logger.info(f"[{inst_name}] Trade rejected: {decision.rejection_reason}")
            return

        # 9. Execute
        if self.mode == "live":
            result = self.order_manager.open_position(
                symbol=mt5_symbol,
                direction=direction,
                lots=decision.lots,
                sl=decision.sl_price,
                tp=decision.tp_price,
                comment=f"PPO_{inst_name}",
            )
            if result.success:
                logger.info(f"[{inst_name}] EXECUTED: {direction} {decision.lots} @ {result.price}")
            else:
                logger.error(f"[{inst_name}] Execution failed: {result.comment}")
        else:
            logger.info(
                f"[PAPER] [{inst_name}] {direction.upper()} {decision.lots} lots "
                f"@ {current_price} SL={sl_price:.2f} TP={tp_price:.2f}"
            )

    def _manage_existing_position(self, inst_name: str, mt5_symbol: str,
                                  price_data: dict, gamma):
        """Manage trailing stops and breakeven for open positions."""
        if self.mode != "live":
            return

        position = self.position_tracker.get_position_for_symbol(mt5_symbol)
        if not position:
            return

        config = self.instruments[inst_name]
        m5_data = price_data.get("M5")
        if m5_data is None:
            return

        atr = self._compute_atr(m5_data)
        current_price = m5_data["close"].iloc[-1]

        # Breakeven logic
        breakeven_atr = config.get("breakeven_at_atr", 1.0)
        if position.direction == "buy":
            profit_distance = current_price - position.open_price
            if profit_distance >= atr * breakeven_atr and position.sl < position.open_price:
                self.order_manager.modify_sl_tp(position.ticket, sl=position.open_price + atr * 0.1)
                logger.info(f"[{inst_name}] Moved SL to breakeven")
        elif position.direction == "sell":
            profit_distance = position.open_price - current_price
            if profit_distance >= atr * breakeven_atr and position.sl > position.open_price:
                self.order_manager.modify_sl_tp(position.ticket, sl=position.open_price - atr * 0.1)
                logger.info(f"[{inst_name}] Moved SL to breakeven")

    @staticmethod
    def _compute_atr(m5_data: pd.DataFrame, period: int = 14) -> float:
        """Compute ATR from M5 data."""
        import pandas as pd
        if len(m5_data) < period + 1:
            return 1.0

        high = m5_data["high"].values
        low = m5_data["low"].values
        close = m5_data["close"].values

        tr = np.maximum(
            high[1:] - low[1:],
            np.maximum(abs(high[1:] - close[:-1]), abs(low[1:] - close[:-1])),
        )
        return float(np.mean(tr[-period:]))
