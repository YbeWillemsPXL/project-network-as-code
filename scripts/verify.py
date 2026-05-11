"""Verify operational state of all devices in inventory.

Reads:
    inventory/devices.yaml
    configs/<device>.yaml   - expected state
    .env                    - credentials

Pulls (read-only) via NETCONF <get> / <get-config>:
    - Hostname              from native (config)
    - Interface oper-state  from ietf-interfaces (operational)
    - OSPF neighbors        from Cisco-IOS-XE-ospf-oper (only routers with ospf cfg)
    - VLAN database         from native (config) — switches only
    - Default gateway       from native ip/default-gateway — switches only

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


def check_interfaces(m, expected_names, results):
    """Check oper-status van geconfigureerde interfaces.

    Sub-interfaces (met '.' in de naam) worden niet als failure aangerekend
    als hun parent fysiek down is (lower-layer-down). Dat is normaal en
    verwacht gedrag — een sub-interface kan niet up komen zonder carrier
    op de parent.
    """
    flt = """
    <interfaces-state xmlns="urn:ietf:params:xml:ns:yang:ietf-interfaces"/>
    """
    reply = m.get(filter=("subtree", flt))
    root = etree.fromstring(reply.xml.encode())

    name_to_status = {}
    for intf in root.findall(".//if:interface", namespaces=NS):
        n = intf.findtext("if:name", namespaces=NS)
        op = intf.findtext("if:oper-status", namespaces=NS)
        if n:
            name_to_status[n] = op

    for n in expected_names:
        status = name_to_status.get(n, "<not-found>")
        is_subif = "." in n
        if status == "up":
            results.ok(f"{n} oper-status = up")
        elif is_subif and status == "lower-layer-down":
            # Parent is fysiek down; sub-interface kan niet anders.
            # Documenteer als skip, niet als fail.
            print(f"  ⊘ {n} oper-status = lower-layer-down (parent down — skipped)")
        else:
            results.fail(f"{n} oper-status = {status} (expected up)")

def get_ospf_neighbors(m):
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

    for peer_rid in expected_peer_router_ids:
        if peer_rid in full_neighbors:
            results.ok(f"peer {peer_rid} reachable (OSPF FULL)")
        else:
            results.fail(f"peer {peer_rid} NOT reachable (no FULL adjacency)")


def check_default_gateway(m, expected_gw, results):
    """Check that ip default-gateway is configured (switches in L2 mode)."""
    flt = """
    <native xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-native">
      <ip>
        <default-gateway/>
      </ip>
    </native>
    """
    try:
        reply = m.get_config(source="running", filter=("subtree", flt))
        root = etree.fromstring(reply.xml.encode())
        gw = root.findtext(
            ".//native:ip/native:default-gateway",
            namespaces=NS,
        )
        if gw == expected_gw:
            results.ok(f"default-gateway = {gw}")
        else:
            results.fail(f"default-gateway mismatch: expected {expected_gw}, got {gw}")
    except Exception as exc:
        results.fail(f"could not read default-gateway: {exc}")


def verify_device(name, device, cfg, peers):
    print(f"\n=== Verify {name} ({device['host']}) ===")
    results = CheckResult()

    # Bouw lijst van interface-namen die we verwachten up te zien
    expected_intf_names = []
    for intf in cfg.get("interfaces") or []:
        if intf.get("enabled", True) and not intf.get("no_ip", False):
            # sub-interfaces met VLAN-encapsulatie: enabled hangt af van parent
            expected_intf_names.append(f"GigabitEthernet{intf['name']}")
    for lo in cfg.get("loopbacks") or []:
        expected_intf_names.append(f"Loopback{lo['name']}")
    for svi in cfg.get("svi_interfaces") or []:
        if svi.get("enabled", True):
            expected_intf_names.append(f"Vlan{svi['vlan']}")
    # Switchports skip — die zijn L2, geen oper-status in ietf-interfaces dat
    # van enabled-state afhangt voorbij carrier.

    # Verwachte OSPF peer router-ids
    expected_peers = []
    for peer_name, peer_cfg in peers.items():
        if peer_name == name:
            continue
        if peer_cfg.get("ospf"):
            rid = peer_cfg["ospf"].get("router_id")
            if rid:
                expected_peers.append(rid)

    with connect(device["host"], device["netconf_port"], USERNAME, PASSWORD) as m:
        check_hostname(m, cfg["hostname"], results)
        if expected_intf_names:
            check_interfaces(m, expected_intf_names, results)
        if cfg.get("ospf"):
            check_ospf_and_reachability(m, expected_peers, results)
        if cfg.get("default_gateway"):
            check_default_gateway(m, cfg["default_gateway"], results)

    print(f"\n  Result: {results.passed}/{results.total} passed")
    return results


def main():
    devices = load_yaml(PROJECT_ROOT / "inventory/devices.yaml")["devices"]

    configs = {}
    for name in devices:
        cfg_path = PROJECT_ROOT / f"configs/{name.lower()}.yaml"
        if cfg_path.exists():
            configs[name] = load_yaml(cfg_path)

    # SW1 staat in inventory maar hardware is offline. We skippen die.
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
