"""
Training Entry Point

Loads collected data, creates Gymnasium environment, trains PPO agent,
saves model and normalization stats.

Usage:
    python -m training.train --symbol NAS100 --timesteps 2000000
"""

import argparse
import logging
from pathlib import Path

import pandas as pd
import yaml
from stable_baselines3.common.vec_env import DummyVecEnv, VecNormalize

from src.agent.trading_env import TradingEnv
from src.agent.ppo_agent import PPOAgent
from src.agent.callbacks import create_training_callbacks

logger = logging.getLogger(__name__)


def load_data(data_dir: str, symbol: str, gamma_ticker: str):
    """Load all collected Parquet files for a symbol."""
    data_path = Path(data_dir)

    price_files = sorted(data_path.glob(f"price_{symbol}_*.parquet"))
    gamma_files = sorted(data_path.glob(f"gamma_{gamma_ticker}_*.parquet"))

    if not price_files:
        logger.warning(f"No price data found for {symbol} in {data_dir}")
        return None, None

    price_df = pd.concat([pd.read_parquet(f) for f in price_files], ignore_index=True)
    logger.info(f"Loaded {len(price_df)} price rows from {len(price_files)} files")

    gamma_df = None
    if gamma_files:
        gamma_df = pd.concat([pd.read_parquet(f) for f in gamma_files], ignore_index=True)
        logger.info(f"Loaded {len(gamma_df)} gamma rows from {len(gamma_files)} files")

    return price_df, gamma_df


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Train PPO trading agent")
    parser.add_argument("--symbol", default="NAS100", help="Instrument name")
    parser.add_argument("--gamma-ticker", default="QQQ", help="SpotGamma ticker")
    parser.add_argument("--timesteps", type=int, default=2_000_000)
    parser.add_argument("--data-dir", default="data/")
    parser.add_argument("--version", default="v1", help="Model version tag")
    args = parser.parse_args()

    # Load configs
    with open("config/ppo_hyperparams.yaml") as f:
        ppo_config = yaml.safe_load(f)
    with open("config/symbols.yaml") as f:
        symbols_config = yaml.safe_load(f)

    inst_config = symbols_config["instruments"].get(args.symbol, {})

    # Load data
    price_df, gamma_df = load_data(args.data_dir, args.symbol, args.gamma_ticker)

    # Environment kwargs
    env_kwargs = {
        "price_data": price_df,
        "gamma_data": gamma_df,
        "data_dir": args.data_dir,
        "symbol": args.symbol,
        "initial_balance": ppo_config["environment"]["initial_balance"],
        "commission": ppo_config["environment"]["commission_per_trade"],
        "spread_points": inst_config.get("spread_typical_points", 1.5),
        "base_lot": inst_config.get("base_lot", 0.10),
        "sl_atr_multiple": inst_config.get("sl_atr_multiple", 2.0),
        "tp_atr_multiple": inst_config.get("tp_atr_multiple", 3.0),
        "reward_weights": ppo_config.get("reward_weights"),
    }

    # Create agent
    agent = PPOAgent(symbol=args.symbol)
    agent.create(env_kwargs=env_kwargs, hyperparams=ppo_config["ppo"])

    # Create eval environment
    eval_env = DummyVecEnv([lambda: TradingEnv(**env_kwargs)])
    eval_env = VecNormalize(eval_env, norm_obs=True, norm_reward=False, clip_obs=10.0)

    # Callbacks
    callbacks = create_training_callbacks(
        symbol=args.symbol,
        eval_env=eval_env,
        eval_freq=ppo_config["training"]["eval_freq"],
        checkpoint_freq=ppo_config["training"]["checkpoint_freq"],
        n_eval_episodes=ppo_config["training"]["n_eval_episodes"],
    )

    # Train
    agent.train(total_timesteps=args.timesteps, callbacks=callbacks)

    # Save
    agent.save(version=args.version)
    logger.info(f"Training complete. Model saved as {args.symbol}_{args.version}")

    # Print go-live criteria check
    criteria = ppo_config.get("go_live_criteria", {})
    logger.info("=" * 50)
    logger.info("GO-LIVE CRITERIA (check backtest results):")
    logger.info(f"  Min Sharpe:        {criteria.get('min_sharpe', 1.0)}")
    logger.info(f"  Max Drawdown:      {criteria.get('max_drawdown_pct', 0.10):.0%}")
    logger.info(f"  Min Profit Factor: {criteria.get('min_profit_factor', 1.5)}")
    logger.info(f"  Min Win Rate:      {criteria.get('min_win_rate', 0.45):.0%}")
    logger.info("=" * 50)


if __name__ == "__main__":
    main()
