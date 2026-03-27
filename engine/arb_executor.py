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
from core.math_utils import find_optimal_trade_auto, find_optimal_trade_stable_n
from engine.helpers import check_gas_balance, check_sell_pnl, update_cumulative_profit
from core.notifier import notify_error

logger = logging.getLogger(__name__)

@dataclass
class ArbitrageOpportunity:
    """Represents an arbitrage opportunity"""
    direction: str  # "buy" or "sell"
    optimal_input: int  # Optimal trade size in wei (input amount)
    expected_output: int  # Expected output amount in wei
    estimated_profit: int  # Estimated profit after fees in token B wei
    coin_in_idx: int = -1  # Index of input coin (-1 = use direction-based logic)
    coin_out_idx: int = -1  # Index of output coin (-1 = use direction-based logic)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging/serialization"""
        return {
            'direction': self.direction,
            'optimal_input': self.optimal_input,
            'expected_output': self.expected_output,
            'estimated_profit': self.estimated_profit,
            'coin_in_idx': self.coin_in_idx,
            'coin_out_idx': self.coin_out_idx,
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
        min_profit_usd: int
    ):
        """Initialize arbitrage executor
        
        Args:
            min_profit_usd: Minimum profit threshold in USD (wei scale)
        """
        self.token_a = token_a
        self.token_b = token_b
        self.pool = pool
        self.oracle = oracle
        self.fee_bps = fee_bps
        self.min_profit_usd = min_profit_usd
        self._execution_lock = Lock()
        self.is_executing = False
        self.last_execution_time = 0
    
    def scan_for_opportunity(self) -> Optional[ArbitrageOpportunity]:
        """Scan for arbitrage opportunities"""
        try:
            self.pool.fetch_pool_data(force_refresh=True)
            is_stable_pool = self.pool.is_stable_pool()

            logger.info(
                "pricing path selected: %s (isStable=%s, n_coins=%d)",
                'stable' if is_stable_pool else 'amm',
                is_stable_pool,
                self.pool.n_coins(),
            )

            if is_stable_pool:
                return self._scan_stable()
            return self._scan_amm()

        except Exception as e:
            logger.error(f"Error scanning for opportunities: {e}")
            return None

    # ------------------------------------------------------------------
    # AMM (constant-product) scan
    # ------------------------------------------------------------------
    def _scan_amm(self) -> Optional[ArbitrageOpportunity]:
        reserve_a, reserve_b = self.pool.get_reserves()
        if reserve_a <= 0 or reserve_b <= 0:
            logger.warning("No arbitrage opportunity: Invalid reserves (a=%s, b=%s)", reserve_a, reserve_b)
            return None

        try:
            price_a, price_b = self.oracle.fetch_token_prices(
                self.token_a.symbol, self.token_b.symbol, force_refresh=True
            )
        except ValueError as e:
            logger.error("Failed to get oracle prices: %s", e)
            return None

        if price_b <= 0:
            logger.warning("No arbitrage opportunity: Invalid oracle price_b (%s)", price_b)
            return None

        oracle_price = (price_a * WEI_SCALE) // price_b
        if oracle_price <= 0:
            logger.warning("No arbitrage opportunity: Invalid oracle price ratio (%s)", oracle_price)
            return None

        min_profit_token_b = (self.min_profit_usd * WEI_SCALE) // price_b
        if self.token_a.balance <= 0 and self.token_b.balance <= 0:
            logger.warning("No arbitrage opportunity: No token balances")
            return None

        pool_price = (reserve_b * WEI_SCALE) // reserve_a
        price_diff = oracle_price - pool_price
        price_diff_pct = (price_diff * 10000 // pool_price) / 100 if pool_price > 0 else 0

        logger.info("Pool price: %.6f %s per %s", pool_price / WEI_SCALE, self.token_b.symbol, self.token_a.symbol)
        logger.info("Oracle price: %.6f %s per %s", oracle_price / WEI_SCALE, self.token_b.symbol, self.token_a.symbol)
        logger.info("Price diff: %.6f %s (%.2f%%)", price_diff / WEI_SCALE, self.token_b.symbol, price_diff_pct)

        reason, result = find_optimal_trade_auto(
            reserve_x=reserve_a, reserve_y=reserve_b,
            oracle_price_xy=oracle_price,
            balance_x=check_gas_balance(self.token_a, self.token_a.balance),
            balance_y=check_gas_balance(self.token_b, self.token_b.balance),
            fee_bps=self.fee_bps,
            min_profit=min_profit_token_b,
        )

        if result is None:
            logger.info("No arbitrage opportunity found - %s", reason)
            return None

        side, amount_in, expected_out, profit = result
        direction = "sell" if side == "X->Y" else "buy"
        token_in_symbol = self.token_a.symbol if side == "X->Y" else self.token_b.symbol
        token_out_symbol = self.token_b.symbol if side == "X->Y" else self.token_a.symbol

        logger.info("%s opportunity found (%s):", direction.capitalize(), side)
        logger.info("  Input: %.6f %s (%d wei)", amount_in / WEI_SCALE, token_in_symbol, amount_in)
        logger.info("  Output: %.6f %s (%d wei)", expected_out / WEI_SCALE, token_out_symbol, expected_out)
        logger.info("  Profit: %.6f %s (%d wei)", profit / WEI_SCALE, self.token_b.symbol, profit)

        return ArbitrageOpportunity(
            direction=direction,
            optimal_input=amount_in,
            expected_output=expected_out,
            estimated_profit=profit,
        )

    # ------------------------------------------------------------------
    # Stable-pool scan — n-coin generalised path
    # ------------------------------------------------------------------
    def _scan_stable(self) -> Optional[ArbitrageOpportunity]:
        from market.oracle import get_external_symbol

        coins = self.pool.coins
        n = len(coins)

        reserves = self.pool.get_effective_reserves_n()
        if any(r <= 0 for r in reserves):
            logger.warning("No arbitrage opportunity: Invalid reserves %s", reserves)
            return None

        symbols = [get_external_symbol(c.symbol) for c in coins]
        try:
            price_map = self.oracle.fetch_all_prices(symbols, force_refresh=True)
        except Exception as e:
            logger.error("Failed to get oracle prices: %s", e)
            return None

        oracle_prices = []
        for sym in symbols:
            p = price_map.get(sym)
            if not p or p <= 0:
                logger.warning("No oracle price for %s", sym)
                return None
            oracle_prices.append(p)

        balances = [check_gas_balance(c, c.balance) for c in coins]
        if all(b <= 0 for b in balances):
            logger.warning("No arbitrage opportunity: No token balances")
            return None

        stable_params = self.pool.get_stable_params()
        from core.math_utils import STABLE_A_PRECISION, STABLE_FEE_DENOMINATOR
        amp = stable_params.get("amp", 100 * STABLE_A_PRECISION)
        fee = stable_params.get("fee", self.fee_bps * 1_000_000)
        offpeg = stable_params.get("offpeg_fee_multiplier", STABLE_FEE_DENOMINATOR)
        rates = stable_params.get("rates", [WEI_SCALE] * n)
        while len(rates) < n:
            rates.append(WEI_SCALE)

        min_profit_usd = self.min_profit_usd // 10

        if n == 2:
            pool_price = self.pool.get_stable_aligned_price()
            oracle_price_ab = (oracle_prices[0] * WEI_SCALE) // oracle_prices[1]
            price_diff = oracle_price_ab - pool_price
            price_diff_pct = (price_diff * 10000 // pool_price) / 100 if pool_price > 0 else 0
            reserve_ratio_price = (reserves[1] * WEI_SCALE) // reserves[0] if reserves[0] > 0 else 0
            logger.info("Pool price: %.6f %s per %s", pool_price / WEI_SCALE, coins[1].symbol, coins[0].symbol)
            logger.info("Oracle price: %.6f %s per %s", oracle_price_ab / WEI_SCALE, coins[1].symbol, coins[0].symbol)
            logger.info("Price diff: %.6f %s (%.2f%%)", price_diff / WEI_SCALE, coins[1].symbol, price_diff_pct)
            logger.info(
                "Stable price trace: reserve-ratio=%.6f, stable-aligned-ratio=%.6f",
                reserve_ratio_price / WEI_SCALE,
                pool_price / WEI_SCALE,
            )
        else:
            logger.info("Stable pool with %d coins: %s", n, [c.symbol for c in coins])

        reason, result = find_optimal_trade_stable_n(
            reserves=reserves, rates=rates,
            oracle_prices=oracle_prices, balances=balances,
            amp=amp, fee=fee, offpeg_fee_multiplier=offpeg,
            min_profit_usd=min_profit_usd,
        )

        if result is None:
            logger.info("No arbitrage opportunity found - %s", reason)
            return None

        i, j, amount_in, expected_out, profit_j = result
        token_in = coins[i]
        token_out = coins[j]
        direction = f"{token_in.symbol}->{token_out.symbol}"

        logger.info("Opportunity found (%s, coin %d->%d):", direction, i, j)
        logger.info("  Input: %.6f %s (%d wei)", amount_in / WEI_SCALE, token_in.symbol, amount_in)
        logger.info("  Output: %.6f %s (%d wei)", expected_out / WEI_SCALE, token_out.symbol, expected_out)
        profit_usd = (profit_j * oracle_prices[j]) // WEI_SCALE
        logger.info("  Profit: %.6f %s (~$%.4f)", profit_j / WEI_SCALE, token_out.symbol, profit_usd / WEI_SCALE)

        return ArbitrageOpportunity(
            direction=direction,
            optimal_input=amount_in,
            expected_output=expected_out,
            estimated_profit=profit_j,
            coin_in_idx=i,
            coin_out_idx=j,
        )
    
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
            amount_in = opportunity.optimal_input
            expected_out = opportunity.expected_output
            
            # Use index-based exchange for stable pools with explicit coin indices
            use_exchange = (
                self.pool.is_stable
                and opportunity.coin_in_idx >= 0
                and opportunity.coin_out_idx >= 0
            )

            logger.info(
                "execute_opportunity path use_exchange=%s pool.is_stable=%s scan_is_stable=%s "
                "coin_in_idx=%s coin_out_idx=%s direction=%s amount_in=%s expected_out(min basis)=%s",
                use_exchange,
                self.pool.is_stable,
                self.pool.is_stable_pool(),
                opportunity.coin_in_idx,
                opportunity.coin_out_idx,
                opportunity.direction,
                amount_in,
                expected_out,
            )

            if use_exchange:
                swap_hash = self.pool.exchange_stable(
                    i=opportunity.coin_in_idx,
                    j=opportunity.coin_out_idx,
                    amount_in=amount_in,
                    min_amount_out=expected_out,
                )
            else:
                token_in = self.token_b if opportunity.direction == "buy" else self.token_a
                swap_hash = self.pool.swap(
                    amount_in=amount_in,
                    token_in=token_in,
                    min_amount_out=expected_out,
                )
            
            transactions.append({'type': 'swap', 'hash': swap_hash, 'timestamp': time.time()})
            client.wait_for_transaction(swap_hash)
            
            # Fetch fresh output-token price from oracle for USD conversion
            if opportunity.coin_out_idx >= 0 and opportunity.coin_out_idx < len(self.pool.coins):
                from market.oracle import get_external_symbol
                out_sym = get_external_symbol(self.pool.coins[opportunity.coin_out_idx].symbol)
                price_map = self.oracle.fetch_all_prices([out_sym], force_refresh=True)
                price_out = price_map.get(out_sym)
                if not price_out or price_out <= 0:
                    logger.warning("Could not fetch output token price for profit tracking (symbol=%s)", out_sym)
                    price_out = 0
            else:
                _, price_out = self.oracle.fetch_token_prices(
                    self.token_a.symbol,
                    self.token_b.symbol,
                    force_refresh=True
                )
            
            if price_out > 0:
                update_cumulative_profit(opportunity.estimated_profit, price_out)
            
            return ExecutionResult(
                success=True,
                opportunity=opportunity,
                transactions=transactions,
                execution_time=time.time() - start_time
            )
            
        except Exception as e:
            error_msg = str(e)
            logger.error("Arbitrage execution failed: %s", error_msg)
            logger.error(
                "execution context at failure: amount_in=%s expected_output(used as min basis)=%s "
                "use_exchange=%s pool.is_stable=%s coin_in=%s coin_out=%s direction=%s",
                opportunity.optimal_input,
                opportunity.expected_output,
                self.pool.is_stable
                and opportunity.coin_in_idx >= 0
                and opportunity.coin_out_idx >= 0,
                self.pool.is_stable,
                opportunity.coin_in_idx,
                opportunity.coin_out_idx,
                opportunity.direction,
            )
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
