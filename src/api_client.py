"""API client for Save4223 backend."""

import requests
import logging
import time
from typing import Optional, Dict, Any, Union
from urllib.parse import urljoin

logger = logging.getLogger(__name__)


class APIError(Exception):
    """API request error."""
    pass


class APIClient:
    """HTTPS client for Save4223 Edge API with SSL/TLS support."""

    def __init__(
        self,
        base_url: str,
        edge_secret: str,
        timeout: int = 5,
        cert_path: Optional[str] = None,
        verify_ssl: bool = True,
        max_retries: int = 3,
        retry_delay: float = 1.0
    ):
        """
        Initialize API client.

        Args:
            base_url: Server base URL (e.g., https://save4223.local:8443)
            edge_secret: Edge device API secret
            timeout: Request timeout in seconds
            cert_path: Path to SSL certificate file (for self-signed certs)
            verify_ssl: Whether to verify SSL certificates
            max_retries: Maximum number of retry attempts
            retry_delay: Delay between retries in seconds
        """
        self.base_url = base_url.rstrip('/')
        self.edge_secret = edge_secret
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay

        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {edge_secret}',
            'Content-Type': 'application/json',
        })

        # Configure SSL/TLS
        if cert_path:
            self.session.verify = cert_path
            logger.info(f"Using custom SSL certificate: {cert_path}")
        elif verify_ssl:
            self.session.verify = True
        else:
            self.session.verify = False
            logger.warning("SSL verification disabled - INSECURE!")

        # Configure connection pooling
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=10,
            pool_maxsize=10,
            max_retries=0  # We handle retries manually
        )
        self.session.mount('https://', adapter)
        self.session.mount('http://', adapter)
    
    def _request(
        self,
        method: str,
        path: str,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Make HTTPS request with retry logic and error handling.

        Args:
            method: HTTP method
            path: API path
            **kwargs: Additional request arguments

        Returns:
            JSON response as dictionary

        Raises:
            APIError: On request failure after all retries
        """
        url = urljoin(self.base_url + '/', path.lstrip('/'))

        last_error = None
        for attempt in range(self.max_retries):
            try:
                response = self.session.request(
                    method=method,
                    url=url,
                    timeout=self.timeout,
                    **kwargs
                )
                response.raise_for_status()
                return response.json()

            except requests.exceptions.Timeout as e:
                last_error = APIError(f"Request timeout (attempt {attempt + 1}/{self.max_retries})")
                logger.warning(f"Request timeout to {path}, attempt {attempt + 1}")

            except requests.exceptions.SSLError as e:
                last_error = APIError(f"SSL/TLS error: {str(e)}")
                logger.error(f"SSL error: {e}")
                # Don't retry SSL errors
                raise last_error

            except requests.exceptions.ConnectionError as e:
                last_error = APIError(f"Connection failed (attempt {attempt + 1}/{self.max_retries})")
                logger.warning(f"Connection failed to {path}, attempt {attempt + 1}")

            except requests.exceptions.HTTPError as e:
                status_code = e.response.status_code if e.response else 0
                # Don't retry client errors (4xx) except 429 (rate limit)
                if 400 <= status_code < 500 and status_code != 429:
                    raise APIError(f"HTTP {status_code}: {e.response.text}")
                last_error = APIError(f"HTTP {status_code}: {e.response.text}")
                logger.warning(f"HTTP error {status_code}, attempt {attempt + 1}")

            except Exception as e:
                last_error = APIError(f"Request failed: {e}")
                logger.error(f"Unexpected error: {e}")

            # Wait before retry (except on last attempt)
            if attempt < self.max_retries - 1:
                time.sleep(self.retry_delay * (attempt + 1))  # Exponential backoff

        # All retries exhausted
        raise last_error or APIError("Request failed after all retries")
    
    def authorize(self, card_uid: str, cabinet_id: int) -> Dict[str, Any]:
        """
        Authenticate card UID with server.
        
        POST /api/edge/authorize
        """
        logger.debug(f"Authorizing card: {card_uid[:10]}...")
        
        result = self._request('POST', '/api/edge/authorize', json={
            'card_uid': card_uid,
            'cabinet_id': cabinet_id,
        })
        
        return result
    
    def sync_session(self, session_id: str, cabinet_id: int, 
                     user_id: str, rfids: list) -> Dict[str, Any]:
        """
        Sync RFID scan results with server.
        
        POST /api/edge/sync-session
        """
        logger.debug(f"Syncing session: {session_id[:8]}... with {len(rfids)} tags")
        
        result = self._request('POST', '/api/edge/sync-session', json={
            'session_id': session_id,
            'cabinet_id': cabinet_id,
            'user_id': user_id,
            'rfids_present': rfids,
        })
        
        return result
    
    def local_sync(self, cabinet_id: int) -> Dict[str, Any]:
        """
        Get local sync data (cached auth and items).
        
        GET /api/edge/local-sync
        """
        logger.debug(f"Fetching local sync data for cabinet {cabinet_id}")
        
        result = self._request('GET', '/api/edge/local-sync', params={
            'cabinet_id': cabinet_id,
        })
        
        return result
    
    def health_check(self) -> bool:
        """Check if server is reachable."""
        try:
            self._request('GET', '/api/health')
            return True
        except APIError:
            return False

    def edge_health_check(self) -> Dict[str, Any]:
        """
        Check edge API health with detailed response.

        GET /api/edge/health
        """
        try:
            return self._request('GET', '/api/edge/health')
        except APIError as e:
            return {'healthy': False, 'error': str(e)}
    
    def pair_card(self, pairing_token: str, card_uid: str, cabinet_id: int) -> Dict[str, Any]:
        """
        Pair an NFC card with a user using a pairing token.
        
        POST /api/edge/pair-card
        """
        logger.debug(f"Pairing card: {card_uid[:10]}... with token")
        
        result = self._request('POST', '/api/edge/pair-card', json={
            'pairing_token': pairing_token,
            'card_uid': card_uid,
            'cabinet_id': cabinet_id,
        })
        
        return result
