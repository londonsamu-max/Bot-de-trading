"""
MT5 Market Data Provider

Fetches OHLCV price data from MetaTrader 5 for multiple timeframes.
Used for technical indicator calculation and PPO state building.
"""

import logging
from datetime import datetime
from typing import Optional

import MetaTrader5 as mt5
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

TIMEFRAME_MAP = {
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
}


class MT5MarketData:
    """Fetches price data from MT5 terminal."""

    def get_bars(self, symbol: str, timeframe: str = "M5",
                 count: int = 200) -> Optional[pd.DataFrame]:
        """
        Get OHLCV bars from MT5.

        Args:
            symbol: MT5 symbol name
            timeframe: "M1", "M5", "M15", "M30", "H1", "H4", "D1"
            count: Number of bars to fetch

        Returns:
            DataFrame with columns: time, open, high, low, close, volume
        """
        tf = TIMEFRAME_MAP.get(timeframe)
        if tf is None:
            logger.error(f"Unknown timeframe: {timeframe}")
            return None

        rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
        if rates is None or len(rates) == 0:
            logger.error(f"No data for {symbol} {timeframe}: {mt5.last_error()}")
            return None

        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        df = df[["time", "open", "high", "low", "close", "tick_volume"]]
        df.rename(columns={"tick_volume": "volume"}, inplace=True)
        return df

    def get_multi_timeframe(self, symbol: str,
                            timeframes: Optional[list[str]] = None) -> dict[str, pd.DataFrame]:
        """
        Fetch bars for multiple timeframes.

        Returns:
            Dict mapping timeframe string to DataFrame.
        """
        if timeframes is None:
            timeframes = ["M5", "H1", "H4"]

        counts = {"M1": 500, "M5": 200, "M15": 200, "M30": 100,
                  "H1": 100, "H4": 50, "D1": 30}

        result = {}
        for tf in timeframes:
            bars = self.get_bars(symbol, tf, counts.get(tf, 200))
            if bars is not None:
                result[tf] = bars
        return result

    def get_current_price(self, symbol: str) -> Optional[dict]:
        """Get current bid/ask/last price."""
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return None
        return {
            "bid": tick.bid,
            "ask": tick.ask,
            "last": tick.last,
            "time": datetime.fromtimestamp(tick.time),
            "spread": tick.ask - tick.bid,
        }

    def get_vix_data(self, count: int = 50) -> Optional[pd.DataFrame]:
        """
        Attempt to fetch VIX data from MT5.

        VIX availability depends on broker. Pepperstone may offer it as
        "VIX", "VIX.r", "VIXINDEX", or similar.
        """
        for name in ["VIX", "VIX.r", "VIXINDEX", "VIX25"]:
            bars = self.get_bars(name, "H1", count)
            if bars is not None:
                return bars
        logger.warning("VIX data not available on this broker")
        return None
