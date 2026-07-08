"""
Authentication for the GO DAIKIN Philippine region.

Unlike other regions, PH login is not a direct AWS Cognito call. The app posts
credentials to a server-side "universallogin" Lambda which performs the Cognito
authentication (it holds the app-client secret) and returns bearer tokens plus
the account's userID. We replicate that call and reuse the AccessToken as the
`authorization` header on subsequent API requests.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime as dt, timedelta
import logging

import aiohttp

from .const import LOGIN_BASE_URL

# Refresh a bit before the token actually expires, to absorb clock drift and
# in-flight requests.
EXPIRY_BUFFER = timedelta(minutes=5)

_LOGGER = logging.getLogger(__name__)


class AuthError(Exception):
    """Raised when authentication fails (bad credentials or unexpected response)."""


@dataclass
class Session:
    """A logged-in GO DAIKIN session."""

    access_token: str
    user_id: str
    expires_at: dt


class AuthClient:
    """Manages GO DAIKIN PH login and access-token lifetime."""

    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password

        self.session = aiohttp.ClientSession()
        self._login_lock = asyncio.Semaphore(1)
        self._session_state: Session | None = None

    @property
    def user_id(self) -> str | None:
        """The account userID, available after the first successful login."""
        return self._session_state.user_id if self._session_state else None

    async def async_get_token(self) -> str:
        """Return a valid AccessToken, logging in (or re-logging in) as needed."""
        async with self._login_lock:
            state = self._session_state
            if state is None or state.expires_at <= dt.now() + EXPIRY_BUFFER:
                self._session_state = await self._login()
            return self._session_state.access_token

    async def async_login(self) -> Session:
        """Force a fresh login (used to validate credentials during setup)."""
        async with self._login_lock:
            self._session_state = await self._login()
            return self._session_state

    async def _login(self) -> Session:
        _LOGGER.debug("Logging in to GO DAIKIN (PH) for %s", self.username)
        try:
            async with self.session.post(
                f"{LOGIN_BASE_URL}universallogin",
                json={
                    "requestData": {
                        "username": self.username,
                        "password": self.password,
                    }
                },
            ) as resp:
                if resp.status != 200:
                    raise AuthError(f"universallogin returned HTTP {resp.status}")
                data = await resp.json()
        except aiohttp.ClientError as err:
            raise AuthError(f"Login request failed: {err}") from err

        access_token = data.get("AccessToken")
        user_id = data.get("userID")
        expires_in = data.get("ExpiresIn")
        if not access_token or not user_id:
            # Bad credentials come back as a 200 with a message and no tokens.
            message = data.get("message", "no AccessToken/userID in response")
            raise AuthError(f"Login failed: {message}")

        expires_at = dt.now() + timedelta(seconds=int(expires_in or 28800))
        _LOGGER.debug(
            "GO DAIKIN (PH) login successful, token expires at %s",
            expires_at.isoformat(),
        )
        return Session(
            access_token=access_token,
            user_id=user_id,
            expires_at=expires_at,
        )
