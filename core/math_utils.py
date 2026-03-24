"""
Mathematical utilities for arbitrage calculations
All calculations use integer arithmetic in wei scale for precision
"""

from typing import Optional, Tuple

import math

from core.constants import WEI_SCALE, BPS_DENOM

STABLE_FEE_DENOMINATOR = 10_000_000_000
STABLE_A_PRECISION = 100

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


def _stable_dynamic_fee(xpi: int, xpj: int, fee: int, offpeg_fee_multiplier: int) -> int:
    if offpeg_fee_multiplier <= STABLE_FEE_DENOMINATOR:
        return fee
    xps2 = (xpi + xpj) * (xpi + xpj)
    if xps2 <= 0:
        return fee
    num = offpeg_fee_multiplier * fee
    den = (((offpeg_fee_multiplier - STABLE_FEE_DENOMINATOR) * 4 * xpi * xpj) // xps2) + STABLE_FEE_DENOMINATOR
    return num // den if den > 0 else fee


def _stable_get_d(xp0: int, xp1: int, amp: int) -> int:
    if xp0 <= 0 or xp1 <= 0:
        return 0
    s = xp0 + xp1
    d = s
    ann = amp * 2
    for _ in range(256):
        d_p = d
        d_p = (d_p * d) // xp0
        d_p = (d_p * d) // xp1
        d_p //= 4  # n^n for n=2
        d_prev = d
        num = (((ann * s) // STABLE_A_PRECISION) + (d_p * 2)) * d
        den = ((((ann - STABLE_A_PRECISION) * d) // STABLE_A_PRECISION) + (3 * d_p))
        if den <= 0:
            return 0
        d = num // den
        if abs(d - d_prev) <= 1:
            return d
    return 0


def _stable_get_y(i: int, j: int, x: int, xp0: int, xp1: int, amp: int, d: int) -> int:
    if i == j or i < 0 or j < 0 or i > 1 or j > 1:
        return 0
    ann = amp * 2
    c = d
    s_ = 0
    for idx in (0, 1):
        if idx == i:
            _x = x
        elif idx != j:
            _x = xp0 if idx == 0 else xp1
        else:
            continue
        if _x <= 0:
            return 0
        s_ += _x
        c = (c * d) // (_x * 2)

    c = (c * d * STABLE_A_PRECISION) // (ann * 2)
    b = s_ + ((d * STABLE_A_PRECISION) // ann)
    y = d
    for _ in range(256):
        y_prev = y
        den = (2 * y + b - d)
        if den <= 0:
            return 0
        y = ((y * y) + c) // den
        if abs(y - y_prev) <= 1:
            return y
    return 0


def _stable_quote_output(
    dx: int,
    reserve_x: int,
    reserve_y: int,
    is_x_to_y: bool,
    amp: int,
    fee: int,
    offpeg_fee_multiplier: int,
    rate_x: int = WEI_SCALE,
    rate_y: int = WEI_SCALE,
) -> int:
    if dx <= 0 or reserve_x <= 0 or reserve_y <= 0 or amp <= 0:
        return 0

    xp0 = (rate_x * reserve_x) // WEI_SCALE
    xp1 = (rate_y * reserve_y) // WEI_SCALE

    i, j = (0, 1) if is_x_to_y else (1, 0)
    rate_i = rate_x if i == 0 else rate_y
    rate_j = rate_y if j == 1 else rate_x
    xp_i = xp0 if i == 0 else xp1
    xp_j = xp1 if j == 1 else xp0

    x = xp_i + (dx * rate_i) // WEI_SCALE
    d = _stable_get_d(xp0, xp1, amp)
    if d <= 0:
        return 0
    y = _stable_get_y(i, j, x, xp0, xp1, amp, d)
    if y <= 0:
        return 0
    dy = xp_j - y - 1
    if dy <= 0:
        return 0
    dy_fee = (dy * _stable_dynamic_fee((xp_i + x) // 2, (xp_j + y) // 2, fee, offpeg_fee_multiplier)) // STABLE_FEE_DENOMINATOR
    dy_net = ((dy - dy_fee) * WEI_SCALE) // rate_j
    return dy_net if dy_net > 0 else 0


def find_optimal_trade_stable_auto(
    reserve_x: int,
    reserve_y: int,
    oracle_price_xy: int,   # Y per X, 1e18
    balance_x: int,
    balance_y: int,
    fee_bps: int,
    min_profit: int,        # token Y wei (caller converts from USD)
    stable_params: Optional[dict] = None,
    pool_price_xy_override: Optional[int] = None
) -> Tuple[Optional[str], Optional[Tuple[str, int, int, int]]]:
    """
    Stable-pool arbitrage sizing via ternary search for profit-maximizing trade.
    """
    if reserve_x <= 0 or reserve_y <= 0 or oracle_price_xy <= 0:
        return ("Invalid inputs (reserve_x={}, reserve_y={}, oracle_price_xy={})".format(reserve_x, reserve_y, oracle_price_xy), None)
    if balance_x <= 0 and balance_y <= 0:
        return ("No balances (balance_x={}, balance_y={})".format(balance_x, balance_y), None)
    if not (0 <= fee_bps < BPS_DENOM):
        return ("Invalid fee_bps ({})".format(fee_bps), None)

    pool_price_xy = int(pool_price_xy_override) if pool_price_xy_override and pool_price_xy_override > 0 else (reserve_y * WEI_SCALE) // reserve_x
    if pool_price_xy <= 0:
        return ("Invalid pool price ({})".format(pool_price_xy), None)

    params = stable_params or {}
    amp = int(params.get("amp", 100 * STABLE_A_PRECISION))
    fee_1e10 = int(params.get("fee", fee_bps * 1_000_000))
    offpeg_fee_multiplier = int(params.get("offpeg_fee_multiplier", STABLE_FEE_DENOMINATOR))
    rate_x = int(params.get("rate_a", WEI_SCALE))
    rate_y = int(params.get("rate_b", WEI_SCALE))

    quote_kwargs = dict(
        reserve_x=reserve_x, reserve_y=reserve_y,
        amp=amp, fee=fee_1e10, offpeg_fee_multiplier=offpeg_fee_multiplier,
        rate_x=rate_x, rate_y=rate_y,
    )

    if pool_price_xy < oracle_price_xy:
        # Pool underprices X -> buy X with Y (Y->X)
        max_input = min(reserve_y // 2, balance_y) if balance_y > 0 else 0
        if max_input <= 0:
            return ("No Y balance for Y->X (balance_y={})".format(balance_y), None)

        def buy_profit(dy):
            x_out = _stable_quote_output(dx=dy, is_x_to_y=False, **quote_kwargs)
            if x_out <= 0:
                return -(dy or 1)
            return (x_out * oracle_price_xy) // WEI_SCALE - dy

        lo, hi = 1, max_input
        for _ in range(128):
            if hi - lo < 3:
                break
            m1 = lo + (hi - lo) // 3
            m2 = hi - (hi - lo) // 3
            if buy_profit(m1) < buy_profit(m2):
                lo = m1
            else:
                hi = m2

        best_dy = (lo + hi) // 2
        x_out = _stable_quote_output(dx=best_dy, is_x_to_y=False, **quote_kwargs)
        if x_out <= 0:
            return ("No output for Y->X", None)
        profit_y = (x_out * oracle_price_xy) // WEI_SCALE - best_dy
        if profit_y > 0 and profit_y >= min_profit:
            return (None, ("Y->X", best_dy, x_out, profit_y))
        return ("Profit too low for Y->X (profit={:.6f}, min_profit={:.6f})".format(profit_y / WEI_SCALE, min_profit / WEI_SCALE), None)

    if pool_price_xy > oracle_price_xy:
        # Pool overprices X -> sell X for Y (X->Y)
        max_input = min(reserve_x // 2, balance_x) if balance_x > 0 else 0
        if max_input <= 0:
            return ("No X balance for X->Y (balance_x={})".format(balance_x), None)

        def sell_profit(dx):
            y_out = _stable_quote_output(dx=dx, is_x_to_y=True, **quote_kwargs)
            if y_out <= 0:
                return -(dx or 1)
            return y_out - (dx * oracle_price_xy) // WEI_SCALE

        lo, hi = 1, max_input
        for _ in range(128):
            if hi - lo < 3:
                break
            m1 = lo + (hi - lo) // 3
            m2 = hi - (hi - lo) // 3
            if sell_profit(m1) < sell_profit(m2):
                lo = m1
            else:
                hi = m2

        best_dx = (lo + hi) // 2
        y_out = _stable_quote_output(dx=best_dx, is_x_to_y=True, **quote_kwargs)
        if y_out <= 0:
            return ("No output for X->Y", None)
        profit_y = y_out - (best_dx * oracle_price_xy) // WEI_SCALE
        if profit_y > 0 and profit_y >= min_profit:
            return (None, ("X->Y", best_dx, y_out, profit_y))
        return ("Profit too low for X->Y (profit={:.6f}, min_profit={:.6f})".format(profit_y / WEI_SCALE, min_profit / WEI_SCALE), None)

    return ("Pool price equals oracle price (no arbitrage opportunity)", None)

