"""Highway names from OSRM route refs and from station addresses, in one comparable form.

'I 40; US 287' and 'I-40, EXIT 76 & SR-72' both become sets like {('I', '40'), ('US', '287')}.
Interstates and US highways keep their type; every state or county road becomes 'S'.
"""
import re

ROUTE_REF = re.compile(r"\b(I|US|[A-Z]{2})[ -]?(\d+)")                     # OSRM: 'TX 71', 'CA 58'
STATION_ADDRESS = re.compile(r"\b(I|US|SR|SH|CR|HWY|RT|ROUTE)[ -]?(\d+)")


def highways_in(text, from_route):
    """The highways named in an OSRM `ref` (from_route=True) or a station address."""
    pattern = ROUTE_REF if from_route else STATION_ADDRESS
    found = set()
    for kind, number in pattern.findall((text or "").upper()):
        found.add((kind if kind in ("I", "US") else "S", number))
    return frozenset(found)


def encode(highways):
    """{('I', '44'), ('US', '69')} -> 'I:44,US:69' (for storing on a station)."""
    return ",".join(sorted(f"{kind}:{number}" for kind, number in highways))


def decode(text):
    """'I:44,US:69' -> frozenset({('I', '44'), ('US', '69')})."""
    return frozenset(tuple(item.split(":", 1)) for item in text.split(",") if item)
