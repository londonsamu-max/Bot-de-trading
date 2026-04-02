"""
MetaTrader 5 Connection Manager

Handles initialization, login, symbol discovery, and connection health
for the Pepperstone MT5 terminal.

Requires: Windows OS with MetaTrader 5 installed and running.
"""

import logging
import time
from typing import Optional

import MetaTrader5 as mt5

logger = logging.getLogger(__name__)


class MT5Connector:
    """
    Manages the connection to MetaTrader 5 terminal.

    Usage:
        connector = MT5Connector(login=12345, password="xxx", server="Pepperstone-MT5-Live01")
        connector.connect()
        symbol = connector.discover_symbol("NAS100", ["USTEC", "NAS100.r"])
        connector.disconnect()
    """

    def __init__(self, login: int, password: str, server: str,
                 terminal_path: Optional[str] = None, magic_number: int = 123456):
        self.login = login
        self.password = password
        self.server = server
        self.terminal_path = terminal_path
        self.magic_number = magic_number
        self._connected = False
        self._symbols_cache: dict[str, str] = {}

    def connect(self) -> bool:
        """Initialize MT5 terminal and login."""
        init_kwargs = {}
        if self.terminal_path:
            init_kwargs["path"] = self.terminal_path

        if not mt5.initialize(**init_kwargs):
            logger.error(f"MT5 initialize failed: {mt5.last_error()}")
            return False

        authorized = mt5.login(
            login=self.login,
            password=self.password,
            server=self.server,
        )
        if not authorized:
            logger.error(f"MT5 login failed: {mt5.last_error()}")
            mt5.shutdown()
            return False

        account = mt5.account_info()
        logger.info(
            f"MT5 connected: {account.name} | "
            f"Balance: {account.balance} {account.currency} | "
            f"Server: {account.server}"
        )
        self._connected = True
        return True

    def disconnect(self):
        """Shutdown MT5 connection."""
        if self._connected:
            mt5.shutdown()
            self._connected = False
            logger.info("MT5 disconnected")

    def is_connected(self) -> bool:
        """Check if MT5 terminal is still responsive."""
        if not self._connected:
            return False
        info = mt5.terminal_info()
        return info is not None and info.connected

    def reconnect(self, max_retries: int = 3, delay: float = 5.0) -> bool:
        """Attempt to reconnect with retries."""
        for attempt in range(1, max_retries + 1):
            logger.warning(f"Reconnecting to MT5 (attempt {attempt}/{max_retries})")
            self.disconnect()
            time.sleep(delay)
            if self.connect():
                return True
        logger.error("MT5 reconnection failed after all retries")
        return False

    def discover_symbol(self, base_name: str, alternatives: list[str]) -> Optional[str]:
        """
        Find the actual tradeable symbol name on this broker.

        Pepperstone may use different suffixes (.r, .cash, etc.) depending
        on account type. This tries the base name first, then alternatives.
        """
        if base_name in self._symbols_cache:
            return self._symbols_cache[base_name]

        for name in [base_name] + alternatives:
            info = mt5.symbol_info(name)
            if info is not None and info.trade_mode == mt5.SYMBOL_TRADE_MODE_FULL:
                mt5.symbol_select(name, True)  # Add to Market Watch
                self._symbols_cache[base_name] = name
                logger.info(f"Symbol discovered: {base_name} -> {name}")
                return name

        logger.error(
            f"No tradeable symbol found for {base_name}. "
            f"Tried: {[base_name] + alternatives}"
        )
        return None

    def get_account_info(self) -> Optional[dict]:
        """Get current account state."""
        info = mt5.account_info()
        if info is None:
            return None
        return {
            "balance": info.balance,
            "equity": info.equity,
            "margin": info.margin,
            "free_margin": info.margin_free,
            "margin_level": info.margin_level,
            "profit": info.profit,
            "currency": info.currency,
        }

    def get_symbol_info(self, symbol: str) -> Optional[dict]:
        """Get symbol trading parameters."""
        info = mt5.symbol_info(symbol)
        if info is None:
            return None
        return {
            "symbol": info.name,
            "bid": info.bid,
            "ask": info.ask,
            "spread": info.spread,
            "volume_min": info.volume_min,
            "volume_max": info.volume_max,
            "volume_step": info.volume_step,
            "trade_tick_size": info.trade_tick_size,
            "trade_tick_value": info.trade_tick_value,
            "trade_contract_size": info.trade_contract_size,
        }
