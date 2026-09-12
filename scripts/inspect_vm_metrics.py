#!/usr/bin/env python
"""Standalone diagnostic tool that samples raw metric (stat) and property data for
VMs directly from a VCF Operations / Aria Operations Suite API endpoint.

This script is intentionally self-contained: it has NO dependency on this project's
`app` package (no SQLAlchemy, no httpx, no cryptography) and uses only the Python
standard library (urllib, ssl, json). That means it can be run with a bare system
`python3` - no virtualenv, no `pip install` - which is the whole point: you connect
it straight to VCF Operations with the base URL/credentials on the command line,
instead of looking up an already-registered integration account in the app's
database (that lookup is what `inspect_integration_account.py` does, and why it
needs the app's own virtualenv to run).

It samples a VM's full property set AND its available performance metrics (stat
keys + latest values, optionally cross-referenced against the metric catalog for
readable names/units). Nothing here is wired into the collector - this is purely
for eyeballing what the real environment actually returns before deciding how (or
whether) to fold usage metrics into billing.

Endpoints used (VMware Aria/vRealize Operations Suite API - exact response shape
can vary by version/management pack):
  - POST /suite-api/api/auth/token/acquire                                  auth
  - GET  /suite-api/api/resources?resourceKind=VirtualMachine               VM list (paged)
  - GET  /suite-api/api/resources/{id}/properties                          properties
  - GET  /suite-api/api/resources/{id}/stats/latest[?statKey=...]          latest metric sample
  - GET  /suite-api/api/adapterkinds/{adapterKindKey}/resourcekinds/{resourceKindKey}/statkeys
         metric catalog (key -> name/unit/description) for the VM's resource kind

Usage:
    python3 inspect_vm_metrics.py --base-url https://vrops.example.com --username svc --insecure
    python3 inspect_vm_metrics.py --base-url https://vrops.example.com --username svc --insecure --vm-name web01
    python3 inspect_vm_metrics.py --base-url https://vrops.example.com --username svc --insecure --vm-id <resourceId>
    python3 inspect_vm_metrics.py --base-url https://vrops.example.com --username svc --insecure --stat-key cpu|demandPct
    python3 inspect_vm_metrics.py --base-url https://vrops.example.com --username svc --insecure --show-catalog
    python3 inspect_vm_metrics.py --base-url https://vrops.example.com --username svc --insecure --output-json sample.json

If --password is omitted you will be prompted for it (hidden input), so it never
needs to be typed into shell history.
"""
from __future__ import annotations

import argparse
import getpass
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request


def _build_ssl_context(verify_ssl: bool):
    """Mirror app/integrations/vcf_ops_client.py's SSL context, without importing it.

    verify_ssl=False means "this account points at an appliance with a self-signed
    (or otherwise unverifiable) certificate" - so we not only skip certificate/
    hostname verification, but also relax the cipher/TLS-version floor, since older
    VCF Operations/Aria Operations appliances can fail the handshake entirely under
    Python's default OpenSSL security level even with verify=False.
    """
    if verify_ssl:
        return ssl.create_default_context()

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
    except ssl.SSLError:  # noqa: BLE001 - some OpenSSL builds reject SECLEVEL=0
        print("[!] Failed to set SSL SECLEVEL=0 - continuing with default ciphers")
    if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
        ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
    try:
        ctx.minimum_version = ssl.TLSVersion.TLSv1
    except (ValueError, AttributeError):  # noqa: BLE001 - platform may not allow this low a floor
        pass
    return ctx


class SuiteApiClient:
    """Minimal Suite API client (auth + GET with 401 retry) using only urllib."""

    def __init__(self, base_url: str, username: str, password: str, auth_source: str, verify_ssl: bool):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.auth_source = auth_source
        self._ctx = _build_ssl_context(verify_ssl)
        self._token: str | None = None

    def _authenticate(self, force: bool = False) -> str:
        if self._token and not force:
            return self._token
        body = json.dumps(
            {"username": self.username, "password": self.password, "authSource": self.auth_source}
        ).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/suite-api/api/auth/token/acquire",
            data=body,
            method="POST",
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, context=self._ctx, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        self._token = payload["token"]
        return self._token

    def _get(self, path: str, params: list[tuple[str, str]] | None = None) -> dict:
        query = f"?{urllib.parse.urlencode(params, doseq=True)}" if params else ""
        url = self.base_url + path + query

        def _do(force_reauth: bool) -> dict:
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"vRealizeOpsToken {self._authenticate(force=force_reauth)}",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, context=self._ctx, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))

        try:
            return _do(force_reauth=False)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                print(f"[i] Auth token appears to have expired - re-authenticating and retrying ({path})")
                return _do(force_reauth=True)
            raise

    def iter_resources(self, resource_kind: str, page_size: int = 1000):
        page = 0
        while True:
            body = self._get(
                "/suite-api/api/resources",
                params=[("resourceKind", resource_kind), ("page", str(page)), ("pageSize", str(page_size))],
            )
            items = body.get("resourceList", [])
            yield from items
            if not items:
                break
            page_info = body.get("pageInfo", {}) or {}
            total = page_info.get("totalCount", len(items))
            page += 1
            if page * page_size >= total:
                break

    def fetch_properties(self, resource_id: str) -> dict:
        body = self._get(f"/suite-api/api/resources/{resource_id}/properties")
        return {p["name"]: p.get("value") for p in body.get("property", [])}

    def fetch_latest_stats(self, resource_id: str, stat_keys: list[str] | None) -> dict:
        """GET .../stats/latest, returning {statKey: {"value": ..., "timestamp": ...}}.

        Omitting statKey entirely returns every stat key currently available for the
        resource, which is exactly what a "sample everything" investigation wants.
        """
        params = [("statKey", k) for k in stat_keys] if stat_keys else None
        body = self._get(f"/suite-api/api/resources/{resource_id}/stats/latest", params=params)
        result: dict[str, dict] = {}
        for value_block in body.get("values", []):
            stat_list = (value_block.get("stat-list") or {}).get("stat", [])
            for stat in stat_list:
                key = (stat.get("statKey") or {}).get("key")
                if not key:
                    continue
                data = stat.get("data") or []
                timestamps = stat.get("timestamps") or []
                result[key] = {
                    "value": data[-1] if data else None,
                    "timestamp": timestamps[-1] if timestamps else None,
                }
        return result

    def fetch_metric_catalog(self, adapter_kind_key: str | None, resource_kind_key: str | None) -> dict:
        """GET the stat-key catalog (name/description/unit) for a resource kind.

        Best-effort: some environments restrict this endpoint, so failures here
        should not abort the whole sample.
        """
        if not adapter_kind_key or not resource_kind_key:
            print("[!] Skipping metric catalog fetch - adapterKindKey/resourceKindKey unknown for this VM.")
            return {}
        try:
            body = self._get(f"/suite-api/api/adapterkinds/{adapter_kind_key}/resourcekinds/{resource_kind_key}/statkeys")
        except Exception as exc:  # noqa: BLE001 - diagnostic script, show the failure and move on
            print(f"[!] Metric catalog fetch failed ({adapter_kind_key}/{resource_kind_key}): {exc}")
            return {}
        catalog: dict[str, dict] = {}
        for attr in body.get("resourceTypeAttributes", []):
            key = attr.get("key")
            if not key:
                continue
            catalog[key] = {"name": attr.get("name"), "unit": attr.get("unit"), "description": attr.get("description")}
        return catalog


def _print_json(label: str, obj, limit: int | None = 4000) -> None:
    print(f"\n--- {label} ---")
    text = json.dumps(obj, indent=2, ensure_ascii=False)
    print(text[:limit] if limit else text)


def _select_vms(client: SuiteApiClient, vm_id: str | None, vm_name: str | None, max_vms: int) -> list[dict]:
    if vm_id:
        for item in client.iter_resources("VirtualMachine"):
            if item["identifier"] == vm_id:
                return [item]
        print(f"[!] VM id {vm_id!r} was not found in the VirtualMachine resource list - using it anyway with a placeholder name.")
        return [{"identifier": vm_id, "resourceKey": {"name": vm_id, "adapterKindKey": None, "resourceKindKey": "VirtualMachine"}}]

    vm_items = list(client.iter_resources("VirtualMachine"))
    if vm_name:
        needle = vm_name.lower()
        vm_items = [v for v in vm_items if needle in v.get("resourceKey", {}).get("name", "").lower()]
        if not vm_items:
            print(f"No VM name contains {vm_name!r}.")
            return []
    return vm_items[:max_vms]


def inspect(
    client: SuiteApiClient,
    vm_id: str | None,
    vm_name: str | None,
    max_vms: int,
    stat_keys: list[str] | None,
    show_catalog: bool,
    max_property_values: int,
    output_json: str | None,
) -> None:
    output: dict = {"base_url": client.base_url, "vms": []}

    token = client._authenticate()  # noqa: SLF001 - diagnostic script, calling internals directly
    print(f"Authenticated OK (token length {len(token)})")

    vms = _select_vms(client, vm_id, vm_name, max_vms)
    if not vms:
        return

    for item in vms:
        vm_res_id = item["identifier"]
        key_info = item.get("resourceKey", {})
        vm_name_disp = key_info.get("name", vm_res_id)
        adapter_kind_key = key_info.get("adapterKindKey")
        resource_kind_key = key_info.get("resourceKindKey")
        print(f"\n{'=' * 70}\nVM: {vm_name_disp} (id={vm_res_id}, adapterKindKey={adapter_kind_key}, resourceKindKey={resource_kind_key})")

        try:
            props = client.fetch_properties(vm_res_id)
        except Exception as exc:  # noqa: BLE001 - keep sampling other VMs even if one fails
            print(f"[!] Property fetch failed: {exc}")
            props = {}
        print(f"\nTotal properties: {len(props)}")
        _print_json("Property key list (names only)", sorted(props.keys()))
        _print_json(f"Property values (first {max_property_values})", dict(list(props.items())[:max_property_values]))

        catalog: dict = {}
        if show_catalog:
            catalog = client.fetch_metric_catalog(adapter_kind_key, resource_kind_key)
            print(f"\nMetric catalog entries for resource kind {resource_kind_key!r}: {len(catalog)}")
            _print_json("Metric catalog sample (first 40)", dict(list(catalog.items())[:40]))

        try:
            stats = client.fetch_latest_stats(vm_res_id, stat_keys)
        except Exception as exc:  # noqa: BLE001
            print(f"[!] Latest-stats fetch failed: {exc}")
            stats = {}
        print(f"\nAvailable stat keys with a current value: {len(stats)}")
        for key in sorted(stats.keys()):
            sample = stats[key]
            label = catalog.get(key, {}).get("name")
            unit = catalog.get(key, {}).get("unit")
            suffix = f"  ({label}, unit={unit})" if label else ""
            print(f"  {key} = {sample['value']}  @ {sample['timestamp']}{suffix}")

        output["vms"].append(
            {
                "id": vm_res_id,
                "name": vm_name_disp,
                "adapterKindKey": adapter_kind_key,
                "resourceKindKey": resource_kind_key,
                "property_count": len(props),
                "properties": props,
                "metric_catalog": catalog,
                "latest_stats": stats,
            }
        )

    if output_json:
        with open(output_json, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
        print(f"\nFull sample written to {output_json}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Sample raw VM metric (stat) and property data directly from VCF Operations/Aria Operations "
        "(no app dependency - stdlib only, run with plain python3)"
    )
    parser.add_argument("--base-url", required=True, help="e.g. https://vrops.example.com (no trailing /suite-api)")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", default=None, help="Omit to be prompted (hidden input) instead of typing it into shell history")
    parser.add_argument("--auth-source", default="LOCAL", help="Aria Operations auth source (default: LOCAL)")
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Skip TLS certificate/hostname verification (self-signed appliance certs, common in on-prem VCF Operations)",
    )
    parser.add_argument("--vm-id", type=str, default=None, help="Inspect one specific VM resource identifier (skips VM selection)")
    parser.add_argument("--vm-name", type=str, default=None, help="Filter VMs by a case-insensitive name substring")
    parser.add_argument("--max-vms", type=int, default=1, help="Number of VMs to sample when --vm-id is not given (default 1)")
    parser.add_argument(
        "--stat-key",
        action="append",
        dest="stat_keys",
        default=None,
        help="Restrict the latest-stat sample to this stat key (repeatable). Omit to sample every available stat key.",
    )
    parser.add_argument(
        "--show-catalog",
        action="store_true",
        help="Also fetch and print the metric catalog (key -> name/unit/description) for the VM's resource kind",
    )
    parser.add_argument(
        "--max-property-values",
        type=int,
        default=40,
        help="How many property VALUES to print (key list is always printed in full, default 40)",
    )
    parser.add_argument("--output-json", type=str, default=None, help="Also write the full sample (properties + catalog + latest stats) to this JSON file")
    args = parser.parse_args()

    password = args.password if args.password is not None else getpass.getpass("Password: ")

    client = SuiteApiClient(
        base_url=args.base_url,
        username=args.username,
        password=password,
        auth_source=args.auth_source,
        verify_ssl=not args.insecure,
    )
    inspect(
        client,
        args.vm_id,
        args.vm_name,
        args.max_vms,
        args.stat_keys,
        args.show_catalog,
        args.max_property_values,
        args.output_json,
    )
