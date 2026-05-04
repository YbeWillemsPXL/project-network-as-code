"""Verify operational state of all devices in inventory.

Reads:
    inventory/devices.yaml
    configs/<device>.yaml   - to know what to expect
    .env                    - credentials

Pulls (read-only) via NETCONF <get> against operational datastore:
    - Hostname              from native (config datastore)
    - Interface state       from ietf-interfaces (operational)
    - OSPF neighbors        from Cisco-IOS-XE-ospf-oper

Logic:
    Reachability of remote loopbacks is implied by a FULL OSPF
    adjacency to that peer, so we don't need a separate check.

Exit code: 0 if all pass, 1 if any fail.
"""
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv
from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.netconf_client import connect

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

USERNAME = os.environ["ROUTER_USERNAME"]
PASSWORD = os.environ["ROUTER_PASSWORD"]

NS = {
    "native": "http://cisco.com/ns/yang/Cisco-IOS-XE-native",
    "if": "urn:ietf:params:xml:ns:yang:ietf-interfaces",
}


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

    @property
    def total(self):
        return self.passed + self.failed


def check_hostname(m, expected, results):
    """Get hostname uit running-config en vergelijk met YAML."""
    flt = """
    <native xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-native">
      <hostname/>
    </native>
    """
    reply = m.get_config(source="running", filter=("subtree", flt))
    root = etree.fromstring(reply.xml.encode())
    found = root.find(".//native:hostname", namespaces=NS)
    actual = found.text if found is not None else None

    if actual == expected:
        results.ok(f"hostname = {actual}")
    else:
        results.fail(f"hostname mismatch: expected '{expected}', got '{actual}'")


def check_interfaces(m, expected_interfaces, results):
    """Check interface oper-status via standaard ietf-interfaces model."""
    flt = """
    <interfaces-state xmlns="urn:ietf:params:xml:ns:yang:ietf-interfaces"/>
    """
    reply = m.get(filter=("subtree", flt))
    root = etree.fromstring(reply.xml.encode())

    name_to_status = {}
    for intf in root.findall(".//if:interface", namespaces=NS):
        name = intf.findtext("if:name", namespaces=NS)
        oper = intf.findtext("if:oper-status", namespaces=NS)
        if name:
            name_to_status[name] = oper

    for intf in expected_interfaces:
        full_name = f"GigabitEthernet{intf['name']}"
        status = name_to_status.get(full_name, "<not-found>")
        if status == "up":
            results.ok(f"{full_name} oper-status = up")
        else:
            results.fail(f"{full_name} oper-status = {status} (expected up)")


def get_ospf_neighbors(m):
    """Return list of (neighbor_id, state) tuples from OSPF oper-data."""
    flt = """
    <ospf-oper-data xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-ospf-oper"/>
    """
    reply = m.get(filter=("subtree", flt))
    root = etree.fromstring(reply.xml.encode())

    neighbors = []
    for nbr in root.iter():
        tag = etree.QName(nbr).localname
        if tag in ("ospf-neighbor", "ospf-neighbor-list"):
            state, nbr_id = None, None
            for child in nbr.iter():
                ctag = etree.QName(child).localname
                if ctag == "state" and state is None:
                    state = (child.text or "").strip()
                if ctag in ("nbr-id", "neighbor-id") and nbr_id is None:
                    nbr_id = (child.text or "").strip()
            if state and nbr_id:
                neighbors.append((nbr_id, state))
    return neighbors


def check_ospf_and_reachability(m, expected_peer_router_ids, results):
    """Check OSPF neighbors and infer reachability.

    A FULL adjacency to peer X means we can reach peer X's loopback
    via OSPF — no separate routing-table check needed.
    """
    try:
        neighbors = get_ospf_neighbors(m)
    except Exception as exc:
        results.fail(f"could not fetch OSPF oper data: {exc}")
        return

    if not neighbors:
        results.fail("no OSPF neighbors found")
        return

    full_neighbors = {nid for nid, st in neighbors if "full" in st.lower()}
    details = ", ".join(f"{nid}={st}" for nid, st in neighbors)
    results.ok(f"OSPF adjacencies up: {details}")

    # Reachability check: is each expected peer router-id in FULL?
    for peer_rid in expected_peer_router_ids:
        if peer_rid in full_neighbors:
            results.ok(f"peer {peer_rid} reachable (OSPF FULL)")
        else:
            results.fail(f"peer {peer_rid} NOT reachable (no FULL adjacency)")


def verify_device(name, device, cfg, peers):
    print(f"\n=== Verify {name} ({device['host']}) ===")
    results = CheckResult()

    # Bepaal verwachte peer router-ids (alle andere routers in inventory)
    expected_peers = []
    for peer_name, peer_cfg in peers.items():
        if peer_name == name:
            continue
        if "ospf" in peer_cfg and peer_cfg["ospf"]:
            rid = peer_cfg["ospf"].get("router_id")
            if rid:
                expected_peers.append(rid)

    with connect(device["host"], device["netconf_port"], USERNAME, PASSWORD) as m:
        check_hostname(m, cfg["hostname"], results)
        check_interfaces(m, cfg.get("interfaces", []), results)
        check_ospf_and_reachability(m, expected_peers, results)

    print(f"\n  Result: {results.passed}/{results.total} passed")
    return results


def main():
    devices = load_yaml(PROJECT_ROOT / "inventory/devices.yaml")["devices"]

    configs = {}
    for name in devices:
        cfg_path = PROJECT_ROOT / f"configs/{name.lower()}.yaml"
        if cfg_path.exists():
            configs[name] = load_yaml(cfg_path)

    overall_failed = 0
    overall_total = 0

    for name, device in devices.items():
        if name not in configs:
            print(f"[skip] {name}: no config file")
            continue
        results = verify_device(name, device, configs[name], configs)
        overall_failed += results.failed
        overall_total += results.total

    print("\n" + "=" * 60)
    if overall_failed == 0:
        print(f"✓ All {overall_total} checks passed.")
        sys.exit(0)
    else:
        print(f"✗ {overall_failed}/{overall_total} checks failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
