"""
Read-only smoke test for EdgeConnectClient.

This is NOT a unit test -- it needs a real Orchestrator to talk to. Its purpose
is to confirm the REST wrapper works against YOUR Orchestrator (stateless
API-key auth, plus every read method) before any MCP client is involved. It
never calls a write method, so it cannot change device state.

Point it at an Orchestrator with the same environment variables the server
uses:

    export EDGECONNECT_BASE_URL=https://orchestrator.example.com
    export EDGECONNECT_API_KEY=...
    export EDGECONNECT_VERIFY_TLS=false   # only if self-signed
    python test_client.py

Or inside the container image:

    docker run --rm -e EDGECONNECT_BASE_URL -e EDGECONNECT_API_KEY \
        hpe-edgeconnect-mcp python test_client.py

Read the notes it prints: any 4xx/5xx means a path or an auth constant in
edgeconnect_client.py needs adjusting for your Orchestrator version.
"""

from __future__ import annotations

import asyncio
import json
import os

import httpx

from edgeconnect_client import (
    API_KEY_HEADER,
    EdgeConnectClient,
)

# The appliance nePk is carried in the "id" field of a list_appliances entry
# (e.g. "2.NE"). NOTE: the numeric "applianceId" field is a DIFFERENT id that
# the nePk-based endpoints reject -- do not use it here.
APPLIANCE_ID_KEYS = ("nePk", "id")


def _mask(secret: str) -> str:
    if not secret:
        return "(empty!)"
    return f"{secret[:4]}...{secret[-2:]} (len {len(secret)})" if len(secret) > 6 else "(set)"


def _preview(data: object, limit: int = 600) -> str:
    text = json.dumps(data, indent=2, default=str)
    return text if len(text) <= limit else text[:limit] + f"\n... [truncated, {len(text)} chars total]"


def _find_appliance_id(appliances: object) -> str | None:
    # Response might be a list of appliances or a dict keyed by id.
    candidates: list = []
    if isinstance(appliances, list):
        candidates = appliances
    elif isinstance(appliances, dict):
        # Either a dict keyed by appliance id, or {"data": [...]} style.
        for wrapper_key in ("data", "appliances", "result"):
            if isinstance(appliances.get(wrapper_key), list):
                candidates = appliances[wrapper_key]
                break
        else:
            # Keyed-by-id shape: first key is often the appliance id itself.
            first_key = next(iter(appliances), None)
            if first_key is not None:
                return str(first_key)
    for item in candidates:
        if isinstance(item, dict):
            for key in APPLIANCE_ID_KEYS:
                if item.get(key):
                    return str(item[key])
    return None


async def _run_read(label: str, coro) -> object | None:
    print(f"\n=== {label} ===")
    try:
        data = await coro
    except httpx.HTTPStatusError as exc:
        print(f"  HTTP {exc.response.status_code}: {exc.response.text[:300]}")
        return None
    except Exception as exc:  # noqa: BLE001 -- smoke test: report and keep going
        print(f"  ERROR ({type(exc).__name__}): {exc}")
        return None
    print(_preview(data))
    return data


async def main() -> None:
    base_url = os.environ.get("EDGECONNECT_BASE_URL", "")
    api_key = os.environ.get("EDGECONNECT_API_KEY", "")
    verify = os.environ.get("EDGECONNECT_VERIFY_TLS", "(unset -> defaults true)")

    print("EdgeConnect client smoke test")
    print("-" * 40)
    print(f"  EDGECONNECT_BASE_URL   = {base_url or '(empty!)'}")
    print(f"  EDGECONNECT_API_KEY    = {_mask(api_key)}")
    print(f"  EDGECONNECT_VERIFY_TLS = {verify}")
    print(f"  auth: header           = {API_KEY_HEADER}: <key>  (stateless; no key in URL)")

    if not base_url or not api_key:
        print("\nMissing EDGECONNECT_BASE_URL or EDGECONNECT_API_KEY -- check .env.")
        raise SystemExit(1)

    client = EdgeConnectClient()
    try:
        # Stateless API-key auth: the first read call is also the auth check.
        # A 401 "API Key is invalid" here means the key value (not the wiring)
        # needs attention -- see the notes printed at the end.
        appliances = await _run_read("list_appliances", client.list_appliances())

        # Overlays are network-wide (no appliance id needed).
        await _run_read("get_overlay_config", client.get_overlay_config())

        # Appliance groups are network-wide (no appliance id needed).
        await _run_read("get_appliance_groups", client.get_appliance_groups())

        ne_pk = _find_appliance_id(appliances) if appliances is not None else None
        if ne_pk:
            print(f"\n(Using ne_pk = {ne_pk!r} for per-appliance reads.)")
            await _run_read("get_appliance_status", client.get_appliance_status(ne_pk))
            await _run_read("get_interfaces", client.get_interfaces(ne_pk))
            await _run_read("get_tunnel_status (appliance)", client.get_tunnel_status(ne_pk))
            await _run_read("get_axis_appliance_association", client.get_axis_appliance_association())
            await _run_read("get_passthrough_tunnels", client.get_passthrough_tunnels(ne_pk))
            await _run_read("get_appliance_extra_info", client.get_appliance_extra_info(ne_pk))
        else:
            print(
                "\n(No nePk found in list_appliances output -- skipping "
                "per-appliance reads. If the list wasn't empty, check "
                "APPLIANCE_ID_KEYS.)"
            )

        await _run_read("get_tunnel_status (all)", client.get_tunnel_status())
    finally:
        await client.close()

    print("\nDone. Any block above with an HTTP/ERROR line needs a path or "
          "auth-constant tweak in edgeconnect_client.py.")


if __name__ == "__main__":
    asyncio.run(main())
