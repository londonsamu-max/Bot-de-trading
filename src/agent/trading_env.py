"""
Trading Gymnasium Environment

Simulates the trading loop for PPO training. Replays historical
price + gamma data, processes actions, computes rewards.

Episode = one trading day (or configurable window).
Step = one tick interval (5 min bar).
"""

import logging
from pathlib import Path
from typing import Optional

import gymnasium as gym
import numpy as np
import pandas as pd
from gymnasium import spaces

from src.agent.reward import RewardCalculator
from src.data.feature_engine import FeatureEngine
from src.data.state_builder import StateBuilder, OBSERVATION_DIM
from src.data.spotgamma_client import GammaSnapshot, KeyLevels, GreekExposure, HiroData

logger = logging.getLogger(__name__)


class TradingEnv(gym.Env):
    """
    Gymnasium environment for training the PPO trading agent.

    Observation: 46-dim vector (gamma + Greeks + technicals + position + time)
    Actions: Discrete(5) - Hold, Buy small, Buy full, Sell small, Sell full
    """

    metadata = {"render_modes": ["human"]}

    def __init__(
        self,
        price_data: Optional[pd.DataFrame] = None,
        gamma_data: Optional[pd.DataFrame] = None,
        data_dir: str = "data/",
        symbol: str = "NAS100",
        initial_balance: float = 10000.0,
        commission: float = 2.0,
        spread_points: float = 1.5,
        base_lot: float = 0.10,
        sl_atr_multiple: float = 2.0,
        tp_atr_multiple: float = 3.0,
        max_steps_per_episode: int = 78,  # ~6.5 hours of 5-min bars
        reward_weights: Optional[dict] = None,
    ):
        super().__init__()

        self.initial_balance = initial_balance
        self.commission = commission
        self.spread_points = spread_points
        self.base_lot = base_lot
        self.sl_atr_multiple = sl_atr_multiple
        self.tp_atr_multiple = tp_atr_multiple
        self.max_steps = max_steps_per_episode

        # Spaces
        self.observation_space = spaces.Box(
            low=-np.inf, high=np.inf,
            shape=(OBSERVATION_DIM,), dtype=np.float32,
        )
        self.action_space = spaces.Discrete(5)
        # 0=Hold, 1=Buy small(0.5x), 2=Buy full(1.0x), 3=Sell small, 4=Sell full

        # Components
        self.feature_engine = FeatureEngine()
        self.state_builder = StateBuilder()
        self.reward_calc = RewardCalculator(reward_weights)

        # Load data
        self.data_dir = Path(data_dir)
        self._price_data = price_data
        self._gamma_data = gamma_data
        self._episodes = self._prepare_episodes()

        # State (reset in reset())
        self._current_episode = None
        self._step_idx = 0
        self._balance = initial_balance
        self._equity = initial_balance
        self._peak_equity = initial_balance
        self._position = 0  # -1, 0, 1
        self._position_size = 0.0
        self._entry_price = 0.0
        self._sl_price = 0.0
        self._tp_price = 0.0
        self._bars_in_position = 0
        self._initial_risk = 0.0

    def _prepare_episodes(self) -> list[dict]:
        """
        Load and prepare episode data from files or provided DataFrames.

        Each episode is a dict with 'price' (DataFrame) and 'gamma' (DataFrame).
        If no data provided, creates dummy episodes for environment testing.
        """
        if self._price_data is not None:
            # Split into daily episodes
            episodes = []
            if "time" in self._price_data.columns:
                self._price_data["date"] = self._price_data["time"].dt.date
                for date, group in self._price_data.groupby("date"):
                    if len(group) >= 20:  # Minimum bars per episode
                        episodes.append({
                            "price": group.reset_index(drop=True),
                            "gamma": self._gamma_data,
                        })
            if episodes:
                return episodes

        # Dummy data for env testing / check_env
        np.random.seed(42)
        n = self.max_steps + 50
        prices = 100 + np.cumsum(np.random.randn(n) * 0.5)
        dummy_price = pd.DataFrame({
            "time": pd.date_range("2024-01-01 09:30", periods=n, freq="5min"),
            "open": prices,
            "high": prices + abs(np.random.randn(n) * 0.3),
            "low": prices - abs(np.random.randn(n) * 0.3),
            "close": prices + np.random.randn(n) * 0.2,
            "volume": np.random.randint(100, 10000, n).astype(float),
        })
        return [{"price": dummy_price, "gamma": None}]

    def reset(self, seed=None, options=None):
        super().reset(seed=seed)

        # Pick random episode
        ep_idx = self.np_random.integers(0, len(self._episodes))
        self._current_episode = self._episodes[ep_idx]
        self._step_idx = 50  # Skip first 50 bars for indicator warmup
        self._balance = self.initial_balance
        self._equity = self.initial_balance
        self._peak_equity = self.initial_balance
        self._position = 0
        self._position_size = 0.0
        self._entry_price = 0.0
        self._sl_price = 0.0
        self._tp_price = 0.0
        self._bars_in_position = 0
        self._initial_risk = 0.0

        # Reset feature engine histories
        self.feature_engine = FeatureEngine()

        obs = self._get_observation()
        return obs, {}

    def step(self, action: int):
        price_df = self._current_episode["price"]
        current_bar = price_df.iloc[self._step_idx]
        price = current_bar["close"]
        high = current_bar["high"]
        low = current_bar["low"]

        trade_closed = False
        trade_pnl = 0.0
        transaction_cost = 0.0
        pnl_step = 0.0

        # Check SL/TP hits on current bar
        if self._position != 0:
            if self._position > 0:  # Long
                if self._sl_price > 0 and low <= self._sl_price:
                    trade_pnl = (self._sl_price - self._entry_price) * self._position_size * 100
                    trade_closed = True
                elif self._tp_price > 0 and high >= self._tp_price:
                    trade_pnl = (self._tp_price - self._entry_price) * self._position_size * 100
                    trade_closed = True
            elif self._position < 0:  # Short
                if self._sl_price > 0 and high >= self._sl_price:
                    trade_pnl = (self._entry_price - self._sl_price) * self._position_size * 100
                    trade_closed = True
                elif self._tp_price > 0 and low <= self._tp_price:
                    trade_pnl = (self._entry_price - self._tp_price) * self._position_size * 100
                    trade_closed = True

            if trade_closed:
                self._balance += trade_pnl - self.commission
                self._position = 0
                self._position_size = 0.0

        # Process action
        if not trade_closed:
            prev_position = self._position

            if action == 0:  # Hold
                pass
            elif action in (1, 2):  # Buy small/full
                if self._position <= 0:
                    # Close short if exists
                    if self._position < 0:
                        close_pnl = (self._entry_price - price) * self._position_size * 100
                        self._balance += close_pnl - self.commission
                        transaction_cost += self.commission + self.spread_points
                        trade_pnl = close_pnl
                        trade_closed = True

                    # Open long
                    lot_mult = 0.5 if action == 1 else 1.0
                    self._position = 1
                    self._position_size = self.base_lot * lot_mult
                    self._entry_price = price + self.spread_points / 2
                    atr = self._current_atr()
                    self._sl_price = self._entry_price - atr * self.sl_atr_multiple
                    self._tp_price = self._entry_price + atr * self.tp_atr_multiple
                    self._initial_risk = atr * self.sl_atr_multiple * self._position_size * 100
                    self._bars_in_position = 0
                    transaction_cost += self.commission + self.spread_points

            elif action in (3, 4):  # Sell small/full
                if self._position >= 0:
                    if self._position > 0:
                        close_pnl = (price - self._entry_price) * self._position_size * 100
                        self._balance += close_pnl - self.commission
                        transaction_cost += self.commission + self.spread_points
                        trade_pnl = close_pnl
                        trade_closed = True

                    lot_mult = 0.5 if action == 3 else 1.0
                    self._position = -1
                    self._position_size = self.base_lot * lot_mult
                    self._entry_price = price - self.spread_points / 2
                    atr = self._current_atr()
                    self._sl_price = self._entry_price + atr * self.sl_atr_multiple
                    self._tp_price = self._entry_price - atr * self.tp_atr_multiple
                    self._initial_risk = atr * self.sl_atr_multiple * self._position_size * 100
                    self._bars_in_position = 0
                    transaction_cost += self.commission + self.spread_points

        # Update equity
        floating = 0.0
        if self._position > 0:
            floating = (price - self._entry_price) * self._position_size * 100
        elif self._position < 0:
            floating = (self._entry_price - price) * self._position_size * 100
        self._equity = self._balance + floating
        self._peak_equity = max(self._peak_equity, self._equity)
        pnl_step = self._equity - self.initial_balance  # Simplified

        if self._position != 0:
            self._bars_in_position += 1

        # Drawdown
        dd = (self._peak_equity - self._equity) / max(self._peak_equity, 1) if self._peak_equity > 0 else 0

        # Build gamma snapshot (from data or dummy)
        gamma = self._get_gamma_for_step()

        # Compute reward
        reward = self.reward_calc.compute(
            pnl_step=pnl_step,
            equity=self._equity,
            atr=self._current_atr(),
            position=float(self._position),
            price=price,
            gamma_regime=1 if gamma.greeks.net_gex > 0 else 0,
            gamma_flip=gamma.key_levels.gamma_flip,
            call_wall=gamma.key_levels.call_wall,
            put_wall=gamma.key_levels.put_wall,
            vol_trigger=gamma.key_levels.volatility_trigger,
            net_vex=gamma.greeks.net_vex,
            iv_change=0.0,  # TODO: compute from data
            net_chex=gamma.greeks.net_chex,
            transaction_cost=transaction_cost,
            max_drawdown_pct=dd,
            trade_closed=trade_closed,
            trade_pnl=trade_pnl,
            initial_risk=self._initial_risk,
        )

        # Advance step
        self._step_idx += 1

        # Termination conditions
        terminated = False
        if self._equity <= self.initial_balance * 0.90:  # 10% loss = margin call
            terminated = True
        if dd > 0.05:  # 5% drawdown circuit breaker
            terminated = True

        truncated = self._step_idx >= min(
            len(self._current_episode["price"]) - 1,
            50 + self.max_steps,
        )

        obs = self._get_observation()
        info = {
            "equity": self._equity,
            "balance": self._balance,
            "drawdown": dd,
            "position": self._position,
            "trade_closed": trade_closed,
            "trade_pnl": trade_pnl,
        }

        return obs, reward, terminated, truncated, info

    def _get_observation(self) -> np.ndarray:
        """Build current observation vector."""
        price_df = self._current_episode["price"]
        end = self._step_idx + 1
        start = max(0, end - 200)
        window = price_df.iloc[start:end].copy()

        current_price = window["close"].iloc[-1]
        gamma = self._get_gamma_for_step()

        position_state = {
            "direction": self._position,
            "size_normalized": self._position_size / max(self.base_lot, 1e-6),
            "unrealized_pnl_normalized": 0.0,
            "bars_in_position": self._bars_in_position,
            "distance_to_sl": 0.0,
        }
        if self._position != 0:
            atr = self._current_atr()
            safe_atr = max(atr, 1e-6)
            if self._position > 0:
                floating = (current_price - self._entry_price) * self._position_size * 100
                position_state["distance_to_sl"] = (current_price - self._sl_price) / safe_atr
            else:
                floating = (self._entry_price - current_price) * self._position_size * 100
                position_state["distance_to_sl"] = (self._sl_price - current_price) / safe_atr
            position_state["unrealized_pnl_normalized"] = floating / max(self._equity, 1)

        features = self.feature_engine.compute(
            gamma=gamma,
            price_data={"M5": window},
            current_price=current_price,
            position_state=position_state,
        )
        return self.state_builder.build(features)

    def _get_gamma_for_step(self) -> GammaSnapshot:
        """Get gamma data for current step (from data or dummy)."""
        gamma_df = self._current_episode.get("gamma")
        if gamma_df is not None and len(gamma_df) > 0:
            idx = min(self._step_idx, len(gamma_df) - 1)
            row = gamma_df.iloc[idx]
            return GammaSnapshot(
                key_levels=KeyLevels(
                    put_wall=float(row.get("put_wall", 0)),
                    call_wall=float(row.get("call_wall", 0)),
                    gamma_flip=float(row.get("gamma_flip", 0)),
                    volatility_trigger=float(row.get("vol_trigger", 0)),
                ),
                greeks=GreekExposure(
                    net_gex=float(row.get("net_gex", 0)),
                    net_dex=float(row.get("net_dex", 0)),
                    net_vex=float(row.get("net_vex", 0)),
                    net_chex=float(row.get("net_chex", 0)),
                ),
                hiro=HiroData(flow_direction=float(row.get("hiro_flow", 0))),
            )

        # Dummy gamma centered around current price
        price_df = self._current_episode["price"]
        price = price_df.iloc[self._step_idx]["close"]
        return GammaSnapshot(
            key_levels=KeyLevels(
                put_wall=price * 0.98,
                call_wall=price * 1.02,
                gamma_flip=price * 1.0,
                volatility_trigger=price * 0.99,
            ),
            greeks=GreekExposure(
                net_gex=np.random.randn() * 1e6,
                net_dex=np.random.randn() * 1e6,
                net_vex=np.random.randn() * 1e5,
                net_chex=np.random.randn() * 1e5,
            ),
            hiro=HiroData(flow_direction=np.random.randn()),
        )

    def _current_atr(self) -> float:
        """Compute ATR(14) at current step."""
        price_df = self._current_episode["price"]
        end = self._step_idx + 1
        start = max(0, end - 20)
        window = price_df.iloc[start:end]

        if len(window) < 2:
            return 1.0

        high = window["high"].values
        low = window["low"].values
        close = window["close"].values

        tr = np.maximum(
            high[1:] - low[1:],
            np.maximum(
                abs(high[1:] - close[:-1]),
                abs(low[1:] - close[:-1]),
            ),
        )
        return float(np.mean(tr[-14:])) if len(tr) >= 14 else float(np.mean(tr))
