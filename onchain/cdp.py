"""
CDP (Collateralized Debt Position) contract wrapper for Strato
"""

import logging

from core.strato_client import strato_client, api_request, TIMEOUTS

logger = logging.getLogger(__name__)


class CDP:
    """Wrapper for CDP liquidation endpoints on Strato"""

    def __init__(self):
        client = strato_client()
        self._base_url = client.strato_node_url
        self._oauth = client.oauth

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._oauth.get_access_token()}",
            "Content-Type": "application/json",
        }

    def get_liquidatable_positions(self) -> list[dict]:
        """Fetch all liquidatable CDP positions.

        Returns:
            List of position dicts as returned by the API.
        """
        try:
            response = api_request(
                "GET",
                f"{self._base_url}api/cdp/liquidatable",
                headers=self._headers(),
            )
            return response.json()
        except Exception as e:
            logger.error(f"Failed to fetch liquidatable positions: {e}")
            return []

    def liquidate(
        self,
        collateral_asset: str,
        borrower: str,
        debt_to_cover: str,
    ) -> dict:
        """Execute a CDP liquidation.

        Args:
            collateral_asset: Collateral asset address.
            borrower: Borrower address to liquidate.
            debt_to_cover: USDST amount to cover (decimal string).

        Returns:
            Liquidation transaction result from the API.
        """
        response = api_request(
            "POST",
            f"{self._base_url}api/cdp/liquidate",
            headers=self._headers(),
            json={
                "collateralAsset": collateral_asset,
                "borrower": borrower,
                "debtToCover": debt_to_cover,
            },
            timeout=TIMEOUTS["SUBMIT"],
        )
        return response.json()
