"""
Thin wrapper around the HPE EdgeConnect (Orchestrator) REST API.

This is the piece that knows about the API key, endpoint paths, and response
shapes. Nothing MCP-specific lives here on purpose -- server.py is the only
file that knows this is being exposed as MCP tools. That separation means this
client is reusable on its own (scripts, tests, a future non-MCP integration)
without any protocol baggage.

Auth model: API-key auth is stateless -- no login call, no session cookie, no
CSRF token. The key is presented on every request in the `X-Auth-Token` header
(NOT as a URL query param -- see the note in the constants block below). The
exact mechanics live in that clearly-labeled block -- adjust there and re-run
test_client.py to confirm against your Orchestrator.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

# ---- EdgeConnect Orchestrator auth mechanics -------------------------------
# API-key auth is STATELESS -- there is no login call and no CSRF token. (The
# login endpoint is the separate *session* username/password path; probing it
# with an API key returns "Unable to validate CSRF token".)
#
# The key is sent ONLY in this header. The Orchestrator also accepts it as an
# `?apiKey=` query param, but we deliberately avoid that: TLS encrypts it in
# transit either way, yet query strings get written to client/server/proxy
# access logs in cleartext (at rest), while headers do not.
#
# If your Orchestrator version differs, adjust HERE (one place) and re-run
# test_client.py to confirm.
API_KEY_HEADER = "X-Auth-Token"
# ----------------------------------------------------------------------------


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class EdgeConnectClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        verify_tls: bool | None = None,
    ) -> None:
        self.base_url = (base_url or os.environ["EDGECONNECT_BASE_URL"]).rstrip("/")
        self.api_key = api_key or os.environ["EDGECONNECT_API_KEY"]
        # Lab appliances often use a self-signed cert; EDGECONNECT_VERIFY_TLS=false
        # turns verification off. Defaults to on (secure) when unset.
        if verify_tls is None:
            verify_tls = _env_bool("EDGECONNECT_VERIFY_TLS", True)
        self._client = httpx.AsyncClient(base_url=self.base_url, verify=verify_tls, timeout=30.0)

    def _headers(self) -> dict[str, str]:
        return {API_KEY_HEADER: self.api_key}

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = await self._client.get(path, params=params, headers=self._headers())
        resp.raise_for_status()
        return resp.json()

    async def _post(
        self, path: str, payload: dict[str, Any], params: dict[str, Any] | None = None
    ) -> Any:
        resp = await self._client.post(
            path, json=payload, params=params, headers=self._headers()
        )
        resp.raise_for_status()
        # Write endpoints often return an empty body on success.
        if not resp.content:
            return {"status": "ok", "httpStatus": resp.status_code}
        try:
            return resp.json()
        except ValueError:
            return {"status": "ok", "httpStatus": resp.status_code, "body": resp.text}

    # ---- Read operations -------------------------------------------------
    # Endpoints and the `nePk` appliance identifier were verified live against
    # Orchestrator v9.7.0.43266, cross-checked against its OpenAPI spec.
    # `nePk` (e.g. "2.NE") is the appliance's "id" field from list_appliances --
    # NOT the numeric "applianceId" field, which these endpoints do not accept.

    async def list_appliances(self) -> Any:
        """All appliances the Orchestrator knows about (omit nePk = list all)."""
        return await self._get("/gms/rest/appliance")

    async def get_appliance_status(self, ne_pk: str) -> Any:
        """Live record for one appliance: state, model, software version,
        reachability, and the reboot/unsaved-changes flags."""
        return await self._get("/gms/rest/appliance", params={"nePk": ne_pk})

    async def get_tunnel_status(self, ne_pk: str | None = None) -> Any:
        """Physical tunnels (operStatus/adminStatus) for one appliance, or
        across all appliances when ne_pk is omitted."""
        params = {"nePk": ne_pk} if ne_pk else None
        return await self._get("/gms/rest/tunnels2/physical", params=params)

    async def get_overlay_config(self) -> Any:
        """SD-WAN overlay configuration. Overlays are network-wide on
        EdgeConnect, so this is not per-appliance."""
        return await self._get("/gms/rest/gms/overlays/config")

    async def get_appliance_groups(self) -> Any:
        """The Orchestrator's appliance group tree, returned as a flat list of
        nodes (id, name, subType, parentId; parentId is null for the root).
        Groups are network-wide, not per-appliance."""
        return await self._get("/gms/rest/gms/group")

    async def get_interfaces(self, ne_pk: str) -> Any:
        """Interface + system state for one appliance."""
        return await self._get("/gms/rest/interfaceState", params={"nePk": ne_pk})

    async def get_interface_bandwidth_timeseries(
        self,
        ne_pk: str,
        start_time: int,
        end_time: int,
        granularity: str = "hour",
        traffic_type: str = "all_traffic",
        interface_name: str | None = None,
        limit: int = 10000,
    ) -> Any:
        """Interface bandwidth time series for one appliance over the
        [start_time, end_time] window (Unix epoch seconds). Each row carries
        rx_bytes/tx_bytes and max_bw_rx/max_bw_tx per interval. Omit
        interface_name for the appliance-wide total (the endpoint returns a
        null interfaceName in that case). READ."""
        params: dict[str, Any] = {
            "nePk": ne_pk,
            "startTime": start_time,
            "endTime": end_time,
            "granularity": granularity,
            "trafficType": traffic_type,
            "limit": limit,
        }
        if interface_name:
            params["interfaceName"] = interface_name
        return await self._get("/gms/rest/stats/timeseries/interface", params=params)

    # ---- Write operations ------------------------------------------------
    # These change state on the Orchestrator. Nothing in this file confirms
    # anything first -- see the note on the write tools in server.py.
    #
    # Setting an appliance's contact/location metadata is safe, reversible and
    # traffic-neutral. Implemented as read-modify-write so it preserves fields
    # we aren't changing -- notably overlaySettings, which carries real config
    # (ipsecUdpPort) that a blind overwrite would clobber.

    async def get_appliance_extra_info(self, ne_pk: str) -> dict[str, Any]:
        """Current contact/location/overlay metadata for one appliance."""
        return await self._get("/gms/rest/appliance/extraInfo", params={"nePk": ne_pk})

    async def set_appliance_location(
        self,
        ne_pk: str,
        location: dict[str, Any] | None = None,
        contact: dict[str, Any] | None = None,
    ) -> Any:
        """Update an appliance's location and/or contact metadata. Only the
        fields provided are changed; everything else (including overlaySettings)
        is read first and preserved. WRITE operation.
        """
        current = await self.get_appliance_extra_info(ne_pk)
        if not isinstance(current, dict):
            current = {}
        # Deep-merge only the provided sub-fields so a partial update (e.g. just
        # city) doesn't blank out the rest of that section.
        if location:
            current.setdefault("location", {})
            current["location"].update({k: v for k, v in location.items() if v is not None})
        if contact:
            current.setdefault("contact", {})
            current["contact"].update({k: v for k, v in contact.items() if v is not None})
        return await self._post(
            "/gms/rest/appliance/extraInfo", current, params={"nePk": ne_pk}
        )

    # ---- HPE SSE (Axis) service orchestration ----------------------------
    # "HPE SSE" in the Orchestrator UI is the Axis Security service (Axis was
    # rebranded HPE SSE). An appliance's SSE IPsec tunnel(s) are enabled or
    # disabled by its MEMBERSHIP in the service's appliance-association list:
    # GET returns the currently-enabled set, POST writes it. Enable = ensure
    # ne_pk is in the list; disable = POST the list without it. Read-modify-
    # write so we preserve any OTHER enabled appliances rather than replacing
    # the whole set with just this one. The single enabled entry drives ALL of
    # that appliance's HPE SSE tunnels together (e.g. an A1 + A2 pair), not one
    # tunnel individually.

    async def get_axis_appliance_association(self) -> dict[str, Any]:
        """Appliances currently enabled for HPE SSE (Axis) tunnel integration,
        shaped {"enabled": [{"nePk", "priority", "lastUpdateTime"}, ...]}."""
        return await self._get("/gms/rest/thirdPartyServices/axis/applianceAssociation")

    async def get_passthrough_tunnels(self, ne_pk: str) -> dict[str, Any]:
        """All pass-through tunnels for one appliance, keyed by tunnel id. The
        HPE SSE IPsec tunnels are the entries whose alias starts with "HPESSE"
        (mode "ipsec_ip"). NOTE: these are distinct from the physical/underlay
        tunnels that get_tunnel_status reads (/tunnels2/physical), which are
        empty on a single-appliance lab regardless of SSE state."""
        return await self._get("/gms/rest/tunnels2/passThrough", params={"nePk": ne_pk})

    async def set_appliance_sse_tunnel(
        self, ne_pk: str, enabled: bool, priority: int = 1
    ) -> Any:
        """Enable or disable the HPE SSE IPsec tunnel(s) for one appliance by
        adding/removing it from the service's enabled-appliance list. WRITE --
        this is traffic-affecting. priority=1 requests an immediate
        orchestration poll (the Orchestrator auto-resets it to 0 after
        processing)."""
        current = await self.get_axis_appliance_association()
        enabled_list = current.get("enabled") if isinstance(current, dict) else None
        if not isinstance(enabled_list, list):
            enabled_list = []
        # Drop any existing entry for this appliance, then re-add it only when
        # enabling. This makes the call idempotent and avoids duplicate entries.
        others = [
            e for e in enabled_list
            if isinstance(e, dict) and e.get("nePk") != ne_pk
        ]
        if enabled:
            others.append(
                {"nePk": ne_pk, "priority": priority, "lastUpdateTime": int(time.time() * 1000)}
            )
        return await self._post(
            "/gms/rest/thirdPartyServices/axis/applianceAssociation", {"enabled": others}
        )

    async def close(self) -> None:
        await self._client.aclose()
