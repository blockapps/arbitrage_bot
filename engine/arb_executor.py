"""
Arbitrage execution engine
"""

import logging
import time
from decimal import Decimal
from threading import Lock
from typing import Optional, Dict, Any, List
from dataclasses import dataclass

from core.strato_client import strato_client
from core.constants import WEI_SCALE
from onchain.token import Token
from onchain.pool import Pool
from market.oracle import PriceOracle
from core.math_utils import find_optimal_trade_auto
from engine.helpers import check_gas_balance, check_sell_pnl, update_cumulative_profit

logger = logging.getLogger(__name__)

@dataclass
class ArbitrageOpportunity:
    """Represents an arbitrage opportunity"""
    direction: str  # "buy" or "sell"
    optimal_input: int  # Optimal trade size in wei (input amount)
    expected_output: int  # Expected output amount in wei
    estimated_profit: int  # Estimated profit after fees in wei
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging/serialization"""
        return {
            'direction': self.direction,
            'optimal_input': self.optimal_input,
            'expected_output': self.expected_output,
            'estimated_profit': self.estimated_profit
        }

@dataclass
class ExecutionResult:
    """Result of an arbitrage execution"""
    success: bool
    opportunity: ArbitrageOpportunity
    transactions: List[Dict[str, Any]]
    actual_profit: Optional[Decimal] = None
    execution_time: Optional[float] = None
    error_message: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging"""
        result = {
            'success': self.success,
            'opportunity': self.opportunity.to_dict(),
            'transactions': self.transactions,
            'execution_time': self.execution_time,
        }
        if self.actual_profit is not None:
            result['actual_profit'] = float(self.actual_profit)
        if self.error_message:
            result['error_message'] = self.error_message
        return result

class ArbitrageExecutor:
    """Arbitrage execution engine with position tracking"""
    
    def __init__(
        self,
        token_a: Token,
        token_b: Token,
        pool: Pool,
        oracle: PriceOracle,
        fee_bps: int,
        min_profit: int
    ):
        """Initialize arbitrage executor"""
        self.token_a = token_a
        self.token_b = token_b
        self.pool = pool
        self.oracle = oracle
        self.fee_bps = fee_bps
        self.min_profit = min_profit
        self._execution_lock = Lock()
        self.is_executing = False
        self.last_execution_time = 0
    
    def scan_for_opportunity(self) -> Optional[ArbitrageOpportunity]:
        """Scan for arbitrage opportunities"""
        try:
            # Refresh pool data and balances
            self.pool.fetch_pool_data(force_refresh=True)
            reserve_a, reserve_b = self.pool.get_reserves()
            
            # Early validation: reject invalid inputs
            if reserve_a <= 0 or reserve_b <= 0:
                logger.warning(f"No arbitrage opportunity: Invalid reserves (a={reserve_a}, b={reserve_b})")
                return None
            
            # Fetch prices for BOTH tokens in the pool
            try:
                price_a, price_b = self.oracle.fetch_token_prices(
                    self.token_a.name,
                    self.token_b.name,
                    force_refresh=True
                )
            except ValueError as e:
                logger.error(f"Failed to get oracle prices: {e}")
                return None
            
            # Calculate oracle price ratio: how many token_b equals one token_a in value
            # oracle_price = price_a / price_b (scaled by WEI_SCALE)
            if price_b <= 0:
                logger.warning(f"No arbitrage opportunity: Invalid oracle price_b ({price_b})")
                return None
            
            oracle_price = (price_a * WEI_SCALE) // price_b
            
            if oracle_price <= 0:
                logger.warning(f"No arbitrage opportunity: Invalid oracle price ratio ({oracle_price})")
                return None
            
            # Validate balances
            if self.token_a.balance <= 0 and self.token_b.balance <= 0:
                logger.warning(f"No arbitrage opportunity: No token balances")
                return None
            
            pool_price = (reserve_b * WEI_SCALE) // reserve_a
            price_diff = oracle_price - pool_price
            price_diff_pct = (price_diff * 10000 // pool_price) / 100 if pool_price > 0 else 0
            
            logger.info(f"Pool price: {pool_price / WEI_SCALE:.6f} {self.token_b.symbol} per {self.token_a.symbol}")
            logger.info(f"Oracle price: {oracle_price / WEI_SCALE:.6f} {self.token_b.symbol} per {self.token_a.symbol}")
            logger.info(f"Price diff: {price_diff / WEI_SCALE:.6f} {self.token_b.symbol} ({price_diff_pct:.2f}%)")
            
            # Use auto-direction selection to find optimal trade (with gas-adjusted balances)
            reason, result = find_optimal_trade_auto(
                reserve_x=reserve_a,
                reserve_y=reserve_b,
                oracle_price_xy=oracle_price,
                balance_x=check_gas_balance(self.token_a, self.token_a.balance),
                balance_y=check_gas_balance(self.token_b, self.token_b.balance),
                fee_bps=self.fee_bps,
                min_profit=self.min_profit
            )
            
            if result is None:
                logger.info("No arbitrage opportunity found - {}".format(reason))
                return None
            
            side, amount_in, expected_out, profit = result
            
            # Map side to direction
            # "X->Y" = token_a -> token_b = "sell" token_a
            # "Y->X" = token_b -> token_a = "buy" token_a
            direction = "sell" if side == "X->Y" else "buy"
            token_in_symbol = self.token_a.symbol if side == "X->Y" else self.token_b.symbol
            token_out_symbol = self.token_b.symbol if side == "X->Y" else self.token_a.symbol
            
            logger.info(f"{direction.capitalize()} opportunity found ({side}):")
            logger.info(f"  Input: {amount_in / WEI_SCALE:.6f} {token_in_symbol} ({amount_in} wei)")
            logger.info(f"  Output: {expected_out / WEI_SCALE:.6f} {token_out_symbol} ({expected_out} wei)")
            logger.info(f"  Profit: {profit / WEI_SCALE:.6f} {self.token_b.symbol} ({profit} wei)")
            
            opportunity = ArbitrageOpportunity(
                direction=direction,
                optimal_input=amount_in,
                expected_output=expected_out,
                estimated_profit=profit
            )
            
            
            return opportunity
            
        except Exception as e:
            logger.error(f"Error scanning for opportunities: {e}")
            return None
    
    def execute_opportunity(self, opportunity: ArbitrageOpportunity) -> ExecutionResult:
        """Execute an arbitrage opportunity"""
        # Thread-safe execution guard
        with self._execution_lock:
            if self.is_executing:
                return ExecutionResult(
                    success=False,
                    opportunity=opportunity,
                    transactions=[],
                    error_message="Executor is already running"
                )
            
            self.is_executing = True
        
        start_time = time.time()
        transactions = []
        
        try:
            client = strato_client()
            token_in = self.token_b if opportunity.direction == "buy" else self.token_a
            amount_in = opportunity.optimal_input
            expected_out = opportunity.expected_output
            
            # Execute swap
            swap_hash = self.pool.swap(
                amount_in=amount_in,
                token_in=token_in,
                min_amount_out=expected_out
            )
            
            transactions.append({'type': 'swap', 'hash': swap_hash, 'timestamp': time.time()})
            client.wait_for_transaction(swap_hash)
            
            # Update cumulative profit after successful swap
            update_cumulative_profit(opportunity.estimated_profit)
            
            return ExecutionResult(
                success=True,
                opportunity=opportunity,
                transactions=transactions,
                execution_time=time.time() - start_time
            )
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Arbitrage execution failed: {error_msg}")
            return ExecutionResult(
                success=False,
                opportunity=opportunity,
                transactions=transactions,
                execution_time=time.time() - start_time,
                error_message=error_msg
            )
        finally:
            with self._execution_lock:
                self.is_executing = False
            self.last_execution_time = time.time()
