"""
Matchbook Exchange REST API client.
Handles authentication, session refresh, market data, and order placement/cancellation.
Credentials loaded from .env via python-dotenv.
"""

import logging
import time
from typing import Any, Optional

import requests
from dotenv import load_dotenv
import os

load_dotenv()

logger = logging.getLogger(__name__)

BASE_URL = "https://api.matchbook.com"
SESSION_ENDPOINT = "/bpapi/rest/security/session"
EVENTS_ENDPOINT = "/edge/rest/events"
OFFERS_ENDPOINT = "/edge/rest/v2/offers"

# Request timeouts and retry config
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2  # seconds for exponential backoff
RATE_LIMIT_BACKOFF = 60  # seconds when 429 received


class MatchbookAPIError(Exception):
    """Base exception for Matchbook API errors."""

    pass


class MatchbookAuthError(MatchbookAPIError):
    """Authentication failed (401)."""

    pass


class MatchbookRateLimitError(MatchbookAPIError):
    """Rate limit exceeded (429)."""

    pass


class MatchbookClient:
    """
    Matchbook REST API client with session management.
    Caches session-token and account (balance, free-funds, exposure).
    Re-login on 401. Exponential backoff on 429 and network errors.
    """

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        timeout: int = REQUEST_TIMEOUT,
    ):
        self.username = username or os.getenv("MATCHBOOK_USERNAME", "")
        self.password = password or os.getenv("MATCHBOOK_PASSWORD", "")
        self.timeout = timeout
        self._session_token: Optional[str] = None
        self._account: Optional[dict] = None

    def _headers(self, include_auth: bool = True) -> dict:
        """Build request headers. Accept JSON, optional session-token."""
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "User-Agent": "aladin3-trading-bot/1.0",
        }
        if include_auth and self._session_token:
            h["session-token"] = self._session_token
        return h

    def _request(
        self,
        method: str,
        path: str,
        json_data: Optional[dict] = None,
        params: Optional[dict] = None,
        retry_on_auth: bool = True,
    ) -> dict:
        """
        Execute HTTP request with retries for timeout, connection error, 401, 429.
        On 401: re-login and retry once if retry_on_auth.
        On 429: exponential backoff, retry.
        """
        url = BASE_URL + path
        last_error = None

        for attempt in range(MAX_RETRIES):
            try:
                resp = requests.request(
                    method=method,
                    url=url,
                    json=json_data,
                    params=params,
                    headers=self._headers(),
                    timeout=self.timeout,
                )

                if resp.status_code == 401 and retry_on_auth:
                    logger.warning("Session expired (401), re-loginning...")
                    self.login()
                    return self._request(method, path, json_data, params, retry_on_auth=False)

                if resp.status_code == 429:
                    wait = RATE_LIMIT_BACKOFF * (RETRY_BACKOFF_BASE ** attempt)
                    logger.warning("Rate limit (429), backing off %s seconds", wait)
                    time.sleep(wait)
                    continue

                if resp.status_code >= 400:
                    try:
                        err_body = resp.json()
                    except Exception:
                        err_body = resp.text
                    raise MatchbookAPIError(
                        f"API error {resp.status_code}: {err_body}"
                    )

                return resp.json() if resp.content else {}

            except requests.exceptions.Timeout as e:
                last_error = e
                logger.warning("Request timeout (attempt %s/%s)", attempt + 1, MAX_RETRIES)
                time.sleep(RETRY_BACKOFF_BASE ** attempt)
            except requests.exceptions.ConnectionError as e:
                last_error = e
                logger.warning("Connection error (attempt %s/%s)", attempt + 1, MAX_RETRIES)
                time.sleep(RETRY_BACKOFF_BASE ** attempt)

        raise MatchbookAPIError(f"Request failed after {MAX_RETRIES} attempts: {last_error}")

    def login(self) -> dict:
        """
        Login to Matchbook. POST /bpapi/rest/security/session.
        Returns account object with balance, free-funds, exposure.
        Caches session-token and account for subsequent requests.
        """
        if not self.username or not self.password:
            raise MatchbookAuthError("MATCHBOOK_USERNAME and MATCHBOOK_PASSWORD must be set in .env")

        payload = {"username": self.username, "password": self.password}
        resp = requests.post(
            BASE_URL + SESSION_ENDPOINT,
            json=payload,
            headers=self._headers(include_auth=False),
            timeout=self.timeout,
        )

        if resp.status_code == 400:
            try:
                err = resp.json()
                msg = err.get("errors", [{}])[0].get("messages", ["Login failed"])[0]
            except Exception:
                msg = resp.text
            raise MatchbookAuthError(msg)

        if resp.status_code != 200:
            raise MatchbookAuthError(f"Login failed: {resp.status_code} {resp.text}")

        data = resp.json()
        self._session_token = data.get("session-token")
        self._account = data.get("account", {})

        if not self._session_token:
            raise MatchbookAuthError("No session-token in login response")

        logger.info("Logged in successfully. Balance: %s", self._account.get("balance"))
        return data

    def get_session(self) -> Optional[dict]:
        """
        Validate session is still active. GET /bpapi/rest/security/session.
        Returns session info or None if 401.
        """
        try:
            resp = requests.get(
                BASE_URL + SESSION_ENDPOINT,
                headers=self._headers(),
                timeout=self.timeout,
            )
            if resp.status_code == 401:
                return None
            return resp.json() if resp.content else {}
        except Exception as e:
            logger.warning("Session check failed: %s", e)
            return None

    def get_account(self) -> dict:
        """
        Return cached account (balance, free-funds, exposure).
        Call login() to refresh if needed.
        """
        if self._account is None:
            self.login()
        return self._account or {}

    def refresh_account(self) -> dict:
        """Re-login to refresh account balance and exposure."""
        self.login()
        return self.get_account()

    def get_events(
        self,
        include_prices: bool = True,
        price_depth: int = 3,
        states: str = "open,suspended",
        per_page: int = 20,
        offset: int = 0,
        tag_url_names: Optional[str] = None,
        category_ids: Optional[str] = None,
        ids: Optional[str] = None,
        currency: str = "GBP",
    ) -> dict:
        """
        Fetch events with markets and prices. GET /edge/rest/events.
        Returns events with nested markets and runners (prices).
        ids: comma-separated event IDs to filter (e.g. "1234567890").
        """
        params = {
            "include-prices": include_prices,
            "price-depth": price_depth,
            "states": states,
            "per-page": per_page,
            "offset": offset,
            "odds-type": "DECIMAL",
            "exchange-type": "back-lay",
            "currency": currency,
        }
        if tag_url_names:
            params["tag-url-names"] = tag_url_names
        if category_ids:
            params["category-ids"] = category_ids
        if ids:
            params["ids"] = ids

        return self._request("GET", EVENTS_ENDPOINT, params=params)

    def submit_offers(
        self,
        offers: list[dict],
        odds_type: str = "DECIMAL",
        exchange_type: str = "back-lay",
    ) -> dict:
        """
        Submit one or more offers. POST /edge/rest/v2/offers.
        Each offer: {runner-id, side, odds, stake, keep-in-play?}
        Matchbook auto-rounds odds to valid ladder (Back: round up, Lay: round down).
        """
        payload = {
            "odds-type": odds_type,
            "exchange-type": exchange_type,
            "offers": offers,
        }
        return self._request("POST", OFFERS_ENDPOINT, json_data=payload)

    def cancel_offers(
        self,
        offer_ids: Optional[list[int]] = None,
        event_ids: Optional[list[int]] = None,
        market_ids: Optional[list[int]] = None,
        runner_ids: Optional[list[int]] = None,
    ) -> dict:
        """
        Cancel offers. DELETE /edge/rest/v2/offers.
        Pass comma-separated ids as query params.
        """
        params = {}
        if offer_ids:
            params["offer-ids"] = ",".join(str(x) for x in offer_ids)
        if event_ids:
            params["event-ids"] = ",".join(str(x) for x in event_ids)
        if market_ids:
            params["market-ids"] = ",".join(str(x) for x in market_ids)
        if runner_ids:
            params["runner-ids"] = ",".join(str(x) for x in runner_ids)

        if not params:
            raise ValueError("At least one of offer_ids, event_ids, market_ids, runner_ids required")

        return self._request("DELETE", OFFERS_ENDPOINT, params=params)

    def get_offers(
        self,
        status: Optional[str] = None,
        per_page: int = 50,
        offset: int = 0,
    ) -> dict:
        """
        Fetch current unsettled offers. GET /edge/rest/v2/offers.
        status: 'open', 'matched', 'open,matched', etc.
        """
        params = {"per-page": per_page, "offset": offset}
        if status:
            params["status"] = status
        return self._request("GET", OFFERS_ENDPOINT, params=params)


def add_ticks_to_odds(odds: float, ticks: int, side: str = "back") -> float:
    """
    Add ticks to decimal odds for Phase 1 "discount" (Back at higher price).
    Uses simple tick steps: 1.01-2.0: 0.01, 2.0-3.0: 0.02, 3.0-4.0: 0.05, 4.0-6.0: 0.1, 6.0-10.0: 0.2.
    Matchbook will auto-round to valid ladder values.
    For Back: we want HIGHER odds (e.g. 2.0 -> 2.04 for 2 ticks).
    """
    if odds < 1.01:
        return odds
    tick_size = 0.01
    if odds >= 2.0 and odds < 3.0:
        tick_size = 0.02
    elif odds >= 3.0 and odds < 4.0:
        tick_size = 0.05
    elif odds >= 4.0 and odds < 6.0:
        tick_size = 0.1
    elif odds >= 6.0:
        tick_size = 0.2

    if side == "back":
        return round(odds + ticks * tick_size, 2)
    else:
        return round(odds - ticks * tick_size, 2)


def lay_liability(lay_stake: float, lay_odds: float) -> float:
    """
    Formula 2: Lay Liability = Lay_Stake * (Lay_Odds - 1)
    The amount at risk if the selection wins.
    """
    return lay_stake * (lay_odds - 1)


def greening_up_lay_stake(back_stake: float, back_odds: float, lay_odds: float) -> float:
    """
    Formula 1: Lay_Stake = (Back_Stake * Back_Odds) / Lay_Odds
    Guarantees equal profit across all outcomes when Lay is matched.
    """
    if lay_odds <= 0:
        return 0.0
    return (back_stake * back_odds) / lay_odds
