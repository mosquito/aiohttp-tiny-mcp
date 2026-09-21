"""OAuth discovery and response binding in the browser client."""

import shutil

import pytest
from test_console_auth import run_js

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def test_discovery_validates_resource_issuer_and_endpoint_origin():
    run_js("""
    const origin = "https://mcp.test";
    const resource = {resource: origin + "/mcp", authorization_servers: [origin + "/oauth"],
      scopes_supported: ["read"]};
    const metadata = {issuer: origin + "/oauth",
      authorization_response_iss_parameter_supported: true,
      code_challenge_methods_supported: ["S256"], response_types_supported: ["code"],
      token_endpoint_auth_methods_supported: ["none"],
      authorization_endpoint: origin + "/oauth/authorize",
      token_endpoint: origin + "/oauth/token"};
    const sent = [];
    globalThis.fetch = async (url, options) => {
      sent.push(String(url));
      assert.equal(options.credentials, "omit");
      assert.equal(options.redirect, "error");
      assert.equal(options.headers, undefined);
      const data = String(url).includes("protected-resource") ? resource : metadata;
      return new Response(JSON.stringify(data));
    };
    const prm = origin + "/.well-known/oauth-protected-resource/mcp";
    const found = await discoverOAuth(prm, origin + "/mcp?mcp=2025-11-25");
    assert.equal(found.resource, resource.resource);
    assert.equal(found.token, metadata.token_endpoint);
    assert.deepEqual(sent, [prm, origin + "/.well-known/oauth-authorization-server/oauth"]);
    resource.resource = origin + "/another";
    await assert.rejects(discoverOAuth(prm, origin + "/mcp"), /another MCP resource/);
    resource.resource = origin + "/mcp";
    metadata.issuer = origin + "/wrong";
    await assert.rejects(discoverOAuth(prm, origin + "/mcp"), /issuer validation/);
    metadata.issuer = origin + "/oauth";
    metadata.token_endpoint = "https://attacker.test/token";
    await assert.rejects(discoverOAuth(prm, origin + "/mcp"), /origin/);
    await assert.rejects(discoverOAuth("https://attacker.test/prm", origin + "/mcp"), /origin/);
    """)


def test_oauth_response_binding_before_code_exchange():
    run_js("""
    const config = {issuer: "https://mcp.test/oauth", token: "https://mcp.test/oauth/token",
      resource: "https://mcp.test/mcp"};
    const response = {code: "code", state: "expected-state", iss: config.issuer};
    const sent = [];
    globalThis.fetch = async (url, options) => {
      sent.push(url);
      assert.equal(url, config.token);
      assert.equal(options.credentials, "omit");
      assert.equal(options.redirect, "error");
      assert.equal(options.body.get("code_verifier"), "v".repeat(43));
      assert.equal(options.body.get("resource"), config.resource);
      return new Response(JSON.stringify({token_type: "Bearer", access_token: "mcp-token"}));
    };
    const exchange = (answer) => exchangeOAuthCode(config, "console", "https://mcp.test/cb",
      "v".repeat(43), "expected-state", answer);
    await assert.rejects(exchange({...response, state: "wrong"}), /state or issuer/);
    await assert.rejects(exchange({...response, iss: "https://attacker.test"}), /state or issuer/);
    await assert.rejects(exchange({...response, error: "access_denied"}), /refused or cancelled/);
    assert.equal(sent.length, 0);
    assert.equal(await exchange(response), "mcp-token");
    assert.equal(sent.length, 1);
    const verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk";
    assert.equal(await oauthChallenge(verifier), "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM");
    """)


def test_metadata_is_read_only_from_the_bearer_challenge():
    run_js(r"""
    assert.equal(bearerMetadata('Basic realm="x", resource_metadata="https://wrong.test"'), null);
    assert.equal(bearerMetadata('Basic realm="x", Bearer error="invalid_token", ' +
      'resource_metadata="https://mcp.test/.well-known/oauth-protected-resource/mcp"'),
      "https://mcp.test/.well-known/oauth-protected-resource/mcp");
    assert.equal(bearerMetadata('Bearer realm="x, not a challenge", ' +
      'resource_metadata="https://mcp.test/prm"'), "https://mcp.test/prm");
    """)
