"""Verify operational state of all devices via RESTCONF.

Each request explicitly logs the HTTP status code so the operator
sees whether the call succeeded (200), the node was missing (404),
or something failed (4xx/5xx).

Exit code: 0 if all checks pass, 1 if any fail.
"""
import os
import sys
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth
import urllib3
import yaml
from dotenv import load_dotenv

# Self-signed cert warnings zijn ruis in lab-context — onderdruk ze.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

USERNAME = os.environ["ROUTER_USERNAME"]
PASSWORD = os.environ["ROUTER_PASSWORD"]


def load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)


class CheckResult:
    def __init__(self):
        self.passed = 0
        self.failed = 0

    def ok(self, msg):
        print(f"  ✓ {msg}")
        self.passed += 1

    def fail(self, msg):
        print(f"  ✗ {msg}")
        self.failed += 1

    def info(self, msg):
        print(f"  ⊘ {msg}")

    @property
    def total(self):
        return self.passed + self.failed


def restconf_get(host, path):
    """RESTCONF GET. Always logs the HTTP status code visibly.

    Returns (status_code, parsed_json or None).
    """
    url = f"https://{host}/restconf/data/{path}"
    headers = {"Accept": "application/yang-data+json"}
    try:
        r = requests.get(
            url,
            auth=HTTPBasicAuth(USERNAME, PASSWORD),
            headers=headers,
            verify=False,
            timeout=10,
        )
    except requests.exceptions.RequestException as exc:
        print(f"  [HTTP ERROR] /{path} → {exc}")
        return None, None

    print(f"  [HTTP {r.status_code}] GET /{path}")

    if r.status_code == 200:
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, None
    return r.status_code, None


# ─────────────────────────────────────────────────────────────
# Checks
# ─────────────────────────────────────────────────────────────

def check_hostname(host, expected, results):
    status, data = restconf_get(host, "Cisco-IOS-XE-native:native/hostname")
    if status != 200:
        results.fail(f"hostname: HTTP {status}")
        return
    actual = data.get("Cisco-IOS-XE-native:hostname") if data else None
    if actual == expected:
        results.ok(f"hostname = {actual}")
    else:
        results.fail(f"hostname mismatch: expected '{expected}', got '{actual}'")


def check_interfaces(host, expected_names, results):
    status, data = restconf_get(host, "ietf-interfaces:interfaces-state")
    if status != 200:
        results.fail(f"interfaces: HTTP {status}")
        return

    interfaces = (data or {}).get("ietf-interfaces:interfaces-state", {}).get("interface", [])
    name_to_status = {i.get("name"): i.get("oper-status") for i in interfaces}

    for n in expected_names:
        st = name_to_status.get(n, "<not-found>")
        if st == "up":
            results.ok(f"{n} oper-status = up")
        elif "." in n and st == "lower-layer-down":
            # Sub-interface kan niet up zonder parent carrier — geen failure.
            results.info(f"{n} oper-status = lower-layer-down (parent down — skipped)")
        else:
            results.fail(f"{n} oper-status = {st} (expected up)")


def check_ospf_and_reachability(host, expected_peer_router_ids, results):
    status, data = restconf_get(host, "Cisco-IOS-XE-ospf-oper:ospf-oper-data")
    if status != 200:
        results.fail(f"OSPF oper data: HTTP {status}")
        return

    # Walk JSON recursively zoeken naar (nbr-id, state) paren.
    neighbors = []

    def walk(obj):
        if isinstance(obj, dict):
            st = obj.get("state")
            nid = obj.get("nbr-id") or obj.get("neighbor-id")
            if st and nid:
                neighbors.append((nid, st))
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(data)

    if not neighbors:
        results.fail("no OSPF neighbors found")
        return

    full = {nid for nid, st in neighbors if "full" in str(st).lower()}
    details = ", ".join(f"{nid}={st}" for nid, st in neighbors)
    results.ok(f"OSPF adjacencies: {details}")

    for peer_rid in expected_peer_router_ids:
        if peer_rid in full:
            results.ok(f"peer {peer_rid} reachable (OSPF FULL)")
        else:
            results.fail(f"peer {peer_rid} NOT reachable (no FULL adjacency)")


def check_default_gateway(host, expected_gw, results):
    status, data = restconf_get(host, "Cisco-IOS-XE-native:native/ip/default-gateway")
    if status == 200:
        gw = data.get("Cisco-IOS-XE-native:default-gateway") if data else None
        if gw == expected_gw:
            results.ok(f"default-gateway = {gw}")
        else:
            results.fail(f"default-gateway mismatch: expected {expected_gw}, got {gw}")
    elif status == 404:
        results.fail(f"default-gateway not configured (HTTP 404)")
    else:
        results.fail(f"default-gateway: HTTP {status}")


# ─────────────────────────────────────────────────────────────
# Per-device + main
# ─────────────────────────────────────────────────────────────

def verify_device(name, device, cfg, peers):
    print(f"\n=== Verify {name} ({device['host']}) via RESTCONF ===")
    results = CheckResult()

    # Build de verwachte interface-namen
    expected_intf_names = []
    for intf in cfg.get("interfaces") or []:
        if intf.get("enabled", True) and not intf.get("no_ip", False):
            expected_intf_names.append(f"GigabitEthernet{intf['name']}")
    for lo in cfg.get("loopbacks") or []:
        expected_intf_names.append(f"Loopback{lo['name']}")
    for svi in cfg.get("svi_interfaces") or []:
        if svi.get("enabled", True):
            expected_intf_names.append(f"Vlan{svi['vlan']}")

    # Verwachte OSPF peer router-ids (alle andere routers in inventory)
    expected_peers = []
    for peer_name, peer_cfg in peers.items():
        if peer_name == name:
            continue
        if peer_cfg.get("ospf"):
            rid = peer_cfg["ospf"].get("router_id")
            if rid:
                expected_peers.append(rid)

    host = device["host"]
    check_hostname(host, cfg["hostname"], results)
    if expected_intf_names:
        check_interfaces(host, expected_intf_names, results)
    if cfg.get("ospf"):
        check_ospf_and_reachability(host, expected_peers, results)
    if cfg.get("default_gateway"):
        check_default_gateway(host, cfg["default_gateway"], results)

    print(f"\n  Result: {results.passed}/{results.total} passed")
    return results


def main():
    devices = load_yaml(PROJECT_ROOT / "inventory/devices.yaml")["devices"]

    configs = {}
    for name in devices:
        cfg_path = PROJECT_ROOT / f"configs/{name.lower()}.yaml"
        if cfg_path.exists():
            configs[name] = load_yaml(cfg_path)

    skip_unreachable = {"SW1"}

    overall_failed = 0
    overall_total = 0
    for name, device in devices.items():
        if name in skip_unreachable:
            print(f"\n[skip] {name} (hardware offline — see README)")
            continue
        if name not in configs:
            print(f"[skip] {name}: no config file")
            continue
        try:
            results = verify_device(name, device, configs[name], configs)
            overall_failed += results.failed
            overall_total += results.total
        except Exception as exc:
            print(f"[error] {name}: {exc}", file=sys.stderr)
            overall_failed += 1
            overall_total += 1

    print("\n" + "=" * 60)
    if overall_failed == 0:
        print(f"✓ All {overall_total} checks passed.")
        sys.exit(0)
    else:
        print(f"✗ {overall_failed}/{overall_total} checks failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
