"""
Feature Engineering Pipeline

Computes technical indicators and gamma/Greek-derived features
from price data and SpotGamma snapshots. Outputs raw feature
values that the StateBuilder normalizes into the PPO observation.
"""

import logging

import numpy as np
import pandas as pd
import ta

from src.data.spotgamma_client import GammaSnapshot, KeyLevels, GreekExposure, HiroData

logger = logging.getLogger(__name__)


class FeatureEngine:
    """Computes all features for the PPO agent's observation vector."""

    def __init__(self, lookback_gex: int = 20):
        self.lookback_gex = lookback_gex
        self._gex_history: list[float] = []
        self._dex_history: list[float] = []
        self._vex_history: list[float] = []
        self._chex_history: list[float] = []
        self._gamma_flip_history: list[float] = []
        self._call_wall_history: list[float] = []
        self._put_wall_history: list[float] = []

    def compute(self, gamma: GammaSnapshot, price_data: dict[str, pd.DataFrame],
                current_price: float, position_state: dict) -> dict[str, float]:
        """
        Compute all 46 features.

        Args:
            gamma: SpotGamma snapshot with all Greek exposures
            price_data: Dict of DataFrames keyed by timeframe ("M5", "H1", "H4")
            current_price: Current market price
            position_state: Dict with keys: direction (0=flat,1=long,-1=short),
                           size_normalized, unrealized_pnl_normalized,
                           bars_in_position, distance_to_sl

        Returns:
            Dict mapping feature name to float value.
        """
        features = {}

        # 1. Gamma + Multi-Greek features (18)
        features.update(self._gamma_features(gamma, current_price))

        # 2. Technical features (15)
        m5 = price_data.get("M5")
        h1 = price_data.get("H1")
        if m5 is not None:
            features.update(self._technical_features(m5, h1))

        # 3. Position state features (5)
        features.update(self._position_features(position_state))

        # 4. Time features (4)
        features.update(self._time_features())

        # 5. VIX context features (4)
        features.update(self._vix_features(price_data))

        return features

    def _gamma_features(self, gamma: GammaSnapshot, price: float) -> dict[str, float]:
        """Compute 18 gamma + multi-Greek features."""
        kl = gamma.key_levels
        gk = gamma.greeks
        hiro = gamma.hiro

        # Update histories
        self._gex_history.append(gk.net_gex)
        self._dex_history.append(gk.net_dex)
        self._vex_history.append(gk.net_vex)
        self._chex_history.append(gk.net_chex)
        self._gamma_flip_history.append(kl.gamma_flip)
        self._call_wall_history.append(kl.call_wall)
        self._put_wall_history.append(kl.put_wall)

        # Trim histories
        for h in (self._gex_history, self._dex_history, self._vex_history,
                  self._chex_history, self._gamma_flip_history,
                  self._call_wall_history, self._put_wall_history):
            if len(h) > self.lookback_gex:
                h.pop(0)

        safe_price = max(price, 0.01)

        features = {
            # Distance features (normalized by price)
            "price_to_gamma_flip": (price - kl.gamma_flip) / safe_price if kl.gamma_flip else 0,
            "price_to_call_wall": (kl.call_wall - price) / safe_price if kl.call_wall else 0,
            "price_to_put_wall": (price - kl.put_wall) / safe_price if kl.put_wall else 0,
            "price_to_vol_trigger": (price - kl.volatility_trigger) / safe_price if kl.volatility_trigger else 0,

            # Regime
            "gex_regime_binary": 1.0 if gk.net_gex > 0 else 0.0,

            # Z-scored net exposures
            "net_gex_zscore": self._zscore(gk.net_gex, self._gex_history),
            "net_dex_zscore": self._zscore(gk.net_dex, self._dex_history),
            "net_vex_zscore": self._zscore(gk.net_vex, self._vex_history),
            "net_chex_zscore": self._zscore(gk.net_chex, self._chex_history),

            # DEX direction
            "dex_direction": 1.0 if gk.net_dex > 0 else -1.0,

            # VEX regime (how delta reacts to IV changes)
            "vex_regime": 1.0 if gk.net_vex > 0 else -1.0,

            # CHEX decay pressure
            "chex_decay_pressure": np.tanh(gk.net_chex / max(abs(gk.net_gex), 1e-6)),

            # Structural width
            "wall_width": (kl.call_wall - kl.put_wall) / safe_price if kl.call_wall and kl.put_wall else 0,

            # GEX skew
            "gex_skew": self._compute_gex_skew(gk, price),

            # Momentum of key levels
            "gamma_flip_momentum": self._momentum(self._gamma_flip_history),
            "call_wall_delta": self._momentum(self._call_wall_history),
            "put_wall_delta": self._momentum(self._put_wall_history),

            # HIRO flow
            "hiro_flow_direction": hiro.flow_direction if hiro else 0.0,
        }
        return features

    def _technical_features(self, m5: pd.DataFrame,
                            h1: pd.DataFrame = None) -> dict[str, float]:
        """Compute 15 technical indicator features from M5 bars."""
        close = m5["close"]
        high = m5["high"]
        low = m5["low"]
        volume = m5["volume"].astype(float)

        rsi = ta.momentum.RSIIndicator(close, window=14)
        bb = ta.volatility.BollingerBands(close, window=20, window_dev=2)
        atr = ta.volatility.AverageTrueRange(high, low, close, window=14)
        adx_ind = ta.trend.ADXIndicator(high, low, close, window=14)
        macd_ind = ta.trend.MACD(close)
        stoch = ta.momentum.StochasticOscillator(high, low, close, window=14, smooth_window=3)

        ema9 = close.ewm(span=9).mean()
        ema21 = close.ewm(span=21).mean()
        ema50 = close.ewm(span=50).mean()
        ema200 = close.ewm(span=200).mean() if len(close) >= 200 else close.ewm(span=len(close)).mean()

        current_price = close.iloc[-1]
        atr_val = atr.average_true_range().iloc[-1]
        safe_atr = max(atr_val, 1e-6)

        # Volume ratio
        vol_avg = volume.rolling(20).mean().iloc[-1]
        vol_ratio = volume.iloc[-1] / max(vol_avg, 1) if vol_avg > 0 else 1.0

        # VWAP approximation (typical price * volume cumsum)
        typical = (high + low + close) / 3
        vwap = (typical * volume).cumsum() / volume.cumsum()
        vwap_val = vwap.iloc[-1] if not np.isnan(vwap.iloc[-1]) else current_price

        features = {
            "rsi_14": rsi.rsi().iloc[-1] / 100.0,
            "atr_14_normalized": atr_val / current_price,
            "bb_position": (current_price - bb.bollinger_lband().iloc[-1]) /
                           max(bb.bollinger_hband().iloc[-1] - bb.bollinger_lband().iloc[-1], 1e-6),
            "ema_9_21_cross": 1.0 if ema9.iloc[-1] > ema21.iloc[-1] else -1.0,
            "ema_50_200_cross": 1.0 if ema50.iloc[-1] > ema200.iloc[-1] else -1.0,
            "vwap_deviation": (current_price - vwap_val) / safe_atr,
            "volume_ratio": np.clip(vol_ratio, 0, 5),
            "price_momentum_5": self._returns_zscore(close, 5),
            "price_momentum_20": self._returns_zscore(close, 20),
            "high_low_range": (high.iloc[-1] - low.iloc[-1]) / safe_atr,
            "close_vs_open": (close.iloc[-1] - m5["open"].iloc[-1]) / safe_atr,
            "adx_14": adx_ind.adx().iloc[-1] / 100.0 if not np.isnan(adx_ind.adx().iloc[-1]) else 0,
            "macd_histogram": macd_ind.macd_diff().iloc[-1] / safe_atr,
            "stoch_k": stoch.stoch().iloc[-1] / 100.0 if not np.isnan(stoch.stoch().iloc[-1]) else 0.5,
            "stoch_d": stoch.stoch_signal().iloc[-1] / 100.0 if not np.isnan(stoch.stoch_signal().iloc[-1]) else 0.5,
        }

        # Clip NaN/Inf
        for k, v in features.items():
            if np.isnan(v) or np.isinf(v):
                features[k] = 0.0

        return features

    def _position_features(self, state: dict) -> dict[str, float]:
        """5 position state features."""
        return {
            "current_position": float(state.get("direction", 0)),
            "position_size_normalized": float(state.get("size_normalized", 0)),
            "unrealized_pnl_normalized": float(state.get("unrealized_pnl_normalized", 0)),
            "time_in_position": float(state.get("bars_in_position", 0)) / 100.0,
            "distance_to_sl": float(state.get("distance_to_sl", 0)),
        }

    def _time_features(self) -> dict[str, float]:
        """4 cyclical time features (sin/cos encoded)."""
        from datetime import datetime
        now = datetime.utcnow()
        hour_frac = (now.hour * 60 + now.minute) / 1440.0  # 0-1 over 24h
        day_frac = now.weekday() / 6.0  # 0=Mon, 1=Sun

        return {
            "time_of_day_sin": np.sin(2 * np.pi * hour_frac),
            "time_of_day_cos": np.cos(2 * np.pi * hour_frac),
            "day_of_week_sin": np.sin(2 * np.pi * day_frac),
            "day_of_week_cos": np.cos(2 * np.pi * day_frac),
        }

    def _vix_features(self, price_data: dict) -> dict[str, float]:
        """4 VIX context features. Returns zeros if VIX data unavailable."""
        return {
            "vix_level": 0.0,
            "vix_change_1d": 0.0,
            "vix_term_structure": 0.0,
            "vix_vs_realized_vol": 0.0,
        }

    # --- Helpers ---

    @staticmethod
    def _zscore(value: float, history: list[float]) -> float:
        if len(history) < 5:
            return 0.0
        arr = np.array(history)
        std = arr.std()
        if std < 1e-10:
            return 0.0
        return np.clip((value - arr.mean()) / std, -3, 3)

    @staticmethod
    def _momentum(history: list[float]) -> float:
        if len(history) < 2:
            return 0.0
        prev = history[-2] if history[-2] != 0 else 1e-6
        return (history[-1] - history[-2]) / abs(prev)

    @staticmethod
    def _returns_zscore(series: pd.Series, period: int) -> float:
        if len(series) < period + 20:
            return 0.0
        returns = series.pct_change(period).dropna()
        if len(returns) < 2:
            return 0.0
        std = returns.std()
        if std < 1e-10:
            return 0.0
        return np.clip((returns.iloc[-1] - returns.mean()) / std, -3, 3)

    @staticmethod
    def _compute_gex_skew(greeks: GreekExposure, price: float) -> float:
        """Compute asymmetry of gamma distribution around current price."""
        if not greeks.gex_by_strike:
            return 0.0
        above = sum(v for k, v in greeks.gex_by_strike.items() if k > price)
        below = sum(v for k, v in greeks.gex_by_strike.items() if k <= price)
        total = abs(above) + abs(below)
        if total < 1e-10:
            return 0.0
        return np.clip((above - below) / total, -1, 1)
