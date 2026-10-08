"""
Kerala geography helpers: district polygons (from the same 2011 census
boundaries the app draws), point-in-polygon, distances and a place index for
keyword SMS ("HELP Aluva ...").
"""
import json
import math
from pathlib import Path

_DATA = json.loads((Path(__file__).resolve().parent / "kerala_districts.json").read_text())
DISTRICTS = {d["id"]: d for d in _DATA}

TOWNS = [
    ("Kasaragod", 12.4996, 74.9869), ("Kannur", 11.8745, 75.3704), ("Thalassery", 11.7491, 75.4890),
    ("Kalpetta", 11.6085, 76.0830), ("Mananthavady", 11.8014, 76.0044), ("Kozhikode", 11.2588, 75.7804),
    ("Malappuram", 11.0510, 76.0711), ("Nilambur", 11.2769, 76.2256), ("Palakkad", 10.7867, 76.6548),
    ("Thrissur", 10.5276, 76.2144), ("Guruvayur", 10.5943, 76.0411), ("Chalakudy", 10.3070, 76.3341),
    ("Aluva", 10.1076, 76.3516), ("Perumbavoor", 10.1155, 76.4730), ("Kochi", 9.9312, 76.2673),
    ("Muvattupuzha", 9.9894, 76.5790), ("Thodupuzha", 9.8959, 76.7184), ("Munnar", 10.0889, 77.0595),
    ("Painavu", 9.8497, 76.9681), ("Kottayam", 9.5916, 76.5222), ("Alappuzha", 9.4981, 76.3388),
    ("Kuttanad", 9.4500, 76.4300), ("Chengannur", 9.3180, 76.6110), ("Ranni", 9.3854, 76.7856),
    ("Pathanamthitta", 9.2648, 76.7870), ("Kollam", 8.8932, 76.6141), ("Punalur", 9.0170, 76.9260),
    ("Varkala", 8.7379, 76.7163), ("Thiruvananthapuram", 8.5241, 76.9366), ("Neyyattinkara", 8.4000, 77.0870),
    # common alternative names
    ("Ernakulam", 9.9816, 76.2999), ("Trivandrum", 8.5241, 76.9366), ("Calicut", 11.2588, 75.7804),
    ("Cochin", 9.9312, 76.2673), ("North Paravur", 10.147, 76.225), ("Paravur", 10.147, 76.225),
]
PLACE_INDEX = sorted(
    [(n, la, ln) for n, la, ln in TOWNS] + [(d["name"], d["center"][0], d["center"][1]) for d in _DATA],
    key=lambda p: -len(p[0]),
)


def _in_ring(lat: float, lng: float, ring: list) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        yi, xi = ring[i]
        yj, xj = ring[j]
        if (yi > lat) != (yj > lat) and lng < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def district_at(lat, lng):
    if lat is None or lng is None:
        return None
    for d in _DATA:
        if any(_in_ring(lat, lng, r) for r in d["rings"]):
            return d["id"]
    return None


def nearest_district(lat, lng):
    return min(_DATA, key=lambda d: (d["center"][0] - lat) ** 2 + (d["center"][1] - lng) ** 2)["id"]


def district_of(lat, lng):
    if lat is None or lng is None:
        return None
    return district_at(lat, lng) or nearest_district(lat, lng)


def in_kerala(lat, lng) -> bool:
    return district_at(lat, lng) is not None


def km_between(lat1, lng1, lat2, lng2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def find_place(text: str):
    low = text.lower()
    import re
    for name, la, ln in PLACE_INDEX:
        if re.search(r"\b" + re.escape(name.lower()) + r"\b", low):
            return name, la, ln
    return None
