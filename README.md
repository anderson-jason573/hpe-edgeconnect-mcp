# hpe-edgeconnect-mcp

An [MCP](https://modelcontextprotocol.io) server that exposes an **HPE Aruba
EdgeConnect Orchestrator** as tools an LLM client can call — list appliances,
read interface and tunnel state, pull WAN bandwidth time series, and check or
change HPE SSE tunnel state.

It speaks MCP over Streamable HTTP, so it runs as its own service on the
network rather than as a local subprocess of one client.

> **Not affiliated with, endorsed by, or supported by Hewlett Packard
> Enterprise.** "HPE," "Aruba," "EdgeConnect," and "Silver Peak" are
> trademarks of their respective owners and are used here only to describe what
> this software talks to.

---

## Read this before you point it at production

**Two of the eleven tools change state, and this server does not ask before
executing them.** There is no preview, no confirmation, no dry-run. If a model
decides to call one, it happens.

| Tool | What it does | Risk |
|---|---|---|
| `write_set_appliance_location` | Sets an appliance's address/contact metadata | Cosmetic. Reversible, traffic-neutral. |
| `write_set_appliance_sse_tunnel` | Enables/disables an appliance's HPE SSE IPsec tunnel(s) | **Traffic-affecting on a live edge appliance.** |

That second one deserves a straight description: disabling SSE for an appliance
takes down *all* of its HPE SSE tunnels together (e.g. an A1 + A2 pair), and
bringing them back is not instant — re-enabling kicks off an orchestration
cycle, and tunnels have been observed taking **10–15 minutes** to come back up.
It is not an operation you want a model performing speculatively while it
"investigates" something.

Two ways to handle that:

1. **Scope the API key read-only.** This is the real control, and it lives on
   the Orchestrator, not in this code. Give the API key a read-only role and
   the write tools fail at the API no matter what any model decides. Do this
   unless you specifically intend the writes to work.

2. **Gate the writes in your client.** Every state-changing tool is prefixed
   `write_`. That prefix is a convention of this codebase precisely so a client
   can match on it and route those calls through a human-approval step before
   execution. This server deliberately doesn't do that itself — it has no user
   to ask.

## The MCP endpoint has no authentication

This server exposes its tools to anyone who can reach its port. There is no
token, no client auth, no TLS on the MCP side.

`MCP_HOST` defaults to `127.0.0.1` for that reason. The container image sets
`0.0.0.0` because it has to, which means **publishing the container's port puts
an unauthenticated control plane for your SD-WAN on that interface.** Keep it
on an internal network with the client, or terminate TLS and authentication in
front of it.

---

## Tools

**Reads (9)**

| Tool | Returns |
|---|---|
| `list_appliances` | All appliances the Orchestrator knows about. Each entry's `id` (e.g. `2.NE`) is the `nePk` every other tool wants. |
| `get_appliance_status` | State, model, software version, reachability, reboot/unsaved-changes flags for one appliance. |
| `get_interfaces` | Interface and system state for one appliance. |
| `get_tunnel_status` | Physical/underlay tunnel `operStatus`/`adminStatus`, for one appliance or all. |
| `get_overlay_config` | The network-wide SD-WAN overlay configuration. |
| `get_appliance_groups` | The appliance group tree, as a flat list of nodes. |
| `get_wan_bandwidth_timeseries` | WAN bandwidth over time for one appliance, projected to the bandwidth-relevant fields and sorted oldest-first, with average bits/sec per interval. |
| `get_appliance_location` | The address/contact metadata currently stored for an appliance. |
| `get_appliance_sse_tunnel_status` | HPE SSE enrollment *and* live SSE tunnel state in one call. |

**Writes (2)** — `write_set_appliance_location`, `write_set_appliance_sse_tunnel`.
See the warning above.

A note on `get_appliance_sse_tunnel_status`, because it is the one place the
API is genuinely confusing: HPE SSE tunnels are **pass-through** tunnels, not
the physical/underlay tunnels `get_tunnel_status` reports. On a
single-appliance deployment `get_tunnel_status` returns empty regardless of SSE
state. Don't infer SSE health from it — that's what this tool is for.

## Secret redaction

The Orchestrator returns secrets embedded in otherwise-useful objects — the
appliance list, for instance, carries device admin passwords in cleartext.
Every tool result is scrubbed before it leaves the server, replacing the value
of any sensitive-named field (`password`, `psk`, `presharedkey`,
`snmpcommunity`, `token`, …) with `***REDACTED***`.

It's a denylist by exact field name, kept deliberately narrow to avoid
over-redacting benign fields like `publicKey`. If your Orchestrator version
returns a secret under a field name that isn't in `_SENSITIVE_FIELDS` in
`server.py`, it will pass through to the model. Worth a look before you trust
it with a large deployment.

## Configuration

All configuration is environment variables.

| Variable | Required | Default | Notes |
|---|---|---|---|
| `EDGECONNECT_BASE_URL` | yes | — | e.g. `https://orchestrator.example.com` |
| `EDGECONNECT_API_KEY` | yes | — | Sent as an `X-Auth-Token` header on every request. |
| `EDGECONNECT_VERIFY_TLS` | no | `true` | Set `false` only for self-signed lab certs. |
| `MCP_HOST` | no | `127.0.0.1` | `0.0.0.0` in the container image. |
| `MCP_PORT` | no | `8000` | `8001` in the container image. |

Auth is stateless: no login call, no session cookie, no CSRF token. The key
goes in a header rather than the `?apiKey=` query param the Orchestrator also
accepts — TLS protects both in transit, but query strings get written to proxy
and server access logs in cleartext.

## Running it

Directly:

```bash
pip install -r requirements.txt
export EDGECONNECT_BASE_URL=https://orchestrator.example.com
export EDGECONNECT_API_KEY=...
python server.py
```

In Docker:

```bash
docker build -t hpe-edgeconnect-mcp .
docker run --rm -p 127.0.0.1:8001:8001 \
    -e EDGECONNECT_BASE_URL=https://orchestrator.example.com \
    -e EDGECONNECT_API_KEY=... \
    hpe-edgeconnect-mcp
```

The MCP endpoint is at `/mcp` on that port. Note the `127.0.0.1:` in the port
publish — see the authentication section above.

## Checking it against your Orchestrator

`test_client.py` is a **read-only smoke test that requires a live
Orchestrator** — not a unit test, and it won't run without real gear. It
exercises every read method and prints what comes back:

```bash
export EDGECONNECT_BASE_URL=... EDGECONNECT_API_KEY=...
python test_client.py
```

Any block that prints an `HTTP 4xx/5xx` line means an endpoint path or the auth
constant needs adjusting for your Orchestrator version. It never calls a write
method, so it cannot change device state.

## Compatibility

Endpoint paths and the `nePk` identifier were verified live against
**Orchestrator v9.7.0.43266** and cross-checked against its OpenAPI spec. Other
versions will likely need path adjustments — they're all in
`edgeconnect_client.py`, which is a plain REST wrapper with no MCP dependency,
so it's straightforward to correct and to reuse outside MCP entirely.

One identifier gotcha that costs people time: the appliance-scoped endpoints
want the `nePk` — the `id` field from `list_appliances`, like `2.NE`. They
reject the numeric `applianceId` field that appears alongside it.

## License

Copyright 2026 Jason Anderson

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).
