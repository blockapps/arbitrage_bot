"""
CDP liquidation execution engine

Scans for liquidatable positions and executes profitable liquidations.
"""

import logging
import time
from threading import Lock
from typing import Optional
from dataclasses import dataclass, field

from onchain.cdp import CDP
from engine.helpers import update_cumulative_profit
from core.constants import WEI_SCALE
from core.notifier import notify_error

logger = logging.getLogger(__name__)


@dataclass
class LiquidationOpportunity:
    collateral_asset: str
    borrower: str
    debt_to_cover: str
    raw_position: dict = field(default_factory=dict)


@dataclass
class LiquidationResult:
    success: bool
    opportunity: LiquidationOpportunity
    response: Optional[dict] = None
    execution_time: Optional[float] = None
    error_message: Optional[str] = None


class LiquidationExecutor:
    """Scans for and executes CDP liquidations"""

    def __init__(self):
        self.cdp = CDP()
        self._execution_lock = Lock()
        self.is_executing = False
        self.last_execution_time = 0

    def scan_for_opportunities(self) -> list[LiquidationOpportunity]:
        """Fetch all liquidatable positions and convert to opportunities."""
        try:
            positions = self.cdp.get_liquidatable_positions()

            if not positions:
                logger.info("No liquidatable CDP positions found")
                return []

            

            opportunities = []
            for pos in positions:
                collateral_asset = pos.get("asset", "")
                borrower = pos.get("borrower", "")
                debt_to_cover = pos.get("debtAmount", 0)

                if not collateral_asset or not borrower:
                    logger.warning(f"Skipping position with missing fields: {pos}")
                    continue

                if int(debt_to_cover) <= 0:
                    continue


                opp = LiquidationOpportunity(
                    collateral_asset=collateral_asset,
                    borrower=borrower,
                    debt_to_cover=str(debt_to_cover),
                    raw_position=pos,
                )
                opportunities.append(opp)

                logger.info(
                    f"  Liquidatable: borrower={borrower[:16]}… "
                    f"collateral={collateral_asset[:16]}… "
                    f"debt={debt_to_cover}"
                )

            logger.info(f"Found {len(opportunities)} liquidatable CDP position(s)")
            return opportunities

        except Exception as e:
            logger.error(f"Error scanning for liquidation opportunities: {e}")
            notify_error(f"Error scanning for liquidation opportunities: {e}")
            return []

    def execute_liquidation(self, opp: LiquidationOpportunity) -> LiquidationResult:
        """Execute a single liquidation."""
        with self._execution_lock:
            if self.is_executing:
                return LiquidationResult(
                    success=False,
                    opportunity=opp,
                    error_message="Executor is already running",
                )
            self.is_executing = True

        start_time = time.time()
        try:
            logger.info(
                f"Executing liquidation: borrower={opp.borrower[:16]}… "
                f"collateral={opp.collateral_asset[:16]}… "
                f"debtToCover={opp.debt_to_cover}"
            )

            result = self.cdp.liquidate(
                collateral_asset=opp.collateral_asset,
                borrower=opp.borrower,
                debt_to_cover=opp.debt_to_cover,
            )

            elapsed = time.time() - start_time
            logger.info(f"Liquidation succeeded in {elapsed:.2f}s: {result}")

            # profit = debt_to_cover * closeFactorBps * liquidationPenlatyBps
            # profit = debt_to_cover * 0.5 * 0.1
            profit_wei = int(opp.debt_to_cover) * 5 // 100
            update_cumulative_profit(profit_wei, WEI_SCALE, file_path="liquidation_profit.json")

            return LiquidationResult(
                success=True,
                opportunity=opp,
                response=result,
                execution_time=elapsed,
            )

        except Exception as e:
            error_msg = str(e)
            logger.error(f"Liquidation failed: {error_msg}")
            notify_error(f"Liquidation failed: {error_msg}")
            return LiquidationResult(
                success=False,
                opportunity=opp,
                execution_time=time.time() - start_time,
                error_message=error_msg,
            )
        finally:
            with self._execution_lock:
                self.is_executing = False
            self.last_execution_time = time.time()
