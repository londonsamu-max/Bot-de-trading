"""
State Builder

Assembles the full 46-dimensional observation vector for the PPO agent
from the features computed by FeatureEngine. Handles normalization
using running statistics (compatible with VecNormalize for training).
"""

import numpy as np

# Ordered list of all 46 features that form the observation vector.
# This order MUST match what the PPO agent expects.
FEATURE_ORDER = [
    # Gamma + Multi-Greek features (18)
    "price_to_gamma_flip",
    "price_to_call_wall",
    "price_to_put_wall",
    "price_to_vol_trigger",
    "gex_regime_binary",
    "net_gex_zscore",
    "net_dex_zscore",
    "net_vex_zscore",
    "net_chex_zscore",
    "dex_direction",
    "vex_regime",
    "chex_decay_pressure",
    "wall_width",
    "gex_skew",
    "gamma_flip_momentum",
    "call_wall_delta",
    "put_wall_delta",
    "hiro_flow_direction",
    # Technical features (15)
    "rsi_14",
    "atr_14_normalized",
    "bb_position",
    "ema_9_21_cross",
    "ema_50_200_cross",
    "vwap_deviation",
    "volume_ratio",
    "price_momentum_5",
    "price_momentum_20",
    "high_low_range",
    "close_vs_open",
    "adx_14",
    "macd_histogram",
    "stoch_k",
    "stoch_d",
    # Position state features (5)
    "current_position",
    "position_size_normalized",
    "unrealized_pnl_normalized",
    "time_in_position",
    "distance_to_sl",
    # Time features (4)
    "time_of_day_sin",
    "time_of_day_cos",
    "day_of_week_sin",
    "day_of_week_cos",
    # VIX context (4)
    "vix_level",
    "vix_change_1d",
    "vix_term_structure",
    "vix_vs_realized_vol",
]

OBSERVATION_DIM = len(FEATURE_ORDER)  # Should be 46


class StateBuilder:
    """
    Converts a feature dictionary into a numpy observation vector.

    The vector order is defined by FEATURE_ORDER and must remain
    consistent between training and inference.
    """

    def __init__(self):
        self.feature_order = FEATURE_ORDER
        self.dim = OBSERVATION_DIM

    def build(self, features: dict[str, float]) -> np.ndarray:
        """
        Build observation vector from feature dict.

        Missing features default to 0.0. Extra features are ignored.
        """
        obs = np.zeros(self.dim, dtype=np.float32)
        for i, name in enumerate(self.feature_order):
            obs[i] = features.get(name, 0.0)

        # Replace any NaN/Inf with 0
        obs = np.nan_to_num(obs, nan=0.0, posinf=3.0, neginf=-3.0)
        return obs

    def get_feature_names(self) -> list[str]:
        """Return ordered feature names (useful for debugging/logging)."""
        return list(self.feature_order)

    def describe(self, obs: np.ndarray) -> dict[str, float]:
        """Convert observation vector back to named dict (for debugging)."""
        return {name: float(obs[i]) for i, name in enumerate(self.feature_order)}
