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
from core.constants import WEI_SCALE, BLOCKAPPS_ORACLE_TOKENS
from onchain.pool import Pool
from market.oracle import PriceOracle, get_external_symbol
from engine.arb_executor import ArbitrageExecutor
from engine.helpers import ensure_pool_approvals

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

    def init_components(self):
        c = strato_client()
        if not c.is_connected():
            raise RuntimeError("cannot connect to blockchain")

        pools = self.cfg.get("pools", [])
        if not pools:
            raise RuntimeError("No pools configured in config.yaml")
        
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

        # Initialize executor for each pool
        for pool_config in pools:
            pool_addr = pool_config.get("address")
            
            pool = Pool(pool_addr, fee_bps=fee_bps)
            pool.fetch_pool_data()
            
            # Auto-register BlockApps tokens based on token names
            # These use the on-chain BlockApps PriceOracle instead of Alchemy
            for token in [pool.token_a, pool.token_b]:
                external_symbol = get_external_symbol(token.name)
                if external_symbol in BLOCKAPPS_ORACLE_TOKENS:
                    self.oracle.register_blockapps_token(external_symbol, token.address)
            
            executor = ArbitrageExecutor(
                token_a=pool.token_a,
                token_b=pool.token_b,
                pool=pool,
                oracle=self.oracle,
                fee_bps=fee_bps,
                min_profit=min_profit_wei,
            )
            
            # Ensure pool approvals
            ensure_pool_approvals(pool.token_a, pool.token_b, pool)
            
            self.executors.append(executor)
            log.info(f"initialized {pool.token_a.symbol}-{pool.token_b.symbol} pool at {pool_addr}")
        
        # Pre-fetch prices for all tokens in all pools
        all_token_symbols = set()
        for executor in self.executors:
            all_token_symbols.add(get_external_symbol(executor.token_a.name))
            all_token_symbols.add(get_external_symbol(executor.token_b.name))
        
        if all_token_symbols:
            self.oracle.fetch_all_prices(list(all_token_symbols), force_refresh=True)
        
        if self.dry_run:
            log.info("dry-run mode enabled")

    def scan_once(self):
        """Scan all pools for opportunities and execute the first profitable one found"""
        for i, executor in enumerate(self.executors):
            opp = executor.scan_for_opportunity()
            
            # Add newline between pools (but not after the last one)
            if i < len(self.executors) - 1:
                print()
            
            if not opp:
                continue

            if self.dry_run:
                log.info(f"dry-run: would execute trade on {executor.pool.token_a.symbol}-{executor.pool.token_b.symbol} pool")
                return True

            res = executor.execute_opportunity(opp)
            log.info(f"exec result on {executor.pool.token_a.symbol}-{executor.pool.token_b.symbol}: {res.success}")
            return res.success
        
        return False

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
    bot.run()


if __name__ == "__main__":
    main()
