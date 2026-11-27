# Remote operation

Run one `rigol-mcp` HTTPS server on the computer connected to the scope. MCP
clients connect to that server using its URL and a shared bearer token. The
server computer needs the Python/VISA dependencies and, for USB, the appropriate
driver. Client computers need an MCP client that supports Streamable HTTP.

```text
Codex / Claude Code / another MCP client
    |  Streamable HTTP over TLS + Authorization: Bearer <token>
    v
Lab computer: rigol-mcp at https://192.168.1.50:8000/mcp
    |  USB, or SCPI over TCP port 5555
    v
Rigol oscilloscope
```

`192.168.1.50` is an example **server computer** address throughout this guide.
Replace it with your server's reachable IP address or hostname. `RIGOL_IP`, when
used, is the **oscilloscope's** address. These can be different machines.

## 1. Configure and start the server

On the lab computer, follow the [installation](../README.md#installation) and
[scope connection](../README.md#configuration) instructions. Run the following
commands from the repository directory.

Generate a token once, and copy its value into the server's `.env` and the client
configuration described below:

```sh
uv run python -c "import secrets; print(secrets.token_hex(32))"
```

For a USB scope, the server's `.env` can contain:

```dotenv
RIGOL_USB=1
RIGOL_MCP_TRANSPORT=http
RIGOL_MCP_HOST=0.0.0.0
RIGOL_MCP_PORT=8000
RIGOL_MCP_TOKEN=REPLACE_WITH_YOUR_GENERATED_TOKEN
RIGOL_MCP_SSL_CERTFILE=certs/server.pem
RIGOL_MCP_SSL_KEYFILE=certs/server-key.pem
```

For a LAN-connected scope, replace `RIGOL_USB=1` with these settings, using the
scope's actual address:

```dotenv
RIGOL_USB=0
RIGOL_IP=192.168.1.123
```

### Certificates for a private lab

Use a certificate from your organization's CA, or generate a lab certificate
with [mkcert](https://github.com/FiloSottile/mkcert#installation). Install mkcert
using its instructions, then run these commands from the repository directory.
Create `certs/` first if it does not exist:

```sh
mkdir certs
mkcert -cert-file certs/server.pem -key-file certs/server-key.pem 192.168.1.50 localhost 127.0.0.1
mkcert -CAROOT
```

Replace the example IP with your server's address. Add any DNS names clients
will use to the certificate command; issuing a certificate does not create DNS
records. The final command prints the CA directory, containing `rootCA.pem`.
Copy that public CA certificate to each client computer. The server key stays on
the server; the CA's `rootCA-key.pem` stays with the certificate issuer.

Each client must trust this CA. On Windows, import the copied certificate into
the current user's Trusted Root Certification Authorities store:

```powershell
certutil -user -addstore Root C:\path\to\rootCA.pem
```

For macOS/Linux, follow mkcert's
[instructions for installing the CA on another system](https://github.com/FiloSottile/mkcert#installing-the-ca-on-other-systems),
using the copied `rootCA.pem`. Running `mkcert -install` against a newly generated
CA on another computer will not trust the server's existing CA.

Restart the MCP client after updating trust. Client runtimes can use their own
CA bundles: [Codex documents `CODEX_CA_CERTIFICATE`](https://learn.chatgpt.com/docs/config-file/environment-variables),
and [Claude Code documents `NODE_EXTRA_CA_CERTS`](https://code.claude.com/docs/en/network-config#custom-ca-certificates)
if the installed system certificate is not picked up. These settings point to
the public CA certificate, while the bearer token stays in the client config.
Keep certificate and hostname verification enabled.

The server accepts a PEM certificate chain (leaf first, followed by any
intermediates) and an unencrypted PEM private key. `certs/` is ignored by Git;
restrict access to its private key. Paths are relative to the server's working
directory, or can be absolute. Renew the certificate before expiry and restart
the server to load it. If its IP/hostname changes, issue a certificate covering
the new address. Certificate issuance and renewal are external to `rigol-mcp`.

### Start the server

With the files and `.env` configured, start the server:

```sh
uv run rigol-mcp --transport http
```

The explicit flag also works if `RIGOL_MCP_TRANSPORT` is absent from `.env`.
The transport is still `http`; the certificate/key settings enable TLS. You can
also pass `--ssl-certfile certs/server.pem --ssl-keyfile certs/server-key.pem`.
Both files are required together. Missing files, invalid PEM, encrypted keys,
and mismatched certificate/key pairs stop startup without falling back to HTTP.
TLS settings with `--transport stdio` are rejected.
Leave this process running while clients use it. Stop it with Ctrl+C. For a
managed service, set the working directory to this repository, provide the same
configuration, and run a single server process per instrument.

- `RIGOL_MCP_HOST` is a **local bind address**. `0.0.0.0` listens on all IPv4
  interfaces; `127.0.0.1` permits clients on the same computer only. You can also
  bind to a specific address assigned to the server computer.
- Clients use `https://192.168.1.50:8000/mcp`, including `/mcp`. They should not use
  `0.0.0.0` as the destination. `localhost` on a client means that client's own
  computer.
- Allow inbound TCP port 8000 on the server's firewall from the intended clients.
  Port 5555 is the separate server-to-scope LAN connection.
- The server loads `.env` at startup. Existing process environment variables take
  precedence over `.env`; CLI flags override the corresponding environment
  settings. Restart the server after changing its configuration.
- Setting only host, port, and token does not enable HTTP: the transport defaults
  to `stdio`. It also does not change any client's configuration.

The token is required on every HTTP request when configured; HTTPS encrypts
the token and MCP traffic. TLS does not replace bearer authentication. Keep the
token out of version control. See the [HTTP settings table](../README.md#running-over-http)
for the explicit no-auth option and host/origin restrictions.

To serve plain HTTP behind a local HTTPS reverse proxy, leave both TLS file
settings unset or empty and bind the server to `127.0.0.1`. Configure the proxy
to preserve authorization and MCP session headers and support streaming
responses. Clients connect to the proxy's HTTPS URL. Native TLS accepts only
HTTPS on its port; it does not provide an HTTP listener or redirect.

## 2. Switch the client to HTTP

Put the URL and token directly in the client's configuration. Replace
`REPLACE_WITH_YOUR_GENERATED_TOKEN` below with the value from the server's `.env`,
keeping the `Bearer ` prefix and its space. The bearer token is stored directly
in the client configuration. Keep files containing the token private and out of Git.

Replace the existing stdio entry for `rigol`, including its command, arguments,
and server-only environment block. Ensure other client configurations are not
still launching a separate local server for the same scope. One HTTP server can
serve multiple clients; separate server processes do not share its instrument
lock.

### Codex desktop / CLI / IDE extension

Edit `~/.codex/config.toml` (on Windows, `%USERPROFILE%\.codex\config.toml`),
replacing the existing Rigol entry with:

```toml
[mcp_servers.rigol]
url = "https://192.168.1.50:8000/mcp"
http_headers = { Authorization = "Bearer REPLACE_WITH_YOUR_GENERATED_TOKEN" }
```

The repository's `.mcp.json` does not replace this Codex configuration. Check
for a project-specific `.codex/config.toml` that also defines `rigol`.

Restart the MCP connection after saving, or quit and reopen the app. Inspect
the saved entry with `codex mcp get rigol`; it should show an HTTP URL instead of
a launch command. This inspection alone does not prove connectivity.

These fields and configuration locations are documented in the
[Codex MCP guide](https://developers.openai.com/codex/mcp/).

### Claude Code

Replace the `rigol` entry in the project's `.mcp.json`, preserving other servers:

```json
{
  "mcpServers": {
    "rigol": {
      "type": "http",
      "url": "https://192.168.1.50:8000/mcp",
      "headers": {
        "Authorization": "Bearer REPLACE_WITH_YOUR_GENERATED_TOKEN"
      }
    }
  }
}
```

This repository ignores `.mcp.json`; keep it untracked when it contains a token.
Restart the connection after saving. Use `claude mcp get rigol` to check the
active definition; a local-scope entry can override the project entry. Use
`/mcp` in Claude Code to check connection status and complete project server
approval if prompted. See the
[Claude Code MCP documentation](https://code.claude.com/docs/en/mcp) for headers
and configuration scopes.

For other clients, choose Streamable HTTP, supply the same URL, and configure
the `Authorization: Bearer <token>` header using that client's authentication
settings. This server uses a static bearer token; it does not implement OAuth
login or the legacy `/sse` transport.

## 3. Verify the connection

First check reachability and TLS verification from the client computer. Use the
copied `rootCA.pem` for the lab CA. On Windows:

```powershell
Test-NetConnection 192.168.1.50 -Port 8000
curl.exe --cacert C:\path\to\rootCA.pem --max-time 10 -i https://192.168.1.50:8000/mcp
```

On macOS/Linux:

```sh
curl --cacert /path/to/rootCA.pem --max-time 10 -i https://192.168.1.50:8000/mcp
```

With a token configured, the unauthenticated request should return
`401 Unauthorized`. That establishes a verified TLS connection and token
enforcement, not successful authentication. `--cacert` explicitly selects the
trusted CA for this check; omit it when curl already trusts the issuer.
`/mcp` is a protocol endpoint, not a browser page.

Then connect the configured MCP client and ask it to:

> Use the rigol tools to call idn, then get_scope_state, then
> get_acquisition_info, sequentially. Report any errors without changing scope
> settings or acquisition state.

Successful tool discovery establishes an authenticated MCP connection. `idn`
then checks the server-to-instrument connection. A successful response includes
the scope identity; read the result for connection errors even if the MCP call
itself succeeds. `get_acquisition_info` additionally requires a supported model.

The `transport: USB` or LAN connection details in `idn` describe the
**server-to-scope** link. They do not indicate whether the client used HTTP or
stdio; check the client's configured URL for that.

## Working remotely

- Keep the HTTP server running independently of the client. Closing a client
  session does not stop the service or release its cached instrument connection.
- `disconnect` releases the HTTP server's shared scope connection. It does not
  shut down HTTP, and another client call can reconnect it.
- Complete tool calls are serialized within one server. A sequence of separate
  calls can interleave with another client's workflow. Coordinate captures and
  configuration changes between users.
- Cancellation does not interrupt an instrument operation already in progress.
  It can finish and change the scope or save a file after the client disconnects.
- Screenshots and acquisition exports are saved on the **server computer**.
  `output_dir`, `RIGOL_SCREENSHOT_DIR`, and `RIGOL_WAVEFORM_DIR` refer to server
  paths. Screenshot image content is also returned over MCP; acquisition export
  paths do not automatically transfer the binary files to the client.

## Troubleshooting

| Symptom | Check |
|---|---|
| Connection refused or timed out | Server process running with `--transport http`; correct host/port; bind address reachable from the client; firewall/routing. |
| Server refuses a non-loopback bind | Set a nonempty `RIGOL_MCP_TOKEN` on the server and restart it. |
| TLS configuration fails at startup | Set both certificate/key paths, use `--transport http`, and supply a matching PEM chain and unencrypted PEM key readable by the server account. |
| Client reports an untrusted certificate | Trust the server's issuing CA in the client's certificate store or supported CA bundle, then restart the client. |
| Client reports a hostname mismatch | Connect using an IP/DNS name in the certificate's subject alternative names, or issue a new certificate for the intended address. |
| Client reports an expired certificate | Renew the server certificate and restart the server to load it. |
| Connection resets after enabling TLS | Use `https://`, including in manual tests. The same port no longer accepts plain HTTP. |
| Client returns 401 | Matching token in the running server and client; `Bearer ` prefix with a space; client connection reloaded after config changes. A reverse proxy must preserve authorization. |
| Client still launches `uv` or a local scope connection | Replace the active stdio definition. Check global/project configuration precedence and restart the client connection. |
| `idn` reports USB when HTTP was expected | USB is the server-to-scope transport. Verify HTTP in the client configuration. |
| Tools are listed, but scope reads fail | Check the server computer's `RIGOL_USB`/`RIGOL_IP`, USB drivers, cable, and scope availability. MCP discovery does not open the instrument. |
| HTTP 404 | Use `/mcp`; check any reverse proxy route. Reconnect after a server restart if a previous MCP session is no longer known. |
| HTTP 400/406 from a manual request | Use a Streamable HTTP MCP client with initialization, required headers, and session handling. An arbitrary GET/POST is not a complete MCP connection. |
| Changes to `.env` appear ignored | Restart the server; check inherited environment variables and CLI flags for overrides. Client configuration must be changed separately. |
| Export path is missing on the client | Retrieve it from the server computer; it is not a local client path. |
| `uv sync` reports a locked executable on Windows | Stop the server using that virtual environment before updating it, then restart. Use a separate environment for diagnostic clients. |

To return to stdio, stop the HTTP server before allowing a local server to access
the same instrument, restore the client's command-based configuration, and
remove the TLS file settings, and remove `RIGOL_MCP_TRANSPORT=http` from the local
server environment (or pass `--transport stdio`).
