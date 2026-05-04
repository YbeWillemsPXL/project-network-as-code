"""Test NETCONF-verbinding met R1 en haal de hostname op.

Doel: bewijzen dat onze hele toolchain werkt — credentials, libraries,
SSH-tunnel, NETCONF-handshake, RPC parsing. Geen configuratiewijzigingen.
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from lxml import etree

# Maak `from lib.netconf_client import connect` werkbaar wanneer we het
# script direct draaien (`python scripts/test_connection.py`). Dit voegt
# de scripts/-folder toe aan Python's import-pad.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.netconf_client import connect

# Laad credentials uit .env (in de root van het project)
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

USERNAME = os.environ["ROUTER_USERNAME"]
PASSWORD = os.environ["ROUTER_PASSWORD"]

# R1 — voor nu hardcoded, straks komt dit uit inventory/devices.yaml
R1_HOST = "192.168.100.1"
R1_PORT = 830


def pretty_xml(xml_string):
    """Pretty-print XML voor mensen-leesbare output."""
    parsed = etree.fromstring(xml_string.encode())
    return etree.tostring(parsed, pretty_print=True).decode()


def main():
    # Een NETCONF subtree-filter is een XML-fragment dat zegt:
    # "geef me ALLEEN dit deel van de config terug, niet alles".
    # Hier vragen we enkel de <hostname> uit het Cisco-IOS-XE-native model.
    hostname_filter = """
    <native xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-native">
        <hostname/>
    </native>
    """

    print(f"Connecting to R1 at {R1_HOST}:{R1_PORT} ...")

    with connect(R1_HOST, R1_PORT, USERNAME, PASSWORD) as m:
        print(f"Connected. Session ID: {m.session_id}")
        print()

        # get-config tegen de running datastore, met onze subtree-filter.
        # Dit is exact wat YANG Suite doet als je daar 'get-config' kiest.
        reply = m.get_config(source="running", filter=("subtree", hostname_filter))

        print("RPC reply (pretty-printed):")
        print("-" * 60)
        print(pretty_xml(reply.xml))


if __name__ == "__main__":
    main()
