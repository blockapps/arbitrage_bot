"""
ERC20 Token contract wrapper for Strato
"""

import logging

from core.strato_client import strato_client

logger = logging.getLogger(__name__)


class Token:
    """ERC20 token contract wrapper for Strato blockchain"""
    
    def __init__(self, address: str):
        """
        Initialize token contract
        
        Args:
            address: Token contract address
        """
        self.address = address
        
        # Token info
        self.name: str = ""
        self.symbol: str = ""
        
        # Balance and allowance from pool query
        self.balance: int = 0
        self.allowance: int = 0
    
    def approve(self, spender: str, amount: int) -> str:
        """
        Approve spender to use tokens
        
        Args:
            spender: Address to approve
            amount: Amount to approve (in wei)
            
        Returns:
            Transaction hash
        """
        client = strato_client()
        
        # Build Strato transaction format
        transaction = {
            'from': client.account.address,
            'to': self.address,
            'contract_address': self.address,
            'method': 'approve',
            'args': {
                'spender': spender,
                'value': amount  # Contract expects 'value', not 'amount'
            }
        }
        
        return client.send_transaction(transaction)
    
    def __str__(self) -> str:
        return f"Token({self.symbol}, {self.address})"
    
    def __repr__(self) -> str:
        return f"Token(address='{self.address}', symbol='{self.symbol}')"
