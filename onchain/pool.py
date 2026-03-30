"""
AMM Pool contract wrapper for Strato
"""

import logging
import requests
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import List, Optional, Tuple

from core.strato_client import strato_client
from core.constants import WEI_SCALE, USDST_ADDRESS
from .token import Token

logger = logging.getLogger(__name__)


@dataclass
class PoolData:
    """Pool data from Cirrus search"""
    address: str
    tokenA: 'Token'  # Reference to Token object
    tokenB: 'Token'  # Reference to Token object
    tokenABalance: int  # Balance in wei (raw units)
    tokenBBalance: int  # Balance in wei (raw units)
    aToBRatio: int  # Pool ratio tokenB per tokenA (wei scaled)
    bToARatio: int  # Pool ratio tokenA per tokenB (wei scaled)
    isStable: bool  # True for stable pools, False for AMM pools
    stableFee: int = 0
    offpegFeeMultiplier: int = 0
    initialA: int = 0
    futureA: int = 0
    initialATime: int = 0
    futureATime: int = 0
    rateMultiplierA: int = 0
    rateMultiplierB: int = 0
    adminBalanceA: int = 0
    adminBalanceB: int = 0
    rateOraclePriceA: int = 0
    rateOraclePriceB: int = 0
    # N-coin fields (populated for stable pools)
    coin_addresses: List[str] = field(default_factory=list)
    coin_reserves: List[int] = field(default_factory=list)
    coin_admin_balances: List[int] = field(default_factory=list)
    coin_rate_multipliers: List[int] = field(default_factory=list)
    coin_oracle_prices: List[int] = field(default_factory=list)




class Pool:
    """AMM Pool contract wrapper for Strato blockchain"""
    
    def __init__(
        self,
        address: str,
        fee_bps: int = 30,
        slippage_factor_amm: float = 0.96,
        slippage_factor_stable: float = 0.92,
    ):
        """
        Initialize pool contract
        
        Args:
            address: Pool contract address
            fee_bps: Pool fee in basis points
            slippage_factor_amm: Min-out multiplier for AMM swaps
            slippage_factor_stable: Min-out multiplier for stable swaps
        """
        self.address = address
        self.fee_bps = fee_bps
        self.slippage_factor_amm = self._normalize_slippage_factor(slippage_factor_amm)
        self.slippage_factor_stable = self._normalize_slippage_factor(slippage_factor_stable)
        
        # Tokens will be initialized when fetch_pool_data() is called
        self.token_a: Optional[Token] = None
        self.token_b: Optional[Token] = None
        self.coins: List[Token] = []
        
        # Cache pool data
        self._pool_data: Optional[PoolData] = None
        self.is_stable: bool = False
        self._stable_table: Optional[str] = None

    @staticmethod
    def _normalize_slippage_factor(value: float, default: float = 0.96) -> float:
        try:
            factor = float(value)
        except (TypeError, ValueError):
            return default
        # Keep factor in sane bounds for minAmountOut guard.
        if factor <= 0 or factor > 1:
            return default
        return factor

    @staticmethod
    def _to_int(value, default: int = 0) -> int:
        if value is None:
            return default
        try:
            return int(str(value))
        except Exception:
            try:
                return int(float(value))
            except Exception:
                return default

    @staticmethod
    def _to_scaled_ratio(value, default: int = 0) -> int:
        """
        Parse pool ratio values to 1e18-scaled integer.
        Handles both decimal strings (e.g. "0.9165") and already-scaled ints.
        """
        if value is None:
            return default

        text = str(value).strip()
        if not text:
            return default

        # If ratio comes as decimal, scale it to wei.
        if any(ch in text for ch in (".", "e", "E")):
            try:
                scaled = int(Decimal(text) * Decimal(WEI_SCALE))
                return scaled if scaled > 0 else default
            except (InvalidOperation, ValueError):
                return default

        # Integer path: value may already be wei-scaled or unscaled integer ratio.
        try:
            parsed = int(text)
        except ValueError:
            return default

        if parsed <= 0:
            return default

        # Heuristic: small ints represent unscaled ratios (e.g. "1"), scale them.
        return parsed * WEI_SCALE if parsed < 10**12 else parsed
    
    def fetch_pool_data(self, force_refresh: bool = False) -> PoolData:
        """
        Fetch pool data from Cirrus search including user balances and allowances
        
        Args:
            force_refresh: If True, force a fresh fetch even if cached data exists
        """
        if self._pool_data and not force_refresh:
            return self._pool_data
        
        try:
            client = strato_client()
            access_token = client.oauth.get_access_token()
            account_address = client.account.address
            
            # Build select query with nested balances and allowances for user
            # Use !left instead of !inner so we get token info even if no balances/allowances
            select_query = (
                f'address,tokenABalance,tokenBBalance,aToBRatio,bToARatio,isStable,'
                f'tokenA:tokenA_fkey(address,_symbol,_name,'
                f'balances:BlockApps-Token-_balances!left(key,value::text),'
                f'allowances:BlockApps-Token-_allowances!left(key,key2,value::text)),'
                f'tokenB:tokenB_fkey(address,_symbol,_name,'
                f'balances:BlockApps-Token-_balances!left(key,value::text),'
                f'allowances:BlockApps-Token-_allowances!left(key,key2,value::text))'
            )
            params = {
                'address': f'eq.{self.address}',
                'select': select_query,
                # Filter balances to user's address
                'tokenA.balances.key': f'eq.{account_address}',
                'tokenB.balances.key': f'eq.{account_address}',
                # Filter allowances to user's address (owner) and pool address (spender)
                'tokenA.allowances.key': f'eq.{account_address}',
                'tokenA.allowances.key2': f'eq.{self.address}',
                'tokenB.allowances.key': f'eq.{account_address}',
                'tokenB.allowances.key2': f'eq.{self.address}'
            }
            
            response = requests.get(
                f'{client.strato_node_url}/cirrus/search/BlockApps-Pool',
                headers={
                    'Authorization': f'Bearer {access_token}',
                    'Content-Type': 'application/json'
                },
                params=params,
                timeout=10000
            )
            response.raise_for_status()
            
            data = response.json()
            if not data or len(data) == 0:
                raise ValueError(f"No pool data found for address {self.address}")
            
            pool_dict = data[0]
            
            # Parse token data
            token_a_dict = pool_dict.get('tokenA') or {}
            token_b_dict = pool_dict.get('tokenB') or {}
            
            # Create or update token objects with data from pool
            if self.token_a is None:
                self.token_a = Token(token_a_dict.get('address', ''))
            else:
                self.token_a.address = token_a_dict.get('address', '')
            self.token_a.symbol = token_a_dict.get('_symbol', '')
            self.token_a.name = token_a_dict.get('_name', '')
            
            # Extract user balance and allowance from nested query
            token_a_balances = token_a_dict.get('balances', [])
            token_a_allowances = token_a_dict.get('allowances', [])
            self.token_a.balance = int(token_a_balances[0].get('value', '0')) if token_a_balances else 0
            self.token_a.allowance = int(token_a_allowances[0].get('value', '0')) if token_a_allowances else 0
            
            if self.token_b is None:
                self.token_b = Token(token_b_dict.get('address', ''))
            else:
                self.token_b.address = token_b_dict.get('address', '')
            self.token_b.symbol = token_b_dict.get('_symbol', '')
            self.token_b.name = token_b_dict.get('_name', '')
            
            # Extract user balance and allowance from nested query
            token_b_balances = token_b_dict.get('balances', [])
            token_b_allowances = token_b_dict.get('allowances', [])
            self.token_b.balance = int(token_b_balances[0].get('value', '0')) if token_b_balances else 0
            self.token_b.allowance = int(token_b_allowances[0].get('value', '0')) if token_b_allowances else 0

            self.coins = [self.token_a, self.token_b]
            
            # Create PoolData with references to token objects
            is_stable_raw = pool_dict.get('isStable', False)
            is_stable = str(is_stable_raw).lower() in ('true', '1', 't', 'yes')
            self.is_stable = is_stable
            token_a_addr = token_a_dict.get('address', '')
            token_b_addr = token_b_dict.get('address', '')
            stable_meta = self._fetch_stable_meta(
                client.strato_node_url, access_token, token_a_addr, token_b_addr
            ) if is_stable else {}
            self._pool_data = PoolData(
                address=pool_dict.get('address', self.address),
                tokenA=self.token_a,
                tokenB=self.token_b,
                tokenABalance=self._to_int(pool_dict.get('tokenABalance', 0)),
                tokenBBalance=self._to_int(pool_dict.get('tokenBBalance', 0)),
                aToBRatio=self._to_scaled_ratio(pool_dict.get('aToBRatio', 0)),
                bToARatio=self._to_scaled_ratio(pool_dict.get('bToARatio', 0)),
                isStable=self.is_stable,
                stableFee=self._to_int(stable_meta.get('fee', 0)),
                offpegFeeMultiplier=self._to_int(stable_meta.get('offpegFeeMultiplier', 0)),
                initialA=self._to_int(stable_meta.get('initialA', 0)),
                futureA=self._to_int(stable_meta.get('futureA', 0)),
                initialATime=self._to_int(stable_meta.get('initialATime', 0)),
                futureATime=self._to_int(stable_meta.get('futureATime', 0)),
                rateMultiplierA=self._to_int(stable_meta.get('rateMultiplierA', 0)),
                rateMultiplierB=self._to_int(stable_meta.get('rateMultiplierB', 0)),
                adminBalanceA=self._to_int(stable_meta.get('adminBalanceA', 0)),
                adminBalanceB=self._to_int(stable_meta.get('adminBalanceB', 0)),
                rateOraclePriceA=self._to_int(stable_meta.get('rateOraclePriceA', 0)),
                rateOraclePriceB=self._to_int(stable_meta.get('rateOraclePriceB', 0)),
                coin_addresses=stable_meta.get('coin_addresses', []),
                coin_reserves=stable_meta.get('coin_reserves', []),
                coin_admin_balances=stable_meta.get('coin_admin_balances', []),
                coin_rate_multipliers=stable_meta.get('coin_rate_multipliers', []),
                coin_oracle_prices=stable_meta.get('coin_oracle_prices', []),
            )

            # Append extra coins for n>2 stable pools
            if is_stable and self._pool_data.coin_addresses:
                extra_addrs = self._pool_data.coin_addresses[2:]
                for addr in extra_addrs:
                    existing = next((t for t in self.coins if t.address.lower() == addr.lower()), None)
                    if existing is None:
                        tok = Token(addr)
                        self._populate_extra_coin(
                            tok, addr, client.strato_node_url, access_token,
                            client.account.address
                        )
                        self.coins.append(tok)
            
            return self._pool_data
            
        except Exception as e:
            logger.error(f"Failed to fetch pool data: {e}")
            raise

    def _fetch_stable_meta(
        self, strato_node_url: str, access_token: str,
        token_a_addr: str = "", token_b_addr: str = ""
    ) -> dict:
        """
        Best-effort stable pool metadata lookup including mapping tables
        for rateMultipliers, adminBalances, and rateOracles.
        Discovers all coins from the contract's coins array for n-coin support.
        """
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }
        params = {
            'address': f'eq.{self.address}',
            'select': 'fee,offpegFeeMultiplier,initialA,futureA,initialATime,futureATime'
        }

        result = {}
        resolved_table = None

        for table in ("BlockApps-StablePool", "StablePool"):
            try:
                response = requests.get(
                    f'{strato_node_url}/cirrus/search/{table}',
                    headers=headers,
                    params=params,
                    timeout=10000
                )
                if not response.ok:
                    continue
                data = response.json()
                if data and len(data) > 0:
                    result = data[0]
                    resolved_table = table
                    break
            except Exception:
                continue

        if not resolved_table:
            logger.warning(
                "Stable pool metadata not available in Cirrus for %s; using conservative defaults",
                self.address
            )
            return result

        self._stable_table = resolved_table

        # Discover all coins from the contract's coins array
        coin_addresses = self._fetch_coin_addresses(strato_node_url, headers, resolved_table)
        if not coin_addresses and token_a_addr and token_b_addr:
            coin_addresses = [token_a_addr, token_b_addr]
        result['coin_addresses'] = coin_addresses

        if coin_addresses:
            self._fetch_stable_mappings_n(
                strato_node_url, headers, resolved_table, coin_addresses, result
            )
            # Populate legacy A/B fields from the n-coin arrays for backward compat
            for idx, suffix in enumerate(['A', 'B']):
                if idx < len(result.get('coin_rate_multipliers', [])):
                    result[f'rateMultiplier{suffix}'] = result['coin_rate_multipliers'][idx]
                if idx < len(result.get('coin_admin_balances', [])):
                    result[f'adminBalance{suffix}'] = result['coin_admin_balances'][idx]
                if idx < len(result.get('coin_oracle_prices', [])):
                    result[f'rateOraclePrice{suffix}'] = result['coin_oracle_prices'][idx]

        return result

    def _fetch_coin_addresses(
        self, strato_node_url: str, headers: dict, table_prefix: str
    ) -> List[str]:
        """Fetch the ordered list of coin addresses from the StablePool coins array."""
        url = f'{strato_node_url}/cirrus/search/{table_prefix}-coins'
        params = {
            'address': f'eq.{self.address}',
            'select': 'key,value',
            'order': 'key.asc',
        }
        try:
            response = requests.get(url, headers=headers, params=params, timeout=10000)
            if response.ok:
                data = response.json()
                if data:
                    return [row['value'] for row in sorted(data, key=lambda r: int(r.get('key', 0)))]
        except Exception as e:
            logger.debug("Failed to fetch coins array from %s: %s", url, e)
        return []

    def _fetch_token_balance(
        self, strato_node_url: str, headers: dict, table_prefix: str, token_addr: str
    ) -> int:
        """Fetch a token's pool-side balance from the tokenBalances mapping."""
        val = self._fetch_mapping_value(
            strato_node_url, headers, table_prefix, 'tokenBalances', token_addr
        )
        return self._to_int(val) if val is not None else 0

    def _fetch_stable_mappings_n(
        self, strato_node_url: str, headers: dict, table_prefix: str,
        coin_addresses: List[str], result: dict
    ) -> None:
        """Fetch rateMultipliers, adminBalances, rateOracles, and tokenBalances for all coins."""
        n = len(coin_addresses)
        rate_multipliers: List[int] = []
        admin_balances: List[int] = []
        oracle_prices: List[int] = []
        reserves: List[int] = []

        for idx, token_addr in enumerate(coin_addresses):
            val = self._fetch_mapping_value(
                strato_node_url, headers, table_prefix, 'rateMultipliers', token_addr
            )
            rate_multipliers.append(self._to_int(val) if val is not None else 0)

            val = self._fetch_mapping_value(
                strato_node_url, headers, table_prefix, 'adminBalances', token_addr
            )
            admin_balances.append(self._to_int(val) if val is not None else 0)

            oracle_addr = self._fetch_mapping_value(
                strato_node_url, headers, table_prefix, 'rateOracles', token_addr
            )
            price = 0
            if oracle_addr and str(oracle_addr).replace('0', '') != '':
                fetched = self._fetch_oracle_price(
                    strato_node_url, headers, str(oracle_addr), token_addr
                )
                if fetched is not None and int(fetched) > 0:
                    price = int(fetched)
            oracle_prices.append(price)

            # For coins 0 and 1, the caller already has tokenABalance/tokenBBalance
            # from the main pool query; only fetch from mapping for coin index >= 2.
            if idx >= 2:
                reserves.append(self._fetch_token_balance(
                    strato_node_url, headers, table_prefix, token_addr
                ))
            else:
                reserves.append(0)  # placeholder; filled by caller from top-level fields

        result['coin_rate_multipliers'] = rate_multipliers
        result['coin_admin_balances'] = admin_balances
        result['coin_oracle_prices'] = oracle_prices
        result['coin_reserves'] = reserves

    def _fetch_mapping_value(
        self, strato_node_url: str, headers: dict,
        table_prefix: str, mapping_name: str, key: str
    ):
        """Fetch a single value from a StablePool mapping sub-table."""
        url = f'{strato_node_url}/cirrus/search/{table_prefix}-{mapping_name}'
        params = {
            'address': f'eq.{self.address}',
            'key': f'eq.{key}',
            'select': 'value'
        }
        try:
            response = requests.get(url, headers=headers, params=params, timeout=10000)
            if response.ok:
                data = response.json()
                if data and len(data) > 0:
                    return data[0].get('value')
        except Exception as e:
            logger.debug("Failed to fetch %s-%s for key %s: %s", table_prefix, mapping_name, key, e)
        return None

    def _fetch_oracle_price(
        self, strato_node_url: str, headers: dict,
        oracle_address: str, token_address: str
    ):
        """Fetch asset price from an on-chain PriceOracle contract via Cirrus."""
        for table in ("BlockApps-PriceOracle-prices", "PriceOracle-prices"):
            url = f'{strato_node_url}/cirrus/search/{table}'
            params = {
                'address': f'eq.{oracle_address}',
                'key': f'eq.{token_address}',
                'select': 'value'
            }
            try:
                response = requests.get(url, headers=headers, params=params, timeout=10000)
                if response.ok:
                    data = response.json()
                    if data and len(data) > 0:
                        return data[0].get('value')
            except Exception:
                continue
        return None
    
    def _populate_extra_coin(
        self, token: 'Token', token_addr: str,
        strato_node_url: str, access_token: str, wallet_addr: str
    ) -> None:
        """Fetch symbol, name, balance, and allowance for an extra coin (index >= 2)."""
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }
        for table in ("BlockApps-Token", "Token"):
            try:
                resp = requests.get(
                    f'{strato_node_url}/cirrus/search/{table}',
                    headers=headers,
                    params={'address': f'eq.{token_addr}', 'select': '_symbol,_name'},
                    timeout=10000
                )
                if resp.ok:
                    data = resp.json()
                    if data:
                        token.symbol = data[0].get('_symbol', '')
                        token.name = data[0].get('_name', '')
                        break
            except Exception:
                continue

        for table in ("BlockApps-Token-_balances", "Token-_balances"):
            try:
                resp = requests.get(
                    f'{strato_node_url}/cirrus/search/{table}',
                    headers=headers,
                    params={
                        'address': f'eq.{token_addr}',
                        'key': f'eq.{wallet_addr}',
                        'select': 'value::text'
                    },
                    timeout=10000
                )
                if resp.ok:
                    data = resp.json()
                    if data:
                        token.balance = self._to_int(data[0].get('value', 0))
                        break
            except Exception:
                continue

        for table in ("BlockApps-Token-_allowances", "Token-_allowances"):
            try:
                resp = requests.get(
                    f'{strato_node_url}/cirrus/search/{table}',
                    headers=headers,
                    params={
                        'address': f'eq.{token_addr}',
                        'key': f'eq.{wallet_addr}',
                        'key2': f'eq.{self.address}',
                        'select': 'value::text'
                    },
                    timeout=10000
                )
                if resp.ok:
                    data = resp.json()
                    if data:
                        token.allowance = self._to_int(data[0].get('value', 0))
                        break
            except Exception:
                continue

    def get_reserves(self) -> Tuple[int, int]:
        """
        Get current pool reserves from Cirrus search
        
        Returns:
            Tuple of (reserve_a, reserve_b) as ints in wei
        """
        try:
            pool_data = self.fetch_pool_data()
            
            # Return raw balances (in wei)
            reserve_a = int(pool_data.tokenABalance)
            reserve_b = int(pool_data.tokenBBalance)
            
            return reserve_a, reserve_b
            
        except Exception as e:
            logger.error(f"Failed to get reserves: {e}")
            raise

    def is_stable_pool(self) -> bool:
        """
        Return pool type flag discovered from on-chain pool state.
        """
        if self._pool_data is None:
            self.fetch_pool_data()
        return self.is_stable

    def _compute_amp(self) -> int:
        """Compute the current amplification parameter accounting for ramping."""
        p = self._pool_data
        amp = p.futureA
        now = int(time.time())
        if p.futureATime > 0 and now < p.futureATime and p.initialATime < p.futureATime:
            if p.futureA > p.initialA:
                amp = p.initialA + ((p.futureA - p.initialA) * (now - p.initialATime)) // (p.futureATime - p.initialATime)
            else:
                amp = p.initialA - ((p.initialA - p.futureA) * (now - p.initialATime)) // (p.futureATime - p.initialATime)
        return int(amp) if amp > 0 else 0

    def get_stable_params(self) -> dict:
        """
        Return stable-pool parameters needed for contract-aligned quote math,
        including stored rates computed as rateMultiplier * oraclePrice / PRECISION
        to match the contract's _storedRates().
        """
        if self._pool_data is None:
            self.fetch_pool_data()
        p = self._pool_data
        if not p or not p.isStable:
            return {}

        amp = self._compute_amp()

        params: dict = {}
        if amp > 0:
            params["amp"] = amp
        if p.stableFee > 0:
            params["fee"] = int(p.stableFee)
        if p.offpegFeeMultiplier > 0:
            params["offpeg_fee_multiplier"] = int(p.offpegFeeMultiplier)

        # Build per-coin stored rates
        n = len(p.coin_rate_multipliers) if p.coin_rate_multipliers else 0
        rates: List[int] = []
        for idx in range(n):
            rm = p.coin_rate_multipliers[idx] if p.coin_rate_multipliers[idx] > 0 else WEI_SCALE
            op = p.coin_oracle_prices[idx] if p.coin_oracle_prices[idx] > 0 else WEI_SCALE
            rates.append((rm * op) // WEI_SCALE)
        params["rates"] = rates

        # Legacy 2-coin fields for backward compat
        if len(rates) >= 2:
            params["rate_a"] = rates[0]
            params["rate_b"] = rates[1]

        logger.info(
            "Stable params: amp=%s, fee=%s, offpeg=%s, rates=%s",
            params.get("amp"), params.get("fee"), params.get("offpeg_fee_multiplier"),
            rates,
        )

        return params

    def get_effective_reserves(self) -> Tuple[int, int]:
        """
        Get effective 2-coin pool reserves (tokenBalances - adminBalances) to match
        the contract's _balances() used in swap math.
        """
        pool_data = self.fetch_pool_data()
        reserve_a = max(0, pool_data.tokenABalance - pool_data.adminBalanceA)
        reserve_b = max(0, pool_data.tokenBBalance - pool_data.adminBalanceB)
        return reserve_a, reserve_b

    def get_effective_reserves_n(self) -> List[int]:
        """
        Get effective reserves for all n coins (reserves - adminBalances).
        For coins 0 and 1, uses tokenABalance/tokenBBalance from the main query.
        For coins 2+, uses values fetched from the tokenBalances mapping.
        """
        p = self.fetch_pool_data()
        n = len(p.coin_addresses) if p.coin_addresses else 2

        raw_reserves = list(p.coin_reserves) if p.coin_reserves else []
        # Backfill coins 0/1 from the top-level fields
        if len(raw_reserves) >= 2:
            raw_reserves[0] = p.tokenABalance
            raw_reserves[1] = p.tokenBBalance
        else:
            raw_reserves = [p.tokenABalance, p.tokenBBalance]

        admin = list(p.coin_admin_balances) if p.coin_admin_balances else [0] * n
        while len(admin) < len(raw_reserves):
            admin.append(0)

        return [max(0, raw_reserves[i] - admin[i]) for i in range(len(raw_reserves))]

    def n_coins(self) -> int:
        """Return the number of coins in this pool (2 for AMM or 2-coin stable)."""
        if self._pool_data and self._pool_data.coin_addresses:
            return len(self._pool_data.coin_addresses)
        return 2
    
    def get_price(self) -> int:
        """
        Get current pool price (token_b per token_a) in wei scale
        
        Returns:
            Price as int in wei scale (token_b per token_a * 10^18)
        """
        reserve_a, reserve_b = self.get_reserves()
        if reserve_a == 0:
            return 0
        # Price in wei scale: (reserve_b * 10^18) // reserve_a
        return (reserve_b * WEI_SCALE) // reserve_a

    def get_stable_aligned_price(self) -> int:
        """
        Preferred pool price for stable pools: use pool ratio fields when available.
        Falls back to reserve ratio for AMM pools or missing ratio fields.
        """
        pool_data = self.fetch_pool_data()
        if pool_data.isStable and pool_data.aToBRatio > 0:
            return pool_data.aToBRatio
        return self.get_price()
    
    def swap(
        self,
        amount_in: int,
        token_in: Token,
        min_amount_out: int
    ) -> str:
        """
        Execute swap transaction
        
        Args:
            amount_in: Input amount (in wei)
            token_in: Input token
            min_amount_out: Minimum output amount (in wei)
            
        Returns:
            Transaction hash
        """
        client = strato_client()
        
        # Determine swap direction: true if swapping A to B, false if B to A
        is_a_to_b = token_in.address.lower() == self.token_a.address.lower()
        
        # Default deadline: 1 minute from now
        deadline = int(time.time()) + 60
        
        # Build args matching contract signature
        slippage_factor = self.slippage_factor_stable if self.is_stable else self.slippage_factor_amm
        min_amount_out_guarded = int(min_amount_out * slippage_factor)
        args = {
            'isAToB': is_a_to_b,
            'amountIn': amount_in,
            'minAmountOut': min_amount_out_guarded,
            'deadline': deadline
        }

        logger.info(
            "swap preflight pool=%s is_stable=%s is_a_to_b=%s "
            "slippage_factor=%s amount_in=%s min_out_sim=%s min_out_onchain(_minDy)=%s deadline=%s",
            self.address,
            self.is_stable,
            is_a_to_b,
            slippage_factor,
            amount_in,
            min_amount_out,
            min_amount_out_guarded,
            deadline,
        )

        transaction = {
            'from': client.account.address,
            'to': self.address,
            'contract_address': self.address,
            'method': 'swap',
            'args': args
        }

        return client.send_transaction(transaction)

    def exchange_stable(self, i: int, j: int, amount_in: int, min_amount_out: int) -> str:
        """
        Execute a StablePool exchange by coin indices.
        Calls exchange(i, j, _dx, _minDy, _receiver) on the contract.
        """
        client = strato_client()
        slippage_factor = self.slippage_factor_stable
        min_amount_out_guarded = int(min_amount_out * slippage_factor)
        args = {
            'i': i,
            'j': j,
            '_dx': amount_in,
            '_minDy': min_amount_out_guarded,
            '_receiver': client.account.address,
        }

        logger.info(
            "exchange_stable preflight pool=%s i=%s j=%s "
            "slippage_factor=%s amount_in(_dx)=%s min_out_sim=%s min_out_onchain(_minDy)=%s receiver=%s",
            self.address,
            i,
            j,
            slippage_factor,
            amount_in,
            min_amount_out,
            min_amount_out_guarded,
            client.account.address,
        )

        transaction = {
            'from': client.account.address,
            'to': self.address,
            'contract_address': self.address,
            'method': 'exchange',
            'args': args,
        }
        return client.send_transaction(transaction)
    
    def get_position_data(self, token_address: str) -> int:
        """
        Weighted-average *buy* cost (USDST per token, wei-scaled).
        Buy-only VWAP: does not adjust for sells.
        """
        wallet = strato_client().account.address
        
        try:
            client = strato_client()
            access_token = client.oauth.get_access_token()
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            }
            base_url = f"{client.strato_node_url}/cirrus/search/BlockApps-Pool-Swap"
            
            # one request, two aggregates; cast to text so we can int() safely
            params = {
                "address": f"eq.{self.address}",
                "sender": f"eq.{wallet}",
                "tokenIn": f"eq.{USDST_ADDRESS}",
                "tokenOut": f"eq.{token_address}",
                "select": "spent:amountIn.sum()::text,bought:amountOut.sum()::text",
            }
            
            response = requests.get(base_url, headers=headers, params=params, timeout=10000)
            response.raise_for_status()
            data = response.json() or [{}]
            row = data[0]
            
            spent_raw = (row.get("spent") or "0").strip()
            bought_raw = (row.get("bought") or "0").strip()
            
            # integers only; Cirrus sums over wei amounts -> integral
            usdst_spent = int(spent_raw)
            token_bought = int(bought_raw)
            
            if token_bought > 0:
                return (usdst_spent * WEI_SCALE) // token_bought
            return 0
            
        except Exception as e:
            logger.error(f"Failed to get position data from Cirrus: {e}")
            return 0
    
    def __str__(self) -> str:
        return f"Pool({self.token_a.symbol}/{self.token_b.symbol}, {self.address})"
    
    def __repr__(self) -> str:
        return f"Pool(address='{self.address}', tokens='{self.token_a.symbol}/{self.token_b.symbol}', fee_bps={self.fee_bps})"
