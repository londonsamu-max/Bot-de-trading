"""
SpotGamma Dashboard Client

Extracts GEX, DEX, VEX, CHEX, HIRO, key levels, and TRACE data
from the SpotGamma dashboard by consuming the same internal API
endpoints used by the web frontend.

Requires: Active SpotGamma subscription (Alpha recommended for HIRO + TRACE).

IMPORTANT: The user must discover the exact API endpoints by opening
DevTools (F12 > Network tab) on dashboard.spotgamma.com and capturing
the requests. The endpoints below are templates that need to be updated
with the real URLs.
"""

import time
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import requests

logger = logging.getLogger(__name__)


@dataclass
class KeyLevels:
    """SpotGamma key structural levels."""
    put_wall: float = 0.0
    call_wall: float = 0.0
    gamma_flip: float = 0.0
    volatility_trigger: float = 0.0
    timestamp: Optional[datetime] = None


@dataclass
class GreekExposure:
    """Greek exposure data by strike."""
    gex_by_strike: dict = field(default_factory=dict)  # strike -> net gamma
    dex_by_strike: dict = field(default_factory=dict)  # strike -> net delta
    vex_by_strike: dict = field(default_factory=dict)  # strike -> net vanna
    chex_by_strike: dict = field(default_factory=dict)  # strike -> net charm
    net_gex: float = 0.0
    net_dex: float = 0.0
    net_vex: float = 0.0
    net_chex: float = 0.0
    timestamp: Optional[datetime] = None


@dataclass
class HiroData:
    """HIRO (Hedging Impact Real-Time Options) data."""
    cumulative_delta: float = 0.0
    flow_direction: float = 0.0  # positive = dealers buying, negative = selling
    intensity: float = 0.0  # magnitude of hedging flows
    timestamp: Optional[datetime] = None


@dataclass
class TraceData:
    """TRACE heatmap data for SPX."""
    gamma_pressure: dict = field(default_factory=dict)  # strike -> pressure value
    delta_pressure: dict = field(default_factory=dict)
    charm_pressure: dict = field(default_factory=dict)
    timestamp: Optional[datetime] = None


@dataclass
class GammaSnapshot:
    """Complete SpotGamma data snapshot for one ticker."""
    ticker: str = ""
    key_levels: KeyLevels = field(default_factory=KeyLevels)
    greeks: GreekExposure = field(default_factory=GreekExposure)
    hiro: Optional[HiroData] = None
    trace: Optional[TraceData] = None
    regime: str = "unknown"  # "positive" or "negative"
    timestamp: Optional[datetime] = None


class SpotGammaClient:
    """
    Client for extracting data from SpotGamma's dashboard.

    The dashboard at dashboard.spotgamma.com is a SPA that makes
    fetch requests to internal API endpoints. This client replicates
    those same requests using the user's authenticated session.

    Usage:
        client = SpotGammaClient(email="...", password="...")
        client.authenticate()
        snapshot = client.get_snapshot("QQQ")
    """

    # ---------------------------------------------------------------
    # ENDPOINTS: These must be discovered by the user via DevTools.
    # Open F12 > Network tab on dashboard.spotgamma.com, navigate
    # through the dashboard, and capture the URLs for each data type.
    #
    # Replace the placeholders below with the real endpoints.
    # ---------------------------------------------------------------
    ENDPOINTS = {
        "auth": "/api/auth/login",
        "key_levels": "/api/levels/{ticker}",
        "greek_exposure": "/api/exposure/{ticker}",
        "hiro": "/api/hiro/{ticker}",
        "trace": "/api/trace",
    }

    def __init__(self, email: str, password: str,
                 base_url: str = "https://dashboard.spotgamma.com",
                 cache_ttl: int = 300):
        self.email = email
        self.password = password
        self.base_url = base_url.rstrip("/")
        self.cache_ttl = cache_ttl
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Referer": f"{self.base_url}/",
        })
        self._token: Optional[str] = None
        self._cache: dict[str, tuple[float, object]] = {}
        self._authenticated = False

    def authenticate(self) -> bool:
        """
        Authenticate with SpotGamma dashboard.

        NOTE: The exact auth flow depends on SpotGamma's implementation.
        Common patterns:
        1. POST email/password -> receive JWT token
        2. POST email/password -> receive session cookie
        3. OAuth flow with redirect

        The user should check DevTools > Network tab during login
        to see the exact request format.
        """
        url = f"{self.base_url}{self.ENDPOINTS['auth']}"
        payload = {"email": self.email, "password": self.password}

        try:
            resp = self.session.post(url, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            # Try common token patterns
            token = (
                data.get("token")
                or data.get("access_token")
                or data.get("data", {}).get("token")
            )
            if token:
                self._token = token
                self.session.headers["Authorization"] = f"Bearer {token}"

            self._authenticated = True
            logger.info("SpotGamma authentication successful")
            return True

        except requests.RequestException as e:
            logger.error(f"SpotGamma authentication failed: {e}")
            logger.error(
                "Please verify the auth endpoint by checking DevTools "
                "during login at dashboard.spotgamma.com"
            )
            return False

    def _get_cached(self, cache_key: str):
        """Return cached data if still valid, else None."""
        if cache_key in self._cache:
            ts, data = self._cache[cache_key]
            if time.time() - ts < self.cache_ttl:
                return data
        return None

    def _set_cache(self, cache_key: str, data):
        self._cache[cache_key] = (time.time(), data)

    def _request(self, endpoint_key: str, ticker: str = "") -> Optional[dict]:
        """Make authenticated request to SpotGamma API."""
        if not self._authenticated:
            logger.error("Not authenticated. Call authenticate() first.")
            return None

        path = self.ENDPOINTS[endpoint_key].format(ticker=ticker)
        url = f"{self.base_url}{path}"

        cache_key = f"{endpoint_key}:{ticker}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        try:
            resp = self.session.get(url, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            self._set_cache(cache_key, data)
            return data
        except requests.RequestException as e:
            logger.error(f"SpotGamma request failed [{endpoint_key}]: {e}")
            return None

    def get_key_levels(self, ticker: str) -> KeyLevels:
        """Fetch put wall, call wall, gamma flip, volatility trigger."""
        data = self._request("key_levels", ticker)
        if not data:
            return KeyLevels()

        # NOTE: Actual field names depend on SpotGamma's response format.
        # The user must inspect the JSON response to map fields correctly.
        return KeyLevels(
            put_wall=float(data.get("put_wall", data.get("putWall", 0))),
            call_wall=float(data.get("call_wall", data.get("callWall", 0))),
            gamma_flip=float(data.get("gamma_flip", data.get("gammaFlip",
                             data.get("zero_gamma", 0)))),
            volatility_trigger=float(data.get("volatility_trigger",
                                     data.get("volTrigger", 0))),
            timestamp=datetime.utcnow(),
        )

    def get_greek_exposure(self, ticker: str) -> GreekExposure:
        """Fetch GEX, DEX, VEX, CHEX by strike."""
        data = self._request("greek_exposure", ticker)
        if not data:
            return GreekExposure()

        # NOTE: The response structure varies. Common patterns:
        # { "strikes": [...], "gex": [...], "dex": [...], "vex": [...], "chex": [...] }
        # or: [ { "strike": 100, "gex": 0.5, "dex": 0.3, ... }, ... ]
        #
        # The user must inspect the actual response to set up parsing.

        exposure = GreekExposure(timestamp=datetime.utcnow())

        if isinstance(data, list):
            for row in data:
                strike = float(row.get("strike", 0))
                if strike > 0:
                    exposure.gex_by_strike[strike] = float(row.get("gex", 0))
                    exposure.dex_by_strike[strike] = float(row.get("dex", 0))
                    exposure.vex_by_strike[strike] = float(row.get("vex", 0))
                    exposure.chex_by_strike[strike] = float(row.get("chex", 0))
        elif isinstance(data, dict):
            strikes = data.get("strikes", [])
            for i, strike in enumerate(strikes):
                s = float(strike)
                exposure.gex_by_strike[s] = float(data.get("gex", [0])[i]) if i < len(data.get("gex", [])) else 0
                exposure.dex_by_strike[s] = float(data.get("dex", [0])[i]) if i < len(data.get("dex", [])) else 0
                exposure.vex_by_strike[s] = float(data.get("vex", [0])[i]) if i < len(data.get("vex", [])) else 0
                exposure.chex_by_strike[s] = float(data.get("chex", [0])[i]) if i < len(data.get("chex", [])) else 0

        exposure.net_gex = sum(exposure.gex_by_strike.values())
        exposure.net_dex = sum(exposure.dex_by_strike.values())
        exposure.net_vex = sum(exposure.vex_by_strike.values())
        exposure.net_chex = sum(exposure.chex_by_strike.values())

        return exposure

    def get_hiro(self, ticker: str) -> HiroData:
        """Fetch HIRO hedging flow data."""
        data = self._request("hiro", ticker)
        if not data:
            return HiroData()

        return HiroData(
            cumulative_delta=float(data.get("cumulative_delta",
                                   data.get("cumulativeDelta", 0))),
            flow_direction=float(data.get("flow_direction",
                                 data.get("flowDirection", 0))),
            intensity=float(data.get("intensity", 0)),
            timestamp=datetime.utcnow(),
        )

    def get_trace(self) -> TraceData:
        """Fetch TRACE heatmap data (SPX only)."""
        data = self._request("trace")
        if not data:
            return TraceData()

        return TraceData(
            gamma_pressure=data.get("gamma_pressure",
                           data.get("gammaPressure", {})),
            delta_pressure=data.get("delta_pressure",
                           data.get("deltaPressure", {})),
            charm_pressure=data.get("charm_pressure",
                           data.get("charmPressure", {})),
            timestamp=datetime.utcnow(),
        )

    def get_snapshot(self, ticker: str) -> GammaSnapshot:
        """Get complete SpotGamma data snapshot for a ticker."""
        key_levels = self.get_key_levels(ticker)
        greeks = self.get_greek_exposure(ticker)
        hiro = self.get_hiro(ticker)
        trace = self.get_trace() if ticker in ("SPX", "SPY", "QQQ") else None

        regime = "positive" if greeks.net_gex > 0 else "negative"

        return GammaSnapshot(
            ticker=ticker,
            key_levels=key_levels,
            greeks=greeks,
            hiro=hiro,
            trace=trace,
            regime=regime,
            timestamp=datetime.utcnow(),
        )

    def clear_cache(self):
        """Clear all cached data."""
        self._cache.clear()
