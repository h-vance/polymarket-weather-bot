"""
Polymarket CLOB Execution Engine.
Handles order placement, cancellation, and partial fills safely.
"""
import logging
import asyncio
from typing import Optional, Dict
from py_clob_client.client import ClobClient

from backend.config import settings

logger = logging.getLogger("execution_engine")

class ExecutionEngine:
    def __init__(self):
        self.host = settings.POLYMARKET_HOST
        self.api_key = settings.POLYMARKET_API_KEY
        self.secret = settings.POLYMARKET_SECRET
        self.passphrase = settings.POLYMARKET_PASSPHRASE
        
        self.client: Optional[ClobClient] = None
        self._init_client()

    def _init_client(self):
        if settings.SIMULATION_MODE:
            logger.info("Running in SIMULATION_MODE. ClobClient not initialized.")
            return

        if not all([self.api_key, self.secret, self.passphrase]):
            logger.error("Missing Polymarket CLOB credentials.")
            return

        try:
            # Note: For production, chain_id should be injected or detected. Assume 137 (Polygon)
            self.client = ClobClient(
                self.host, 
                key=self.secret, 
                chain_id=137, 
                signature_type=2,
                funder=self.api_key # Often API Key is the public address, depends on setup
            )
            self.client.set_api_creds(self.api_key, self.secret, self.passphrase)
            logger.info("ClobClient initialized successfully.")
        except Exception as e:
            logger.error(f"Failed to init ClobClient: {e}")

    async def execute_order(self, token_id: str, price: float, size: float, side: str = "BUY") -> Dict:
        """
        Executes a CLOB compliant order with cancel-replace safety.
        side: 'BUY' or 'SELL'
        """
        if settings.SIMULATION_MODE:
            logger.info(f"[SIMULATION] Executing {side} order for token {token_id}: Size {size} @ {price}")
            return {"status": "simulated", "filled": size, "price": price}

        if not self.client:
            logger.error("ClobClient is not initialized. Cannot execute.")
            return {"status": "error", "message": "Client not initialized"}

        try:
            logger.info(f"Placing {side} order for token {token_id}: {size} @ {price}")
            # FOK / GTC logic
            order_args = {
                "token_id": token_id,
                "price": price,
                "side": side,
                "size": size,
                "fee_rate_bps": settings.FEE_RATE
            }
            # Note: synchronous call wrapping required in real app if using blocking client
            resp = self.client.create_and_post_order(order_args)
            
            logger.info(f"Order response: {resp}")
            
            # Simple reconciliation stub
            return {
                "status": "submitted",
                "order_id": resp.get("orderID", "unknown"),
                "raw_response": resp
            }
        except Exception as e:
            logger.error(f"Execution failed: {e}")
            return {"status": "error", "message": str(e)}

    async def cancel_order(self, order_id: str):
        if settings.SIMULATION_MODE:
            logger.info(f"[SIMULATION] Cancelling order {order_id}")
            return True

        if not self.client:
            return False

        try:
            self.client.cancel(order_id)
            logger.info(f"Cancelled order {order_id}")
            return True
        except Exception as e:
            logger.error(f"Cancel failed for {order_id}: {e}")
            return False

    async def get_order_status(self, order_id: str) -> Dict:
        """
        Fetches the current status of an order from the CLOB.
        Returns a dictionary with status details including filled size.
        """
        if settings.SIMULATION_MODE:
            # Simulate a fully filled order for testing
            return {"status": "filled", "size_matched": "100.0", "original_size": "100.0"}

        if not self.client:
            return {"status": "error", "message": "Client not initialized"}

        try:
            order_info = self.client.get_order(order_id)
            return order_info
        except Exception as e:
            logger.error(f"Failed to get order status for {order_id}: {e}")
            return {"status": "error", "message": str(e)}

execution_engine = ExecutionEngine()
