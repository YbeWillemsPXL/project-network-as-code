"""Deploy desired configuration from YAML to a Cisco IOS-XE device via NETCONF.

Usage:
    python scripts/deploy.py R1
    python scripts/deploy.py R2

Reads:
    inventory/devices.yaml   - which devices and how to reach them
    configs/<device>.yaml    - desired configuration for the device
    .env                     - credentials (not in git)

Pattern:
    lock -> discard -> edit -> validate -> commit -> unlock
    With discard-changes on any error.
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
NS_NC = "urn:ietf:params:xml:ns:netconf:base:1.0"


def load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)


def build_payload(cfg):
    """Construct the full <config> XML payload from a config dict.

    We bouwen de XML programmatisch met lxml in plaats van strings te
    interpoleren — robuuster, geen escaping-problemen, en dichter bij
    hoe je later naar templating zou groeien.
    """
    nsmap = {None: NS_NATIVE, "nc": NS_NC}
    config = etree.Element("config")
    native = etree.SubElement(config, "native", nsmap=nsmap)

    # Hostname
    if "hostname" in cfg:
        h = etree.SubElement(native, "hostname")
        h.text = cfg["hostname"]
        h.set(f"{{{NS_NC}}}operation", "merge")

    # Physical + loopback interfaces under a single <interface> wrapper
    interface_wrapper = etree.SubElement(native, "interface")

    for intf in cfg.get("interfaces", []):
        gi = etree.SubElement(interface_wrapper, "GigabitEthernet")
        gi.set(f"{{{NS_NC}}}operation", "merge")
        etree.SubElement(gi, "name").text = intf["name"]
        if "description" in intf:
            etree.SubElement(gi, "description").text = intf["description"]
        # IP address: native -> ip -> address -> primary -> address+mask
        ip = etree.SubElement(gi, "ip")
        addr = etree.SubElement(ip, "address")
        primary = etree.SubElement(addr, "primary")
        etree.SubElement(primary, "address").text = intf["ip"]
        etree.SubElement(primary, "mask").text = intf["mask"]
        # enabled=true means: ensure no shutdown. We model 'enabled: false'
        # by adding <shutdown/>, and 'enabled: true' by removing it.
        if intf.get("enabled", True):
            shut = etree.SubElement(gi, "shutdown")
            shut.set(f"{{{NS_NC}}}operation", "remove")
        else:
            etree.SubElement(gi, "shutdown")

    for lo in cfg.get("loopbacks", []):
        lb = etree.SubElement(interface_wrapper, "Loopback")
        lb.set(f"{{{NS_NC}}}operation", "merge")
        etree.SubElement(lb, "name").text = lo["name"]
        ip = etree.SubElement(lb, "ip")
        addr = etree.SubElement(ip, "address")
        primary = etree.SubElement(addr, "primary")
        etree.SubElement(primary, "address").text = lo["ip"]
        etree.SubElement(primary, "mask").text = lo["mask"]

    # OSPF — augmenting module: Cisco-IOS-XE-ospf
    if "ospf" in cfg:
        ospf_cfg = cfg["ospf"]
        # router subcontainer onder native, met augment-namespace voor 'router'
        router = etree.SubElement(native, "router")
        ospf = etree.SubElement(
            router, "{http://cisco.com/ns/yang/Cisco-IOS-XE-ospf}router-ospf"
        )
        ospf.set(f"{{{NS_NC}}}operation", "merge")
        ospf_proc = etree.SubElement(ospf, "ospf")
        proc = etree.SubElement(ospf_proc, "process-id-list")
        etree.SubElement(proc, "id").text = str(ospf_cfg["process_id"])
        # router-id
        if "router_id" in ospf_cfg:
            rid = etree.SubElement(proc, "router-id")
            rid.text = ospf_cfg["router_id"]
        # networks
        for net in ospf_cfg.get("networks", []):
            n = etree.SubElement(proc, "network")
            etree.SubElement(n, "ip").text = net["prefix"]
            etree.SubElement(n, "wildcard").text = net["wildcard"]
            etree.SubElement(n, "area").text = str(net["area"])

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
    parser.add_argument("device", help="Device name from inventory (e.g. R1)")
    args = parser.parse_args()
    deploy(args.device)


if __name__ == "__main__":
    main()
