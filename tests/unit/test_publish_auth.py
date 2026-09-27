"""Publish-auth OAuth redirect tests (WEB-client loopback; NO network).

Covers Phase 0.7B OAuth fix: the production default redirect is the
documented WEB-client loopback URI (the obsolete out-of-band flow is gone),
``--redirect-uri`` is honored, the consent URL carries the selected redirect
URI, the authorization-code exchange sends the SAME redirect URI, and the
CLI wiring uses the shared oauth constant. The consent/exchange paths are
exercised with fakes only — no Google contact, no real credentials.
"""
import io
import json
import urllib.parse

from ayce.cli import _build_parser
from ayce.publishing import GoogleTokenClient
from ayce.publishing.oauth import DEFAULT_REDIRECT_URI

OOB = "urn:ietf:wg:oauth:2.0:oob"
CLIENT_ID = "test-client-id.apps.googleusercontent.com"
CLIENT_SECRET = "test-client-secret"


class _FakeUrlopen:
    """Captures the POSTed token request; returns a canned Google response."""

    def __init__(self, payload=None):
        self.requests = []
        self._payload = payload if payload is not None else {
            "access_token": "fake-access-token",
            "refresh_token": "fake-refresh-token",
            "expires_in": 3600,
        }

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        return io.BytesIO(json.dumps(self._payload).encode("utf-8"))


def _form_fields(request) -> dict:
    return dict(urllib.parse.parse_qsl(
        request.data.decode("utf-8"), keep_blank_values=True))


def _url_query(url: str) -> dict:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)


# ---- default redirect behavior --------------------------------------------------


def test_default_redirect_uri_is_the_web_loopback_not_oob():
    args = _build_parser().parse_args(["publish-auth"])
    assert args.redirect_uri == DEFAULT_REDIRECT_URI
    assert args.redirect_uri != OOB
    assert "oob" not in args.redirect_uri
    assert args.redirect_uri.startswith("http://localhost")


def test_cli_default_stays_in_sync_with_oauth_constant():
    # drift guard: the CLI literal and the oauth constant must not diverge
    args = _build_parser().parse_args(["publish-auth"])
    assert args.redirect_uri == DEFAULT_REDIRECT_URI == "http://localhost:8080"


def test_explicit_redirect_uri_is_honored():
    args = _build_parser().parse_args(
        ["publish-auth", "--redirect-uri", "http://localhost:9999"])
    assert args.redirect_uri == "http://localhost:9999"


# ---- consent URL carries the selected redirect URI -------------------------------


def test_authorization_url_contains_default_redirect():
    url = GoogleTokenClient.authorization_url(CLIENT_ID, DEFAULT_REDIRECT_URI)
    assert _url_query(url)["redirect_uri"] == [DEFAULT_REDIRECT_URI]
    assert _url_query(url)["response_type"] == ["code"]
    assert _url_query(url)["access_type"] == ["offline"]
    assert "oob" not in url


def test_authorization_url_contains_explicit_redirect():
    url = GoogleTokenClient.authorization_url(CLIENT_ID, "http://localhost:9999")
    assert _url_query(url)["redirect_uri"] == ["http://localhost:9999"]


# ---- authorization-code exchange sends the SAME redirect URI ---------------------


def test_exchange_sends_default_redirect_uri():
    fake = _FakeUrlopen()
    client = GoogleTokenClient(urlopen=fake)
    client.exchange_authorization_code(
        client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
        authorization_code="abc", redirect_uri=DEFAULT_REDIRECT_URI)
    fields = _form_fields(fake.requests[0])
    assert fields["redirect_uri"] == DEFAULT_REDIRECT_URI
    assert fields["code"] == "abc"
    assert fields["grant_type"] == "authorization_code"
    assert fields["client_id"] == CLIENT_ID
    assert fields["client_secret"] == CLIENT_SECRET


def test_exchange_sends_explicit_redirect_uri():
    fake = _FakeUrlopen()
    client = GoogleTokenClient(urlopen=fake)
    client.exchange_authorization_code(
        client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
        authorization_code="abc", redirect_uri="http://localhost:9999")
    assert _form_fields(fake.requests[0])["redirect_uri"] == "http://localhost:9999"


# ---- CLI wiring (fake token client; NO network) ----------------------------------


def _patch_token_client(monkeypatch, fake):
    import ayce.publishing as publishing
    monkeypatch.setattr(publishing, "GoogleTokenClient",
                        lambda: GoogleTokenClient(urlopen=fake))


def test_publish_auth_auth_url_uses_default_redirect(monkeypatch, capsys):
    monkeypatch.setenv("AYCE_YT_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("AYCE_YT_CLIENT_SECRET", CLIENT_SECRET)
    fake = _FakeUrlopen()
    _patch_token_client(monkeypatch, fake)
    from ayce import cli

    rc = cli.main(["publish-auth", "--auth-url"])
    out = capsys.readouterr().out
    assert rc == 0
    url_lines = [l for l in out.splitlines() if l.startswith("https://")]
    assert len(url_lines) == 1
    assert _url_query(url_lines[0])["redirect_uri"] == [DEFAULT_REDIRECT_URI]
    assert fake.requests == []  # the auth-url step never performs an exchange


def test_publish_auth_exchange_uses_default_redirect(monkeypatch, capsys):
    monkeypatch.setenv("AYCE_YT_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("AYCE_YT_CLIENT_SECRET", CLIENT_SECRET)
    fake = _FakeUrlopen()
    _patch_token_client(monkeypatch, fake)
    from ayce import cli

    rc = cli.main(["publish-auth", "--auth-code", "abc"])
    out = capsys.readouterr().out
    assert rc == 0
    assert _form_fields(fake.requests[0])["redirect_uri"] == DEFAULT_REDIRECT_URI
    assert "fake-refresh-token" in out  # operator stores it in the git-ignored .env


def test_publish_auth_exchange_uses_explicit_redirect(monkeypatch, capsys):
    monkeypatch.setenv("AYCE_YT_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("AYCE_YT_CLIENT_SECRET", CLIENT_SECRET)
    fake = _FakeUrlopen()
    _patch_token_client(monkeypatch, fake)
    from ayce import cli

    rc = cli.main(["publish-auth", "--auth-code", "abc",
                   "--redirect-uri", "http://localhost:9999"])
    assert rc == 0
    assert _form_fields(fake.requests[0])["redirect_uri"] == "http://localhost:9999"