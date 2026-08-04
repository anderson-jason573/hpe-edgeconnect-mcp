"""
EdgeConnect MCP server.

Exposes EdgeConnect operations as MCP tools over Streamable HTTP, so an MCP
client can connect to it over the network rather than as a local subprocess --
this is what lets it run as its own container.

Tool naming convention: every write-capable tool is prefixed with `write_`.
This server does NOT gate those calls -- it executes them. The prefix exists so
that a client which wants a human-approval step can find the write tools by
name and stop them before execution. That is a convention of this codebase, not
an MCP feature; keep it consistent as you add tools, and see the README for
what running with writes enabled actually means.
"""

from __future__ import annotations

import json
import os
import time

from mcp.server.fastmcp import FastMCP

from edgeconnect_client import EdgeConnectClient

# FastMCP's constructor always passes explicit host/port defaults into its
# Settings object, which means env vars can never override them unless we
# read and forward the values ourselves.
mcp = FastMCP(
    "edgeconnect",
    stateless_http=True,
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("MCP_PORT", "8000")),
)
_client: EdgeConnectClient | None = None


def get_client() -> EdgeConnectClient:
    global _client
    if _client is None:
        _client = EdgeConnectClient()
    return _client


# ---- Sensitive-field redaction --------------------------------------------
# The Orchestrator returns secrets inside otherwise-useful objects (e.g. the
# appliance list carries the device admin password in cleartext). This is the
# one seam that knows data is about to cross into the LLM, so we scrub it here
# on the way out -- the client stays a faithful REST wrapper. Denylist by exact
# field name (case-insensitive) to avoid over-redacting benign fields like
# publicKey; extend the set as new payloads reveal more secret-bearing fields.
_SENSITIVE_FIELDS = {
    "password", "passphrase", "secret", "privatekey", "sharedsecret",
    "presharedkey", "psk", "snmpcommunity", "communitystring",
    "token", "apikey", "authkey",
}
_REDACTED = "***REDACTED***"


def _redact(obj):
    """Recursively replace the value of any sensitive-named field. Non-empty
    values only (leaves null/empty as-is), placeholder rather than deletion so
    the model can see a field was withheld without seeing its value."""
    if isinstance(obj, dict):
        return {
            k: (_REDACTED if k.lower() in _SENSITIVE_FIELDS and v not in (None, "")
                else _redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def _out(data) -> str:
    """Serialize a tool result, scrubbing secrets before it reaches the LLM."""
    return json.dumps(_redact(data))


# ---- Read tools ------------------------------------------------------------

@mcp.tool()
async def list_appliances() -> str:
    """List all EdgeConnect appliances known to the Orchestrator. Each entry's
    "id" field (e.g. "2.NE") is the appliance's nePk -- pass it as ne_pk to the
    other tools."""
    data = await get_client().list_appliances()
    return _out(data)


@mcp.tool()
async def get_appliance_status(ne_pk: str) -> str:
    """Get the live status record for a single EdgeConnect appliance (state,
    model, software version, reachability, reboot/unsaved-changes flags).

    Args:
        ne_pk: The appliance nePk (the "id" field from list_appliances, e.g. "2.NE").
    """
    data = await get_client().get_appliance_status(ne_pk)
    return _out(data)


@mcp.tool()
async def get_tunnel_status(ne_pk: str | None = None) -> str:
    """Get physical tunnel status (operStatus/adminStatus) for one appliance or all.

    Args:
        ne_pk: Optional appliance nePk (e.g. "2.NE"). If omitted, returns tunnels
            across all appliances.
    """
    data = await get_client().get_tunnel_status(ne_pk)
    return _out(data)


@mcp.tool()
async def get_overlay_config() -> str:
    """Get the network-wide SD-WAN overlay configuration. Overlays are not
    per-appliance on EdgeConnect."""
    data = await get_client().get_overlay_config()
    return _out(data)


@mcp.tool()
async def get_appliance_groups() -> str:
    """List the appliance groups configured on the Orchestrator.

    Returns a flat list of group nodes that form a parent/child tree. Each
    node has an id (e.g. "0.Network"), name, subType, and parentId (null for
    the root group). Use an appliance's groupId (from list_appliances) to see
    which group it belongs to. Groups are network-wide, not per-appliance."""
    data = await get_client().get_appliance_groups()
    return _out(data)


@mcp.tool()
async def get_interfaces(ne_pk: str) -> str:
    """List interface and system state for a specific appliance.

    Args:
        ne_pk: The appliance nePk (the "id" field from list_appliances, e.g. "2.NE").
    """
    data = await get_client().get_interfaces(ne_pk)
    return _out(data)


_GRANULARITY_SECONDS = {"minute": 60, "hour": 3600, "day": 86400}


@mcp.tool()
async def get_wan_bandwidth_timeseries(
    ne_pk: str,
    hours_back: int = 24,
    granularity: str = "hour",
    traffic_type: str = "all_traffic",
    interface_name: str | None = None,
) -> str:
    """Get WAN-side bandwidth usage over time for one appliance, as a time
    series ready to graph. READ (safe).

    Returns one point per interval over the last `hours_back` hours ending now,
    with received/transmitted bytes and the average throughput in bits/sec.
    Points are projected to just the bandwidth-relevant fields (the raw
    endpoint returns ~40 counters per point) and sorted oldest-to-newest.

    Note: with no interface_name the endpoint reports the appliance-wide total
    (interfaceName comes back null). Pass an interface_name, and/or a
    pass-through traffic_type, to narrow to a specific WAN view.

    Args:
        ne_pk: The appliance nePk (the "id" from list_appliances, e.g. "2.NE").
        hours_back: How far back from now to fetch, in hours (default 24).
        granularity: Aggregation interval -- "minute", "hour", or "day"
            (default "hour"). "minute" over a long window returns many points.
        traffic_type: Which traffic to measure -- "all_traffic" (default),
            "optimized_traffic", "pass_through_shaped", or
            "pass_through_unshaped". Pass-through types are WAN-side traffic
            that isn't carried in an SD-WAN tunnel.
        interface_name: Optional -- limit to a single interface by name; omit
            for the appliance-wide total.
    """
    if granularity not in _GRANULARITY_SECONDS:
        return _out({"error": f"granularity must be one of {sorted(_GRANULARITY_SECONDS)}"})
    if hours_back <= 0:
        return _out({"error": "hours_back must be a positive number of hours"})

    now = int(time.time())
    start = now - hours_back * 3600
    rows = await get_client().get_interface_bandwidth_timeseries(
        ne_pk, start, now, granularity, traffic_type, interface_name
    )

    interval = _GRANULARITY_SECONDS[granularity]
    series = []
    for r in rows if isinstance(rows, list) else []:
        rx = r.get("rx_bytes") or 0
        tx = r.get("tx_bytes") or 0
        series.append(
            {
                "timestamp": r.get("timestamp"),
                "interfaceName": r.get("interfaceName"),
                "rx_bytes": rx,
                "tx_bytes": tx,
                # Average throughput over the interval, in bits/sec.
                "rx_bps_avg": round(rx * 8 / interval, 2),
                "tx_bps_avg": round(tx * 8 / interval, 2),
                "max_bw_rx": r.get("max_bw_rx"),
                "max_bw_tx": r.get("max_bw_tx"),
            }
        )
    series.sort(key=lambda p: p["timestamp"] or 0)  # endpoint returns newest-first

    return _out(
        {
            "nePk": ne_pk,
            "granularity": granularity,
            "trafficType": traffic_type,
            "interfaceName": interface_name,
            "startTime": start,
            "endTime": now,
            "points": len(series),
            "series": series,
        }
    )


@mcp.tool()
async def get_appliance_location(ne_pk: str) -> str:
    """Get an appliance's stored location and contact metadata.

    This is the read side of write_set_appliance_location -- use it to see the
    address/city/state/zip/country and contact name/email/phone currently
    stored for an appliance (e.g. before changing them). Empty strings mean the
    field is unset. Note: this is administrative metadata, not the appliance's
    logical "site" (which appears in list_appliances).

    Args:
        ne_pk: The appliance nePk (the "id" field from list_appliances, e.g. "2.NE").
    """
    info = await get_client().get_appliance_extra_info(ne_pk)
    location = info.get("location") if isinstance(info, dict) else None
    contact = info.get("contact") if isinstance(info, dict) else None
    return _out({"ne_pk": ne_pk, "location": location, "contact": contact})


# ---- Write tools -----------------------------------------------------------
# These CHANGE state on the Orchestrator when called. Nothing here asks for
# confirmation first. If you want a human in the loop, gate them in your client
# (they are the `write_`-prefixed tools) or hand this server an API key that is
# scoped read-only, in which case they fail at the Orchestrator.

@mcp.tool()
async def write_set_appliance_location(
    ne_pk: str,
    address: str | None = None,
    address2: str | None = None,
    city: str | None = None,
    state: str | None = None,
    zip_code: str | None = None,
    country: str | None = None,
    contact_name: str | None = None,
    contact_email: str | None = None,
    contact_phone: str | None = None,
) -> str:
    """Set an appliance's location and/or contact metadata. This is a WRITE
    operation. Only the fields you provide are changed; all other existing
    fields (including overlay/IPsec settings) are preserved. Non-disruptive to
    traffic and reversible.

    Args:
        ne_pk: The appliance nePk (the "id" field from list_appliances, e.g. "2.NE").
        address, address2, city, state, zip_code, country: Location fields to set.
        contact_name, contact_email, contact_phone: Contact fields to set.
    """
    location = {
        "address": address, "address2": address2, "city": city,
        "state": state, "zipCode": zip_code, "country": country,
    }
    contact = {"name": contact_name, "email": contact_email, "phoneNumber": contact_phone}
    location = {k: v for k, v in location.items() if v is not None} or None
    contact = {k: v for k, v in contact.items() if v is not None} or None
    data = await get_client().set_appliance_location(ne_pk, location=location, contact=contact)
    return _out(data)


@mcp.tool()
async def get_appliance_sse_tunnel_status(ne_pk: str) -> str:
    """Get the state of an appliance's HPE SSE (Axis) IPsec tunnel(s).

    Combines two facts so you don't have to infer SSE state from other tools:
      * enrollment -- whether the appliance is a member of the HPE SSE service
        (the membership that drives its tunnels), and its priority.
      * tunnels -- the live pass-through tunnels whose alias starts with
        "HPESSE" (the SSE IPsec tunnels), each with adminStatus, operStatus,
        mode, and id.

    IMPORTANT: SSE tunnels are pass-through tunnels, NOT the physical/underlay
    tunnels that get_tunnel_status reports. get_tunnel_status returns empty on a
    single-appliance lab regardless of SSE state -- do NOT infer SSE state from
    it. Use THIS tool. An operStatus starting with "Up" means the tunnel is up.

    Args:
        ne_pk: The appliance nePk (the "id" field from list_appliances, e.g. "2.NE").
    """
    client = get_client()
    assoc = await client.get_axis_appliance_association()
    enabled_list = assoc.get("enabled") if isinstance(assoc, dict) else None
    if not isinstance(enabled_list, list):
        enabled_list = []
    member = next(
        (e for e in enabled_list if isinstance(e, dict) and e.get("nePk") == ne_pk),
        None,
    )
    raw = await client.get_passthrough_tunnels(ne_pk)
    entries = raw.values() if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
    tunnels = [
        {
            "alias": t.get("alias"),
            "id": t.get("id"),
            "adminStatus": t.get("adminStatus"),
            "operStatus": t.get("operStatus"),
            "mode": t.get("mode"),
        }
        for t in entries
        if isinstance(t, dict) and str(t.get("alias", "")).startswith("HPESSE")
    ]
    up = all(str(t.get("operStatus", "")).startswith("Up") for t in tunnels) if tunnels else False
    return _out(
        {
            "ne_pk": ne_pk,
            "sse_enabled": member is not None,
            "priority": member.get("priority") if member else None,
            "tunnel_count": len(tunnels),
            "all_tunnels_up": up,
            "tunnels": tunnels,
        }
    )


@mcp.tool()
async def write_set_appliance_sse_tunnel(ne_pk: str, enabled: bool) -> str:
    """Enable or disable the HPE SSE IPsec tunnel(s) for one appliance. This is
    a WRITE operation.

    "HPE SSE" (formerly Axis Security) is a cloud security service. An
    appliance reaches it over IPsec tunnel(s) that the Orchestrator builds and
    orchestrates. Those tunnels are controlled by whether the appliance is
    enabled for the service: enabled=True brings it into the service and the
    tunnel(s) come up; enabled=False removes it and the tunnel(s) go admin-down.
    This affects ALL of that appliance's HPE SSE tunnels together (e.g. the
    A1 + A2 pair), not one individually.

    Args:
        ne_pk: The appliance nePk (the "id" field from list_appliances, e.g. "2.NE").
        enabled: True to enable the HPE SSE tunnel(s); False to disable them.
    """
    data = await get_client().set_appliance_sse_tunnel(ne_pk, enabled)
    return _out(data)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
