"""
Module: capital_adapter
Purpose: Capital.com REST API adapter implementing BaseExchangeAdapter
Location: /opt/tickles/shared/connectors/capital_adapter.py
"""

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional, Dict, Any

import aiohttp

from .base import BaseExchangeAdapter, Candle, Instrument, MarketStatus
from ..utils.config import CANDLE_FETCH_BATCH_SIZE

logger = logging.getLogger(__name__)

# Capital.com API endpoints
DEMO_BASE_URL = "https://demo-api-capital.backend-capital.com/api/v1"
LIVE_BASE_URL = "https://api-capital.backend-capital.com/api/v1"

# Timeframe mapping to Capital.com resolution strings
TIMEFRAME_MAP = {
    "1m": "MINUTE",
    "5m": "MINUTE_5",
    "15m": "MINUTE_15",
    "1h": "HOUR",
    "4h": "HOUR_4",
    "1d": "DAY",
}


class CapitalAdapter(BaseExchangeAdapter):
    """Capital.com REST API adapter.

    Uses direct REST API calls (aiohttp) since CCXT doesn't support Capital.com.
    Auth via email/password + API key -> CST + X-SECURITY-TOKEN headers.

    Usage:
        adapter = CapitalAdapter(environment="demo")
        await adapter.authenticate(email, password, api_key)
        candles = await adapter.fetch_ohlcv("CS.D.EURUSD.CFD.IP", "5m")
    """

    def __init__(self, environment: str = "demo"):
        """Initialize the Capital.com adapter.

        Args:
            environment: 'demo' or 'live'
        """
        self._environment = environment.lower()
        self._base_url = DEMO_BASE_URL if self._environment == "demo" else LIVE_BASE_URL
        self._session: Optional[aiohttp.ClientSession] = None
        self._cst: Optional[str] = None
        self._x_security_token: Optional[str] = None
        self._authenticated = False
        self._lock = asyncio.Lock()
        self._api_key: Optional[str] = None
        self._account_id: Optional[str] = None

    @property
    def name(self) -> str:
        """Get the exchange name/identifier."""
        return "capitalcom"

    @property
    def cst(self) -> Optional[str]:
        """Get the Client Session Token (CST)."""
        return self._cst

    @property
    def security_token(self) -> Optional[str]:
        """Get the X-SECURITY-TOKEN."""
        return self._x_security_token

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create the shared aiohttp session (no auth headers).

        Auth headers (CST, X-SECURITY-TOKEN) are passed per-request via
        ``_auth_headers()`` so that token updates from ``switch_account()``
        take effect immediately without recreating the session.
        """
        if self._session is None or self._session.closed:
            async with self._lock:
                if self._session is None or self._session.closed:
                    headers = {
                        "Content-Type": "application/json",
                        "X-CAP-API-KEY": self._api_key or "",
                    }
                    timeout = aiohttp.ClientTimeout(total=30)
                    self._session = aiohttp.ClientSession(
                        headers=headers, timeout=timeout
                    )
        return self._session

    def _auth_headers(self) -> Dict[str, str]:
        """Return per-request auth headers with the current tokens.

        Using per-request headers avoids the race condition where
        ``switch_account()`` closes a session that another coroutine
        is still using.
        """
        hdrs: Dict[str, str] = {}
        if self._cst:
            hdrs["CST"] = self._cst
        if self._x_security_token:
            hdrs["X-SECURITY-TOKEN"] = self._x_security_token
        return hdrs

    async def authenticate(
        self, email: str, password: str, api_key: str, account_id: Optional[str] = None
    ) -> bool:
        """Authenticate with Capital.com API.

        Args:
            email: Account email
            password: Account password
            api_key: API key from Capital.com
            account_id: Optional account ID to switch to after login

        Returns:
            True if authentication successful

        Raises:
            ConnectionError: if authentication fails after retries
        """
        self._api_key = api_key
        url = f"{self._base_url}/session"
        payload = {"identifier": email, "password": password}

        max_retries = 3
        for attempt in range(max_retries):
            try:
                headers = {
                    "Content-Type": "application/json",
                    "X-CAP-API-KEY": api_key,
                }
                async with aiohttp.ClientSession(headers=headers) as session:
                    async with session.post(
                        url, json=payload, timeout=aiohttp.ClientTimeout(total=30)
                    ) as resp:
                        if resp.status != 200:
                            body = await resp.text()
                            logger.error(
                                "Auth failed (attempt %d/%d): %s - %s",
                                attempt + 1, max_retries, resp.status, body
                            )
                            if attempt < max_retries - 1:
                                await asyncio.sleep(1.5 ** attempt)
                                continue
                            raise ConnectionError(
                                f"Authentication failed: {resp.status}"
                            )

                        self._cst = resp.headers.get("CST")
                        self._x_security_token = resp.headers.get("X-SECURITY-TOKEN")

                        if not self._cst or not self._x_security_token:
                            raise ConnectionError("No auth tokens in response")

                        self._authenticated = True
                        # Ensure the shared session exists (auth headers are
                        # passed per-request via _auth_headers(), so no need
                        # to recreate the session here).
                        await self._get_session()

                        logger.info(
                            "Capital.com authenticated (%s)", self._environment
                        )

                        if account_id:
                            await self.switch_account(account_id)

                        return True

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.warning(
                    "Auth network error (attempt %d/%d): %s",
                    attempt + 1, max_retries, e
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(1.5 ** attempt)
                else:
                    raise ConnectionError(
                        f"Failed to authenticate after {max_retries} attempts"
                    ) from e

        return False

    async def switch_account(self, account_id: str) -> bool:
        """Switch to a specific account ID.

        Args:
            account_id: The ID of the account to switch to.

        Returns:
            True if successful.
        """
        if not self._authenticated:
            raise ConnectionError("Not authenticated. Call authenticate() first.")

        url = f"{self._base_url}/session"
        payload = {"accountId": account_id}

        try:
            session = await self._get_session()
            async with session.put(url, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    if "error.not-different.accountId" in body:
                        logger.info("Already on account %s", account_id)
                        self._account_id = account_id
                        return True
                    logger.error("Failed to switch account to %s: %s", account_id, body)
                    return False

                # Update tokens — per-request headers in _auth_headers()
                # will pick up the new values on the next API call, so no
                # session recreation is needed (avoids race condition).
                self._cst = resp.headers.get("CST") or self._cst
                self._x_security_token = resp.headers.get("X-SECURITY-TOKEN") or self._x_security_token
                self._account_id = account_id

                logger.info("Switched to Capital.com account %s", account_id)
                return True
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) as e:
            logger.error("Error switching account: %s", e)
            return False

    async def fetch_ohlcv(
        self,
        epic: str,
        timeframe: str = "5m",
        since: Optional[datetime] = None,
        limit: int = CANDLE_FETCH_BATCH_SIZE,
    ) -> List[Candle]:
        """Fetch historical OHLCV data from Capital.com.

        Args:
            epic: Market epic identifier (e.g., 'CS.D.EURUSD.CFD.IP')
            timeframe: Candle interval ('1m', '5m', '15m', '1h', '4h', '1d')
            since: Start datetime (UTC). If None, fetches most recent.
            limit: Maximum number of candles to fetch.

        Returns:
            List of Candle objects.

        Raises:
            ConnectionError: if the API is unreachable after retries
            ValueError: if timeframe is invalid
        """
        if not self._authenticated:
            raise ConnectionError("Not authenticated. Call authenticate() first.")

        resolution = TIMEFRAME_MAP.get(timeframe)
        if not resolution:
            raise ValueError(f"Unsupported timeframe: {timeframe}")

        # Calculate date range
        if since:
            from_date = since.strftime("%Y-%m-%dT%H:%M:%S")
        else:
            # Default: last 24 hours for 5m candles
            from_date = (
                datetime.now(timezone.utc)
                .replace(hour=0, minute=0, second=0, microsecond=0)
                .strftime("%Y-%m-%dT%H:%M:%S")
            )

        to_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

        url = f"{self._base_url}/prices/{epic}"
        params = {
            "resolution": resolution,
            "from": from_date,
            "to": to_date,
            "max": min(limit, 5000),  # Capital.com max is 5000
        }

        max_retries = 3
        for attempt in range(max_retries):
            try:
                session = await self._get_session()
                async with session.get(url, params=params, headers=self._auth_headers()) as resp:
                    if resp.status == 401:
                        self._authenticated = False
                        raise ConnectionError(
                            "Session expired. Re-authenticate required."
                        )

                    if resp.status != 200:
                        body = await resp.text()
                        raise ConnectionError(
                            f"API error {resp.status}: {body}"
                        )

                    data = await resp.json()
                    candles = self._parse_price_history(
                        data, epic, timeframe
                    )
                    return candles

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.warning(
                    "Network error fetching OHLCV (attempt %d/%d): %s",
                    attempt + 1, max_retries, e
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(1.5 ** attempt)
                else:
                    raise ConnectionError(
                        f"Failed to reach Capital.com after {max_retries} attempts"
                    ) from e

        return []

    def _parse_price_history(
        self, data: Dict[str, Any], epic: str, timeframe: str
    ) -> List[Candle]:
        """Parse Capital.com price history response into Candle objects.

        Args:
            data: API response with 'prices' array
            epic: Market epic identifier
            timeframe: Timeframe string

        Returns:
            List of Candle objects
        """
        candles = []
        prices = data.get("prices", [])

        for row in prices:
            try:
                ts_str = row.get("openTime", row.get("snapshotTime", ""))
                if not ts_str:
                    continue

                # Parse ISO timestamp
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)

                open_price = Decimal(str(row.get("open", 0)))
                high_price = Decimal(str(row.get("high", 0)))
                low_price = Decimal(str(row.get("low", 0)))
                close_price = Decimal(str(row.get("close", 0)))
                volume = Decimal(str(row.get("volume", 0)))

                hash_input = (
                    f"{epic}:{timeframe}:{ts.isoformat()}:"
                    f"{open_price}:{high_price}:{low_price}:{close_price}:{volume}"
                )
                data_hash = hashlib.sha256(hash_input.encode()).hexdigest()

                quote_volume = volume * close_price if volume and close_price else Decimal("0")

                candle = Candle(
                    instrument_id=epic,
                    timeframe=timeframe,
                    timestamp=ts,
                    open=open_price,
                    high=high_price,
                    low=low_price,
                    close=close_price,
                    volume=volume,
                    quote_volume=quote_volume,
                    trades_count=None,
                    data_source="api",
                    candle_data_hash=data_hash,
                    exchange="capitalcom",
                )
                candles.append(candle)

            except (ValueError, TypeError) as e:
                logger.warning("Failed to parse price row: %s - %s", row, e)
                continue

        return candles

    async def get_market_status(self, epic: str) -> MarketStatus:
        """Get market status for a market epic.

        Args:
            epic: Market epic identifier

        Returns:
            MarketStatus enum value
        """
        if not self._authenticated:
            return MarketStatus.UNKNOWN

        try:
            session = await self._get_session()
            url = f"{self._base_url}/markets/{epic}"
            async with session.get(url, headers=self._auth_headers()) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    dealing_rules = data.get("dealingRules", [])
                    # If there are dealing rules, market is likely open
                    if dealing_rules or data.get("snapshot", {}).get("bid"):
                        return MarketStatus.OPEN
                    return MarketStatus.CLOSED
                return MarketStatus.UNKNOWN
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning("Failed to get market status for %s: %s", epic, e)
            return MarketStatus.UNKNOWN

    async def get_instruments(self) -> List[Instrument]:
        """Search for available instruments on Capital.com.

        Note: Capital.com doesn't have a single endpoint to list all instruments.
        This searches for common CFD categories and returns unique results.

        Returns:
            List of Instrument dataclasses
        """
        if not self._authenticated:
            raise ConnectionError("Not authenticated. Call authenticate() first.")

        search_terms = [
            "EUR", "GBP", "USD", "JPY", "BTC", "ETH", "GOLD",
            "OIL", "NASDAQ", "SP500", "DOW", "DAX", "FTSE",
        ]

        instruments = []
        seen_epics = set()

        for term in search_terms:
            try:
                session = await self._get_session()
                url = f"{self._base_url}/markets"
                async with session.get(url, params={"searchTerm": term}, headers=self._auth_headers()) as resp:
                    if resp.status != 200:
                        continue

                    data = await resp.json()
                    markets = data.get("markets", [])

                    for market in markets:
                        epic = market.get("epic", "")
                        if epic and epic not in seen_epics:
                            seen_epics.add(epic)
                            instrument = self._parse_market_to_instrument(market)
                            if instrument:
                                instruments.append(instrument)

                await asyncio.sleep(0.5)  # Rate limiting

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.warning("Failed to search markets for %s: %s", term, e)
                continue

        logger.info(
            "Loaded %d instruments from Capital.com", len(instruments)
        )
        return instruments

    def _parse_market_to_instrument(
        self, market: Dict[str, Any]
    ) -> Optional[Instrument]:
        """Parse a market response to an Instrument object.

        Args:
            market: Market data from API

        Returns:
            Instrument or None if parsing fails
        """
        try:
            epic = market.get("epic", "")
            name = market.get("instrument", {}).get("name", epic)
            market_type = market.get("instrument", {}).get("type", "CFD")

            # Parse base/quote from epic (e.g., CS.D.EURUSD.CFD.IP -> EUR, USD)
            parts = epic.split(".")
            base_asset = ""
            quote_asset = ""
            if len(parts) >= 3:
                symbol_part = parts[2]
                if len(symbol_part) == 6:
                    base_asset = symbol_part[:3]
                    quote_asset = symbol_part[3:]
                else:
                    base_asset = symbol_part
                    quote_asset = "USD"

            precision = market.get("instrument", {}).get("pip", "0.0001")
            min_amount = market.get("instrument", {}).get("minControlledRisk", "1")

            return Instrument(
                id=epic,
                symbol=name,
                base_asset=base_asset or epic,
                quote_asset=quote_asset or "USD",
                type="cfd",
                precision_price=Decimal(str(precision)),
                precision_amount=Decimal(str(min_amount)),
                min_amount=Decimal(str(min_amount)),
                raw_data=market,
            )
        except (KeyError, TypeError, ValueError) as e:
            logger.warning("Failed to parse market to instrument: %s", e)
            return None

    async def fetch_balance(self) -> Dict[str, Any]:
        """Fetch account balance from Capital.com.

        Returns:
            Dict in CCXT-like format: {'USD': {'free': 100.0, ...}, 'free': {'USD': 100.0}, ...}
        """
        if not self._authenticated:
            raise ConnectionError("Not authenticated. Call authenticate() first.")

        url = f"{self._base_url}/accounts"
        max_retries = 3
        for attempt in range(max_retries):
            try:
                session = await self._get_session()
                async with session.get(url, headers=self._auth_headers()) as resp:
                    if resp.status == 401:
                        self._authenticated = False
                        raise ConnectionError("Session expired. Re-authenticate required.")

                    if resp.status != 200:
                        body = await resp.text()
                        raise ConnectionError(f"API error {resp.status}: {body}")

                    data = await resp.json()
                    accounts = data.get("accounts", [])

                    # Standard CCXT-like balance structure
                    balance: Dict[str, Any] = {"info": data, "free": {}, "used": {}, "total": {}}

                    for acc in accounts:
                        currency = acc.get("currency", "USD")
                        acc_balance = acc.get("balance", {})

                        # Use Decimal for monetary precision, then convert
                        # to float at the boundary for CCXT compatibility.
                        total_dec = Decimal(str(acc_balance.get("balance", 0)))
                        free_dec = Decimal(str(acc_balance.get("available", 0)))
                        used_dec = total_dec - free_dec

                        total = float(total_dec)
                        free = float(free_dec)
                        used = float(used_dec)

                        balance[currency] = {"free": free, "used": used, "total": total}
                        balance["free"][currency] = free
                        balance["used"][currency] = used
                        balance["total"][currency] = total

                    return balance

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.warning("Network error fetching balance (attempt %d/%d): %s", attempt + 1, max_retries, e)
                if attempt < max_retries - 1:
                    await asyncio.sleep(1.5 ** attempt)
                else:
                    raise ConnectionError(f"Failed to reach Capital.com after {max_retries} attempts") from e

        return {"free": {}, "used": {}, "total": {}}

    async def fetch_ticker(self, epic: str) -> Dict[str, Any]:
        """Fetch current ticker (bid/ask/last) for a symbol.

        Args:
            epic: Market epic identifier

        Returns:
            Dict with 'bid', 'ask', 'last', 'spread', 'timestamp'.
        """
        if not self._authenticated:
            raise ConnectionError("Not authenticated. Call authenticate() first.")

        url = f"{self._base_url}/markets/{epic}"
        try:
            session = await self._get_session()
            async with session.get(url, headers=self._auth_headers()) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise ConnectionError(f"API error {resp.status}: {body}")

                data = await resp.json()
                snapshot = data.get("snapshot", {})

                bid = float(snapshot.get("bid", 0))
                ask = float(snapshot.get("offer", 0))
                # Use lastTradedPrice if available; fall back to mid-price.
                last = float(snapshot.get("lastTradedPrice", 0)) or (
                    (bid + ask) / 2 if bid and ask else 0
                )

                return {
                    "symbol": epic,
                    "bid": bid,
                    "ask": ask,
                    "last": last,
                    "spread": ask - bid if ask and bid else 0,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "info": data
                }
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) as e:
            logger.error("Failed to fetch ticker for %s: %s", epic, e)
            raise

    async def fetch_funding_rate(self, epic: str) -> Dict[str, Any]:
        """Fetch current funding or overnight holding rate.

        Args:
            epic: Market epic identifier

        Returns:
            Dict with 'symbol', 'rate', 'next_funding_time'.
        """
        if not self._authenticated:
            raise ConnectionError("Not authenticated. Call authenticate() first.")

        url = f"{self._base_url}/markets/{epic}"
        try:
            session = await self._get_session()
            async with session.get(url, headers=self._auth_headers()) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise ConnectionError(f"API error {resp.status}: {body}")

                data = await resp.json()
                # Capital.com uses 'overnightHolding'
                holding = data.get("instrument", {}).get("overnightHolding", {})

                # Usually expressed as a percentage or absolute value per day
                buy_rate = float(holding.get("buy", 0))
                sell_rate = float(holding.get("sell", 0))

                return {
                    "symbol": epic,
                    "rate": buy_rate,  # Defaulting to buy rate for 'funding rate' equivalent
                    "buy_rate": buy_rate,
                    "sell_rate": sell_rate,
                    "next_funding_time": None,  # Capital.com applies daily at specific time
                    "info": holding
                }
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) as e:
            logger.error("Failed to fetch funding rate for %s: %s", epic, e)
            raise

    async def fetch_sentiment(self, epic: str) -> Dict[str, Any]:
        """Fetch market sentiment (buyers vs sellers).

        Args:
            epic: Market epic identifier

        Returns:
            Dict with 'symbol', 'long_pct', 'short_pct', 'num_buyers', 'num_sellers'.
        """
        if not self._authenticated:
            raise ConnectionError("Not authenticated. Call authenticate() first.")

        url = f"{self._base_url}/markets/{epic}"
        try:
            session = await self._get_session()
            async with session.get(url, headers=self._auth_headers()) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise ConnectionError(f"API error {resp.status}: {body}")

                data = await resp.json()
                snapshot = data.get("snapshot", {})

                # Capital.com often provides sentiment in a separate field
                # or not at all in some environments.
                sentiment = snapshot.get("clientSentiment", {})
                long_pct = float(sentiment.get("long", 50))
                short_pct = float(sentiment.get("short", 50))

                return {
                    "symbol": epic,
                    "long_pct": long_pct,
                    "short_pct": short_pct,
                    "num_buyers": None,
                    "num_sellers": None,
                    "info": sentiment
                }
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) as e:
            logger.error("Failed to fetch sentiment for %s: %s", epic, e)
            raise

    async def fetch_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch currently active/pending orders (working orders)."""
        if not self._authenticated:
            raise ConnectionError("Not authenticated. Call authenticate() first.")

        url = f"{self._base_url}/workingorders"
        try:
            session = await self._get_session()
            async with session.get(url, headers=self._auth_headers()) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise ConnectionError(f"API error {resp.status}: {body}")

                data = await resp.json()
                orders = data.get("workingOrders", [])

                if symbol:
                    orders = [o for o in orders if o.get("epic") == symbol]

                return orders
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) as e:
            logger.error("Failed to fetch open orders: %s", e)
            raise

    async def fetch_closed_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch recently closed/filled orders via activity history."""
        if not self._authenticated:
            raise ConnectionError("Not authenticated. Call authenticate() first.")

        # Capital.com uses /history/activity
        url = f"{self._base_url}/history/activity"
        try:
            session = await self._get_session()
            async with session.get(url, headers=self._auth_headers()) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise ConnectionError(f"API error {resp.status}: {body}")

                data = await resp.json()
                activities = data.get("activities", [])

                # Filter for order-related activities
                orders = [a for a in activities if a.get("type") in ["ORDER", "TRADE"]]

                if symbol:
                    orders = [o for o in orders if o.get("epic") == symbol]

                return orders
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) as e:
            logger.error("Failed to fetch closed orders: %s", e)
            raise

    async def fetch_positions(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch currently open positions."""
        if not self._authenticated:
            raise ConnectionError("Not authenticated. Call authenticate() first.")

        url = f"{self._base_url}/positions"
        try:
            session = await self._get_session()
            async with session.get(url, headers=self._auth_headers()) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise ConnectionError(f"API error {resp.status}: {body}")

                data = await resp.json()
                raw_positions = data.get("positions", [])

                # Standardize to a flatter format for tools/agents
                positions = []
                for p in raw_positions:
                    pos_data = p.get("position", {})
                    mkt_data = p.get("market", {})

                    standard_pos = {
                        "symbol": mkt_data.get("epic"),
                        "instrumentName": mkt_data.get("instrumentName"),
                        "side": pos_data.get("direction"),
                        "size": float(pos_data.get("size", 0)),
                        "entryPrice": float(pos_data.get("level", 0)),
                        "unrealizedPnl": float(pos_data.get("upl", 0)),
                        "currency": pos_data.get("currency"),
                        "leverage": pos_data.get("leverage"),
                        "dealId": pos_data.get("dealId"),
                        "raw": p
                    }

                    if symbol and standard_pos["symbol"] != symbol:
                        continue
                    positions.append(standard_pos)

                return positions
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) as e:
            logger.error("Failed to fetch positions: %s", e)
            raise

    async def fetch_trades(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch trade/transaction history."""
        return await self.fetch_closed_orders(symbol)

    async def get_market_hours(self, epic: str) -> Dict[str, Any]:
        """Get detailed market open/close schedule for a symbol."""
        if not self._authenticated:
            raise ConnectionError("Not authenticated. Call authenticate() first.")

        url = f"{self._base_url}/markets/{epic}"
        try:
            session = await self._get_session()
            async with session.get(url, headers=self._auth_headers()) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise ConnectionError(f"API error {resp.status}: {body}")

                data = await resp.json()
                instrument = data.get("instrument", {})
                snapshot = data.get("snapshot", {})

                # Capital.com provides openingHours
                hours = instrument.get("openingHours", [])

                # Determine market status from the same response instead of
                # making a redundant second API call to get_market_status().
                dealing_rules = data.get("dealingRules", [])
                is_open = bool(dealing_rules or snapshot.get("bid"))

                return {
                    "symbol": epic,
                    "timezone": "UTC",
                    "schedule": hours,
                    "is_open": is_open,
                    "info": instrument
                }
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) as e:
            logger.error("Failed to fetch market hours for %s: %s", epic, e)
            raise

    async def close(self) -> None:
        """Close the aiohttp session and logout."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
            self._authenticated = False
            logger.info("Capital.com session closed")
