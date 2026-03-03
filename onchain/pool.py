"""
AMM Pool contract wrapper for Strato
"""

import logging
import requests
import time
from dataclasses import dataclass
from typing import Optional, Tuple

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
    isStable: bool  # True for stable pools, False for AMM pools
    stableFee: int = 0
    offpegFeeMultiplier: int = 0
    initialA: int = 0
    futureA: int = 0
    initialATime: int = 0
    futureATime: int = 0




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
        
        # Cache pool data
        self._pool_data: Optional[PoolData] = None
        self.is_stable: bool = False

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
                f'address,tokenABalance,tokenBBalance,isStable,'
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
            
            # Create PoolData with references to token objects
            is_stable_raw = pool_dict.get('isStable', False)
            is_stable = str(is_stable_raw).lower() in ('true', '1', 't', 'yes')
            self.is_stable = is_stable
            stable_meta = self._fetch_stable_meta(client.strato_node_url, access_token) if is_stable else {}
            self._pool_data = PoolData(
                address=pool_dict.get('address', self.address),
                tokenA=self.token_a,
                tokenB=self.token_b,
                tokenABalance=int(pool_dict.get('tokenABalance', 0)),
                tokenBBalance=int(float(pool_dict.get('tokenBBalance', 0))),  # Handle float conversion
                isStable=self.is_stable,
                stableFee=self._to_int(stable_meta.get('fee', 0)),
                offpegFeeMultiplier=self._to_int(stable_meta.get('offpegFeeMultiplier', 0)),
                initialA=self._to_int(stable_meta.get('initialA', 0)),
                futureA=self._to_int(stable_meta.get('futureA', 0)),
                initialATime=self._to_int(stable_meta.get('initialATime', 0)),
                futureATime=self._to_int(stable_meta.get('futureATime', 0)),
            )
            
            return self._pool_data
            
        except Exception as e:
            logger.error(f"Failed to fetch pool data: {e}")
            raise

    def _fetch_stable_meta(self, strato_node_url: str, access_token: str) -> dict:
        """
        Best-effort stable pool metadata lookup.
        This must never break the base pool fetch flow.
        """
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }
        params = {
            'address': f'eq.{self.address}',
            'select': 'fee,offpegFeeMultiplier,initialA,futureA,initialATime,futureATime'
        }

        # Cirrus table names can differ by deployment; try likely candidates.
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
                    return data[0]
            except Exception:
                continue

        logger.warning(
            "Stable pool metadata not available in Cirrus for %s; using conservative defaults",
            self.address
        )
        return {}
    
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

    def get_stable_params(self) -> dict:
        """
        Return stable-pool parameters needed for contract-aligned quote math.
        """
        if self._pool_data is None:
            self.fetch_pool_data()
        p = self._pool_data
        if not p or not p.isStable:
            return {}

        now = int(time.time())
        amp = p.futureA
        if p.futureATime > 0 and now < p.futureATime and p.initialATime < p.futureATime:
            if p.futureA > p.initialA:
                amp = p.initialA + ((p.futureA - p.initialA) * (now - p.initialATime)) // (p.futureATime - p.initialATime)
            else:
                amp = p.initialA - ((p.initialA - p.futureA) * (now - p.initialATime)) // (p.futureATime - p.initialATime)
        # Only provide params that were actually discovered.
        params = {}
        if amp > 0:
            params["amp"] = int(amp)
        if p.stableFee > 0:
            params["fee"] = int(p.stableFee)
        if p.offpegFeeMultiplier > 0:
            params["offpeg_fee_multiplier"] = int(p.offpegFeeMultiplier)
        return params
    
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
        
        transaction = {
            'from': client.account.address,
            'to': self.address,
            'contract_address': self.address,
            'method': 'swap',
            'args': args
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
