"""
Backtesting

Runs a trained PPO model against held-out test data and computes
performance metrics: Sharpe, max drawdown, win rate, profit factor.

Usage:
    python -m training.backtest --symbol NAS100 --version v1
"""

import argparse
import logging

import numpy as np
import pandas as pd
import yaml
from stable_baselines3.common.vec_env import DummyVecEnv, VecNormalize

from src.agent.trading_env import TradingEnv
from src.agent.ppo_agent import PPOAgent

logger = logging.getLogger(__name__)


def compute_metrics(equity_curve: list[float], trades: list[float]) -> dict:
    """Compute trading performance metrics."""
    equity = np.array(equity_curve)

    # Returns
    returns = np.diff(equity) / equity[:-1]
    returns = returns[~np.isnan(returns)]

    # Sharpe (annualized, assuming 5-min bars ~78/day ~252 days)
    if len(returns) > 1 and returns.std() > 0:
        sharpe = (returns.mean() / returns.std()) * np.sqrt(78 * 252)
    else:
        sharpe = 0.0

    # Max drawdown
    peak = np.maximum.accumulate(equity)
    drawdowns = (peak - equity) / peak
    max_dd = float(drawdowns.max())

    # Trade metrics
    wins = [t for t in trades if t > 0]
    losses = [t for t in trades if t < 0]
    total = len(trades)

    win_rate = len(wins) / total if total > 0 else 0
    avg_win = np.mean(wins) if wins else 0
    avg_loss = np.mean(losses) if losses else 0
    profit_factor = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else 0
    total_pnl = sum(trades)

    return {
        "sharpe_ratio": round(sharpe, 2),
        "max_drawdown": round(max_dd, 4),
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 2),
        "total_trades": total,
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "total_pnl": round(total_pnl, 2),
        "total_return": round((equity[-1] / equity[0] - 1) * 100, 2) if len(equity) > 0 else 0,
    }


def check_go_live(metrics: dict, criteria: dict) -> bool:
    """Check if metrics meet go-live criteria."""
    checks = {
        "Sharpe": metrics["sharpe_ratio"] >= criteria.get("min_sharpe", 1.0),
        "Max DD": metrics["max_drawdown"] <= criteria.get("max_drawdown_pct", 0.10),
        "Profit Factor": metrics["profit_factor"] >= criteria.get("min_profit_factor", 1.5),
        "Win Rate": metrics["win_rate"] >= criteria.get("min_win_rate", 0.45),
    }

    all_pass = all(checks.values())
    for name, passed in checks.items():
        status = "PASS" if passed else "FAIL"
        logger.info(f"  [{status}] {name}")

    return all_pass


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Backtest trained PPO agent")
    parser.add_argument("--symbol", default="NAS100")
    parser.add_argument("--gamma-ticker", default="QQQ")
    parser.add_argument("--version", default="v1")
    parser.add_argument("--data-dir", default="data/")
    parser.add_argument("--episodes", type=int, default=50)
    args = parser.parse_args()

    with open("config/ppo_hyperparams.yaml") as f:
        ppo_config = yaml.safe_load(f)
    with open("config/symbols.yaml") as f:
        symbols_config = yaml.safe_load(f)

    inst_config = symbols_config["instruments"].get(args.symbol, {})

    # Load trained agent
    agent = PPOAgent(symbol=args.symbol)
    agent.load(version=args.version)

    # Create test environment
    env_kwargs = {
        "symbol": args.symbol,
        "data_dir": args.data_dir,
        "initial_balance": ppo_config["environment"]["initial_balance"],
        "commission": ppo_config["environment"]["commission_per_trade"],
        "spread_points": inst_config.get("spread_typical_points", 1.5),
        "base_lot": inst_config.get("base_lot", 0.10),
        "sl_atr_multiple": inst_config.get("sl_atr_multiple", 2.0),
        "tp_atr_multiple": inst_config.get("tp_atr_multiple", 3.0),
    }
    env = TradingEnv(**env_kwargs)

    # Run episodes
    all_equity = []
    all_trades = []

    for ep in range(args.episodes):
        obs, _ = env.reset()
        done = False
        episode_equity = [env._equity]

        while not done:
            action, _ = agent.predict(obs, deterministic=True)
            obs, reward, terminated, truncated, info = env.step(action)
            done = terminated or truncated
            episode_equity.append(info["equity"])
            if info.get("trade_closed") and info.get("trade_pnl", 0) != 0:
                all_trades.append(info["trade_pnl"])

        all_equity.extend(episode_equity)

    # Compute metrics
    metrics = compute_metrics(all_equity, all_trades)

    logger.info("=" * 60)
    logger.info(f"BACKTEST RESULTS: {args.symbol} ({args.version})")
    logger.info(f"  Episodes:      {args.episodes}")
    logger.info(f"  Sharpe Ratio:  {metrics['sharpe_ratio']}")
    logger.info(f"  Max Drawdown:  {metrics['max_drawdown']:.2%}")
    logger.info(f"  Win Rate:      {metrics['win_rate']:.2%}")
    logger.info(f"  Profit Factor: {metrics['profit_factor']}")
    logger.info(f"  Total Trades:  {metrics['total_trades']}")
    logger.info(f"  Avg Win:       ${metrics['avg_win']}")
    logger.info(f"  Avg Loss:      ${metrics['avg_loss']}")
    logger.info(f"  Total P&L:     ${metrics['total_pnl']}")
    logger.info(f"  Total Return:  {metrics['total_return']}%")
    logger.info("=" * 60)

    # Go-live check
    criteria = ppo_config.get("go_live_criteria", {})
    logger.info("GO-LIVE CHECK:")
    passed = check_go_live(metrics, criteria)
    logger.info(f"  Result: {'APPROVED' if passed else 'NOT READY'}")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
