"""
Price oracle for fetching external market prices via Alchemy Price API
and BlockApps on-chain price oracle for synthetic tokens (GOLDST, SILVST, etc.)
"""

import os
import time
import logging
import requests
from typing import Dict, Tuple
from core.constants import WEI_SCALE
from core.strato_client import strato_client

logger = logging.getLogger(__name__)


class PriceOracle:
    def __init__(self, timeout: int = 10, cache_duration: int = 60, blockapps_price_oracle: str = ""):
        self.timeout = timeout
        self.cache_duration = cache_duration
        self.key = os.getenv("ALCHEMY_API_KEY")
        self.blockapps_price_oracle = blockapps_price_oracle
        self._cache: Dict[str, Tuple[int, float]] = {}
        
        # Mapping of symbol -> token_address for BlockApps-based price lookups
        self._blockapps_tokens: Dict[str, str] = {}

        if self.key:
            logger.info("Alchemy API key configured for price oracle")
        else:
            logger.warning("ALCHEMY_API_KEY not set - only BlockApps prices available")
        
        if self.blockapps_price_oracle:
            logger.info(f"BlockApps price oracle configured at {self.blockapps_price_oracle}")

    def register_blockapps_token(self, symbol: str, token_address: str) -> None:
        """
        Register a token to use BlockApps on-chain oracle for price lookup.
        
        Args:
            symbol: Token symbol (e.g., "GOLDST", "SILVST")
            token_address: On-chain token contract address
        """
        self._blockapps_tokens[symbol] = token_address
        logger.info(f"Registered {symbol} ({token_address}) for BlockApps price oracle")

    def _fetch_blockapps_prices(self, symbols: list[str]) -> dict[str, int]:
        """
        Fetch prices from BlockApps on-chain price oracle.
        
        Args:
            symbols: List of token symbols registered for BlockApps lookup
            
        Returns:
            Dict mapping symbol -> price in wei
        """
        if not symbols:
            return {}
        
        prices = {}
        
        try:
            client = strato_client()
            access_token = client.oauth.get_access_token()
            
            # Fetch all prices from BlockApps PriceOracle
            url = f"{client.strato_node_url}/cirrus/search/BlockApps-PriceOracle-prices"
            params = {"address": f"eq.{self.blockapps_price_oracle}"}
            
            response = requests.get(
                url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json"
                },
                params=params,
                timeout=self.timeout
            )
            response.raise_for_status()
            data = response.json()
            
            # Build lookup: token_address (lowercase) -> price
            price_by_address = {}
            for entry in data:
                token_addr = entry.get("key", "").lower()
                price_value = entry.get("value")
                if token_addr and price_value is not None:
                    price_by_address[token_addr] = int(price_value)
            
            # Map requested symbols to prices
            now = time.time()
            for symbol in symbols:
                token_addr = self._blockapps_tokens.get(symbol, "").lower()
                if token_addr in price_by_address:
                    price_wei = price_by_address[token_addr]
                    prices[symbol] = price_wei
                    self._cache[symbol] = (price_wei, now)
                    logger.debug(f"{symbol}: {price_wei / WEI_SCALE:.2f} USDST (BlockApps)")
                else:
                    logger.warning(f"No BlockApps price found for {symbol} (address: {token_addr})")
                    
        except Exception as e:
            logger.error(f"Failed to fetch BlockApps prices: {e}")
        
        return prices

    def fetch_all_prices(self, symbols: list[str], force_refresh: bool = False) -> dict[str, int]:
        """
        Fetch prices for all requested symbols from appropriate sources.
        
        - BlockApps tokens (registered via register_blockapps_token) use on-chain oracle
        - Other tokens use Alchemy API
        
        Args:
            symbols: List of token symbols to fetch prices for
            force_refresh: If True, bypass cache
            
        Returns:
            Dict mapping symbol -> price in wei
        """
        if not symbols:
            return {}

        now = time.time()
        prices = {}
        alchemy_to_fetch = []
        blockapps_to_fetch = []

        # Check cache and categorize symbols by source
        for s in symbols:
            cached = self._cache.get(s)
            if cached and not force_refresh and now - cached[1] < self.cache_duration:
                prices[s] = cached[0]
            elif s in self._blockapps_tokens:
                blockapps_to_fetch.append(s)
            else:
                alchemy_to_fetch.append(s)

        # Fetch BlockApps prices
        if blockapps_to_fetch:
            blockapps_prices = self._fetch_blockapps_prices(blockapps_to_fetch)
            prices.update(blockapps_prices)

        # Fetch Alchemy prices
        if alchemy_to_fetch and self.key:
            for s in alchemy_to_fetch:
                try:
                    url = f"https://api.g.alchemy.com/prices/v1/{self.key}/tokens/by-symbol"
                    r = requests.get(url, params={"symbols": s}, timeout=self.timeout)
                    r.raise_for_status()
                    data = r.json().get("data", [])

                    if not data or not data[0].get("prices"):
                        logger.warning(f"No Alchemy price data for {s}: {data}")
                        continue

                    value = float(data[0]["prices"][0]["value"])
                    price_wei = int(value * WEI_SCALE)
                    self._cache[s] = (price_wei, now)
                    prices[s] = price_wei
                    logger.debug(f"{s}: ${value:.2f} (Alchemy)")

                except Exception as e:
                    logger.warning(f"Failed to fetch {s} from Alchemy: {e}")
                    continue

        return prices
