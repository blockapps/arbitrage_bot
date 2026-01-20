"""
Helper functions for arbitrage execution
"""
import json
import logging
import os
import fcntl
from onchain.token import Token
from onchain.pool import Pool
from core.strato_client import strato_client
from core.constants import MAX_UINT256, USDST_ADDRESS, WEI_SCALE

logger = logging.getLogger(__name__)


def ensure_pool_approvals(token_a: Token, token_b: Token, pool: Pool):
    """Ensure infinite pool allowance for both tokens."""
    client = strato_client()
    pool_addr = pool.address
    
    for token in (token_a, token_b):
        if not token or not token.address:
            continue
        if token.allowance >= MAX_UINT256:
            continue
        try:
            logger.info(f"Approving {token.symbol} for pool...")
            tx = token.approve(pool_addr, MAX_UINT256)
            client.wait_for_transaction(tx)
            logger.info(f"{token.symbol} approved")
        except Exception as e:
            logger.error(f"Approval failed for {token.symbol}: {e}")
            raise


def check_gas_balance(token_in: Token, amount_in: int) -> int:
    """
    Check if user has gas available (voucher or 0.01 USDST).
    Returns adjusted_amount:
    - If no gas available, returns 0
    - If token is USDST, returns balance - 0.01 USDST (reserves 0.01 USDST for gas)
    - Otherwise returns amount_in
    """
    client = strato_client()
    usdst_balance, voucher_balance = client.get_balance(client.account.address)
    
    # Minimum USDST needed for gas (0.01 USDST)
    min_usdst_for_gas = WEI_SCALE // 100  # 0.01 USDST
    
    # Check for voucher first
    has_gas = voucher_balance >= WEI_SCALE
    
    # If no voucher, check for 0.01 USDST
    if not has_gas:
        has_gas = usdst_balance >= min_usdst_for_gas
        if not has_gas:
            logger.warning(f"Insufficient gas")
            return 0
    
    # If token is USDST, return adjusted balance (reserve 0.01 USDST for gas)
    if token_in.address.lower() == USDST_ADDRESS.lower():
        adjusted_amount = max(0, token_in.balance - min_usdst_for_gas)
        return adjusted_amount
    
    # Otherwise return original amount
    return amount_in


def check_sell_pnl(pool: Pool, token_address: str, opportunity) -> bool:
    """Block sells that would realize a loss vs average cost."""
    if opportunity.direction != "sell":
        return True
    
    qty = opportunity.optimal_input
    if qty <= 0:
        return False
    
    avg_cost_wei = pool.get_position_data(token_address)
    if avg_cost_wei <= 0:
        return True
    
    sell_price_wei = (opportunity.expected_output * WEI_SCALE) // qty
    return sell_price_wei > avg_cost_wei


def update_cumulative_profit(profit_token_b_wei: int, price_b: int, file_path: str = "profit.json"):
    """
    Update cumulative profit by adding new profit to existing total.
    Thread-safe file operations using file locking.
    
    Args:
        profit_token_b_wei: Profit in token B wei
        price_b: USD price of token B in wei scale (e.g., 30 * 10^18 for $30)
        file_path: Path to profit tracking file (default: profit.json)
    """
    try:
        # Convert profit from token B to USD
        # profit_usd_wei = profit_token_b_wei * price_b / WEI_SCALE
        profit_usd_wei = (profit_token_b_wei * price_b) // WEI_SCALE
        
        # Read existing cumulative profit or start at 0, then write back atomically
        cumulative_profit_wei = 0
        if os.path.exists(file_path):
            with open(file_path, 'r') as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)  # Exclusive lock
                try:
                    data = json.load(f)
                    cumulative_profit_wei = data.get("cumulative_profit_wei", 0)
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)  # Unlock
        
        # Add new profit (now in USD wei)
        cumulative_profit_wei += profit_usd_wei
        cumulative_profit_usd = cumulative_profit_wei / WEI_SCALE
        
        # Write back to file with lock (atomic operation)
        with open(file_path, 'w') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)  # Exclusive lock
            try:
                json.dump({
                    "cumulative_profit_wei": cumulative_profit_wei,
                    "cumulative_profit_usd": cumulative_profit_usd
                }, f, indent=2)
                f.flush()  # Ensure data is written
                os.fsync(f.fileno())  # Force write to disk
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)  # Unlock
        
        logger.info(f"Cumulative profit updated: ${cumulative_profit_usd:.6f} ({cumulative_profit_wei} wei)")
        
    except Exception as e:
        logger.error(f"Failed to update cumulative profit: {e}")

