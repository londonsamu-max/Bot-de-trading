"""
Composite Reward Function

7-component reward that teaches the PPO agent to:
1. Maximize risk-adjusted returns
2. Respect gamma regimes (mean revert in +gamma, momentum in -gamma)
3. Align with vanna flows (IV-driven delta changes)
4. Respect charm pressure (time decay effects)
5. Minimize transaction costs
6. Avoid drawdowns
7. Favor high R-multiple wins
"""

import numpy as np


class RewardCalculator:
    """
    Computes composite reward for the trading environment.

    Each component is clipped to [-1, 1] before weighting to prevent
    reward explosion. Total reward is the weighted sum.
    """

    def __init__(self, weights: dict = None):
        self.weights = weights or {
            "risk_adjusted_return": 0.35,
            "gamma_alignment": 0.12,
            "vanna_alignment": 0.08,
            "charm_alignment": 0.05,
            "transaction_cost_penalty": 0.10,
            "drawdown_penalty": 0.20,
            "win_quality_bonus": 0.10,
        }

    def compute(
        self,
        pnl_step: float,
        equity: float,
        atr: float,
        position: float,
        price: float,
        gamma_regime: int,
        gamma_flip: float,
        call_wall: float,
        put_wall: float,
        vol_trigger: float,
        net_vex: float,
        iv_change: float,
        net_chex: float,
        transaction_cost: float,
        max_drawdown_pct: float,
        trade_closed: bool = False,
        trade_pnl: float = 0.0,
        initial_risk: float = 0.0,
    ) -> float:
        """
        Compute composite reward.

        Args:
            pnl_step: Realized + unrealized P&L this step ($)
            equity: Current account equity
            atr: Current ATR for normalization
            position: Current position (-1=short, 0=flat, 1=long)
            price: Current market price
            gamma_regime: 1=positive gamma, 0=negative gamma
            gamma_flip: Gamma flip price level
            call_wall: Call wall price level
            put_wall: Put wall price level
            vol_trigger: Volatility trigger price level
            net_vex: Net vanna exposure
            iv_change: Recent change in implied volatility
            net_chex: Net charm exposure
            transaction_cost: Cost of any trades this step
            max_drawdown_pct: Current drawdown from equity peak
            trade_closed: Whether a trade was closed this step
            trade_pnl: P&L of the closed trade
            initial_risk: Initial risk (SL distance * lots) of closed trade
        """
        safe_equity = max(equity, 1.0)
        safe_atr = max(atr, 1e-6)

        components = {}

        # 1. Risk-Adjusted Return
        r_return = pnl_step / (safe_equity * safe_atr / price)
        components["risk_adjusted_return"] = np.clip(r_return, -1, 1)

        # 2. Gamma Alignment
        components["gamma_alignment"] = self._gamma_alignment(
            position, price, gamma_regime, gamma_flip, call_wall, put_wall
        )

        # 3. Vanna Alignment
        components["vanna_alignment"] = self._vanna_alignment(
            position, net_vex, iv_change
        )

        # 4. Charm Alignment
        components["charm_alignment"] = self._charm_alignment(
            position, net_chex, price, vol_trigger
        )

        # 5. Transaction Cost Penalty
        r_cost = -abs(transaction_cost) / safe_equity * 100
        components["transaction_cost_penalty"] = np.clip(r_cost, -1, 0)

        # 6. Drawdown Penalty
        r_dd = -max(0, max_drawdown_pct - 0.02) * 10
        components["drawdown_penalty"] = np.clip(r_dd, -1, 0)

        # 7. Win Quality Bonus
        components["win_quality_bonus"] = self._win_quality(
            trade_closed, trade_pnl, initial_risk
        )

        # Weighted sum
        total = sum(
            self.weights.get(k, 0) * v for k, v in components.items()
        )
        return float(total)

    def _gamma_alignment(self, position: float, price: float,
                         regime: int, gamma_flip: float,
                         call_wall: float, put_wall: float) -> float:
        """
        Reward alignment with gamma regime.

        Positive gamma (regime=1): Dealers suppress moves.
            - Reward long below gamma_flip (mean reversion up)
            - Reward short above call_wall (mean reversion down)
            - Penalize chasing beyond walls

        Negative gamma (regime=0): Dealers amplify moves.
            - Reward long when breaking above gamma_flip (momentum)
            - Reward short when breaking below put_wall (momentum)
        """
        if position == 0 or gamma_flip == 0:
            return 0.0

        if regime == 1:  # Positive gamma: mean reversion
            if position > 0 and price < gamma_flip:
                return 0.5  # Long below flip = good mean reversion
            if position < 0 and price > call_wall and call_wall > 0:
                return 0.5  # Short above call wall = good mean reversion
            if position > 0 and price > call_wall and call_wall > 0:
                return -0.5  # Long above call wall = fighting the wall
            if position < 0 and price < put_wall and put_wall > 0:
                return -0.5  # Short below put wall = fighting the wall
        else:  # Negative gamma: momentum
            if position > 0 and price > gamma_flip:
                return 0.5  # Long above flip = riding momentum
            if position < 0 and price < gamma_flip:
                return 0.5  # Short below flip = riding momentum
            if position > 0 and price < put_wall and put_wall > 0:
                return -0.3  # Long below put wall in neg gamma = dangerous

        return 0.0

    def _vanna_alignment(self, position: float, net_vex: float,
                         iv_change: float) -> float:
        """
        Reward alignment with vanna-driven dealer flows.

        When VEX > 0 and IV rising: dealers forced to buy -> bullish
        When VEX > 0 and IV falling: dealers forced to sell -> bearish
        When VEX < 0: opposite effects
        """
        if position == 0 or abs(net_vex) < 1e-6:
            return 0.0

        # Vanna effect direction: VEX * IV_change determines flow direction
        vanna_flow = np.sign(net_vex) * np.sign(iv_change)

        # If flow is positive (bullish) and we're long, good
        # If flow is negative (bearish) and we're short, good
        alignment = vanna_flow * position
        return np.clip(alignment * 0.5, -1, 1)

    def _charm_alignment(self, position: float, net_chex: float,
                         price: float, vol_trigger: float) -> float:
        """
        Reward alignment with charm-driven time decay pressure.

        High CHEX near expiration creates pinning effects.
        Above vol_trigger: charm supports, below: charm pressures.
        """
        if position == 0 or vol_trigger == 0:
            return 0.0

        above_trigger = price > vol_trigger
        charm_positive = net_chex > 0

        if above_trigger and charm_positive and position > 0:
            return 0.3  # Charm supporting longs above trigger
        if not above_trigger and not charm_positive and position < 0:
            return 0.3  # Charm pressuring below trigger, short is good

        return 0.0

    def _win_quality(self, closed: bool, pnl: float,
                     risk: float) -> float:
        """Reward trades with good risk/reward ratio."""
        if not closed or abs(risk) < 1e-6:
            return 0.0
        r_multiple = pnl / abs(risk)
        return np.clip(r_multiple * 0.3, -1, 1)
