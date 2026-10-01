import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

# Links in tests use made-up hosts; treat them as public, except the ones that stand for Ursula's own
# network (1.4.1's link guard).
PRIVATE_HOSTS = {"localhost": "127.0.0.1", "internal.test": "10.0.0.5", "metadata.test": "169.254.169.254"}


@pytest.fixture(autouse=True)
def _fake_dns(monkeypatch):
    from ursula import netguard

    def addresses(host):
        if host in PRIVATE_HOSTS:
            return [PRIVATE_HOSTS[host]]
        try:
            import ipaddress
            ipaddress.ip_address(host)
            return [host]
        except ValueError:
            return ["93.184.216.34"]
    monkeypatch.setattr(netguard, "_addresses", addresses)
