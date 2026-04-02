"""
Order Manager for MetaTrader 5

Handles order execution, modification, and closure via the MT5 Python API.
All orders are tagged with the bot's magic number for identification.
"""

import logging
from dataclasses import dataclass
from typing import Optional

import MetaTrader5 as mt5

logger = logging.getLogger(__name__)


@dataclass
class OrderResult:
    success: bool
    ticket: int = 0
    price: float = 0.0
    volume: float = 0.0
    comment: str = ""
    retcode: int = 0


class OrderManager:
    """
    Executes and manages orders on MT5.

    All orders use the configured magic number so the bot
    only interacts with its own trades.
    """

    RETCODE_OK = 10009  # mt5.TRADE_RETCODE_DONE

    def __init__(self, magic_number: int = 123456, deviation: int = 20):
        self.magic = magic_number
        self.deviation = deviation

    def open_position(self, symbol: str, direction: str, lots: float,
                      sl: float = 0.0, tp: float = 0.0,
                      comment: str = "PPO_BOT") -> OrderResult:
        """
        Open a market order.

        Args:
            symbol: MT5 symbol name (e.g. "NAS100")
            direction: "buy" or "sell"
            lots: Position volume
            sl: Stop loss price (0 = no SL)
            tp: Take profit price (0 = no TP)
            comment: Order comment
        """
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return OrderResult(success=False, comment=f"No tick data for {symbol}")

        if direction == "buy":
            order_type = mt5.ORDER_TYPE_BUY
            price = tick.ask
        elif direction == "sell":
            order_type = mt5.ORDER_TYPE_SELL
            price = tick.bid
        else:
            return OrderResult(success=False, comment=f"Invalid direction: {direction}")

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": lots,
            "type": order_type,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": self.deviation,
            "magic": self.magic,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result is None:
            return OrderResult(success=False, comment=f"order_send returned None: {mt5.last_error()}")

        if result.retcode == self.RETCODE_OK:
            logger.info(
                f"Order opened: {direction.upper()} {lots} {symbol} @ {result.price} "
                f"SL={sl} TP={tp} ticket={result.order}"
            )
            return OrderResult(
                success=True,
                ticket=result.order,
                price=result.price,
                volume=lots,
                retcode=result.retcode,
            )

        logger.error(
            f"Order failed: {direction} {lots} {symbol} | "
            f"retcode={result.retcode} comment={result.comment}"
        )
        return OrderResult(
            success=False,
            retcode=result.retcode,
            comment=result.comment,
        )

    def close_position(self, ticket: int) -> OrderResult:
        """Close a specific position by ticket."""
        position = self._get_position_by_ticket(ticket)
        if position is None:
            return OrderResult(success=False, comment=f"Position {ticket} not found")

        close_type = mt5.ORDER_TYPE_SELL if position.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        tick = mt5.symbol_info_tick(position.symbol)
        if tick is None:
            return OrderResult(success=False, comment=f"No tick for {position.symbol}")

        price = tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": position.symbol,
            "volume": position.volume,
            "type": close_type,
            "position": ticket,
            "price": price,
            "deviation": self.deviation,
            "magic": self.magic,
            "comment": "PPO_CLOSE",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result is None:
            return OrderResult(success=False, comment=f"Close returned None: {mt5.last_error()}")

        if result.retcode == self.RETCODE_OK:
            logger.info(f"Position {ticket} closed @ {result.price}")
            return OrderResult(success=True, ticket=ticket, price=result.price, retcode=result.retcode)

        logger.error(f"Close failed ticket={ticket} retcode={result.retcode} {result.comment}")
        return OrderResult(success=False, retcode=result.retcode, comment=result.comment)

    def modify_sl_tp(self, ticket: int, sl: float = 0.0, tp: float = 0.0) -> OrderResult:
        """Modify stop loss and/or take profit of an open position."""
        position = self._get_position_by_ticket(ticket)
        if position is None:
            return OrderResult(success=False, comment=f"Position {ticket} not found")

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": position.symbol,
            "position": ticket,
            "sl": sl if sl > 0 else position.sl,
            "tp": tp if tp > 0 else position.tp,
            "magic": self.magic,
        }

        result = mt5.order_send(request)
        if result is None:
            return OrderResult(success=False, comment=f"Modify returned None: {mt5.last_error()}")

        if result.retcode == self.RETCODE_OK:
            logger.info(f"Position {ticket} modified: SL={sl} TP={tp}")
            return OrderResult(success=True, ticket=ticket, retcode=result.retcode)

        logger.error(f"Modify failed ticket={ticket} retcode={result.retcode}")
        return OrderResult(success=False, retcode=result.retcode, comment=result.comment)

    def close_all(self, symbol: Optional[str] = None) -> list[OrderResult]:
        """Close all bot positions, optionally filtered by symbol."""
        positions = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        if positions is None:
            return []

        results = []
        for pos in positions:
            if pos.magic == self.magic:
                results.append(self.close_position(pos.ticket))
        return results

    def _get_position_by_ticket(self, ticket: int):
        """Get position object by ticket number."""
        positions = mt5.positions_get(ticket=ticket)
        if positions and len(positions) > 0:
            return positions[0]
        return None
