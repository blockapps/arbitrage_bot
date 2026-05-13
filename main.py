#!/usr/bin/env python3
"""
Multi-Pool Arbitrage Bot

Detects and executes arbitrage opportunities across multiple AMM pools
on the Strato blockchain by comparing pool prices with external market prices.
"""

import time
import yaml
import logging
from decimal import Decimal

from core.strato_client import strato_client
from core.constants import WEI_SCALE
from onchain.pool import Pool
from market.oracle import PriceOracle
from engine.arb_executor import ArbitrageExecutor
from engine.liquidation_executor import LiquidationExecutor
from engine.helpers import ensure_pool_approvals
from core.health import start_health_server
from core.notifier import notify_error

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger("arb_bot")


class ArbitrageBot:
    def __init__(self, cfg_path="config.yaml"):
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f)
        self.cfg = cfg
        self.dry_run = True  # Default to dry run for safety
        self.interval = cfg.get("execution", {}).get("execution_interval", 10)
        self.running = False
        self.executors = []  # List of executors, one per pool
        self.liquidation_executor = None

    def init_components(self):
        c = strato_client()
        if not c.is_connected():
            raise RuntimeError("cannot connect to blockchain")

        pools = self.cfg.get("pools", [])
    
        if not pools: 
            log.info("no pools configured in config.yaml")
        else: 
            # Validate all pool configs
            for i, pool_config in enumerate(pools):
                pool_addr = pool_config.get("address")
                
                if not pool_addr:
                    raise RuntimeError(f"Pool {i+1}: address is required in config.yaml")
            
            fee_bps = self.cfg["trading"]["fee_bps"]
            oracle_cfg = self.cfg["oracle"]
            self.oracle = PriceOracle(
                timeout=oracle_cfg["timeout"],
                blockapps_price_oracle=oracle_cfg.get("blockapps_price_oracle", "")
            )

        trade_cfg = self.cfg["trading"]
        min_profit = Decimal(str(trade_cfg["min_profit"]))
        min_profit_wei = int(min_profit * WEI_SCALE)
        slippage_factor_amm = float(trade_cfg.get("slippage_factor_amm", 0.96))
        slippage_factor_stable = float(trade_cfg.get("slippage_factor_stable", 0.92))
        
        exec_cfg = self.cfg.get("execution", {})
        self.vault_addr = exec_cfg.get("vault_addr", "")

        # Initialize executor for each pool
        for pool_config in pools:
            pool_addr = pool_config.get("address")
            pool_fee_bps = int(pool_config.get("fee_bps", fee_bps))

            pool_slippage_amm = float(pool_config.get("slippage_factor_amm", slippage_factor_amm))
            pool_slippage_stable = float(pool_config.get("slippage_factor_stable", slippage_factor_stable))
            pool = Pool(
                pool_addr,
                fee_bps=pool_fee_bps,
                slippage_factor_amm=pool_slippage_amm,
                slippage_factor_stable=pool_slippage_stable,
            )
            pool.fetch_pool_data()
            
            # Register every non-USDST coin with the BlockApps on-chain price oracle.
            # USDST is hardcoded to $1 inside the oracle and does not need registration.
            for token in pool.coins:
                if token.symbol == "USDST":
                    continue
                self.oracle.register_blockapps_token(token.symbol, token.address)
            
            executor = ArbitrageExecutor(
                token_a=pool.token_a,
                token_b=pool.token_b,
                pool=pool,
                oracle=self.oracle,
                fee_bps=pool_fee_bps,
                min_profit_usd=min_profit_wei,
            )
            
            # Ensure pool approvals (token_a/token_b + any extra coins in n>2 pools)
            ensure_pool_approvals(pool.token_a, pool.token_b, pool, self.vault_addr)
            for extra in pool.coins[2:]:
                ensure_pool_approvals(extra, extra, pool, self.vault_addr)
            
            self.executors.append(executor)
            log.info(
                f"initialized {pool.token_a.symbol}-{pool.token_b.symbol} pool at {pool_addr} "
                f"(isStable={pool.is_stable_pool()}, source=on-chain BlockApps-Pool.isStable)"
            )
        
        # Pre-fetch prices for all tokens in all pools
        all_token_symbols = set()
        for executor in self.executors:
            for coin in executor.pool.coins:
                all_token_symbols.add(coin.symbol)
        
        if all_token_symbols:
            self.oracle.fetch_all_prices(list(all_token_symbols), force_refresh=True)

        # Initialize liquidation executor if enabled
        liq_cfg = self.cfg.get("liquidation", {})
        if liq_cfg.get("enabled", False):
            self.liquidation_executor = LiquidationExecutor()
            log.info("CDP liquidation scanning enabled")

        if self.dry_run:
            log.info("dry-run mode enabled")

    def scan_once(self):
        """Scan all pools for arbitrage and CDP positions for liquidation"""
        any_executed = False

        # --- Arbitrage scan (all pools every cycle) ---
        for i, executor in enumerate(self.executors):
            opp = executor.scan_for_opportunity()

            if i < len(self.executors) - 1:
                print()

            if not opp:
                continue

            pool_label = f"{executor.pool.token_a.symbol}-{executor.pool.token_b.symbol}"

            if self.dry_run:
                log.info(f"dry-run: would execute trade on {pool_label} pool")
                any_executed = True
                continue

            res = executor.execute_opportunity(opp)
            log.info(f"exec result on {pool_label}: {res.success}")
            if res.success:
                any_executed = True

        # --- Liquidation scan ---
        if self.liquidation_executor:
            print()
            opportunities = self.liquidation_executor.scan_for_opportunities()
            for opp in opportunities:
                if self.dry_run:
                    log.info(
                        f"dry-run: would liquidate borrower={opp.borrower[:16]}… "
                        f"collateral={opp.collateral_asset[:16]}… "
                        f"debt={opp.debt_to_cover}"
                    )
                    continue

                res = self.liquidation_executor.execute_liquidation(opp)
                if res.success:
                    log.info(f"liquidation succeeded for borrower={opp.borrower[:16]}…")
                else:
                    msg = f"liquidation failed for borrower={opp.borrower[:16]}…: {res.error_message}"
                    log.warning(msg)
                    notify_error(msg)

        return any_executed

    def run(self):
        self.running = True
        log.info(f"starting loop interval={self.interval}s")

        while self.running:
            try:
                self.scan_once()
                print("\n\n\n")  # 3 newlines after every scan
                time.sleep(self.interval)
            except KeyboardInterrupt:
                log.info("stopping...")
                self.running = False
            except Exception as e:
                log.error(f"loop error: {e}")
                notify_error(f"loop error: {e}")
                print("\n\n\n")  # 3 newlines even on error
                time.sleep(self.interval)


def main():
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("-c", "--config", default="config.yaml")
    p.add_argument("--live", action="store_true")
    a = p.parse_args()

    bot = ArbitrageBot(a.config)
    bot.dry_run = not a.live
    bot.init_components()

    health_port = bot.cfg.get("health_port", 8080)
    start_health_server(bot, port=health_port)

    bot.run()


if __name__ == "__main__":
    main()
