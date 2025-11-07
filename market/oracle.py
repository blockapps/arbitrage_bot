"""
Price oracle for fetching external market prices via Alchemy Price API
"""

import os
import time
import logging
import requests
from typing import Dict, Tuple
from core.constants import WEI_SCALE

logger = logging.getLogger(__name__)

class PriceOracle:
    def __init__(self, timeout: int = 10, cache_duration: int = 60):
        self.timeout = timeout
        self.cache_duration = cache_duration
        self.key = os.getenv("ALCHEMY_API_KEY")
        self._cache: Dict[str, Tuple[int, float]] = {}

        if self.key:
            logger.info("Alchemy API key configured for price oracle")
        else:
            logger.error("ALCHEMY_API_KEY not set - real price data unavailable")

    def fetch_all_prices(self, symbols: list[str], force_refresh: bool = False) -> dict[str, int]:
        if not symbols or not self.key:
            return {}

        now = time.time()
        prices, to_fetch = {}, []

        for s in symbols:
            cached = self._cache.get(s)
            if cached and not force_refresh and now - cached[1] < self.cache_duration:
                prices[s] = cached[0]
            else:
                to_fetch.append(s)

        if not to_fetch:
            return prices

        for s in to_fetch:
            try:
                url = f"https://api.g.alchemy.com/prices/v1/{self.key}/tokens/by-symbol"
                r = requests.get(url, params={"symbols": s}, timeout=self.timeout)
                r.raise_for_status()
                data = r.json().get("data", [])

                if not data or not data[0].get("prices"):
                    logger.warning(f"No price data for {s}: {data}")
                    continue

                value = float(data[0]["prices"][0]["value"])
                price_wei = int(value * WEI_SCALE)
                self._cache[s] = (price_wei, now)
                prices[s] = price_wei
                logger.debug(f"{s}: ${value:.2f}")

            except Exception as e:
                logger.warning(f"Failed to fetch {s}: {e}")
                continue

        return prices
