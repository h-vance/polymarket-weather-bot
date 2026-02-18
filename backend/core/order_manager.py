"""
Order Lifecycle Management.
Tracks active orders on the CLOB, handles partial fills, and enforces timeouts.
"""
import logging
import asyncio
from datetime import datetime, timedelta
from typing import List

from backend.models.database import SessionLocal, Trade, BotState
from backend.core.execution_engine import execution_engine
from backend.core.scheduler import log_event

logger = logging.getLogger("order_manager")

ORDER_TIMEOUT_SECONDS = 30

async def check_active_orders():
    """
    Polls the CLOB for status of pending orders.
    Enforces cancel-replace/timeout rules.
    """
    db = SessionLocal()
    try:
        # Find all trades that have been submitted to the CLOB but not fully resolved
        active_trades = db.query(Trade).filter(Trade.execution_status == "submitted").all()
        
        if not active_trades:
            return
            
        logger.info(f"Checking status for {len(active_trades)} active orders...")

        state = db.query(BotState).first()

        for trade in active_trades:
            if not trade.order_id or trade.order_id == "unknown":
                # Fallback if order ID wasn't properly captured
                trade.execution_status = "simulated"
                db.commit()
                continue
                
            # Check timeout
            time_elapsed = (datetime.utcnow() - trade.timestamp).total_seconds()
            is_expired = time_elapsed > ORDER_TIMEOUT_SECONDS

            # Query CLOB
            status_resp = await execution_engine.get_order_status(trade.order_id)
            
            if status_resp.get("status") == "error":
                logger.error(f"Failed to fetch status for order {trade.order_id}: {status_resp.get('message')}")
                continue

            order_state = status_resp.get("status", "").lower()
            size_matched = float(status_resp.get("size_matched", 0.0))
            
            trade.filled_size = size_matched

            if order_state == "filled" or order_state == "matched":
                trade.execution_status = "filled"
                log_event("success", f"Order {trade.order_id} fully filled: {size_matched} units.")
            
            elif order_state == "canceled" or order_state == "cancelled":
                trade.execution_status = "canceled"
                log_event("warning", f"Order {trade.order_id} canceled by exchange. Filled: {size_matched}")
                
            elif is_expired:
                # Cancel the resting order due to timeout
                logger.info(f"Order {trade.order_id} expired (> {ORDER_TIMEOUT_SECONDS}s). Cancelling.")
                cancel_success = await execution_engine.cancel_order(trade.order_id)
                if cancel_success:
                    trade.execution_status = "canceled"
                    log_event("warning", f"Order {trade.order_id} timed out and cancelled. Filled: {size_matched}")
                else:
                    logger.error(f"Failed to cancel expired order {trade.order_id}")
            
            else:
                # Still open, might be partially filled
                if size_matched > 0:
                    logger.debug(f"Order {trade.order_id} partially filled: {size_matched}/{trade.size}")
            
            # If the trade is no longer submitted, adjust the bankroll
            # Bankroll is reduced by the actual filled size * entry price
            if trade.execution_status in ["filled", "canceled"]:
                if state:
                    actual_cost = trade.filled_size * trade.entry_price
                    # If it was fully simulated, we already deducted? No, we didn't deduct during submission.
                    # Wait, if we want to track available cash, we should deduct capital here.
                    state.bankroll -= actual_cost
                    
                # If nothing was filled and it's canceled, maybe we mark it as settled=True so we ignore it
                if trade.filled_size == 0 and trade.execution_status == "canceled":
                    trade.settled = True
                    trade.result = "canceled"
                    trade.pnl = 0.0
                    
        db.commit()

    except Exception as e:
        logger.error(f"Error checking active orders: {e}")
        db.rollback()
    finally:
        db.close()
