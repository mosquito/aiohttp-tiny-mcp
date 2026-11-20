"""Automatic access log token redaction without request or logger replacement."""

from __future__ import annotations

import logging

import pytest
from aiohttp import web
from aiohttp.log import access_logger
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from aiohttp.web_log import AccessLogger
from yarl import URL

from aiohttp_tiny_mcp import Endpoint, Registry, SseEndpoint, StaticBasicAuth
from aiohttp_tiny_mcp.access_log import TOKEN_QUERY_FILTER, install_token_filter, mask_token

pytestmark = pytest.mark.usefixtures("restore_access_filters")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("/mcp?token=private&org=team", "/mcp?token=[REDACTED]&org=team"),
        (
            "/mcp?org=a%20b&%74oken=one%26two&token=again&empty=&flag",
            "/mcp?org=a%20b&%74oken=[REDACTED]&token=[REDACTED]&empty=&flag",
        ),
        ("/mcp?TOKEN=value&token=", "/mcp?TOKEN=[REDACTED]&token=[REDACTED]"),
        ("/mcp?token=value with spaces#section", "/mcp?token=[REDACTED]#section"),
        ("/token/private?not_token=visible", "/token/private?not_token=visible"),
        ("/mcp?org=team&token", "/mcp?org=team&token"),
        ("/mcp", "/mcp"),
    ],
)
def test_mask_token_preserves_other_url_parts(url: str, expected: str) -> None:
    assert mask_token(url) == expected


@pytest.mark.parametrize("custom_format", [False, True])
def test_stock_access_logger_redacts_text_and_structured_fields(
    caplog: pytest.LogCaptureFixture, custom_format: bool
) -> None:
    caplog.set_level(logging.INFO, logger=access_logger.name)
    Endpoint(Registry("test", "1")).app()
    request = make_mocked_request(
        "POST",
        "/mcp?org=team&token=request-secret&%74oken=encoded-secret",
        headers={"Referer": "https://example.test/console?token=referer-secret&org=team"},
    )
    response = web.Response(headers={"Location": "/next?token=location-secret&done=1"})
    format_string = "%r %{Referer}i %{Location}o" if custom_format else AccessLogger.LOG_FORMAT
    access = AccessLogger(access_logger, format_string)
    original_path = request.path_qs
    access.log(request, response, 0.1)
    record = caplog.records[-1]
    assert record.first_request_line == (
        "POST /mcp?org=team&token=[REDACTED]&token=[REDACTED] HTTP/1.1"
    )
    assert record.request_header["Referer"] == (
        "https://example.test/console?token=[REDACTED]&org=team"
    )
    if custom_format:
        assert record.response_header["Location"] == "/next?token=[REDACTED]&done=1"
    assert all(
        secret not in repr(record.__dict__)
        for secret in ("request-secret", "encoded-secret", "referer-secret", "location-secret")
    )
    assert request.path_qs == original_path
    assert request.query.getall("token") == ["request-secret", "encoded-secret"]
    assert request.headers["Referer"].endswith("token=referer-secret&org=team")
    assert response.headers["Location"] == "/next?token=location-secret&done=1"


@pytest.mark.parametrize("mount", ["app", "setup", "sse-setup"])
async def test_default_endpoint_setup_masks_access_logs_without_logger_configuration(
    caplog: pytest.LogCaptureFixture, mount: str
) -> None:
    caplog.set_level(logging.INFO, logger=access_logger.name)
    received = []

    @web.middleware
    async def observe(request, handler):
        received.append((request.query.getall("token"), await request.text()))
        return await handler(request)

    registry = Registry("test", "1")
    if mount == "sse-setup":
        endpoint = SseEndpoint(registry)
        app = web.Application(middlewares=[observe])
        endpoint.setup(app)
        path = "/messages"
        status = 404
    else:
        endpoint = Endpoint(registry)
        app = web.Application(middlewares=[observe])
        if mount == "app":
            app = endpoint.app(middlewares=[observe])
        else:
            endpoint.setup(app)
        path = "/mcp"
        status = 200
    body = '{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}'
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            URL(path + "?org=team&token=first-secret&%74oken=second-secret", encoded=True),
            data=body,
            headers={
                "Content-Type": "application/json",
                "MCP-Protocol-Version": "2025-11-25",
                "Referer": "https://example.test/?token=header-secret",
            },
        )
        await response.read()
        assert response.status == status
    assert received == [(["first-secret", "second-secret"], body)]
    records = [record for record in caplog.records if record.name == access_logger.name]
    assert len(records) == 1
    assert "org=team&token=[REDACTED]&%74oken=[REDACTED]" in records[0].getMessage()
    assert all(
        secret not in repr(records[0].__dict__)
        for secret in ("first-secret", "second-secret", "header-secret")
    )


@pytest.mark.parametrize("refusal", ["authentication", "middleware"])
@pytest.mark.parametrize("legacy", [False, True])
async def test_token_masking_includes_rejected_requests(
    caplog: pytest.LogCaptureFixture, refusal: str, legacy: bool
) -> None:
    caplog.set_level(logging.INFO, logger=access_logger.name)
    registry = Registry("test", "1", auth=StaticBasicAuth(("alice", "password")))
    seen = []

    @web.middleware
    async def reject(request, handler):
        seen.append(request.query["token"])
        return web.Response(status=403)

    app = web.Application(middlewares=[reject] if refusal == "middleware" else [])
    if legacy:
        SseEndpoint(registry).setup(app)
        path = "/messages"
    else:
        Endpoint(registry).setup(app)
        path = "/mcp"
    async with TestClient(TestServer(app)) as client:
        response = await client.post(path + "?token=rejected-secret", json={})
        await response.read()
        assert response.status == (403 if refusal == "middleware" else 401)
    records = [record for record in caplog.records if record.name == access_logger.name]
    assert len(records) == 1
    assert "token=[REDACTED]" in records[0].getMessage()
    assert "rejected-secret" not in repr(records[0].__dict__)
    assert seen == (["rejected-secret"] if refusal == "middleware" else [])


async def test_existing_logger_handlers_filters_and_runner_options_are_preserved(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=access_logger.name)
    observed = []

    class ExistingFilter(logging.Filter):
        def filter(self, record):
            record.existing_filter = True
            return True

    class CustomLogger(AccessLogger):
        def log(self, request, response, elapsed):
            observed.append(request.path)
            super().log(request, response, elapsed)

    existing_filter = ExistingFilter()
    access_logger.addFilter(existing_filter)
    handlers = list(access_logger.handlers)
    logger_type = type(access_logger)
    options = {"keepalive_timeout": 19}
    app = Endpoint(Registry("test", "1")).app(handler_args=options)
    install_token_filter()
    Endpoint(Registry("another", "1"))
    SseEndpoint(Registry("legacy", "1"))
    assert access_logger.filters.count(TOKEN_QUERY_FILTER) == 1
    assert existing_filter in access_logger.filters
    assert access_logger.handlers == handlers
    assert type(access_logger) is logger_type
    assert logging.getLogger("aiohttp.access") is access_logger
    assert options == {"keepalive_timeout": 19}
    server = TestServer(app)
    await server.start_server(access_log_class=CustomLogger, access_log_format="%s %r")
    async with TestClient(server) as client:
        response = await client.get("/missing?token=custom-secret")
        assert response.status == 404
        await response.read()
    assert observed == ["/missing"]
    records = [record for record in caplog.records if record.name == access_logger.name]
    assert len(records) == 1
    assert records[0].existing_filter is True
    assert records[0].getMessage() == "404 GET /missing?token=[REDACTED] HTTP/1.1"


def test_filter_preserves_records_without_token_query_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=access_logger.name)
    install_token_filter()
    request_line = "GET /mcp?org=team&not_token=visible HTTP/1.1"
    access_logger.info("%s", request_line, extra={"first_request_line": request_line})
    record = caplog.records[-1]
    assert record.msg == "%s"
    assert record.args == (request_line,)
    assert record.first_request_line == request_line
    assert record.getMessage() == request_line
    access_logger.info("ordinary message: %s", "unchanged")
    assert caplog.records[-1].getMessage() == "ordinary message: unchanged"


@pytest.mark.parametrize("mount", ["app", "setup", "sse-setup"])
async def test_setup_opt_out_leaves_stock_access_logging_unchanged(
    caplog: pytest.LogCaptureFixture, mount: str
) -> None:
    caplog.set_level(logging.INFO, logger=access_logger.name)
    registry = Registry("opt-out", "1")
    app = web.Application()
    if mount == "sse-setup":
        SseEndpoint(registry).setup(app, mask_tokens_in_logs=False)
    elif mount == "app":
        app = Endpoint(registry).app(mask_tokens_in_logs=False)
    else:
        Endpoint(registry).setup(app, mask_tokens_in_logs=False)
    assert TOKEN_QUERY_FILTER not in access_logger.filters
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/missing?token=optout-test-value")
        assert response.status == 404
        await response.read()
    records = [record for record in caplog.records if record.name == access_logger.name]
    assert len(records) == 1
    assert "token=optout-test-value" in records[0].getMessage()
    assert "token=optout-test-value" in records[0].first_request_line


@pytest.mark.parametrize("mount", ["app", "setup", "sse-setup"])
async def test_setup_opt_out_preserves_masking_installed_by_another_application(
    caplog: pytest.LogCaptureFixture, mount: str
) -> None:
    caplog.set_level(logging.INFO, logger=access_logger.name)
    Endpoint(Registry("protected", "1")).app()
    registry = Registry("opt-out", "1")
    app = web.Application()
    if mount == "sse-setup":
        SseEndpoint(registry).setup(app, mask_tokens_in_logs=False)
    elif mount == "app":
        app = Endpoint(registry).app(mask_tokens_in_logs=False)
    else:
        Endpoint(registry).setup(app, mask_tokens_in_logs=False)
    assert access_logger.filters.count(TOKEN_QUERY_FILTER) == 1
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/missing?token=protected-test-value")
        assert response.status == 404
        await response.read()
    records = [record for record in caplog.records if record.name == access_logger.name]
    assert len(records) == 1
    assert "token=[REDACTED]" in records[0].getMessage()
    assert "protected-test-value" not in repr(records[0].__dict__)


@pytest.mark.parametrize("masking_installed", [False, True])
def test_constructors_and_low_level_routes_leave_logging_unchanged(masking_installed: bool) -> None:
    if masking_installed:
        Endpoint(Registry("protected", "1")).app()
    existing_filter = logging.Filter()
    existing_handler = logging.NullHandler()
    access_logger.addFilter(existing_filter)
    access_logger.addHandler(existing_handler)
    filters = list(access_logger.filters)
    handlers = list(access_logger.handlers)
    try:
        endpoint = Endpoint(Registry("low-level", "1"))
        legacy = SseEndpoint(Registry("low-level-legacy", "1"))
        assert access_logger.filters == filters
        app = web.Application()
        app.add_routes(endpoint.routes())
        app.router.add_view("/another", endpoint.view)
        app.add_routes(legacy.routes())
        assert access_logger.filters == filters
        assert access_logger.handlers == handlers
    finally:
        access_logger.removeHandler(existing_handler)
