"""
Data Collector

Collects and stores historical SpotGamma + MT5 price data
for PPO training. Run this for weeks/months before training.

Usage:
    python -m training.data_collector --symbol NAS100 --interval 300
"""

import argparse
import logging
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

from src.data.spotgamma_client import SpotGammaClient
from src.data.mt5_market_data import MT5MarketData
from src.execution.mt5_connector import MT5Connector

logger = logging.getLogger(__name__)


class DataCollector:
    """
    Periodically collects gamma + price data and stores as Parquet files.

    Data structure:
        data/price_{symbol}_{date}.parquet  - OHLCV bars (M5)
        data/gamma_{ticker}_{date}.parquet  - Greek exposures + key levels
    """

    def __init__(
        self,
        mt5_connector: MT5Connector,
        sg_client: SpotGammaClient,
        data_dir: str = "data/",
        interval: int = 300,
    ):
        self.mt5 = mt5_connector
        self.sg = sg_client
        self.market_data = MT5MarketData()
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.interval = interval

    def collect_snapshot(self, mt5_symbol: str, gamma_ticker: str) -> dict:
        """Collect one snapshot of price + gamma data."""
        now = datetime.utcnow()

        # Price data
        bars = self.market_data.get_bars(mt5_symbol, "M5", 1)
        price_row = {}
        if bars is not None and len(bars) > 0:
            row = bars.iloc[-1]
            price_row = {
                "timestamp": now,
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": row["volume"],
            }

        # Gamma data
        snapshot = self.sg.get_snapshot(gamma_ticker)
        gamma_row = {
            "timestamp": now,
            "put_wall": snapshot.key_levels.put_wall,
            "call_wall": snapshot.key_levels.call_wall,
            "gamma_flip": snapshot.key_levels.gamma_flip,
            "vol_trigger": snapshot.key_levels.volatility_trigger,
            "net_gex": snapshot.greeks.net_gex,
            "net_dex": snapshot.greeks.net_dex,
            "net_vex": snapshot.greeks.net_vex,
            "net_chex": snapshot.greeks.net_chex,
            "regime": snapshot.regime,
            "hiro_flow": snapshot.hiro.flow_direction if snapshot.hiro else 0,
        }

        return {"price": price_row, "gamma": gamma_row}

    def run(self, mt5_symbol: str, gamma_ticker: str):
        """
        Run continuous data collection.

        Saves daily Parquet files. Appends to existing file if same day.
        """
        logger.info(f"Collecting data: {mt5_symbol} / {gamma_ticker} every {self.interval}s")

        if not self.mt5.connect():
            logger.error("MT5 connection failed")
            return

        if not self.sg.authenticate():
            logger.warning("SpotGamma auth failed. Collecting price data only.")

        price_rows = []
        gamma_rows = []
        current_date = datetime.utcnow().date()

        try:
            while True:
                now = datetime.utcnow()

                # Save and reset on date change
                if now.date() != current_date:
                    self._save_daily(mt5_symbol, gamma_ticker, current_date, price_rows, gamma_rows)
                    price_rows = []
                    gamma_rows = []
                    current_date = now.date()

                # Collect
                data = self.collect_snapshot(mt5_symbol, gamma_ticker)
                if data["price"]:
                    price_rows.append(data["price"])
                if data["gamma"]:
                    gamma_rows.append(data["gamma"])

                logger.debug(f"Collected: price={len(price_rows)} gamma={len(gamma_rows)}")
                time.sleep(self.interval)

        except KeyboardInterrupt:
            logger.info("Collection stopped by user")
        finally:
            if price_rows or gamma_rows:
                self._save_daily(mt5_symbol, gamma_ticker, current_date, price_rows, gamma_rows)
            self.mt5.disconnect()

    def _save_daily(self, symbol: str, ticker: str, date, price_rows, gamma_rows):
        """Save daily data to Parquet."""
        date_str = date.isoformat()

        if price_rows:
            df = pd.DataFrame(price_rows)
            path = self.data_dir / f"price_{symbol}_{date_str}.parquet"
            df.to_parquet(path, index=False)
            logger.info(f"Saved {len(df)} price rows -> {path}")

        if gamma_rows:
            df = pd.DataFrame(gamma_rows)
            path = self.data_dir / f"gamma_{ticker}_{date_str}.parquet"
            df.to_parquet(path, index=False)
            logger.info(f"Saved {len(df)} gamma rows -> {path}")


def main():
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description="Collect SpotGamma + MT5 data")
    parser.add_argument("--mt5-symbol", default="NAS100")
    parser.add_argument("--gamma-ticker", default="QQQ")
    parser.add_argument("--interval", type=int, default=300)
    args = parser.parse_args()

    import os
    from dotenv import load_dotenv
    load_dotenv()

    mt5_conn = MT5Connector(
        login=int(os.getenv("MT5_LOGIN", "0")),
        password=os.getenv("MT5_PASSWORD", ""),
        server=os.getenv("MT5_SERVER", ""),
    )
    sg_client = SpotGammaClient(
        email=os.getenv("SPOTGAMMA_EMAIL", ""),
        password=os.getenv("SPOTGAMMA_PASSWORD", ""),
    )

    collector = DataCollector(mt5_conn, sg_client, interval=args.interval)
    collector.run(args.mt5_symbol, args.gamma_ticker)


if __name__ == "__main__":
    main()
