"""Background scheduler for Polymarket Weather autonomous trading."""
import asyncio
from datetime import datetime, timedelta
from typing import List, Optional
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import func
import logging

from backend.config import settings
from backend.models.database import SessionLocal, Trade, BotState, Signal
from backend.core.execution_engine import execution_engine
from backend.core.order_manager import check_active_orders
from backend.core.position_manager import position_manager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("trading_bot")

# Global scheduler instance
scheduler: Optional[AsyncIOScheduler] = None

# Event log for terminal display (in-memory, last 200 events)
event_log: List[dict] = []
MAX_LOG_SIZE = 200

def log_event(event_type: str, message: str, data: dict = None):
    """Log an event for terminal display."""
    event = {
        "timestamp": datetime.utcnow().isoformat(),
        "type": event_type,
        "message": message,
        "data": data or {}
    }
    event_log.append(event)

    while len(event_log) > MAX_LOG_SIZE:
        event_log.pop(0)

    log_func = {
        "error": logger.error,
        "warning": logger.warning,
        "success": logger.info,
        "info": logger.info,
        "data": logger.debug,
        "trade": logger.info
    }.get(event_type, logger.info)

    log_func(f"[{event_type.upper()}] {message}")

def get_recent_events(limit: int = 50) -> List[dict]:
    """Get recent events for terminal display."""
    return event_log[-limit:]

async def weather_scan_and_trade_job():
    """
    Background job: Scan weather temperature markets, generate signals, execute trades.
    Runs every 5 minutes.
    """
    log_event("info", "Scanning weather temperature markets...")

    try:
        from backend.core.weather_signals import scan_for_weather_signals

        signals = await scan_for_weather_signals()
        actionable = [s for s in signals if s.passes_threshold]

        log_event("data", f"Weather: {len(signals)} signals, {len(actionable)} actionable", {
            "total_signals": len(signals),
            "actionable": len(actionable),
        })

        if not actionable:
            log_event("info", "No actionable weather signals")
            return

        db = SessionLocal()
        try:
            state = db.query(BotState).first()
            if not state:
                log_event("error", "Bot state not initialized")
                return

            if not state.is_running:
                log_event("info", "Bot is paused, skipping trades")
                return

            MAX_TRADES_PER_SCAN = 2
            MIN_TRADE_SIZE = 10
            MAX_TRADE_FRACTION = 0.05  # 5% max per trade for weather

            trades_executed = 0
            for signal in actionable[:MAX_TRADES_PER_SCAN]:
                existing = db.query(Trade).filter(
                    Trade.market_ticker == signal.market.market_id,
                    Trade.settled == False
                ).first()

                if existing:
                    continue

                trade_size = min(signal.suggested_size, state.bankroll * MAX_TRADE_FRACTION)
                trade_size = max(trade_size, MIN_TRADE_SIZE)
                trade_size = min(trade_size, settings.WEATHER_MAX_TRADE_SIZE)

                if not position_manager.can_enter_trade(db, trade_size):
                    break

                if trades_executed >= MAX_TRADES_PER_SCAN:
                    break

                entry_price = signal.market.yes_price if signal.direction == "yes" else signal.market.no_price
                token_id = signal.market.yes_token_id if signal.direction == "yes" else signal.market.no_token_id

                num_shares = round(trade_size / entry_price, 2)

                # Execute order on the CLOB
                exec_resp = await execution_engine.execute_order(token_id=token_id, price=entry_price, size=num_shares, side="BUY")
                order_id = exec_resp.get("order_id")
                execution_status = exec_resp.get("status", "error")

                trade = Trade(
                    market_ticker=signal.market.market_id,
                    platform=signal.market.platform,
                    event_slug=f"{signal.market.city_key}_{signal.market.target_date.strftime('%Y%m%d')}",
                    direction=signal.direction,
                    entry_price=entry_price,
                    size=num_shares,
                    model_probability=signal.model_probability,
                    market_price_at_entry=signal.market_probability,
                    edge_at_entry=signal.edge,
                    order_id=order_id,
                    execution_status=execution_status
                )

                db.add(trade)
                db.flush()

                matching_signal = db.query(Signal).filter(
                    Signal.market_ticker == signal.market.market_id,
                    Signal.executed == False,
                ).order_by(Signal.timestamp.desc()).first()
                
                if matching_signal:
                    matching_signal.executed = True
                    trade.signal_id = matching_signal.id

                state.total_trades += 1
                trades_executed += 1

                log_event("trade",
                    f"Weather {signal.direction.upper()} ${trade_size:.0f} @ {entry_price:.0%} | {signal.market.city_name} {signal.market.target_date.strftime('%b %d')}",
                    {
                        "city": signal.market.city_name,
                        "direction": signal.direction,
                        "size": trade_size,
                        "edge": signal.edge,
                        "entry_price": entry_price,
                    }
                )

            state.last_run = datetime.utcnow()
            db.commit()

            if trades_executed > 0:
                log_event("success", f"Executed {trades_executed} weather trade(s)")
            else:
                log_event("info", "No new weather trades executed")

        finally:
            db.close()

    except Exception as e:
        log_event("error", f"Weather scan error: {str(e)}")
        logger.exception("Error in weather_scan_and_trade_job")


async def settlement_job():
    """Background job: Check for settled markets and update bankroll."""
    log_event("info", "Checking for settlements...")
    try:
        from backend.core.settlement import settle_pending_trades, update_bot_state_with_settlements
        db = SessionLocal()
        try:
            settled = await settle_pending_trades(db)
            if settled:
                await update_bot_state_with_settlements(db, settled)
                log_event("success", f"Settled {len(settled)} trade(s)")
            else:
                log_event("info", "No trades settled")
        finally:
            db.close()
    except Exception as e:
        log_event("error", f"Settlement error: {str(e)}")
        logger.exception("Error in settlement_job")

def start_scheduler():
    """Start the APScheduler."""
    global scheduler
    if scheduler and scheduler.running:
        return

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        weather_scan_and_trade_job,
        IntervalTrigger(seconds=settings.WEATHER_SCAN_INTERVAL_SECONDS),
        id="weather_scan_job",
        replace_existing=True,
        next_run_time=datetime.now()
    )

    scheduler.add_job(
        settlement_job,
        IntervalTrigger(seconds=settings.WEATHER_SETTLEMENT_INTERVAL_SECONDS),
        id="weather_settlement_job",
        replace_existing=True,
        next_run_time=datetime.now() + timedelta(seconds=15)
    )

    scheduler.add_job(
        check_active_orders,
        IntervalTrigger(seconds=15),
        id="order_manager_job",
        replace_existing=True,
        next_run_time=datetime.now() + timedelta(seconds=5)
    )

    scheduler.start()
    log_event("info", "Scheduler started")

def stop_scheduler():
    """Stop the APScheduler."""
    global scheduler
    if scheduler and scheduler.running:
        scheduler.shutdown()
        log_event("info", "Scheduler stopped")

def is_scheduler_running() -> bool:
    """Check if scheduler is running."""
    global scheduler
    return scheduler is not None and scheduler.running

async def run_manual_scan():
    """Trigger a scan manually."""
    await weather_scan_and_trade_job()
