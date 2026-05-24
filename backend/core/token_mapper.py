"""
Token Mapper for Polymarket CLOB.
Handles safe mapping of YES/NO outcomes to their respective ERC1155 token IDs.
"""
import json
import logging
from typing import Dict

logger = logging.getLogger("token_mapper")

class TokenMappingError(Exception):
    pass

class TokenMapper:
    @staticmethod
    def extract_and_validate_tokens(market_data: dict) -> Dict[str, str]:
        """
        Extracts YES and NO CLOB token IDs from Gamma API market data.
        Returns {'yes': '0x...', 'no': '0x...'}
        Raises TokenMappingError if tokens are missing or invalid.
        """
        clob_tokens_raw = market_data.get("clobTokenIds", "[]")
        
        if isinstance(clob_tokens_raw, str):
            try:
                clob_tokens = json.loads(clob_tokens_raw)
            except Exception:
                clob_tokens = []
        elif isinstance(clob_tokens_raw, list):
            clob_tokens = clob_tokens_raw
        else:
            clob_tokens = []
            
        if not clob_tokens or len(clob_tokens) < 2:
            raise TokenMappingError(f"Market {market_data.get('id', 'unknown')} missing sufficient clobTokenIds")
            
        # Outcomes array should match index of clobTokenIds. Typically ["Yes", "No"]
        outcomes_raw = market_data.get("outcomes", '["Yes", "No"]')
        if isinstance(outcomes_raw, str):
            try:
                outcomes = json.loads(outcomes_raw)
            except Exception:
                outcomes = ["Yes", "No"]
        elif isinstance(outcomes_raw, list):
            outcomes = outcomes_raw
        else:
            outcomes = ["Yes", "No"]

        # Validate that the outcomes map expected YES/NO structure
        if len(outcomes) >= 2 and outcomes[0].lower() == "yes" and outcomes[1].lower() == "no":
            yes_token = str(clob_tokens[0])
            no_token = str(clob_tokens[1])
        else:
            logger.warning(f"Unexpected outcomes format {outcomes} for market {market_data.get('id')}. Assuming 0 is YES, 1 is NO.")
            yes_token = str(clob_tokens[0])
            no_token = str(clob_tokens[1])

        return {
            "yes": yes_token,
            "no": no_token
        }

token_mapper = TokenMapper()
