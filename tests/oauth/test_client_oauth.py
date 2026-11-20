"""Client OAuth against an independent authorization server and protected resource."""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
from yarl import URL

from aiohttp_tiny_mcp import Client
from aiohttp_tiny_mcp.client.callback import OAuthCallback
from aiohttp_tiny_mcp.client.loopback import LoopbackCallback
from aiohttp_tiny_mcp.client.oauth import (
    AuthorizationRequired,
    OAuth,
    OAuthError,
    RegistrationRequired,
)
from aiohttp_tiny_mcp.client.oauth_helpers import bearer_challenge
from tests.oauth.helpers import ADAPTER

CALLBACK = "http://127.0.0.1:8765/callback"


def oauth(server, **kwargs):
    config = dict(
        issuer=server.issuer,
        client_id="native-client",
        redirect_uri=CALLBACK,
        allow_http=True,
        load=server.load,
        save=server.save,
    )
    config.update(kwargs)
    return OAuth(server.resource, **config)


def token_calls(server):
    return [entry for entry in server.calls if entry[0] == "token"]


async def test_login_mcp_and_restore(authority):
    async with oauth(authority) as auth:
        await auth.login(authorize=authority.browser)
        assert await auth.token() == authority.current
        async with Client(authority.resource, ADAPTER, auth=auth) as client:
            await client.initialize()
            result = await client.call_tool("private", {})
            assert result.content[0].text == "private result"
        async with Client(authority.resource, ADAPTER, auth=auth) as client:
            await client.initialize()
    async with oauth(authority) as restored:
        assert await restored.token() == authority.current
    assert len(token_calls(authority)) == 1
    assert authority.saved["issuer"] == authority.issuer
    assert authority.saved["resource"] == authority.resource
    for kind, _, headers in authority.calls:
        if kind in {"metadata", "resource_metadata", "token"}:
            assert "Authorization" not in headers
            assert "Cookie" not in headers


async def test_no_implicit_login(authority):
    async with oauth(authority) as auth:
        async with Client(authority.resource, ADAPTER, auth=auth) as client:
            with pytest.raises(AuthorizationRequired) as error:
                await client.initialize()
        assert error.value.scopes == ("read",)
    assert not token_calls(authority)
    assert not authority.codes


@pytest.mark.parametrize(
    "field,value", [("state", "wrong"), ("iss", "https://other.example"), ("code", "")]
)
async def test_invalid_callback_never_sends_code(authority, field, value):
    async with oauth(authority) as auth:
        attempt = await auth.begin()
        callback = URL(await authority.browser(attempt.url)).update_query({field: value})
        with pytest.raises(OAuthError):
            await auth.complete(attempt, str(callback))
        with pytest.raises(OAuthError, match="consumed"):
            await auth.complete(attempt, str(callback))
    assert not token_calls(authority)


async def test_duplicate_state_and_foreign_callback(authority):
    async with oauth(authority) as auth:
        attempt = await auth.begin()
        callback = await authority.browser(attempt.url)
        with pytest.raises(OAuthError, match="Duplicate"):
            await auth.complete(attempt, callback + "&state=again")
        attempt = await auth.begin()
        callback = URL(await authority.browser(attempt.url)).with_path("/wrong", keep_query=True)
        with pytest.raises(OAuthError, match="URI"):
            await auth.complete(attempt, str(callback))
    assert not token_calls(authority)


@pytest.mark.parametrize(
    "advertised,emitted,success", [(True, False, False), (False, False, True), (False, True, True)]
)
async def test_issuer_response_rules(authority, advertised, emitted, success):
    authority.metadata_changes["authorization_response_iss_parameter_supported"] = advertised
    authority.issuer_response = emitted
    async with oauth(authority) as auth:
        if success:
            await auth.login(authorize=authority.browser)
        else:
            with pytest.raises(OAuthError, match="issuer"):
                await auth.login(authorize=authority.browser)


@pytest.mark.parametrize(
    "target,changes",
    [
        ("metadata_changes", {"issuer": "https://other.example"}),
        ("metadata_changes", {"code_challenge_methods_supported": []}),
        ("resource_changes", {"resource": "https://other.example/mcp"}),
        ("resource_changes", {"authorization_servers": ["https://other.example"]}),
    ],
)
async def test_discovery_rejects_mismatches(authority, target, changes):
    getattr(authority, target).update(changes)
    async with oauth(authority) as auth:
        with pytest.raises(OAuthError):
            await auth.begin()
    assert not token_calls(authority)


async def test_well_known_and_oidc_discovery(authority):
    authority.challenge = False
    authority.oidc = True
    async with oauth(authority) as auth:
        await auth.login(authorize=authority.browser)
    paths = [path for kind, path, _ in authority.calls if kind in {"metadata", "resource_metadata"}]
    assert "/.well-known/oauth-protected-resource/mcp" in paths
    assert "/.well-known/openid-configuration/tenant" in paths


async def test_cimd_and_dcr_fallback(authority):
    async with oauth(
        authority, client_id=None, issuer=None, metadata_url="https://app.example/client.json"
    ) as auth:
        await auth.login(authorize=authority.browser)
        assert authority.saved["client_id"] == "https://app.example/client.json"
    authority.saved = None
    authority.metadata_changes["client_id_metadata_document_supported"] = False
    async with oauth(
        authority, client_id=None, metadata_url="https://app.example/client.json", registration={}
    ) as auth:
        await auth.login(authorize=authority.browser)
    assert authority.saved["client_id"] == "dynamic-client"
    assert authority.registered[0]["application_type"] == "native"
    assert authority.registered[0]["redirect_uris"] == [CALLBACK]


async def test_registration_required(authority):
    async with oauth(authority, client_id=None) as auth:
        with pytest.raises(RegistrationRequired):
            await auth.begin()


@pytest.mark.parametrize("method", ["client_secret_basic", "client_secret_post"])
async def test_confidential_client_auth(authority, method):
    async with oauth(
        authority, client_secret="private:secret", token_endpoint_auth_method=method
    ) as auth:
        await auth.login(authorize=authority.browser)
    _, form, headers = token_calls(authority)[0]
    if method == "client_secret_basic":
        assert headers["Authorization"].startswith("Basic ")
        assert "client_secret" not in form
    else:
        assert "Authorization" not in headers
        assert form["client_secret"] == "private:secret"


async def test_concurrent_refresh_and_rotation(authority):
    async with oauth(authority) as auth:
        await auth.login(authorize=authority.browser)
    previous = authority.saved["tokens"]["refresh_token"]
    authority.saved["expires_at"] = 0
    async with oauth(authority) as auth:
        results = await asyncio.gather(*(auth.token() for _ in range(12)))
        assert len(set(results)) == 1
    assert len(token_calls(authority)) == 2
    assert authority.saved["tokens"]["refresh_token"] != previous
    assert previous not in authority.refresh


async def test_refresh_response_without_new_refresh_token(authority):
    async with oauth(authority) as auth:
        await auth.login(authorize=authority.browser)
    previous = authority.saved["tokens"]["refresh_token"]
    authority.saved["expires_at"] = 0
    authority.return_refresh = False
    async with oauth(authority) as auth:
        await auth.token()
    assert authority.saved["tokens"]["refresh_token"] == previous


async def test_invalid_grant_requires_login_without_retry(authority):
    async with oauth(authority) as auth:
        await auth.login(authorize=authority.browser)
    authority.saved["expires_at"] = 0
    authority.token_error = True
    async with oauth(authority) as auth:
        for _ in range(2):
            with pytest.raises(AuthorizationRequired) as error:
                await auth.token()
            assert "SECRET" not in str(error.value)
    assert len(token_calls(authority)) == 2
    assert authority.saved["tokens"] == {}


async def test_missing_refresh_requires_login(authority):
    authority.return_refresh = False
    async with oauth(authority) as auth:
        await auth.login(authorize=authority.browser)
    authority.saved["expires_at"] = 0
    async with oauth(authority) as auth:
        with pytest.raises(AuthorizationRequired):
            await auth.token()
    assert len(token_calls(authority)) == 1


@pytest.mark.parametrize("binding", ["resource", "issuer", "client_id"])
async def test_storage_binding(authority, binding):
    async with oauth(authority) as auth:
        await auth.login(authorize=authority.browser)
    authority.saved[binding] = "foreign"
    async with oauth(authority) as auth:
        with pytest.raises(OAuthError, match="(another|bindings)"):
            await auth.token()
        # A failed load must not silently become an empty credential store.
        with pytest.raises(OAuthError):
            await auth.begin()
    assert len(token_calls(authority)) == 1


async def test_load_and_save_errors_propagate(authority):
    async def fail_load():
        raise OSError("storage offline")

    async with oauth(authority, load=fail_load) as auth:
        with pytest.raises(OSError):
            await auth.begin()
        with pytest.raises(OSError):
            await auth.begin()
    assert not token_calls(authority)

    async def fail_tokens(record):
        if record["tokens"]:
            raise OSError("storage offline")
        await authority.save(record)

    async with oauth(authority, save=fail_tokens) as auth:
        with pytest.raises(OSError):
            await auth.login(authorize=authority.browser)
        with pytest.raises(OAuthError, match="not saved"):
            await auth.token()


async def test_http_paths_refresh_and_step_up(authority):
    async with oauth(authority) as auth:
        await auth.login(authorize=authority.browser)
        authority.current = "revoked"
        async with Client(authority.resource, ADAPTER, auth=auth) as client:
            await client.initialize()
            await client.send_notification(
                {"jsonrpc": "2.0", "method": "notifications/ping"}, method="notifications/ping"
            )
            await client.reply({"jsonrpc": "2.0", "id": 1, "result": {}})
            frames = [frame async for frame in client.stream_notifications()]
            assert frames[0]["method"] == "changed"
            authority.forbidden = True
            with pytest.raises(AuthorizationRequired) as error:
                await client.initialize()
            assert set(error.value.scopes) == {"read", "write"}
    assert len(token_calls(authority)) == 2


async def test_repeated_401_is_bounded(authority):
    async with oauth(authority) as auth:
        await auth.login(authorize=authority.browser)
        authority.reject = True
        async with Client(authority.resource, ADAPTER, auth=auth) as client:
            with pytest.raises(AuthorizationRequired):
                await client.initialize()
    assert len(token_calls(authority)) == 2
    assert len([entry for entry in authority.calls if entry[:2] == ("mcp", "POST")]) == 2


async def test_mcp_redirect_is_not_followed(authority):
    async with oauth(authority) as auth:
        await auth.login(authorize=authority.browser)
        authority.redirect = True
        async with Client(authority.resource, ADAPTER, auth=auth) as client:
            with pytest.raises(OAuthError, match="redirect"):
                await client.initialize()


async def test_session_conflict_and_ownership(authority):
    async with oauth(authority) as auth:
        async with ClientSession(headers={"Authorization": "Bearer unrelated"}) as session:
            with pytest.raises(ValueError, match="combined"):
                async with Client(authority.resource, ADAPTER, session=session, auth=auth):
                    pass
            assert not session.closed
        with pytest.raises(ValueError, match="another MCP"):
            Client(authority.resource + "/other", ADAPTER, auth=auth)


async def test_cancel_and_timeout(authority):
    async with oauth(authority, login_timeout=0.01) as auth:
        attempt = await auth.begin()
        auth.cancel(attempt)
        with pytest.raises(OAuthError, match="consumed"):
            await auth.complete(attempt, CALLBACK)

        async def never(url):
            await asyncio.Event().wait()

        with pytest.raises(asyncio.TimeoutError):
            await auth.login(authorize=never)
    assert not token_calls(authority)


@pytest.mark.parametrize("fail_save", [False, True])
async def test_custom_success_html_after_token_save(authority, fail_save):
    html = '<!doctype html><title>Signed in</title><p id="custom">Return to Example.</p>'
    response_task = None
    callback_received = asyncio.Event()
    release_save = asyncio.Event()

    async def save(record):
        if record["tokens"]:
            callback_received.set()
            await release_save.wait()
            if fail_save:
                raise OSError("storage unavailable")
        await authority.save(record)

    async with ClientSession() as browser:

        async def open_browser(url):
            nonlocal response_task
            callback = await authority.browser(url)

            async def receive():
                async with browser.get(callback) as response:
                    return response.status, await response.text(), dict(response.headers)

            response_task = asyncio.create_task(receive())

        async with LoopbackCallback(open_browser=open_browser, success_html=html) as receiver:
            async with oauth(authority, redirect_uri=receiver.redirect_uri, save=save) as auth:
                login = asyncio.create_task(receiver.login(auth))
                await asyncio.wait_for(callback_received.wait(), timeout=5)
                assert response_task is not None and not response_task.done()
                release_save.set()
                if fail_save:
                    with pytest.raises(OSError):
                        await login
                else:
                    await login
                status, body, headers = await response_task
                assert status == (400 if fail_save else 200)
                assert (body == html) is not fail_save
                assert "access_token" not in body
                assert headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    "headers,expected",
    [
        (
            [
                'Basic realm="other", Bearer resource_metadata="https://mcp.example/meta", '
                'scope="read write"'
            ],
            {"resource_metadata": "https://mcp.example/meta", "scope": "read write"},
        ),
        (['Bearer realm="a,b", error="invalid_token"'], {"realm": "a,b", "error": "invalid_token"}),
        (["Basic realm=x"], {}),
    ],
)
def test_challenge_parser(headers, expected):
    assert bearer_challenge(headers) == expected


def test_https_and_configuration():
    with pytest.raises(OAuthError):
        OAuth("http://example.com/mcp", redirect_uri=CALLBACK, allow_http=True)
    with pytest.raises(ValueError, match="issuer"):
        OAuth("https://example.com/mcp", client_id="client", redirect_uri=CALLBACK)
    with pytest.raises(ValueError, match="both"):
        OAuth("https://example.com/mcp", redirect_uri=CALLBACK, load=lambda: None)


async def test_remote_callback_behind_proxy(authority):
    public_uri = "https://app.example/oauth/callback"
    html = "<!doctype html><h1>Signed in to Example</h1>"
    async with oauth(authority, redirect_uri=public_uri) as auth:
        callback = OAuthCallback(auth, success_html=html)
        app = web.Application()
        callback.setup(app)
        async with TestServer(app) as internal, ClientSession() as browser:
            attempt = await callback.begin()
            target = URL(await authority.browser(attempt.url))
            assert str(target.with_query(None)) == public_uri
            # The reverse proxy forwards the external path/query to the internal listener.
            async with browser.get(internal.make_url(target.path_qs)) as response:
                assert response.status == 200
                assert await response.text() == html
            await callback.wait()
            assert await auth.token() == authority.current
            async with browser.get(internal.make_url(target.path_qs)) as replay:
                assert replay.status == 400


async def test_remote_callback_rejects_foreign_state_without_consuming_login(authority):
    async with oauth(authority, redirect_uri="https://app.example/callback") as auth:
        callback = OAuthCallback(auth)
        app = web.Application()
        callback.setup(app)
        async with TestServer(app) as internal, ClientSession() as browser:
            attempt = await callback.begin()
            target = URL(await authority.browser(attempt.url))
            async with browser.get(
                internal.make_url("/callback?state=foreign&code=foreign")
            ) as response:
                assert response.status == 400
            assert not token_calls(authority)
            async with browser.get(internal.make_url(target.path_qs)) as response:
                assert response.status == 200
            await callback.wait()


async def test_dcr_fallback_registration_is_reused(authority):
    authority.metadata_changes["client_id_metadata_document_supported"] = False
    options = dict(client_id=None, metadata_url="https://app.example/client.json", registration={})
    async with oauth(authority, **options) as auth:
        await auth.login(authorize=authority.browser)
    async with oauth(authority, **options) as auth:
        assert await auth.token() == authority.current
    assert len(authority.registered) == 1


async def test_resource_and_authorization_server_on_different_origins(authority):
    resource = web.Application()
    resource.router.add_route("*", "/mcp", authority.mcp_handler)
    async with TestServer(resource) as server:
        authority.resource = str(server.make_url("/mcp"))
        assert URL(authority.resource).origin() != URL(authority.issuer).origin()
        async with oauth(authority) as auth:
            await auth.login(authorize=authority.browser)
            async with Client(authority.resource, ADAPTER, auth=auth) as client:
                await client.initialize()
        assert len(token_calls(authority)) == 1


async def test_network_policy_checked_before_fetch(authority):
    seen = []

    async def check(url):
        seen.append(url)
        if url.endswith("/metadata"):
            raise PermissionError("Destination is not allowed")

    async with oauth(authority, check_url=check) as auth:
        with pytest.raises(PermissionError):
            await auth.begin()
    assert seen == [authority.resource, authority.origin + "/metadata"]
    assert not token_calls(authority)


async def test_small_token_lifetime_does_not_force_immediate_refresh(authority):
    authority.token_changes["expires_in"] = 10
    async with oauth(authority) as auth:
        await auth.login(authorize=authority.browser)
        assert await auth.token() == authority.current
    assert len(token_calls(authority)) == 1


async def test_selected_scopes_are_not_lost(authority):
    async with oauth(authority, scopes=["extra"]) as auth:
        attempt = await auth.begin()
        assert set(URL(attempt.url).query["scope"].split()) == {"read", "extra"}


@pytest.mark.parametrize(
    "changes",
    [
        {"token_type": "MAC"},
        {"access_token": "bad\r\nheader"},
        {"expires_in": -1},
        {"expires_in": True},
        {"refresh_token": 123},
    ],
)
async def test_invalid_token_responses_are_not_saved(authority, changes):
    authority.token_changes.update(changes)
    async with oauth(authority) as auth:
        with pytest.raises(OAuthError):
            await auth.login(authorize=authority.browser)
    assert authority.saved["tokens"] == {}


async def test_refresh_network_failure_is_not_retried(authority, monkeypatch):
    async with oauth(authority) as auth:
        await auth.login(authorize=authority.browser)
    authority.saved["expires_at"] = 0
    async with oauth(authority) as auth:
        # Trigger discovery before interrupting the token transport.
        await auth.begin()
        original = auth.session.request

        def fail(method, url, **kwargs):
            if method == "POST":
                raise TimeoutError("private request details")
            return original(method, url, **kwargs)

        monkeypatch.setattr(auth.session, "request", fail)
        with pytest.raises(OAuthError, match="HTTP request failed") as error:
            await auth.token()
        assert "private" not in str(error.value)
        with pytest.raises(AuthorizationRequired):
            await auth.token()
    assert authority.saved["tokens"] == {}
    assert len(token_calls(authority)) == 1


async def test_mutated_client_endpoint_cannot_receive_credentials(authority):
    async with oauth(authority) as auth:
        await auth.login(authorize=authority.browser)
        async with Client(authority.resource, ADAPTER, auth=auth) as client:
            client.base_url = "https://other.example/mcp"
            with pytest.raises(ValueError, match="another MCP"):
                await client.initialize()


async def test_repeated_denial_invalidates_cached_credentials(authority):
    async with oauth(authority) as auth:
        await auth.login(authorize=authority.browser)
        authority.reject = True
        async with Client(authority.resource, ADAPTER, auth=auth) as client:
            for _ in range(2):
                with pytest.raises(AuthorizationRequired):
                    await client.initialize()
    assert len(token_calls(authority)) == 2
    assert authority.saved["tokens"] == {}


@pytest.mark.parametrize("field", ["expires_in", "refresh_token"])
async def test_null_token_fields_are_invalid(authority, field):
    authority.token_changes[field] = None
    async with oauth(authority) as auth:
        with pytest.raises(OAuthError):
            await auth.login(authorize=authority.browser)


async def test_cancelled_refresh_does_not_reuse_rotated_token(authority, monkeypatch):
    async with oauth(authority) as auth:
        await auth.login(authorize=authority.browser)
    authority.saved["expires_at"] = 0
    started = asyncio.Event()
    async with oauth(authority) as auth:
        original = auth.exchange

        async def exchange(form):
            result = await original(form)
            started.set()
            await asyncio.Event().wait()
            return result

        monkeypatch.setattr(auth, "exchange", exchange)
        refresh = asyncio.create_task(auth.token())
        await asyncio.wait_for(started.wait(), timeout=5)
        refresh.cancel()
        with pytest.raises(asyncio.CancelledError):
            await refresh
        with pytest.raises(AuthorizationRequired):
            await auth.token()
    assert authority.saved["tokens"] == {}
    assert len(token_calls(authority)) == 2


async def test_record_snapshot_is_not_mutated_by_application(authority):
    async def save(record):
        await authority.save(record)
        record["tokens"]["access_token"] = "other"

    async with oauth(authority, save=save) as auth:
        await auth.login(authorize=authority.browser)
        assert await auth.token() == authority.current


async def test_resource_identifier_with_query(authority):
    authority.resource += "?tenant=example"
    async with oauth(authority) as auth:
        await auth.login(authorize=authority.browser)
        assert authority.saved["resource"] == authority.resource


async def test_unknown_expiry_does_not_decode_token(authority):
    async with oauth(authority) as auth:
        await auth.login(authorize=authority.browser)
    authority.saved["tokens"].pop("expires_in")
    authority.saved["expires_at"] = None
    async with oauth(authority) as auth:
        assert await auth.token() == authority.current
    assert len(token_calls(authority)) == 1
