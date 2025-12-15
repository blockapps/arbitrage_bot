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




class Pool:
    """AMM Pool contract wrapper for Strato blockchain"""
    
    def __init__(
        self,
        address: str,
        fee_bps: int = 30,
        external_token_name: Optional[str] = None
    ):
        """
        Initialize pool contract
        
        Args:
            address: Pool contract address
            fee_bps: Pool fee in basis points
            external_token_name: External token name for oracle price lookup (e.g., "ETH", "WBTC")
        """
        self.address = address
        self.fee_bps = fee_bps
        self.external_token_name = external_token_name
        
        # Tokens will be initialized when fetch_pool_data() is called
        self.token_a: Optional[Token] = None
        self.token_b: Optional[Token] = None
        
        # Cache pool data
        self._pool_data: Optional[PoolData] = None
    
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
                f'address,tokenABalance,tokenBBalance,'
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
            if token_a_allowances:
                self.token_a.allowance = int(token_a_allowances[0].get('value', '0'))
            
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
            if token_b_allowances:
                self.token_b.allowance = int(token_b_allowances[0].get('value', '0'))
            
            # Create PoolData with references to token objects
            self._pool_data = PoolData(
                address=pool_dict.get('address', self.address),
                tokenA=self.token_a,
                tokenB=self.token_b,
                tokenABalance=int(pool_dict.get('tokenABalance', 0)),
                tokenBBalance=int(float(pool_dict.get('tokenBBalance', 0)))  # Handle float conversion
            )
            
            return self._pool_data
            
        except Exception as e:
            logger.error(f"Failed to fetch pool data: {e}")
            raise
    
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
        args = {
            'isAToB': is_a_to_b,
            'amountIn': amount_in,
            'minAmountOut': int(min_amount_out*0.96),
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
