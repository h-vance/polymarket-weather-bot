"""Trade settlement logic for Polymarket weather markets."""
import httpx
import json
import logging
from datetime import datetime
from typing import Optional, List, Tuple
from sqlalchemy.orm import Session

from backend.config import settings
from backend.models.database import Trade, BotState, Signal
from backend.core.execution_engine import execution_engine

logger = logging.getLogger("trading_bot")

async def fetch_polymarket_resolution(market_id: str, event_slug: Optional[str] = None) -> Tuple[bool, Optional[float]]:
    """
    Fetch actual market resolution from Polymarket API.
    Returns: (is_resolved, settlement_value)
        - settlement_value: 1.0 if YES won, 0.0 if NO won
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if event_slug:
                response = await client.get(
                    "https://gamma-api.polymarket.com/events",
                    params={"slug": event_slug}
                )
                response.raise_for_status()
                events = response.json()

                if events:
                    event = events[0] if isinstance(events, list) else events
                    markets = event.get("markets", [])
                    for m in markets:
                        if str(m.get("id")) == str(market_id):
                            return _parse_market_resolution(m)

            url = f"https://gamma-api.polymarket.com/markets/{market_id}"
            response = await client.get(url)

            if response.status_code == 404:
                return await _search_market_in_events(market_id)

            response.raise_for_status()
            market = response.json()
            return _parse_market_resolution(market)

    except Exception as e:
        logger.warning(f"Failed to fetch resolution for {event_slug or market_id}: {e}")
        return False, None

async def _search_market_in_events(market_id: str) -> Tuple[bool, Optional[float]]:
    """Search for market in events (both active and closed)."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            for closed in [True, False]:
                params = {"closed": str(closed).lower(), "limit": 200}
                response = await client.get("https://gamma-api.polymarket.com/events", params=params)
                response.raise_for_status()
                events = response.json()

                for event in events:
                    for market in event.get("markets", []):
                        if str(market.get("id")) == str(market_id):
                            return _parse_market_resolution(market)

        return False, None
    except Exception as e:
        logger.warning(f"Failed to search for market {market_id}: {e}")
        return False, None

def _parse_market_resolution(market: dict) -> Tuple[bool, Optional[float]]:
    """
    Parse market data to determine if resolved and outcome.
    - outcomePrices[0] > 0.99 -> YES won
    - outcomePrices[0] < 0.01 -> NO won
    """
    is_closed = market.get("closed", False)
    if not is_closed:
        return False, None

    outcome_prices = market.get("outcomePrices", [])
    if not outcome_prices:
        return False, None

    try:
        if isinstance(outcome_prices, str):
            outcome_prices = json.loads(outcome_prices)

        first_price = float(outcome_prices[0]) if outcome_prices else 0.5

        if first_price > 0.99:
            logger.info(f"Market {market.get('id')} resolved: YES won")
            return True, 1.0
        elif first_price < 0.01:
            logger.info(f"Market {market.get('id')} resolved: NO won")
            return True, 0.0
        else:
            return False, None

    except (ValueError, IndexError, TypeError) as e:
        logger.warning(f"Failed to parse outcome prices: {e}")
        return False, None

def calculate_pnl(trade: Trade, settlement_value: float) -> float:
    """
    Calculate P&L for a trade given the settlement value based on actual filled_size.
    settlement_value: 1.0 if YES won, 0.0 if NO won
    """
    if trade.filled_size <= 0:
        return 0.0

    if trade.direction == "yes":
        if settlement_value == 1.0:
            pnl = trade.filled_size * (1.0 - trade.entry_price)
        else:
            pnl = -trade.filled_size * trade.entry_price
    else:  # NO position
        if settlement_value == 0.0:
            pnl = trade.filled_size * (1.0 - trade.entry_price)
        else:
            pnl = -trade.filled_size * trade.entry_price

    return round(pnl, 2)

async def settle_pending_trades(db: Session) -> List[Trade]:
    """Process all pending trades for settlement using Polymarket Gamma API."""
    try:
        # Only settle trades that have been filled and are awaiting settlement
        pending = db.query(Trade).filter(
            ~Trade.settled, 
            Trade.execution_status.in_(["filled", "canceled", "simulated"])
        ).all()
    except Exception as e:
        logger.error(f"Failed to query pending trades: {e}")
        return []

    if not pending:
        logger.info("No pending trades to settle")
        return []

    logger.info(f"Checking {len(pending)} pending trades for settlement...")
    settled_trades = []

    for trade in pending:
        # If canceled with 0 fill, just settle it as canceled.
        if trade.execution_status == "canceled" and trade.filled_size <= 0:
            trade.settled = True
            trade.result = "canceled"
            trade.pnl = 0.0
            trade.settlement_time = datetime.utcnow()
            settled_trades.append(trade)
            continue

        try:
            is_resolved, settlement_value = await fetch_polymarket_resolution(
                trade.market_ticker,
                event_slug=trade.event_slug
            )

            if is_resolved and settlement_value is not None:
                pnl = calculate_pnl(trade, settlement_value)
                
                trade.settled = True
                trade.settlement_value = settlement_value
                trade.pnl = pnl
                trade.settlement_time = datetime.utcnow()

                outcome = "yes" if settlement_value == 1.0 else "no"
                if pnl > 0:
                    trade.result = "win"
                elif pnl < 0:
                    trade.result = "loss"
                else:
                    trade.result = "push"

                settled_trades.append(trade)

                # Fire smart contract redemption to realize profits
                try:
                    if not settings.SIMULATION_MODE and execution_engine.client:
                        logger.info(f"Redeeming winning tokens for condition {trade.market_ticker}")
                        # We must redeem the condition ID, which is the market_ticker in this context
                        execution_engine.client.redeem(trade.market_ticker)
                except Exception as e:
                    logger.error(f"Failed to redeem tokens for {trade.market_ticker}: {e}")

                # Update linked Signal for calibration
                if trade.signal_id:
                    linked_signal = db.query(Signal).filter(Signal.id == trade.signal_id).first()
                    if linked_signal:
                        linked_signal.actual_outcome = outcome
                        linked_signal.outcome_correct = (linked_signal.direction == outcome)
                        linked_signal.settlement_value = settlement_value
                        linked_signal.settled_at = datetime.utcnow()
        except Exception as e:
            logger.error(f"Failed to settle trade {trade.id}: {e}")
            continue

    if settled_trades:
        try:
            db.commit()
            logger.info(f"Settled {len(settled_trades)} trades")
        except Exception as e:
            logger.error(f"Failed to commit settlements: {e}")
            db.rollback()
            return []
    else:
        logger.info("No trades ready for settlement (markets still open)")

    return settled_trades

async def update_bot_state_with_settlements(db: Session, settled_trades: List[Trade]) -> None:
    """Update bot state with P&L and payouts from settled trades."""
    if not settled_trades:
        return

    try:
        state = db.query(BotState).first()
        if not state:
            return

        for trade in settled_trades:
            if trade.pnl is not None and trade.result != "canceled":
                state.total_pnl += trade.pnl
                if trade.result == "win":
                    # We previously deducted entry cost during the fill in order_manager.
                    # A win means we receive $1.00 per share filled.
                    state.bankroll += trade.filled_size
                    state.winning_trades += 1
                # If loss, we receive $0, bankroll stays the same (entry cost already deducted).

        db.commit()
        logger.info(f"Updated bot state: Bankroll ${state.bankroll:.2f}, P&L ${state.total_pnl:+.2f}")
    except Exception as e:
        logger.error(f"Failed to update bot state: {e}")
        db.rollback()