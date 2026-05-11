"""Deploy desired configuration from YAML to a Cisco IOS-XE device via NETCONF.

Usage:
    python scripts/deploy.py R1
    python scripts/deploy.py SW2

Supports routers (interfaces, sub-interfaces, loopbacks, OSPF)
and switches (switchports access/trunk, SVI interfaces, default gateway).

Pattern: lock -> discard -> edit -> validate -> commit -> unlock
         with discard-changes on error.
"""
import argparse
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

NS_NATIVE = "http://cisco.com/ns/yang/Cisco-IOS-XE-native"
NS_OSPF = "http://cisco.com/ns/yang/Cisco-IOS-XE-ospf"
NS_SWITCH = "http://cisco.com/ns/yang/Cisco-IOS-XE-switch"
NS_NC = "urn:ietf:params:xml:ns:netconf:base:1.0"


def load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)


def add_hostname(native, hostname):
    h = etree.SubElement(native, "hostname")
    h.text = hostname
    h.set(f"{{{NS_NC}}}operation", "merge")


def add_routed_interface(wrapper, intf):
    gi = etree.SubElement(wrapper, "GigabitEthernet")
    gi.set(f"{{{NS_NC}}}operation", "merge")
    etree.SubElement(gi, "name").text = intf["name"]
    if "description" in intf:
        etree.SubElement(gi, "description").text = intf["description"]
    if intf.get("encapsulation_vlan"):
        enc = etree.SubElement(gi, "encapsulation")
        dot1q = etree.SubElement(enc, "dot1Q")
        etree.SubElement(dot1q, "vlan-id").text = str(intf["encapsulation_vlan"])
    if not intf.get("no_ip"):
        ip = etree.SubElement(gi, "ip")
        addr = etree.SubElement(ip, "address")
        primary = etree.SubElement(addr, "primary")
        etree.SubElement(primary, "address").text = intf["ip"]
        etree.SubElement(primary, "mask").text = intf["mask"]
    if intf.get("enabled", True):
        shut = etree.SubElement(gi, "shutdown")
        shut.set(f"{{{NS_NC}}}operation", "remove")
    else:
        etree.SubElement(gi, "shutdown")


def add_loopback(wrapper, lo):
    lb = etree.SubElement(wrapper, "Loopback")
    lb.set(f"{{{NS_NC}}}operation", "merge")
    etree.SubElement(lb, "name").text = lo["name"]
    ip = etree.SubElement(lb, "ip")
    addr = etree.SubElement(ip, "address")
    primary = etree.SubElement(addr, "primary")
    etree.SubElement(primary, "address").text = lo["ip"]
    etree.SubElement(primary, "mask").text = lo["mask"]


def add_svi(wrapper, svi):
    vlan_intf = etree.SubElement(wrapper, "Vlan")
    vlan_intf.set(f"{{{NS_NC}}}operation", "merge")
    etree.SubElement(vlan_intf, "name").text = str(svi["vlan"])
    if "description" in svi:
        etree.SubElement(vlan_intf, "description").text = svi["description"]
    if "ip" in svi:
        ip = etree.SubElement(vlan_intf, "ip")
        addr = etree.SubElement(ip, "address")
        primary = etree.SubElement(addr, "primary")
        etree.SubElement(primary, "address").text = svi["ip"]
        etree.SubElement(primary, "mask").text = svi["mask"]
    if svi.get("enabled", True):
        shut = etree.SubElement(vlan_intf, "shutdown")
        shut.set(f"{{{NS_NC}}}operation", "remove")
    else:
        etree.SubElement(vlan_intf, "shutdown")


def add_switchport(wrapper, sp):
    """Build a switchport (L2) interface.

    Uses Cisco IOS-XE Catalyst structure discovered via get-config:
        GigabitEthernet/
          switchport-config/
            switchport/
              mode (in Cisco-IOS-XE-switch namespace)
              access | trunk (same namespace, sibling of mode)
    """
    gi = etree.SubElement(wrapper, "GigabitEthernet")
    gi.set(f"{{{NS_NC}}}operation", "merge")
    etree.SubElement(gi, "name").text = sp["name"]
    if "description" in sp:
        etree.SubElement(gi, "description").text = sp["description"]

    # Outer wrapper (native namespace)
    sp_conf = etree.SubElement(gi, "switchport-config")
    sp_inner = etree.SubElement(sp_conf, "switchport")

    # mode element (Cisco-IOS-XE-switch namespace)
    mode_elem = etree.SubElement(sp_inner, f"{{{NS_SWITCH}}}mode")

    if sp["mode"] == "access":
        etree.SubElement(mode_elem, f"{{{NS_SWITCH}}}access")
        if "access_vlan" in sp:
            access = etree.SubElement(sp_inner, f"{{{NS_SWITCH}}}access")
            vlan = etree.SubElement(access, f"{{{NS_SWITCH}}}vlan")
            etree.SubElement(vlan, f"{{{NS_SWITCH}}}vlan").text = str(sp["access_vlan"])

    elif sp["mode"] == "trunk":
        etree.SubElement(mode_elem, f"{{{NS_SWITCH}}}trunk")
        if "trunk_allowed_vlans" in sp:
            trunk = etree.SubElement(sp_inner, f"{{{NS_SWITCH}}}trunk")
            allowed = etree.SubElement(trunk, f"{{{NS_SWITCH}}}allowed")
            vlan = etree.SubElement(allowed, f"{{{NS_SWITCH}}}vlan")
            etree.SubElement(vlan, f"{{{NS_SWITCH}}}vlans").text = ",".join(
                str(v) for v in sp["trunk_allowed_vlans"]
            )

    if sp.get("enabled", True):
        shut = etree.SubElement(gi, "shutdown")
        shut.set(f"{{{NS_NC}}}operation", "remove")
    else:
        etree.SubElement(gi, "shutdown")


def add_default_gateway(native, gw):
    ip = etree.SubElement(native, "ip")
    dg = etree.SubElement(ip, "default-gateway")
    dg.text = gw
    dg.set(f"{{{NS_NC}}}operation", "merge")


def add_ospf(native, ospf_cfg):
    router = etree.SubElement(native, "router")
    router_ospf = etree.SubElement(router, f"{{{NS_OSPF}}}router-ospf")
    router_ospf.set(f"{{{NS_NC}}}operation", "merge")
    ospf_container = etree.SubElement(router_ospf, f"{{{NS_OSPF}}}ospf")
    process = etree.SubElement(ospf_container, f"{{{NS_OSPF}}}process-id")
    etree.SubElement(process, f"{{{NS_OSPF}}}id").text = str(ospf_cfg["process_id"])
    if "router_id" in ospf_cfg:
        etree.SubElement(process, f"{{{NS_OSPF}}}router-id").text = ospf_cfg["router_id"]
    for net in ospf_cfg.get("networks", []):
        n = etree.SubElement(process, f"{{{NS_OSPF}}}network")
        etree.SubElement(n, f"{{{NS_OSPF}}}ip").text = net["prefix"]
        etree.SubElement(n, f"{{{NS_OSPF}}}wildcard").text = net["wildcard"]
        etree.SubElement(n, f"{{{NS_OSPF}}}area").text = str(net["area"])


def build_payload(cfg):
    nsmap = {None: NS_NATIVE, "nc": NS_NC}
    config = etree.Element("config")
    native = etree.SubElement(config, "native", nsmap=nsmap)

    if "hostname" in cfg:
        add_hostname(native, cfg["hostname"])

    has_intfs = (cfg.get("interfaces") or cfg.get("loopbacks")
                 or cfg.get("svi_interfaces") or cfg.get("switchports"))
    if has_intfs:
        wrapper = etree.SubElement(native, "interface")
        for intf in cfg.get("interfaces") or []:
            add_routed_interface(wrapper, intf)
        for lo in cfg.get("loopbacks") or []:
            add_loopback(wrapper, lo)
        for svi in cfg.get("svi_interfaces") or []:
            add_svi(wrapper, svi)
        for sp in cfg.get("switchports") or []:
            add_switchport(wrapper, sp)

    if cfg.get("default_gateway"):
        add_default_gateway(native, cfg["default_gateway"])

    if cfg.get("ospf"):
        add_ospf(native, cfg["ospf"])

    return etree.tostring(config, pretty_print=True).decode()


def deploy(device_name):
    devices = load_yaml(PROJECT_ROOT / "inventory/devices.yaml")["devices"]
    if device_name not in devices:
        print(f"Unknown device: {device_name}", file=sys.stderr)
        print(f"Available: {', '.join(devices)}", file=sys.stderr)
        sys.exit(2)

    device = devices[device_name]
    cfg_path = PROJECT_ROOT / f"configs/{device_name.lower()}.yaml"
    if not cfg_path.exists():
        print(f"No config file: {cfg_path}", file=sys.stderr)
        sys.exit(2)

    cfg = load_yaml(cfg_path)
    payload = build_payload(cfg)

    print(f"=== Deploy {device_name} ({device['host']}) ===\n")
    print("Generated NETCONF payload:")
    print("-" * 60)
    print(payload)
    print("-" * 60 + "\n")

    with connect(device["host"], device["netconf_port"], USERNAME, PASSWORD) as m:
        print(f"Connected (session {m.session_id})")
        try:
            print("→ Lock candidate")
            m.lock(target="candidate")

            print("→ Discard any leftover candidate changes")
            m.discard_changes()

            print("→ Edit-config to candidate")
            reply = m.edit_config(target="candidate", config=payload)
            print(f"  reply.ok = {reply.ok}")

            print("→ Validate candidate")
            m.validate(source="candidate")

            print("→ Commit candidate -> running")
            m.commit()

            print(f"\n✓ {device_name} configured successfully.")

        except Exception as exc:
            print(f"\n✗ Error during deploy: {exc}", file=sys.stderr)
            print("→ Discarding candidate changes", file=sys.stderr)
            try:
                m.discard_changes()
            except Exception as cleanup_exc:
                print(f"  (cleanup failed: {cleanup_exc})", file=sys.stderr)
            sys.exit(1)

        finally:
            try:
                m.unlock(target="candidate")
                print("→ Unlocked candidate")
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description="Deploy YAML config to a device")
    parser.add_argument("device", help="Device name from inventory (e.g. R1, SW2)")
    args = parser.parse_args()
    deploy(args.device)


if __name__ == "__main__":
    main()
