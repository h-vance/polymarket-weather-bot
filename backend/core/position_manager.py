"""
Position and Risk Exposure Manager.
Enforces limits on maximum simultaneous markets, open orders, and portfolio exposure.
"""
import logging
from sqlalchemy import func
from datetime import datetime
from typing import Tuple

from backend.models.database import SessionLocal, Trade, BotState
from backend.config import settings

logger = logging.getLogger("position_manager")

class PositionManager:
    @staticmethod
    def get_current_exposure(db) -> Tuple[float, int]:
        """
        Returns the total $ exposed in pending/unsettled trades and the count of open positions.
        """
        pending_trades = db.query(Trade).filter(Trade.settled == False).all()
        
        total_exposure = 0.0
        open_positions = 0
        
        for trade in pending_trades:
            # If it's submitted, we count the requested size as exposure.
            # If it's filled, we count the filled size.
            if trade.execution_status == "submitted":
                total_exposure += trade.size
                open_positions += 1
            elif trade.execution_status == "filled" or (trade.execution_status == "canceled" and trade.filled_size > 0):
                total_exposure += (trade.filled_size * trade.entry_price)
                open_positions += 1
                
        return total_exposure, open_positions

    @staticmethod
    def can_enter_trade(db, requested_size: float) -> bool:
        """
        Checks if a new trade can be safely entered without breaching risk limits.
        """
        state = db.query(BotState).first()
        if not state:
            return False

        total_exposure, open_positions = PositionManager.get_current_exposure(db)
        
        if open_positions >= settings.MAX_TOTAL_PENDING_TRADES:
            logger.warning(f"Risk Check Failed: Max open positions reached ({open_positions})")
            return False
            
        # Ensure we don't expose more capital than we actually have (accounting for tied-up funds)
        available_capital = state.bankroll - total_exposure
        if requested_size > available_capital:
            logger.warning(f"Risk Check Failed: Insufficient available capital. Bankroll: ${state.bankroll:.2f}, Exposed: ${total_exposure:.2f}, Requested: ${requested_size:.2f}")
            return False
            
        # Daily loss limit check
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        daily_pnl = db.query(func.coalesce(func.sum(Trade.pnl), 0.0)).filter(
            Trade.settled == True,
            Trade.settlement_time >= today_start
        ).scalar()

        if daily_pnl <= -settings.DAILY_LOSS_LIMIT:
            logger.warning(f"Risk Check Failed: Daily loss limit hit (${daily_pnl:.2f})")
            return False

        return True

position_manager = PositionManager()
