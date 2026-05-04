"""Deploy desired configuration from YAML to a Cisco IOS-XE device via NETCONF.

Usage:
    python scripts/deploy.py R1
    python scripts/deploy.py R2

Reads:
    inventory/devices.yaml
    configs/<device>.yaml
    .env (credentials)

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
NS_NC = "urn:ietf:params:xml:ns:netconf:base:1.0"


def load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)


def add_hostname(native, hostname):
    h = etree.SubElement(native, "hostname")
    h.text = hostname
    h.set(f"{{{NS_NC}}}operation", "merge")


def add_interfaces(native, interfaces, loopbacks):
    """Build <interface> wrapper with all GigabitEthernet + Loopback children."""
    if not interfaces and not loopbacks:
        return
    wrapper = etree.SubElement(native, "interface")

    for intf in interfaces or []:
        gi = etree.SubElement(wrapper, "GigabitEthernet")
        gi.set(f"{{{NS_NC}}}operation", "merge")
        etree.SubElement(gi, "name").text = intf["name"]
        if "description" in intf:
            etree.SubElement(gi, "description").text = intf["description"]
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

    for lo in loopbacks or []:
        lb = etree.SubElement(wrapper, "Loopback")
        lb.set(f"{{{NS_NC}}}operation", "merge")
        etree.SubElement(lb, "name").text = lo["name"]
        ip = etree.SubElement(lb, "ip")
        addr = etree.SubElement(ip, "address")
        primary = etree.SubElement(addr, "primary")
        etree.SubElement(primary, "address").text = lo["ip"]
        etree.SubElement(primary, "mask").text = lo["mask"]


def add_ospf(native, ospf_cfg):
    """Build OSPF config under <native>/<router>/<router-ospf>/<ospf>/<process-id>.

    Structure (verified via YANG Suite on IOS-XE 17.3):
        native
          router
            router-ospf (ns: Cisco-IOS-XE-ospf)
              ospf
                process-id
                  id          <- key
                  router-id
                  network[]   <- list, ip + wildcard + area
    """
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
    """Construct the full <config> XML payload."""
    nsmap = {None: NS_NATIVE, "nc": NS_NC}
    config = etree.Element("config")
    native = etree.SubElement(config, "native", nsmap=nsmap)

    if "hostname" in cfg:
        add_hostname(native, cfg["hostname"])

    add_interfaces(native, cfg.get("interfaces"), cfg.get("loopbacks"))

    if "ospf" in cfg and cfg["ospf"]:   # also skips if YAML key has null value
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
    parser.add_argument("device", help="Device name from inventory (e.g. R1)")
    args = parser.parse_args()
    deploy(args.device)


if __name__ == "__main__":
    main()
