"""Native HTTPS via the real CLI, with temporary certificates and no scope I/O."""

import json
import os
import socket
import ssl
import subprocess
import sys
import time
from datetime import timedelta
from unittest.mock import Mock

import httpx
import pytest
import trustme
import uvicorn
from cryptography.hazmat.primitives import serialization
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

import rigol_mcp.server as srv


TOKEN = "https-test-token"


@pytest.fixture(scope="module")
def certificates(tmp_path_factory):
    directory = tmp_path_factory.mktemp("tls")
    ca = trustme.CA()
    leaf = ca.issue_cert("127.0.0.1")
    paths = {name: directory / f"{name}.pem" for name in ("ca", "cert", "key", "encrypted_key")}
    ca.cert_pem.write_to_path(paths["ca"])
    paths["cert"].write_bytes(b"".join(pem.bytes() for pem in leaf.cert_chain_pems))
    leaf.private_key_pem.write_to_path(paths["key"])
    key = serialization.load_pem_private_key(leaf.private_key_pem.bytes(), password=None)
    paths["encrypted_key"].write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"test-password"),
    ))
    return paths


def _tls_args(certificates):
    return ["--transport", "https", "--ssl-certfile", str(certificates["cert"]),
            "--ssl-keyfile", str(certificates["key"])]


def test_tls_defaults_and_blank_environment(monkeypatch):
    monkeypatch.setenv("RIGOL_MCP_SSL_CERTFILE", "")
    monkeypatch.setenv("RIGOL_MCP_SSL_KEYFILE", "")
    args = srv._parse_args([])
    assert args.ssl_certfile is args.ssl_keyfile is None
    monkeypatch.setenv("RIGOL_MCP_TRANSPORT", "https")
    with pytest.raises(SystemExit):
        srv._parse_args([])


def test_tls_environment_and_cli_precedence(monkeypatch, certificates):
    monkeypatch.setenv("RIGOL_MCP_TRANSPORT", "https")
    monkeypatch.setenv("RIGOL_MCP_SSL_CERTFILE", "env-cert.pem")
    monkeypatch.setenv("RIGOL_MCP_SSL_KEYFILE", "env-key.pem")
    args = srv._parse_args([])
    assert args.transport == "https"
    assert (args.ssl_certfile, args.ssl_keyfile) == ("env-cert.pem", "env-key.pem")
    # Existing http + certificate/key configurations must continue to serve TLS.
    run = Mock()
    monkeypatch.setattr(uvicorn, "run", run)
    srv.main([*_tls_args(certificates), "--transport", "http"])
    run.assert_called_once()
    assert run.call_args.kwargs["ssl_certfile"] == str(certificates["cert"])
    assert run.call_args.kwargs["ssl_keyfile"] == str(certificates["key"])


@pytest.mark.parametrize("transport,options", [
    ("http", ["--ssl-certfile", "cert.pem"]),
    ("http", ["--ssl-keyfile", "key.pem"]),
    ("http", ["--ssl-certfile", "", "--ssl-keyfile", ""]),
    ("https", []),
])
def test_partial_or_empty_tls_config_stops_startup(monkeypatch, capsys, transport, options):
    run = Mock()
    monkeypatch.setattr(uvicorn, "run", run)
    with pytest.raises(SystemExit) as error:
        srv.main(["--transport", transport, *options])
    assert error.value.code == 2
    assert "requires both --ssl-certfile and --ssl-keyfile" in capsys.readouterr().err
    run.assert_not_called()


def test_tls_with_stdio_rejected(capsys, certificates):
    with pytest.raises(SystemExit) as error:
        srv._parse_args([*_tls_args(certificates), "--transport", "stdio"])
    assert error.value.code == 2
    assert "require --transport http" in capsys.readouterr().err


def test_http_startup_without_tls(monkeypatch):
    run = Mock()
    monkeypatch.setattr(uvicorn, "run", run)
    srv.main(["--transport", "http"])
    run.assert_called_once()
    assert not any(name.startswith("ssl_") for name in run.call_args.kwargs)


def test_https_still_requires_token_on_network_bind(certificates):
    with pytest.raises(SystemExit, match="without authentication"):
        srv.main([*_tls_args(certificates), "--host", "0.0.0.0"])


@pytest.mark.parametrize("problem", ["missing_cert", "malformed_cert", "encrypted_key"])
def test_invalid_tls_files_fail_before_serving(monkeypatch, tmp_path, certificates, problem):
    files = dict(certificates)
    if problem == "missing_cert":
        files["cert"] = tmp_path / "missing.pem"
    elif problem == "malformed_cert":
        invalid = tmp_path / "invalid.pem"
        invalid.write_text("not PEM")
        files["cert"] = invalid
    else:
        files["key"] = files["encrypted_key"]
    run = Mock()
    monkeypatch.setattr(uvicorn, "run", run)
    with pytest.raises(SystemExit, match="Cannot load TLS certificate/key"):
        srv.main(_tls_args(files))
    run.assert_not_called()


@pytest.fixture(scope="module")
def https_url(certificates):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    # Override local .env values so this is deterministic and cannot reach a scope.
    env = {**os.environ, "RIGOL_MCP_TOKEN": TOKEN, "RIGOL_USB": "0", "RIGOL_IP": "127.0.0.1",
           "RIGOL_ENABLE_SEND_RAW": "0", "RIGOL_MCP_ALLOW_NO_AUTH": "0", "RIGOL_MCP_ALLOWED_HOSTS": ""}
    process = subprocess.Popen(
        [sys.executable, "-m", "rigol_mcp.server", *_tls_args(certificates),
         "--host", "127.0.0.1", "--port", str(port)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(f"HTTPS server exited during startup: {process.communicate()[0]}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("HTTPS server did not start within 15 seconds")
        yield f"https://127.0.0.1:{port}/mcp"
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)


@pytest.fixture
def trusted_context(certificates):
    return ssl.create_default_context(cafile=str(certificates["ca"]))


async def test_authenticated_mcp_over_https(https_url, trusted_context):
    async with (
        httpx.AsyncClient(verify=trusted_context, trust_env=False, timeout=5,
                          headers={"Authorization": f"Bearer {TOKEN}"}) as http,
        streamable_http_client(https_url, http_client=http) as (read, write, _),
        ClientSession(read, write, read_timeout_seconds=timedelta(seconds=5)) as session,
    ):
        assert (await session.initialize()).serverInfo.name == "rigol-mcp"
        await session.send_ping()
        assert {"idn", "disconnect", "get_scope_state"} <= {t.name for t in (await session.list_tools()).tools}
        result = await session.call_tool("disconnect", {})
        assert not result.isError
        assert json.loads(result.content[0].text)["connected"] is False


def test_https_auth_rejects_missing_token(https_url, trusted_context):
    response = httpx.post(https_url, json={}, verify=trusted_context, trust_env=False, timeout=5)
    assert response.status_code == 401


def test_tls_port_does_not_accept_plain_http(https_url):
    with pytest.raises((httpx.ReadError, httpx.RemoteProtocolError)):
        httpx.get(https_url.replace("https://", "http://"), trust_env=False, timeout=5)
