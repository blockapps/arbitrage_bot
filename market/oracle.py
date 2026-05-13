"""
Price oracle backed by the BlockApps on-chain PriceOracle contract.

All non-USDST token prices are sourced from the BlockApps PriceOracle. USDST
is treated as a fixed $1 stablecoin and never hits the on-chain oracle.
"""

import time
import logging
import requests
from typing import Dict, Tuple
from core.constants import WEI_SCALE
from core.strato_client import strato_client

logger = logging.getLogger(__name__)

# USDST is a stablecoin pegged to $1
USDST_PRICE_WEI = WEI_SCALE  # 1.0 * 10^18


class PriceOracleError(ValueError):
    """Raised when on-chain price data cannot be retrieved or is missing.

    Subclasses ValueError so existing `except ValueError` sites keep working.
    """


class PriceOracle:
    def __init__(self, timeout: int = 10, cache_duration: int = 60, blockapps_price_oracle: str = ""):
        if not blockapps_price_oracle:
            raise ValueError("blockapps_price_oracle address must be configured")

        self.timeout = timeout
        self.cache_duration = cache_duration
        self.blockapps_price_oracle = blockapps_price_oracle
        self._cache: Dict[str, Tuple[int, float]] = {}

        # Mapping of symbol -> token_address for on-chain price lookups
        self._blockapps_tokens: Dict[str, str] = {}

        logger.info(f"BlockApps price oracle configured at {self.blockapps_price_oracle}")

    def register_blockapps_token(self, symbol: str, token_address: str) -> None:
        """
        Register a token for on-chain price lookup.

        Args:
            symbol: Token symbol (cache key, e.g. "ETHST", "GOLDST")
            token_address: On-chain token contract address (oracle lookup key)
        """
        self._blockapps_tokens[symbol] = token_address
        logger.info(f"Registered {symbol} ({token_address}) for BlockApps price oracle")

    def _fetch_blockapps_prices(self, symbols: list[str]) -> dict[str, int]:
        """
        Fetch prices from the BlockApps on-chain PriceOracle contract.

        Args:
            symbols: List of token symbols (must be registered first)

        Returns:
            Dict mapping symbol -> price in wei.

        Raises:
            PriceOracleError: If the on-chain oracle cannot be reached, or
                if any requested symbol has no price entry on-chain.
        """
        if not symbols:
            return {}

        try:
            client = strato_client()
            access_token = client.oauth.get_access_token()

            url = f"{client.strato_node_url}/cirrus/search/BlockApps-PriceOracle-prices"
            params = {"address": f"eq.{self.blockapps_price_oracle}"}

            response = requests.get(
                url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                params=params,
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            raise PriceOracleError(
                f"Failed to query BlockApps price oracle at {self.blockapps_price_oracle}: {e}"
            ) from e

        # Build lookup: token_address (lowercase) -> price
        price_by_address: dict[str, int] = {}
        for entry in data:
            token_addr = entry.get("key", "").lower()
            price_value = entry.get("value")
            if not token_addr or price_value is None:
                continue
            try:
                price_by_address[token_addr] = int(price_value)
            except (TypeError, ValueError):
                logger.warning(f"Ignoring non-integer oracle price for {token_addr}: {price_value!r}")

        prices: dict[str, int] = {}
        missing: list[str] = []
        now = time.time()
        for symbol in symbols:
            token_addr = self._blockapps_tokens.get(symbol, "").lower()
            price_wei = price_by_address.get(token_addr) if token_addr else None
            if price_wei is None or price_wei <= 0:
                missing.append(f"{symbol} ({token_addr or 'unregistered'})")
                continue
            prices[symbol] = price_wei
            self._cache[symbol] = (price_wei, now)
            logger.debug(f"{symbol}: {price_wei / WEI_SCALE:.6f} USDST (BlockApps)")

        if missing:
            raise PriceOracleError(
                f"BlockApps oracle missing price(s) for: {', '.join(missing)}"
            )

        return prices

    def fetch_all_prices(self, symbols: list[str], force_refresh: bool = False) -> dict[str, int]:
        """
        Fetch prices for all requested symbols.

        - USDST always returns a fixed price of $1 (stablecoin).
        - All other symbols must be registered via `register_blockapps_token`
          and have a current price entry in the on-chain PriceOracle.

        Args:
            symbols: List of token symbols to price.
            force_refresh: If True, bypass the in-memory cache.

        Returns:
            Dict mapping symbol -> price in wei.

        Raises:
            PriceOracleError: If any requested non-USDST symbol is unregistered
                or missing from the on-chain oracle.
        """
        if not symbols:
            return {}

        now = time.time()
        prices: dict[str, int] = {}
        to_fetch: list[str] = []

        for s in symbols:
            if s == "USDST":
                prices[s] = USDST_PRICE_WEI
                continue

            cached = self._cache.get(s)
            if cached and not force_refresh and now - cached[1] < self.cache_duration:
                prices[s] = cached[0]
                continue

            if s not in self._blockapps_tokens:
                raise PriceOracleError(
                    f"Token {s} is not registered with the BlockApps price oracle"
                )
            to_fetch.append(s)

        if to_fetch:
            prices.update(self._fetch_blockapps_prices(to_fetch))

        return prices

    def fetch_token_prices(self, token_a_symbol: str, token_b_symbol: str, force_refresh: bool = False) -> tuple[int, int]:
        """
        Fetch prices for two tokens, returning (price_a, price_b) in wei.

        Args:
            token_a_symbol: Symbol of token A.
            token_b_symbol: Symbol of token B.
            force_refresh: If True, bypass the in-memory cache.

        Returns:
            Tuple of (price_a, price_b) in wei.

        Raises:
            PriceOracleError: If either price is unavailable.
        """
        prices = self.fetch_all_prices(
            [token_a_symbol, token_b_symbol], force_refresh=force_refresh
        )

        price_a = prices.get(token_a_symbol)
        price_b = prices.get(token_b_symbol)

        if price_a is None:
            raise PriceOracleError(f"Failed to get price for {token_a_symbol}")
        if price_b is None:
            raise PriceOracleError(f"Failed to get price for {token_b_symbol}")

        return price_a, price_b
