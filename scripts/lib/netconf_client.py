"""NETCONF connection helper.

Centraliseert de logica om met een Cisco IOS-XE toestel te connecteren via
NETCONF. Andere scripts importeren `connect()` en hoeven zich niet bezig te
houden met host-key checks of device-params.
"""
from ncclient import manager


def connect(host, port, username, password):
    """Open een NETCONF-sessie naar een Cisco IOS-XE toestel.

    Returns een ncclient Manager. Gebruik als context manager:
        with connect(...) as m:
            ...
    """
    return manager.connect(
        host=host,
        port=port,
        username=username,
        password=password,
        hostkey_verify=False,        # lab-setup, geen formele PKI
        device_params={"name": "iosxe"},
        look_for_keys=False,         # forceer wachtwoord, geen SSH-key
        allow_agent=False,
    )
