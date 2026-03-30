"""
Mathematical utilities for arbitrage calculations
All calculations use integer arithmetic in wei scale for precision
"""

from typing import List, Optional, Tuple

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


def _stable_get_d(xp: List[int], amp: int) -> int:
    """Compute StableSwap invariant D for n coins. Matches contract getD()."""
    n = len(xp)
    if n < 2 or any(x <= 0 for x in xp):
        return 0
    s = sum(xp)
    d = s
    ann = amp * n
    nn = n ** n
    for _ in range(256):
        d_p = d
        for x in xp:
            d_p = (d_p * d) // x
        d_p //= nn
        d_prev = d
        num = (((ann * s) // STABLE_A_PRECISION) + (d_p * n)) * d
        den = ((((ann - STABLE_A_PRECISION) * d) // STABLE_A_PRECISION) + ((n + 1) * d_p))
        if den <= 0:
            return 0
        d = num // den
        if abs(d - d_prev) <= 1:
            return d
    return 0


def _stable_get_y(i: int, j: int, x: int, xp: List[int], amp: int, d: int) -> int:
    """Compute output balance y for token j given new input balance x for token i.
    Matches contract getY()."""
    n = len(xp)
    if i == j or i < 0 or j < 0 or i >= n or j >= n:
        return 0
    ann = amp * n
    c = d
    s_ = 0
    for idx in range(n):
        if idx == i:
            _x = x
        elif idx != j:
            _x = xp[idx]
        else:
            continue
        if _x <= 0:
            return 0
        s_ += _x
        c = (c * d) // (_x * n)

    c = (c * d * STABLE_A_PRECISION) // (ann * n)
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
    reserves: List[int],
    rates: List[int],
    i: int,
    j: int,
    amp: int,
    fee: int,
    offpeg_fee_multiplier: int,
) -> int:
    """Simulate a StableSwap exchange of dx tokens from coin i to coin j.
    Returns the net output amount in token j native units."""
    n = len(reserves)
    if dx <= 0 or n < 2 or i == j or i >= n or j >= n or amp <= 0:
        return 0
    if any(r <= 0 for r in reserves):
        return 0

    xp = [(rates[k] * reserves[k]) // WEI_SCALE for k in range(n)]
    x_new = xp[i] + (dx * rates[i]) // WEI_SCALE
    d = _stable_get_d(xp, amp)
    if d <= 0:
        return 0
    y = _stable_get_y(i, j, x_new, xp, amp, d)
    if y <= 0:
        return 0
    dy = xp[j] - y - 1
    if dy <= 0:
        return 0
    dy_fee = (dy * _stable_dynamic_fee((xp[i] + x_new) // 2, (xp[j] + y) // 2, fee, offpeg_fee_multiplier)) // STABLE_FEE_DENOMINATOR
    dy_net = ((dy - dy_fee) * WEI_SCALE) // rates[j]
    return dy_net if dy_net > 0 else 0


def find_optimal_trade_stable_n(
    reserves: List[int],
    rates: List[int],
    oracle_prices: List[int],
    balances: List[int],
    amp: int,
    fee: int,
    offpeg_fee_multiplier: int,
    min_profit_usd: int,
) -> Tuple[Optional[str], Optional[Tuple[int, int, int, int, int]]]:
    """
    N-coin stable-pool arbitrage: find the most profitable (i, j) swap.

    Enumerates all directed pairs where the bot holds token i, runs a
    ternary search on the concave profit function for each, and returns
    the single best trade.

    Returns:
        (reason, None) on failure, or
        (None, (i, j, amount_in, expected_out, profit_j))
        where profit_j is denominated in token j (output token).
    """
    n = len(reserves)
    if n < 2 or len(rates) != n or len(oracle_prices) != n or len(balances) != n:
        return ("Invalid array lengths", None)
    if any(r <= 0 for r in reserves) or amp <= 0:
        return ("Invalid reserves or amp", None)
    if any(p <= 0 for p in oracle_prices):
        return ("Invalid oracle prices", None)

    quote_kwargs = dict(reserves=reserves, rates=rates, amp=amp,
                        fee=fee, offpeg_fee_multiplier=offpeg_fee_multiplier)

    best = None  # (profit_usd, i, j, amount_in, expected_out, profit_j)

    for i in range(n):
        if balances[i] <= 0:
            continue
        for j in range(n):
            if i == j:
                continue
            oracle_ratio_ij = (oracle_prices[i] * WEI_SCALE) // oracle_prices[j]
            max_input = balances[i]

            def _profit(dx, _i=i, _j=j, _ratio=oracle_ratio_ij):
                dy = _stable_quote_output(dx=dx, i=_i, j=_j, **quote_kwargs)
                if dy <= 0:
                    return -(dx or 1)
                return dy - (dx * _ratio) // WEI_SCALE

            lo, hi = 1, max_input
            for _ in range(128):
                if hi - lo < 3:
                    break
                m1 = lo + (hi - lo) // 3
                m2 = hi - (hi - lo) // 3
                if _profit(m1) < _profit(m2):
                    lo = m1
                else:
                    hi = m2

            best_dx = (lo + hi) // 2
            dy_out = _stable_quote_output(dx=best_dx, i=i, j=j, **quote_kwargs)
            if dy_out <= 0:
                continue
            profit_j = dy_out - (best_dx * oracle_ratio_ij) // WEI_SCALE
            if profit_j <= 0:
                continue

            min_profit_j = (min_profit_usd * WEI_SCALE) // oracle_prices[j] if oracle_prices[j] > 0 else 0
            if profit_j < min_profit_j:
                continue

            profit_usd = (profit_j * oracle_prices[j]) // WEI_SCALE
            if best is None or profit_usd > best[0]:
                best = (profit_usd, i, j, best_dx, dy_out, profit_j)

    if best is None:
        return ("No profitable stable pair found", None)
    _, i, j, amount_in, expected_out, profit_j = best
    return (None, (i, j, amount_in, expected_out, profit_j))

