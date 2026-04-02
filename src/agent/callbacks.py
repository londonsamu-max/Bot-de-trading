"""
Training Callbacks

Callbacks for monitoring PPO training: checkpoints, metrics logging,
and early stopping based on performance criteria.
"""

import logging
from pathlib import Path

import numpy as np
from stable_baselines3.common.callbacks import BaseCallback, EvalCallback, CheckpointCallback

logger = logging.getLogger(__name__)


class TradingMetricsCallback(BaseCallback):
    """
    Logs trading-specific metrics during training.

    Tracks: win rate, average reward, max drawdown, profit factor.
    """

    def __init__(self, log_freq: int = 1000, verbose: int = 0):
        super().__init__(verbose)
        self.log_freq = log_freq
        self._episode_rewards = []
        self._episode_lengths = []
        self._trade_pnls = []
        self._drawdowns = []

    def _on_step(self) -> bool:
        infos = self.locals.get("infos", [])
        for info in infos:
            if info.get("trade_closed"):
                self._trade_pnls.append(info.get("trade_pnl", 0))
            if "drawdown" in info:
                self._drawdowns.append(info["drawdown"])

        if self.n_calls % self.log_freq == 0 and self._trade_pnls:
            wins = [p for p in self._trade_pnls if p > 0]
            losses = [p for p in self._trade_pnls if p < 0]

            win_rate = len(wins) / len(self._trade_pnls) if self._trade_pnls else 0
            avg_win = np.mean(wins) if wins else 0
            avg_loss = np.mean(losses) if losses else 0
            profit_factor = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else 0
            max_dd = max(self._drawdowns) if self._drawdowns else 0

            self.logger.record("trading/win_rate", win_rate)
            self.logger.record("trading/avg_win", avg_win)
            self.logger.record("trading/avg_loss", avg_loss)
            self.logger.record("trading/profit_factor", profit_factor)
            self.logger.record("trading/max_drawdown", max_dd)
            self.logger.record("trading/total_trades", len(self._trade_pnls))

            if self.verbose > 0:
                logger.info(
                    f"Step {self.n_calls}: WR={win_rate:.2%} PF={profit_factor:.2f} "
                    f"DD={max_dd:.2%} Trades={len(self._trade_pnls)}"
                )

        return True

    def _on_rollout_end(self) -> None:
        pass


def create_training_callbacks(
    symbol: str,
    eval_env,
    model_dir: str = "models/",
    eval_freq: int = 10_000,
    checkpoint_freq: int = 50_000,
    n_eval_episodes: int = 10,
) -> list:
    """
    Create standard set of training callbacks.

    Returns list of: [EvalCallback, CheckpointCallback, TradingMetricsCallback]
    """
    model_path = Path(model_dir)
    model_path.mkdir(parents=True, exist_ok=True)

    eval_cb = EvalCallback(
        eval_env,
        best_model_save_path=str(model_path / f"best_{symbol}"),
        log_path=str(model_path / f"eval_{symbol}"),
        eval_freq=eval_freq,
        n_eval_episodes=n_eval_episodes,
        deterministic=True,
        render=False,
    )

    checkpoint_cb = CheckpointCallback(
        save_freq=checkpoint_freq,
        save_path=str(model_path / f"checkpoints_{symbol}"),
        name_prefix=f"ppo_{symbol}",
    )

    metrics_cb = TradingMetricsCallback(log_freq=1000, verbose=1)

    return [eval_cb, checkpoint_cb, metrics_cb]
