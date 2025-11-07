"""
OAuth client for Strato blockchain authentication
"""

import os
import time
import logging
from typing import Optional
import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

TOKEN_LIFETIME_THRESHOLD_SECONDS = 10


class OAuthClient:
    """OAuth client for Strato blockchain authentication using password grant flow"""
    
    def __init__(self):
        """Initialize OAuth client with environment variables"""
        self.discovery_url = os.getenv('OAUTH_DISCOVERY_URL')
        self.client_id = os.getenv('OAUTH_CLIENT_ID')
        self.client_secret = os.getenv('OAUTH_CLIENT_SECRET')
        self.username = os.getenv('USERNAME')
        self.password = os.getenv('PASSWORD')
        self.strato_node_url = os.getenv('STRATO_NODE_URL')
        
        if not all([self.discovery_url, self.client_id, self.client_secret, self.username, self.password, self.strato_node_url]):
            missing = []
            if not self.discovery_url:
                missing.append('OAUTH_DISCOVERY_URL')
            if not self.client_id:
                missing.append('OAUTH_CLIENT_ID')
            if not self.client_secret:
                missing.append('OAUTH_CLIENT_SECRET')
            if not self.username:
                missing.append('USERNAME')
            if not self.password:
                missing.append('PASSWORD')
            if not self.strato_node_url:
                missing.append('STRATO_NODE_URL')
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
        
        self.access_token: Optional[str] = None
        self.token_expiry: Optional[float] = None
        self.token_endpoint: Optional[str] = None
        
        # Fetch user address during initialization (it won't change)
        self.user_address: str = self._fetch_user_address()
    
    def get_token_endpoint(self) -> str:
        """Get OAuth token endpoint from discovery URL"""
        if self.token_endpoint:
            return self.token_endpoint
        
        try:
            logger.info('OAuth: Discovering token endpoint...')
            response = requests.get(self.discovery_url, timeout=10)
            response.raise_for_status()
            self.token_endpoint = response.json().get('token_endpoint')
            
            if not self.token_endpoint:
                raise ValueError('Token endpoint not found in discovery document')
            
            logger.info(f'OAuth: Token endpoint discovered: {self.token_endpoint}')
            return self.token_endpoint
        except Exception as e:
            logger.error(f'OAuth: Error discovering token endpoint: {e}')
            raise ValueError(f'OAuth discovery failed: {e}')
    
    def get_access_token(self) -> str:
        """Get access token, using cached token if still valid"""
        # Return cached token if still valid
        if self.access_token and self.token_expiry:
            if time.time() < (self.token_expiry - TOKEN_LIFETIME_THRESHOLD_SECONDS):
                return self.access_token
        
        # Request new token
        self.refresh_token()
        return self.access_token
    
    def refresh_token(self) -> str:
        """Refresh OAuth access token using password grant"""
        try:
            # Get the token endpoint from discovery
            token_endpoint = self.get_token_endpoint()
            
            # Use password grant to authenticate as the specific username
            token_data = {
                'grant_type': 'password',
                'username': self.username,
                'password': self.password,
                'client_id': self.client_id,
                'client_secret': self.client_secret
            }
            
            response = requests.post(
                token_endpoint,
                data=token_data,
                headers={
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'Accept': 'application/json'
                },
                timeout=10
            )
            response.raise_for_status()
            
            data = response.json()
            if data.get('access_token'):
                self.access_token = data['access_token']
                expires_in = data.get('expires_in', 3600)  # Default 1 hour
                self.token_expiry = time.time() + expires_in
                
                logger.info('OAuth: Access token refreshed successfully')
                return self.access_token
            else:
                raise ValueError('No access token in response')
        except requests.exceptions.RequestException as e:
            error_message = str(e)
            if hasattr(e.response, 'json') and e.response.json():
                error_data = e.response.json()
                error_message = error_data.get('error_description') or error_data.get('error', error_message)
            logger.error(f'OAuth: Error getting access token: {error_message}')
            raise ValueError(f'OAuth authentication failed: {error_message}')
    
    def validate_token(self) -> bool:
        """Validate OAuth token"""
        try:
            token = self.get_access_token()
            # User address is already fetched during initialization
            return bool(token)
        except Exception as e:
            logger.error(f'OAuth: Token validation failed: {e}')
            raise
    
    def _fetch_user_address(self) -> str:
        """Fetch user address from Strato node during initialization"""
        access_token = self.get_access_token()
        response = requests.get(
            f'{self.strato_node_url}/strato/v2.3/key',
            headers={
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json'
            },
            timeout=10
        )
        response.raise_for_status()
        
        data = response.json()
        user_address = data.get('address')
        
        if not user_address:
            raise ValueError('No address in response')
        
        logger.info(f'OAuth: User address retrieved: {user_address}')
        return user_address
    
    def get_user_address(self) -> str:
        """Get user address (already fetched during initialization)"""
        return self.user_address


# Export singleton instance with lazy initialization
_oauth_client: Optional[OAuthClient] = None


def oauth_client() -> OAuthClient:
    """Get singleton OAuth client instance"""
    global _oauth_client
    if _oauth_client is None:
        _oauth_client = OAuthClient()
    return _oauth_client

