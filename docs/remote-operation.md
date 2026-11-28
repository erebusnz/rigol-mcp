# HTTP server configuration

HTTP mode runs one persistent `rigol-mcp` server that MCP clients connect to by
URL. The server owns the scope connection; clients only need Streamable HTTP
support. HTTP is the default for this mode, with optional HTTPS.

## Start a local server

Follow the [installation](../README.md#installation) instructions on the computer
connected to the scope. For USB, set this in the repository's `.env`:

```dotenv
RIGOL_USB=1
```

For a LAN-connected scope, use `RIGOL_USB=0` and
`RIGOL_IP=192.168.1.123`, replacing the IP with the scope's address.
See [scope connection settings](../README.md#configuration) for other options.

From the repository directory, run:

```sh
uv run rigol-mcp --transport http
```

With the default server settings, the endpoint is
`http://127.0.0.1:8000/mcp`, accessible only from the same computer. No token or
certificate is required for this local setup. Leave the process running while
clients use it.

## Server settings

Set these in the server's `.env` or process environment, or use the listed CLI
flags. Existing environment variables take precedence over `.env`; CLI flags
override their corresponding settings. Restart the server after changes.

| Environment variable | CLI flag | Default | Purpose |
|---|---|---|---|
| `RIGOL_MCP_TRANSPORT` | `--transport` | `stdio` | Select `http` for an HTTP or HTTPS server |
| `RIGOL_MCP_HOST` | `--host` | `127.0.0.1` | Local address to listen on; `0.0.0.0` listens on all IPv4 interfaces |
| `RIGOL_MCP_PORT` | `--port` | `8000` | Server port |
| `RIGOL_MCP_TOKEN` | — | Unset | Shared bearer token required from clients |
| `RIGOL_MCP_SSL_CERTFILE` | `--ssl-certfile` | Unset | PEM certificate chain for native HTTPS |
| `RIGOL_MCP_SSL_KEYFILE` | `--ssl-keyfile` | Unset | Unencrypted PEM private key; requires the certificate file |
| `RIGOL_MCP_ALLOW_NO_AUTH` | `--allow-no-auth` | Off | Explicitly allow a non-loopback bind without a token |
| `RIGOL_MCP_ALLOWED_HOSTS` | `--allowed-hosts` | Unset | Comma-separated client-facing hostnames/IPs accepted when no token is configured |

These settings configure the server. Each client needs its own URL and
authentication configuration.

## Remote access and authentication

Generate a token once:

```sh
uv run python -c "import secrets; print(secrets.token_hex(32))"
```

Keep the scope connection settings and add these to the server's `.env`,
replacing the token placeholder with the generated value:

```dotenv
RIGOL_MCP_TRANSPORT=http
RIGOL_MCP_HOST=0.0.0.0
RIGOL_MCP_PORT=8000
RIGOL_MCP_TOKEN=REPLACE_WITH_YOUR_GENERATED_TOKEN
```

Start the server with `uv run rigol-mcp --transport http` and allow the configured
port through the server's firewall for the intended clients. Clients connect
using the server computer's reachable hostname or IP, not `0.0.0.0`.
`RIGOL_IP` still refers to the scope, which may be a different device.

When a token is configured, clients must send
`Authorization: Bearer <token>`. Plain HTTP does not encrypt that token or the
traffic; use the HTTPS options below when encryption is needed. Keep tokens out
of version control.

A non-loopback bind requires a token unless `--allow-no-auth` is explicitly set.
Without a token, accepted Host/Origin values are restricted to loopback names,
the bind address, and `--allowed-hosts`. If binding to `0.0.0.0` without a token,
list the actual hostname/IP clients will use in `--allowed-hosts`.

## Optional HTTPS

### Native HTTPS

Add both TLS settings to the server's `.env`:

```dotenv
RIGOL_MCP_SSL_CERTFILE=certs/server.pem
RIGOL_MCP_SSL_KEYFILE=certs/server-key.pem
```

Restart the server. The endpoint becomes `https://<server-host>:8000/mcp` on the
same configured port. Keep `RIGOL_MCP_TRANSPORT=http`; there is no separate
`https` transport value. Bearer authentication still applies.

Use a PEM certificate chain and its matching, unencrypted PEM private key.
Relative paths are resolved from the server's working directory. Both files are
required together; invalid TLS configuration stops startup. With neither file
configured, the server uses plain HTTP. A TLS-enabled port accepts HTTPS only.

### Certificates and client trust

Use a certificate issued for the hostname or IP clients will connect to, from a
CA those clients trust. An organization-managed or publicly trusted certificate
can be used directly.

For a private development setup, [mkcert](https://github.com/FiloSottile/mkcert#installation)
can generate a certificate. After installing it, create `certs/` and run:

```sh
mkcert -cert-file certs/server.pem -key-file certs/server-key.pem scope-server.example
mkcert -CAROOT
```

Replace `scope-server.example` with your actual server hostname; add any other
names or IPs clients will use. Copy `rootCA.pem` from the printed CA directory
to each client and follow mkcert's
[trust installation instructions](https://github.com/FiloSottile/mkcert#installing-the-ca-on-other-systems).
Keep `rootCA-key.pem` with the issuer and the server's private key on the server.

Some clients require additional CA configuration; consult your client's
certificate settings if system trust is insufficient. Renew certificates before
expiry and restart the server to load replacements.

### HTTPS reverse proxy

Alternatively, terminate TLS at a reverse proxy. Leave the server's TLS file
settings unset and bind `rigol-mcp` to `127.0.0.1`. Configure the proxy to forward
`/mcp` to `http://127.0.0.1:8000/mcp`, preserve authorization and MCP session
headers, and support streaming responses. Clients use the proxy's HTTPS URL.

## Configure MCP clients

Replace the client's stdio entry with a Streamable HTTP URL. Use the scheme,
hostname, and port for your chosen setup:

| Setup | Example client URL |
|---|---|
| Local HTTP | `http://127.0.0.1:8000/mcp` |
| Remote HTTP | `http://scope-server.example:8000/mcp` |
| Native HTTPS | `https://scope-server.example:8000/mcp` |
| HTTPS proxy on port 443 | `https://scope-server.example/mcp` |

The hostname above is a placeholder. The examples below use authenticated remote
HTTP; change the URL for your setup and use the server's token. Omit the
authorization header if no token is configured.

### Codex

In `~/.codex/config.toml` (on Windows, `%USERPROFILE%\.codex\config.toml`):

```toml
[mcp_servers.rigol]
url = "http://scope-server.example:8000/mcp"
http_headers = { Authorization = "Bearer REPLACE_WITH_YOUR_GENERATED_TOKEN" }
```

Remove the old command/arguments for this server and reconnect the client.
See the [Codex MCP configuration reference](https://developers.openai.com/codex/mcp/).

### Claude Code

In the project's `.mcp.json`, preserving any other server entries:

```json
{
  "mcpServers": {
    "rigol": {
      "type": "http",
      "url": "http://scope-server.example:8000/mcp",
      "headers": {
        "Authorization": "Bearer REPLACE_WITH_YOUR_GENERATED_TOKEN"
      }
    }
  }
}
```

Reconnect using `/mcp`. See the
[Claude Code MCP guide](https://code.claude.com/docs/en/mcp) for configuration
scopes and project approvals. Keep client files containing tokens out of Git.

For other clients, select Streamable HTTP and supply the same URL and
authorization header. The server uses a static bearer token, not OAuth.

## Verify and operate

Connect the MCP client, list tools, then call `idn` and `get_scope_state`.
Tool discovery checks the MCP connection; a successful instrument identity and
state response check the server-to-scope connection. The USB/LAN transport
reported by `idn` describes that instrument link.

- Run one server process per scope; multiple MCP clients can share it.
- Complete tool calls run sequentially. Separate calls in a workflow can
  interleave with another client's calls.
- Cancellation does not interrupt instrument work already in progress.
- `disconnect` releases the shared scope connection without stopping the HTTP
  server. A later tool call can reconnect.
- Screenshots and exports are saved on the server computer. Export paths do not
  automatically transfer files to the client.
- For a managed service, use the same configuration and working directory as
  the manual invocation.

## Troubleshooting

| Symptom | Check |
|---|---|
| Connection refused or timed out | Server running, correct hostname/port, reachable bind address, and firewall access |
| Client still launches a local server | Replace its active stdio configuration with the HTTP entry |
| `401 Unauthorized` | Matching server/client tokens and the `Bearer ` prefix |
| `421` or `403` without a token | Client hostname and Origin allowed by the bind address or `--allowed-hosts` |
| TLS startup fails | Both files configured, matching certificate/key, unencrypted key, readable paths, and `--transport http` |
| Certificate rejected | Trusted issuer, valid dates, and matching server hostname/IP |
| Connection resets after enabling TLS | Client URL uses `https://` |
| Tools appear but scope reads fail | Server's scope settings, drivers, cable, and instrument availability |
| `404` | URL ends in `/mcp`; reconnect if the server restarted and the old session expired |
| Export file absent on client | Retrieve it from the server computer |

To return to stdio, stop the HTTP server, restore the client's command-based
configuration, and remove the HTTP transport and TLS settings from that server's
environment.
