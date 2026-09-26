"""Stage 5 — OAuth token boundary (user/channel OAuth, NOT service accounts).

The publisher authenticates as the channel OWNER via Google's OAuth 2.0
authorization-code flow. The operator performs the ONE-TIME browser
consent step outside AYCE (standard Google flow — no browser automation,
no credential harvesting); AYCE stores ONLY the resulting refresh token
and exchanges it for short-lived access tokens.

Credential rules (§9-§11):

- ``AYCE_YT_CLIENT_ID`` / ``AYCE_YT_CLIENT_SECRET`` / ``AYCE_YT_REFRESH_TOKEN``
  come from the environment / git-ignored ``.env`` (existing convention).
- Access tokens are cached in ONE controlled local file
  (``AYCE_YT_TOKEN_FILE``, default ``<data_dir>/publishing/token.json``) —
  never in Git, never in logs, written with restrictive permissions where
  the OS supports it.
- ``ayce publish-auth --auth-code <code>`` exchanges a one-time
  authorization code for the refresh token and stores it (the only
  credential-writing operation; operator-invoked, never automatic).
- Missing/invalid credentials → deterministic ``auth_configuration``
  error with NO upload attempt (§10).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

__all__ = ["DEFAULT_TOKEN_URL", "TokenProvider", "GoogleTokenClient"]

#: Google's OAuth 2.0 token endpoint (the only fixed external URL here).
DEFAULT_TOKEN_URL = "https://oauth2.googleapis.com/token"

#: Urlopen seam (tests substitute a fake; production uses urllib).
URLOpen = Any

_SCOPES = "https://www.googleapis.com/auth/youtube.upload"


class TokenProvider:
    """Refresh-token → access-token boundary with a local token cache."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        token_file: str | Path | None = None,
        token_client: "GoogleTokenClient | None" = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._token_file = Path(token_file) if token_file else None
        self._token_client = token_client or GoogleTokenClient()
        self._cached_access_token: str | None = None

    @classmethod
    def from_config(cls, config, env: dict[str, str] | None = None) -> "TokenProvider":
        """Build from the AYCE_* configuration. Raises ``PublishError``
        (``auth_configuration``) when credentials are absent — a
        deterministic configuration error with NO upload attempt."""
        from .errors import PublishError

        source = env if env is not None else os.environ
        try:
            client_id = config.require("YT_CLIENT_ID", env=source)
            client_secret = config.require("YT_CLIENT_SECRET", env=source)
            refresh_token = config.require("YT_REFRESH_TOKEN", env=source)
        except Exception as exc:  # ConfigError → classified auth configuration gap
            raise PublishError(
                "auth_configuration",
                "YouTube OAuth credentials are not configured; no upload was "
                "attempted (set AYCE_YT_CLIENT_ID / AYCE_YT_CLIENT_SECRET / "
                "AYCE_YT_REFRESH_TOKEN, or run `ayce publish-auth`)",
                details={"configuration_error": str(exc)},
            ) from exc
        token_file_raw = source.get("AYCE_YT_TOKEN_FILE", "").strip()
        token_file = (
            Path(token_file_raw) if token_file_raw
            else config.resolved_data_dir / "publishing" / "token.json"
        )
        return cls(client_id=client_id, client_secret=client_secret,
                   refresh_token=refresh_token, token_file=token_file)

    def get_access_token(self) -> str:
        """A valid access token: memory → token file → refresh exchange.
        Refresh failures are classified (invalid refresh token =
        authentication_error; network = transient_network_error)."""
        if self._cached_access_token:
            return self._cached_access_token
        cached = self._load_cached_token()
        if cached:
            self._cached_access_token = cached
            return cached
        token = self._refresh()
        self._cached_access_token = token
        self._persist_cached_token(token)
        return token

    def invalidate(self) -> None:
        """Drop the cached token (e.g. after a 401) to force a refresh."""
        self._cached_access_token = None

    @property
    def client_id(self) -> str:
        """The OAuth client id (destination identity derivation; NOT a
        secret — client ids are public identifiers)."""
        return self._client_id

    def _refresh(self) -> str:
        from .errors import PublishError, categorize_exception

        try:
            response = self._token_client.exchange_refresh_token(
                client_id=self._client_id,
                client_secret=self._client_secret,
                refresh_token=self._refresh_token,
            )
        except Exception as exc:  # noqa: BLE001 — classified, never bare
            raise PublishError(
                "transient_network_error",
                "token refresh request failed; no upload was attempted",
                details={"category": categorize_exception(exc)},
            ) from exc
        access_token = response.get("access_token")
        if not access_token:
            raise PublishError(
                "authentication_error",
                "token refresh was rejected; the refresh token may be "
                "invalid or revoked (operator action required)",
                details={"error": response.get("error")},
            )
        return access_token

    def _load_cached_token(self) -> str | None:
        if self._token_file is None or not self._token_file.is_file():
            return None
        try:
            data = json.loads(self._token_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return None
        token = data.get("access_token") if isinstance(data, dict) else None
        return token if isinstance(token, str) and token else None

    def _persist_cached_token(self, token: str) -> None:
        if self._token_file is None:
            return
        self._token_file.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"access_token": token}, indent=2) + "\n"
        tmp = self._token_file.with_name(self._token_file.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        # restrictive permissions where the OS supports them (§11)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self._token_file)


class GoogleTokenClient:
    """The REAL token exchange against the official Google OAuth 2.0 token
    endpoint (stdlib HTTP). Parsed to a small dict; secrets flow only
    between the TokenProvider and Google — never into logs."""

    def __init__(self, token_url: str = DEFAULT_TOKEN_URL,
                 urlopen: URLOpen = None) -> None:
        self._token_url = token_url
        self._urlopen = urlopen

    def exchange_refresh_token(self, *, client_id: str, client_secret: str,
                               refresh_token: str) -> dict:
        body = self._form({
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        })
        return self._post(body)

    def exchange_authorization_code(self, *, client_id: str, client_secret: str,
                                    authorization_code: str,
                                    redirect_uri: str) -> dict:
        """One-time exchange of an operator-obtained authorization code for
        the refresh token (used by `ayce publish-auth`)."""
        body = self._form({
            "client_id": client_id,
            "client_secret": client_secret,
            "code": authorization_code,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        })
        return self._post(body)

    @staticmethod
    def authorization_url(client_id: str, redirect_uri: str) -> str:
        """The consent URL the OPERATOR opens in a browser (one-time
        setup; AYCE itself never drives a browser)."""
        import urllib.parse

        query = urllib.parse.urlencode({
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": _SCOPES,
            "access_type": "offline",
            "prompt": "consent",
        })
        return f"https://accounts.google.com/o/oauth2/v2/auth?{query}"

    # -- internals -------------------------------------------------------------

    @staticmethod
    def _form(fields: dict) -> bytes:
        import urllib.parse

        return urllib.parse.urlencode(fields).encode("utf-8")

    def _post(self, body: bytes) -> dict:
        import urllib.request

        request = urllib.request.Request(
            self._token_url, data=body, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        opener = self._urlopen or urllib.request.urlopen
        with opener(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))