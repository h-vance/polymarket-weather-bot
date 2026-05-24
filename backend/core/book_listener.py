"""
Order Book Listener for Polymarket CLOB.
Ingests live order book depth, calculates mid-price, and provides freshness guards.
"""
import logging
from typing import Optional, Tuple
from py_clob_client.client import ClobClient

from backend.config import settings
from backend.core.market_scanner import WeatherMarket

logger = logging.getLogger("book_listener")

class BookListener:
    def __init__(self):
        self.host = settings.POLYMARKET_HOST
        self.client: Optional[ClobClient] = None
        self._init_client()

    def _init_client(self):
        try:
            # We only need a read-only client for fetching books
            self.client = ClobClient(self.host, chain_id=137)
            logger.info("ClobClient initialized for BookListener.")
        except Exception as e:
            logger.error(f"Failed to init ClobClient for BookListener: {e}")

    async def get_live_prices(self, market: WeatherMarket) -> Tuple[Optional[float], Optional[float]]:
        """
        Fetches the live L2 order book for YES and NO tokens.
        Returns a tuple of (live_yes_price, live_no_price).
        Returns (None, None) if the book is stale or empty.
        """
        if not self.client:
            logger.error("ClobClient not initialized.")
            return None, None

        if settings.SIMULATION_MODE:
            # In pure simulation without real keys, we can fallback to the Gamma API static prices
            # But normally we'd fetch the real book. For this implementation, we will try to fetch the book.
            pass

        try:
            # Fetch YES token order book
            # In a production WebSocket implementation, this would read from local state.
            # Here we poll the REST endpoint for simplicity and reliability.
            book_yes = self.client.get_order_book(market.yes_token_id)
            
            bids = book_yes.get("bids", [])
            asks = book_yes.get("asks", [])
            
            if not bids or not asks:
                logger.warning(f"Thin or empty book for token {market.yes_token_id}")
                # Fallback to static prices if book is empty to prevent halting in simulation
                if settings.SIMULATION_MODE:
                    return market.yes_price, market.no_price
                return None, None
                
            best_bid = float(bids[0].get("price", 0))
            best_ask = float(asks[0].get("price", 1))
            
            spread = best_ask - best_bid
            if spread > 0.10: # 10 cents spread is too wide, consider book stale/thin
                logger.warning(f"Spread too wide ({spread:.2f}) for token {market.yes_token_id}")
                if settings.SIMULATION_MODE:
                    return market.yes_price, market.no_price
                return None, None
                
            mid_price = (best_bid + best_ask) / 2.0
            
            # Polymarket YES + NO roughly equals 1.0 (minus fees/spreads). 
            # We return mid_price for YES, and 1 - mid_price for NO.
            return mid_price, 1.0 - mid_price
            
        except Exception as e:
            logger.error(f"Error fetching order book for {market.market_id}: {e}")
            if settings.SIMULATION_MODE:
                return market.yes_price, market.no_price
            return None, None

book_listener = BookListener()
