"""Configureer de hostname van R1 via NETCONF candidate-datastore.

Demonstreert het volledige veilige edit-pattern:
  1. Lock candidate (niemand anders kan tegelijk editen)
  2. Edit-config naar candidate
  3. Validate (router checkt syntactisch + semantisch)
  4. Commit (candidate -> running, atomair)
  5. Unlock
  Op een fout: discard-changes om de candidate leeg te maken.
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.netconf_client import connect

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
USERNAME = os.environ["ROUTER_USERNAME"]
PASSWORD = os.environ["ROUTER_PASSWORD"]

R1_HOST = "192.168.100.1"
R1_PORT = 830

# De gewenste nieuwe hostname
NEW_HOSTNAME = "R1-NetAsCode"

# YANG-XML payload — exact dezelfde structuur als wat YANG Suite genereert
# voor Task 7 (Change Hostname). De `nc:operation="merge"` op de <hostname>
# tag zegt: vervang/voeg toe maar laat de rest van de native config met rust.
HOSTNAME_PAYLOAD = f"""
<config>
  <native xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-native">
    <hostname xmlns:nc="urn:ietf:params:xml:ns:netconf:base:1.0"
              nc:operation="merge">{NEW_HOSTNAME}</hostname>
  </native>
</config>
"""


def pretty(xml_string):
    return etree.tostring(
        etree.fromstring(xml_string.encode()), pretty_print=True
    ).decode()


def main():
    print(f"Connecting to R1 at {R1_HOST} ...")

    with connect(R1_HOST, R1_PORT, USERNAME, PASSWORD) as m:
        print(f"Connected. Session ID: {m.session_id}\n")

        try:
            # 1. Lock candidate
            print("→ Locking candidate datastore")
            m.lock(target="candidate")

            # 2. Discard any leftover changes from previous failed runs
            #    (idempotency: schone candidate als startpunt)
            print("→ Discarding any pending candidate changes")
            m.discard_changes()

            # 3. Edit candidate
            print(f"→ Editing candidate: hostname → {NEW_HOSTNAME}")
            edit_reply = m.edit_config(target="candidate", config=HOSTNAME_PAYLOAD)
            # Een edit-config reply bevat <ok/> bij succes. ncclient
            # verheft de RPCError tot Python-exception als er <rpc-error>
            # in zit, dus als we hier komen weten we dat 't goed is.
            print(f"  reply: {edit_reply.ok}")

            # 4. Validate (Cisco IOS-XE ondersteunt dit op candidate)
            print("→ Validating candidate")
            m.validate(source="candidate")

            # 5. Commit (atomair candidate -> running)
            print("→ Committing candidate to running")
            m.commit()

            print("\n✓ Hostname successfully changed.")

        except Exception as exc:
            # Bij ELKE fout: candidate leegmaken zodat we de router niet
            # in een halve staat achterlaten. Dit is wat de opdracht
            # noemt onder "foutafhandeling met discard-changes".
            print(f"\n✗ Error: {exc}", file=sys.stderr)
            print("→ Discarding changes to keep router clean", file=sys.stderr)
            try:
                m.discard_changes()
            except Exception as cleanup_exc:
                print(f"  (cleanup failed: {cleanup_exc})", file=sys.stderr)
            sys.exit(1)

        finally:
            # Lock altijd vrijgeven, ook bij fouten — anders blijft de
            # candidate locked tot de NETCONF-sessie sluit.
            try:
                print("→ Unlocking candidate datastore")
                m.unlock(target="candidate")
            except Exception:
                pass


if __name__ == "__main__":
    main()
