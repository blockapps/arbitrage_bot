"""
Strato blockchain client for interactions
"""

import logging
import os
import time
import requests
from typing import Optional, Any
from dataclasses import dataclass

from .oauth_client import oauth_client

logger = logging.getLogger(__name__)

# Timeout configurations
TIMEOUTS = {
    'SUBMIT': 30000,
    'WAIT': 120000,
    'STATUS': 10000
}


@dataclass
class Account:
    """Account wrapper for compatibility"""
    address: str


def api_request(
    method: str,
    url: str,
    timeout: int = 10000,
    **kwargs
) -> requests.Response:
    """Make API request"""
    response = requests.request(method, url, timeout=timeout, **kwargs)
    response.raise_for_status()
    return response


class StratoClient:
    """Strato blockchain client wrapper for interactions"""
    
    def __init__(self):
        """
        Initialize Strato client
        
        Reads STRATO_NODE_URL from environment variable.
        """
        self.oauth = oauth_client()
        self.strato_node_url = os.getenv('STRATO_NODE_URL')
        
        if not self.strato_node_url:
            raise ValueError("STRATO_NODE_URL must be set as environment variable")
        
        # Initialize OAuth and get user address
        try:
            user_address = self.oauth.get_user_address()
            self.account = Account(address=user_address)
            logger.info(f"Initialized Strato client with account: {self.account.address}")
        except Exception as e:
            logger.error(f"Failed to initialize OAuth: {e}")
            raise
    
    def is_connected(self) -> bool:
        """Check if connected to the blockchain (via OAuth token validation)"""
        try:
            return self.oauth.validate_token()
        except Exception as e:
            logger.error(f"Connection check failed: {e}")
            return False
    
    def get_balance(self, address: str) -> tuple[int, int]:
        """
        Get USDST and Voucher balances separately
        
        Args:
            address: Address to check balance for
            
        Returns:
            Tuple of (usdst_balance_wei, voucher_balance) where:
            - usdst_balance_wei: USDST balance in wei
            - voucher_balance: Voucher balance in voucher units (100 vouchers = 1 USDST)
        """
        from core.constants import USDST_ADDRESS
        
        try:
            access_token = self.oauth.get_access_token()
            
            # Get USDST balance
            usdst_response = api_request(
                'GET',
                f'{self.strato_node_url}/cirrus/search/BlockApps-Token-_balances',
                headers={
                    'Authorization': f'Bearer {access_token}',
                    'Content-Type': 'application/json'
                },
                params={
                    'address': f'eq.{USDST_ADDRESS}',
                    'key': f'eq.{address}',
                    'select': 'balance:value::text'
                }
            )
            
            usdst_data = usdst_response.json()
            usdst_balance = 0
            if usdst_data and len(usdst_data) > 0:
                usdst_balance_str = usdst_data[0].get('balance', '0')
                usdst_balance = int(usdst_balance_str)
            
            # Get Voucher balance
            voucher_response = api_request(
                'GET',
                f'{self.strato_node_url}/cirrus/search/BlockApps-Voucher-_balances',
                headers={
                    'Authorization': f'Bearer {access_token}',
                    'Content-Type': 'application/json'
                },
                params={
                    'key': f'eq.{address}',
                    'select': 'balance:value::text'
                }
            )
            
            voucher_data = voucher_response.json()
            voucher_balance = 0
            if voucher_data and len(voucher_data) > 0:
                voucher_balance_str = voucher_data[0].get('balance', '0')
                voucher_balance = int(voucher_balance_str)
            
            return usdst_balance, voucher_balance
            
        except Exception as e:
            logger.error(f"Failed to get balance for {address}: {e}")
            return 0, 0
    
    def send_transaction(self, transaction: dict) -> str:
        """
        Send a transaction using Strato API
        
        Note: Transaction dict must include 'contract_address', 'method', 
        and 'args' fields for Strato format conversion.
        """
        if not self.account:
            raise ValueError("Account required for sending transactions")
        
        try:
            # Extract Strato-specific fields from transaction dict
            contract_address = transaction.get('contract_address') or transaction.get('to')
            method = transaction.get('method')
            args = transaction.get('args', {})
            
            if not all([contract_address, method]):
                raise ValueError("Transaction must include contract_address (or 'to') and method fields")
            
            # Build Strato transaction format
            strato_tx = {
                'txs': [{
                    'type': 'FUNCTION',
                    'payload': {
                        'contractAddress': contract_address,
                        'method': method,
                        'args': args
                    }
                }]
            }
            
            access_token = self.oauth.get_access_token()
            
            response = api_request(
                'POST',
                f'{self.strato_node_url}/strato/v2.3/transaction/parallel?resolve=true',
                headers={
                    'Authorization': f'Bearer {access_token}',
                    'Content-Type': 'application/json'
                },
                json=strato_tx,
                timeout=TIMEOUTS['SUBMIT']
            )
            
            # Extract transaction hash
            data = response.json()
            tx_hash = self._extract_transaction_hash(data)
            
            logger.info(f"Transaction sent: {tx_hash}")
            return tx_hash
            
        except Exception as e:
            logger.error(f"Transaction failed: {e}")
            raise
    
    def _extract_transaction_hash(self, data: Any) -> str:
        """Extract transaction hash from Strato API response"""
        if not data:
            raise ValueError('No transaction data returned from STRATO')
        
        if isinstance(data, list) and len(data) > 0:
            return data[0].get('hash') or str(data[0])
        elif isinstance(data, dict) and data.get('hash'):
            return data['hash']
        elif isinstance(data, str):
            return data
        
        raise ValueError('No transaction hash returned from STRATO')
    
    def wait_for_transaction(self, tx_hash: str, timeout: int = 120) -> dict:
        """Wait for transaction confirmation using Strato API"""
        start_time = time.time()
        timeout_ms = timeout * 1000
        
        try:
            while (time.time() - start_time) * 1000 < timeout_ms:
                try:
                    access_token = self.oauth.get_access_token()
                    
                    response = api_request(
                        'POST',
                        f'{self.strato_node_url}/bloc/v2.2/transactions/results',
                        headers={
                            'Authorization': f'Bearer {access_token}',
                            'Content-Type': 'application/json'
                        },
                        json=[tx_hash],
                        timeout=TIMEOUTS['STATUS']
                    )
                    
                    data = response.json()
                    if not data or len(data) == 0:
                        raise ValueError('No transaction data returned from STRATO')
                    
                    tx_data = data[0]
                    
                    if tx_data.get('status') == 'Success':
                        logger.info(f"Transaction confirmed: {tx_hash}")
                        return {
                            'status': 'Success',
                            'hash': tx_hash,
                            'timestamp': time.time()
                        }
                    elif tx_data.get('status') in ['Failed', 'Failure']:
                        error_message = tx_data.get('txResult', {}).get('message') or tx_data.get('error', 'Unknown error')
                        raise Exception(f"Transaction failed: {error_message}")
                    elif tx_data.get('status') == 'Pending':
                        time.sleep(2)  # Wait 2 seconds before checking again
                        continue
                    else:
                        # Status not Success, Failed, Failure, or Pending - wait and check again
                        time.sleep(2)
                        continue
                        
                except Exception as e:
                    if 'Transaction failed' in str(e):
                        raise
                    
                    logger.warning(f"Error checking transaction status: {e}")
                    time.sleep(2)  # Wait 2 seconds before checking again
            
            raise TimeoutError(f"Transaction timeout after {timeout} seconds")
            
        except Exception as e:
            logger.error(f"Transaction confirmation failed: {e}")
            raise
    


# Singleton instance
_strato_client: Optional[StratoClient] = None


def strato_client() -> StratoClient:
    """Get singleton Strato client instance"""
    global _strato_client
    if _strato_client is None:
        _strato_client = StratoClient()
    return _strato_client

