# Network as Code

Cisco IOS-XE netwerktopologie volledig geautomatiseerd met YANG, Python
en **twee protocollen**: NETCONF om configuratie te pushen, RESTCONF om
operationele staat te verifiëren. GitHub is de single source of truth.

## Architectuur
YAML in Git ───┐
│
▼
deploy.py ───── NETCONF ─────► routers + switches
(schrijven)     (poort 830)    (candidate datastore,
safe-edit pattern)
▲
│ vergelijk
▼
verify.py ───── RESTCONF ────► routers + switches
(lezen)         (HTTPS/443)    (operational state +
HTTP-statuscodes)

Twee protocollen, elk waar het 't sterkst is:

- **NETCONF voor schrijven** — candidate-datastore + transactionele commit
  + atomic discard-on-error. Cruciaal voor consistente configuratie.
- **RESTCONF voor lezen** — HTTP/JSON, eenvoudige client, statuscodes
  direct zichtbaar. Past natuurlijk bij read-only validatie.

## Topologie
    [SW1]                          [SW2]
   offline                       Catalyst
                                 C9200L
      │ trunk                  │ trunk
      │ VLAN 10, 99            │ VLAN 20, 99
    ┌─┴─┐                    ┌─┴─┐
    │R1 │── 192.168.12.0/30 ─│R2 │
    │1.1│   OSPF Area 0      │2.2│
    └───┘                    └───┘
   ISR 4321                ISR 4321

- **Site A**: VLAN 10 (clients), VLAN 99 (management). Gateway op R1.
- **Site B**: VLAN 20 (clients), VLAN 99 (management). Gateway op R2.
- Routers terminen de trunks met dot1Q sub-interfaces.
- OSPF Area 0 distribueert alle subnets tussen de sites.

### IP-plan

| Subnet | Doel |
| --- | --- |
| `192.168.12.0/30` | R1 ↔ R2 routed link (OSPF) |
| `10.10.10.0/24` | VLAN 10, Site A clients |
| `10.20.20.0/24` | VLAN 20, Site B clients |
| `10.99.1.0/24` | VLAN 99 mgmt Site A — SW1: `.10`, R1: `.1` |
| `10.99.2.0/24` | VLAN 99 mgmt Site B — SW2: `.10`, R2: `.1` |
| `1.1.1.1/32`, `2.2.2.2/32` | Router loopbacks (router-IDs) |

## Wat dit project doet

1. **YAML in Git** definieert de gewenste config per toestel.
2. **`deploy.py`** bouwt daaruit een NETCONF `<edit-config>` payload
   en pusht via de candidate datastore met het volledige safe-edit
   pattern: `lock → discard → edit → validate → commit → unlock`.
3. Bij elke fout → `discard-changes`, toestel blijft schoon. `<ok/>`
   of `<rpc-error>` met error-type/error-tag wordt expliciet getoond.
4. **`verify.py`** doet RESTCONF GET-requests naar dezelfde toestellen
   en valideert de live state tegen de YAML. Elke request logt z'n
   HTTP-statuscode (`200`, `404`, `4xx/5xx`).

## Structuur
project-network-as-code/
├── inventory/devices.yaml        # hoe bereiken we elk toestel
├── configs/{r1,r2,sw1,sw2}.yaml  # gewenste config per device
├── bootstrap/                    # CLI-config per device + bring-up
├── scripts/
│   ├── deploy.py                 # NETCONF: YAML → edit-config → commit
│   ├── verify.py                 # RESTCONF: HTTP GET → checks
│   ├── test_connection.py        # minimale NETCONF probe
│   └── lib/netconf_client.py     # ncclient helper
├── requirements.txt
├── .env.example
└── .gitignore

## Setup

```bash
git clone git@github.com:YbeWillemsPXL/project-network-as-code.git
cd project-network-as-code

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env            # vul de credentials in
```

## Per lab-sessie

Bij elke sessie worden de toestellen gereset. De bring-up:

1. Laptop Ethernet IP op `10.99.2.100/24`, gateway `10.99.2.1`.
2. Static routes op de laptop (PowerShell als Admin):
```powershell
   route -p add 10.0.0.0 mask 255.0.0.0 10.99.2.1
   route -p add 1.1.1.1 mask 255.255.255.255 10.99.2.1
   route -p add 2.2.2.2 mask 255.255.255.255 10.99.2.1
   route -p add 192.168.12.0 mask 255.255.255.252 10.99.2.1
```
3. Per toestel via console het bijhorende bootstrap-bestand pasten:
   `bootstrap/r1-bootstrap.txt`, `r2-bootstrap.txt`, `sw2-bootstrap.txt`
   (`sw1-bootstrap.txt` wanneer SW1 terug is). Deze enablen ook
   NETCONF (poort 830) én RESTCONF (HTTPS poort 443).
4. Kabels:
   - Laptop ↔ SW2 Gi1/0/2 (access VLAN 99)
   - SW2 Gi1/0/24 ↔ R2 Gi0/0/0 (trunk)
   - R1 Gi0/0/1 ↔ R2 Gi0/0/1 (OSPF link)

## Gebruik

```bash
python scripts/deploy.py R1      # of R2, SW2 — pusht via NETCONF
python scripts/verify.py         # haalt state op via RESTCONF
```

`verify.py` print per request de HTTP-statuscode en gebruikt exit code
`0` (alle checks gepasseerd) of `1` (één of meer faalt) — bruikbaar
voor toekomstige CI-integratie.

## YANG-modellen

| Model | Waarvoor | Gebruikt door |
| --- | --- | --- |
| `Cisco-IOS-XE-native` | hostname, interfaces, IPs | deploy + verify |
| `Cisco-IOS-XE-ospf` | OSPF process (augmenting) | deploy |
| `Cisco-IOS-XE-switch` | switchports (augmenting) | deploy |
| `ietf-interfaces` | interface oper-state | verify |
| `Cisco-IOS-XE-ospf-oper` | OSPF neighbor state | verify |

Paden zijn gevonden via **Cisco YANG Suite** en via `<get-config>`
probes tegen een werkende running config — daarna 1-op-1 nagebouwd
in de Python payload builder.

## Toetsing aan de opdracht

**Basis (50%)**

- Python met `ncclient` (NETCONF), `requests` (RESTCONF), `lxml`,
  `pyyaml`, `python-dotenv`.
- Pretty-print van NETCONF XML én RESTCONF JSON.
- Parsen/serialiseren YAML ↔ XML (deploy) en JSON ↔ Python dicts
  (verify).
- **Statusfeedback expliciet zichtbaar**:
  - NETCONF: `<ok/>` of `<rpc-error>` met error-type/error-tag/path.
  - RESTCONF: HTTP-statuscode (`200`, `404`, `4xx/5xx`) per request
    in de output.
- GitHub als single source of truth.

**Additioneel (50%)**

- **Twee complementaire protocollen**: NETCONF voor schrijven met
  candidate-datastore + safe-edit pattern, RESTCONF voor lezen met
  HTTP-statuscodes. Elk protocol voor de use-case waar 't 't sterkst
  is.
- End-to-end YANG-gedreven configuratie van fysieke Cisco-hardware,
  routers én Catalyst access-switches.
- Candidate datastore met volledig safe-edit pattern; tijdens
  ontwikkeling werden 2 echte YANG-pad-fouten netjes door
  `discard-changes` afgehandeld zonder dat een toestel half
  geconfigureerd raakte.
- Inventory-driven deploy: één script voor zowel routers als
  switches, intent wordt afgeleid uit welke YAML-secties aanwezig
  zijn.
- L2 + L3 in één topologie: VLANs, trunks, dot1Q sub-interfaces,
  SVIs, inter-site OSPF.

## Beperkingen

- **VLAN-databasebeheer op de Catalyst 9200L valt buiten scope.** De
  VLAN-database (VLANs aanmaken met hun naam) is niet zichtbaar onder
  `Cisco-IOS-XE-native/vlan` noch onder `Cisco-IOS-XE-vlan` op IOS-XE
  17.6.3 — een gekende vendor-quirk. Mitigatie: VLANs staan in de
  bootstrap CLI-files (ook in Git, ook "as code"). Het toewijzen van
  poorten aan een VLAN gebeurt wél volledig via NETCONF.

- **SW1 is offline** (corrupted boot image). Bootstrap, config en
  inventory-entry voor SW1 zijn klaar; re-integratie is één console
  paste wanneer de hardware terug is.

- **Geen CI yet.** `verify.py` is gestructureerd om in een pipeline
  te draaien (exit codes, geen interactieve prompts). GitHub Actions
  zou een natuurlijke volgende stap zijn.

- **Manuele bootstrap via console.** Een zero-touch provisioning
  (PnP / day-0 over USB) is buiten de scope van dit project.
