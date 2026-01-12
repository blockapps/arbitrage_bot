"""
Mathematical utilities for arbitrage calculations
All calculations use integer arithmetic in wei scale for precision
"""

from typing import Optional, Tuple

import math

from core.constants import WEI_SCALE, BPS_DENOM

def get_optimal_input(
    reserve_in: int,
    reserve_out: int,
    market_price_scaled: int,
    fee_basis_points: int
) -> int:
    """
    dx = (sqrt(k / P) - x) / (1 - f), floored
    k = x*y, P = market_price_scaled, f = fee_basis_points/10000
    
    Returns optimal amount of the token you are SENDING (reserve_in).
    Caller orients reserves and price. No direction logic inside.
    
    Assumes caller has validated inputs (reserves > 0, price > 0, fee valid).
    """
    x, y = reserve_in, reserve_out
    k = x * y
    x_target = math.isqrt((k * WEI_SCALE) // market_price_scaled)
    if x_target <= x:
        return 0
    dx_eff = x_target - x
    return (dx_eff * BPS_DENOM) // (BPS_DENOM - fee_basis_points)

def get_output_amount(
    dx: int,
    reserve_x: int,
    reserve_y: int,
    fee_bps: int
) -> int:
    """
    Constant-product with fee on input:
      dx_eff = dx * (1 - f)
      dy = (y * dx_eff) // (x + dx_eff)
    """
    if (
        dx <= 0 or reserve_x <= 0 or reserve_y <= 0
        or fee_bps < 0 or fee_bps >= BPS_DENOM
    ):
        return 0
    
    fee_mult = BPS_DENOM - fee_bps
    dx_eff = (dx * fee_mult) // BPS_DENOM
    
    if dx_eff <= 0:
        return 0
    
    denom = reserve_x + dx_eff
    # denom > 0 by guards above
    return (reserve_y * dx_eff) // denom

def calculate_buy_profit(
    input_amount: int,
    reserve_in: int,
    reserve_out: int,
    oracle_price: int,  # token B per token A, scaled by WEI_SCALE
    fee_bps: int
) -> int:
    """
    Buying token A with token B:
      profit_B = (A_out * oracle_price)/WEI_SCALE - B_in
    
    Assumes caller has validated inputs (input_amount > 0, oracle_price > 0).
    """
    a_out = get_output_amount(input_amount, reserve_in, reserve_out, fee_bps)
    return (a_out * oracle_price) // WEI_SCALE - input_amount

def calculate_sell_profit(
    input_amount: int,
    reserve_in: int,
    reserve_out: int,
    oracle_price: int,  # token B per token A, scaled by WEI_SCALE
    fee_bps: int
) -> int:
    """
    Selling token A for token B:
      profit_B = B_out - (A_in * oracle_price)/WEI_SCALE
    
    Assumes caller has validated inputs (input_amount > 0, oracle_price > 0).
    """
    b_out = get_output_amount(input_amount, reserve_in, reserve_out, fee_bps)
    return b_out - (input_amount * oracle_price) // WEI_SCALE

def find_optimal_trade_auto(
    reserve_x: int,
    reserve_y: int,
    oracle_price_xy: int,   # Y per X, 1e18
    balance_x: int,
    balance_y: int,
    fee_bps: int,
    min_profit: int         # token Y wei (caller converts from USD)
) -> Tuple[Optional[str], Optional[Tuple[str, int, int, int]]]:
    """
    Returns: (reason, result) where:
        - reason: None if successful, error reason string if failed
        - result: None if failed, (side, amount_in, expected_out, profit) if successful
    """
    if reserve_x <= 0 or reserve_y <= 0 or oracle_price_xy <= 0:
        return ("Invalid inputs (reserve_x={}, reserve_y={}, oracle_price_xy={})".format(reserve_x, reserve_y, oracle_price_xy), None)
    if balance_x <= 0 and balance_y <= 0:
        return ("No balances (balance_x={}, balance_y={})".format(balance_x, balance_y), None)
    if not (0 <= fee_bps < BPS_DENOM):
        return ("Invalid fee_bps ({})".format(fee_bps), None)

    P_pool_xy = (reserve_y * WEI_SCALE) // reserve_x

    if P_pool_xy < oracle_price_xy:
        # Pool underprices X → buy X with Y (Y->X)
        P_yx = (WEI_SCALE * WEI_SCALE) // oracle_price_xy
        dy_opt = get_optimal_input(reserve_y, reserve_x, P_yx, fee_bps)
        dy = min(dy_opt, balance_y) if dy_opt > 0 and balance_y > 0 else 0
        if dy <= 0:
            return ("No input available for Y->X (dy_opt={}, balance_y={})".format(dy_opt, balance_y), None)
        x_out = get_output_amount(dy, reserve_y, reserve_x, fee_bps)
        if x_out <= 0:
            return ("No output for Y->X (x_out={})".format(x_out), None)
        profit_y = (x_out * oracle_price_xy) // WEI_SCALE - dy

        if profit_y > 0 and profit_y >= min_profit:
            return (None, ("Y->X", dy, x_out, profit_y))
        return ("Profit too low for Y->X (profit={:.6f}, min_profit={:.6f})".format(profit_y / WEI_SCALE, min_profit / WEI_SCALE), None)

    if P_pool_xy > oracle_price_xy:
        # Pool overprices X → sell X for Y (X->Y)
        dx_opt = get_optimal_input(reserve_x, reserve_y, oracle_price_xy, fee_bps)
        dx = min(dx_opt, balance_x) if dx_opt > 0 and balance_x > 0 else 0
        if dx <= 0:
            return ("No input available for X->Y (dx_opt={}, balance_x={})".format(dx_opt, balance_x), None)
        y_out = get_output_amount(dx, reserve_x, reserve_y, fee_bps)
        if y_out <= 0:
            return ("No output for X->Y (y_out={})".format(y_out), None)
        profit_y = y_out - (dx * oracle_price_xy) // WEI_SCALE

        if profit_y > 0 and profit_y >= min_profit:
            return (None, ("X->Y", dx, y_out, profit_y))
        return ("Profit too low for X->Y (profit={:.6f}, min_profit={:.6f})".format(profit_y / WEI_SCALE, min_profit / WEI_SCALE), None)

    return ("Pool price equals oracle price (no arbitrage opportunity)", None)

