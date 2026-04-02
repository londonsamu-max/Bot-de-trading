"""
Bot de Trading: SpotGamma + PPO + MetaTrader 5

Entry point for live trading, paper trading, training, and backtesting.

Usage:
    python main.py --mode paper     # Paper trading (no real orders)
    python main.py --mode live      # Live trading (real orders)
    python main.py --mode train     # Train PPO agent
    python main.py --mode backtest  # Backtest trained agent
"""

import argparse
import logging
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv


def setup_logging(config: dict):
    """Configure logging from settings."""
    level = getattr(logging, config.get("logging", {}).get("level", "INFO"))
    log_file = config.get("logging", {}).get("file", "logs/trading.log")

    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file),
    ]

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )


def load_configs() -> tuple[dict, dict, dict]:
    """Load all YAML configuration files."""
    with open("config/settings.yaml") as f:
        settings = yaml.safe_load(f)
    with open("config/symbols.yaml") as f:
        symbols = yaml.safe_load(f)
    with open("config/ppo_hyperparams.yaml") as f:
        ppo = yaml.safe_load(f)
    return settings, symbols, ppo


def resolve_env_vars(config: dict) -> dict:
    """Replace ${VAR} placeholders with environment variables."""
    import re

    def _resolve(value):
        if isinstance(value, str):
            pattern = r'\$\{(\w+)\}'
            matches = re.findall(pattern, value)
            for var in matches:
                env_val = os.getenv(var, "")
                value = value.replace(f"${{{var}}}", env_val)
            return value
        elif isinstance(value, dict):
            return {k: _resolve(v) for k, v in value.items()}
        elif isinstance(value, list):
            return [_resolve(v) for v in value]
        return value

    return _resolve(config)


def run_trading(mode: str, settings: dict, symbols: dict, ppo_config: dict):
    """Run live or paper trading."""
    from src.data.spotgamma_client import SpotGammaClient
    from src.execution.mt5_connector import MT5Connector
    from src.agent.ppo_agent import PPOAgent
    from src.orchestrator.main_loop import TradingOrchestrator

    logger = logging.getLogger(__name__)

    # MT5 connection
    mt5_cfg = settings["mt5"]
    mt5_connector = MT5Connector(
        login=int(mt5_cfg["login"]),
        password=mt5_cfg["password"],
        server=mt5_cfg["server"],
        terminal_path=mt5_cfg.get("terminal_path"),
        magic_number=mt5_cfg.get("magic_number", 123456),
    )

    # SpotGamma client
    sg_cfg = settings["spotgamma"]
    sg_client = SpotGammaClient(
        email=sg_cfg["email"],
        password=sg_cfg["password"],
        base_url=sg_cfg.get("base_url", "https://dashboard.spotgamma.com"),
        cache_ttl=sg_cfg.get("cache_ttl_seconds", 300),
    )

    # Load PPO agents (one per instrument)
    instruments = symbols["instruments"]
    agents = {}
    for name in instruments:
        agent = PPOAgent(symbol=name)
        try:
            agent.load(version="v1")
            agents[name] = agent
            logger.info(f"Loaded PPO agent for {name}")
        except FileNotFoundError:
            logger.warning(f"No trained model for {name}. Skipping.")

    if not agents:
        logger.error("No trained models found. Train first with: python main.py --mode train")
        return

    # Create orchestrator
    orchestrator = TradingOrchestrator(
        mt5_connector=mt5_connector,
        spotgamma_client=sg_client,
        agents=agents,
        instruments_config=instruments,
        risk_config=settings["risk"],
        tick_interval=settings["trading"]["tick_interval_seconds"],
        mode=mode,
    )

    orchestrator.run()


def run_train(symbols: dict, ppo_config: dict):
    """Train PPO agents for all instruments."""
    from training.train import main as train_main
    # Train each instrument
    for name, config in symbols["instruments"].items():
        gamma_ticker = config.get("gamma_ticker", "QQQ")
        logger = logging.getLogger(__name__)
        logger.info(f"Training agent for {name} (gamma: {gamma_ticker})")
        sys.argv = [
            "train",
            "--symbol", name,
            "--gamma-ticker", gamma_ticker,
            "--timesteps", str(ppo_config["training"]["total_timesteps"]),
        ]
        train_main()


def run_backtest(symbols: dict):
    """Backtest all trained agents."""
    from training.backtest import main as backtest_main
    for name, config in symbols["instruments"].items():
        gamma_ticker = config.get("gamma_ticker", "QQQ")
        sys.argv = [
            "backtest",
            "--symbol", name,
            "--gamma-ticker", gamma_ticker,
        ]
        backtest_main()


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Bot de Trading: SpotGamma + PPO + MetaTrader 5",
    )
    parser.add_argument(
        "--mode",
        choices=["live", "paper", "train", "backtest"],
        default="paper",
        help="Operating mode",
    )
    args = parser.parse_args()

    settings, symbols, ppo_config = load_configs()
    settings = resolve_env_vars(settings)

    setup_logging(settings)
    logger = logging.getLogger(__name__)
    logger.info(f"Bot de Trading starting in {args.mode} mode")

    if args.mode in ("live", "paper"):
        run_trading(args.mode, settings, symbols, ppo_config)
    elif args.mode == "train":
        run_train(symbols, ppo_config)
    elif args.mode == "backtest":
        run_backtest(symbols)


if __name__ == "__main__":
    main()
