"""
Misuse screening and SMS parsing.

Principle: nothing is ever rejected. Suspicious requests are merged into an
existing one, lowered one priority step, or flagged for a person to verify.
The same rules run in the app's offline demo mode, so keep both in step.
"""
from __future__ import annotations

import re
import time

from geo import district_of, find_place, in_kerala, km_between

TRUST_DEFAULT = 0.6
LOW_TRUST = 0.35
MAX_OPEN_PER_REPORTER = 3
DUP_WINDOW_MIN = 30
DUP_RADIUS_KM = 0.25
CLAIM_TIMEOUT_MIN = 20
PRIORITIES = ["Emergency", "High", "Medium", "Low"]
OPEN = ("Pending", "Assigned", "En Route", "On Site")
OUTCOMES = {
    "helped": (0.10, "genuine"), "safe": (0.02, "genuine"), "duplicate": (0.0, "dup"),
    "notfound": (-0.10, "notfound"), "false": (-0.30, "false_reports"),
}
LABEL_TO_PRIORITY = {"critical": "Emergency", "high": "High", "medium": "Medium", "low": "Low"}


def now_ms() -> int:
    return int(time.time() * 1000)


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def trust_of(rep, now: int | None = None) -> float:
    """Trust drifts back to the default over 30 days, so one bad mark isn't permanent."""
    if rep is None:
        return TRUST_DEFAULT
    now = now or now_ms()
    days = (now - (rep.updated_at or now)) / 86_400_000
    return clamp01(rep.trust + (TRUST_DEFAULT - rep.trust) * min(1.0, days / 30))


def norm_text(t) -> str:
    return re.sub(r"[^a-z0-9ഀ-ൿ]+", " ", str(t or "").lower()).strip()


def text_sim(a, b) -> float:
    A = {w for w in norm_text(a).split() if len(w) > 2}
    B = {w for w in norm_text(b).split() if len(w) > 2}
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def is_junk(text) -> bool:
    if text is None:
        return False
    t = str(text).strip().lower()
    if not t:
        return False
    if len(re.sub(r"[^a-zഀ-ൿ]", "", t)) < 3:
        return True
    if re.match(r"^(test|testing|asdf|qwerty|hi|hello|hai|ok|abc|xyz|sample|dummy|check)\b", t) and len(t.split()) <= 4:
        return True
    if re.search(r"(.)\1{5,}", t):
        return True
    return False


def lower_one(priority: str) -> str:
    i = PRIORITIES.index(priority) if priority in PRIORITIES else 2
    return PRIORITIES[min(3, i + 1)]


def screen(req, open_requests, recent_requests, reporter, user_text, gps):
    """Mutates `req` (a HelpRequest not yet added) with flags and adjusted priority.
    Returns the request to merge into instead, or None."""
    now = now_ms()
    key = req.reporter_key
    mine_open = sorted([r for r in open_requests if r.reporter_key == key], key=lambda r: -r.created_at)
    if len(mine_open) >= MAX_OPEN_PER_REPORTER:
        return mine_open[0]

    flags = []
    if reporter is not None and reporter.blocked:
        flags.append("blocked")
    if trust_of(reporter, now) < LOW_TRUST:
        flags.append("lowtrust")
    if not req.sos and not req.media and is_junk(user_text):
        flags.append("junk")
    for r in open_requests:
        if now - r.created_at < DUP_WINDOW_MIN * 60_000 and (
            r.reporter_key == key or (
                r.lat is not None and req.lat is not None
                and km_between(r.lat, r.lng, req.lat, req.lng) <= DUP_RADIUS_KM
                and text_sim(r.description, req.description) >= 0.3)):
            flags.append("dup")
            req.dup_of = r.id
            break
    nt = norm_text(user_text or req.description)
    if len(nt) > 10:
        same = [r for r in recent_requests if r.reporter_key != key and now - r.created_at < 10 * 60_000
                and norm_text(r.description) == nt]
        if len(same) >= 2:
            flags.append("burst")
    if gps and req.lat is not None and km_between(gps["lat"], gps["lng"], req.lat, req.lng) > 15:
        flags.append("gps")

    # Low trust lowers priority one step. An SOS keeps Emergency but is flagged for a call-back.
    if ("lowtrust" in flags or "blocked" in flags) and not req.sos:
        req.orig_priority = req.priority
        req.priority = lower_one(req.priority)
    req.flags = flags
    req.needs_verification = any(f != "dup" for f in flags)
    req.verified_phone = bool(req.phone and reporter is not None and reporter.verified)
    return None


# ---------------------------------------------------------------------------
# SMS: compact app format + keyword format for any phone
#   App:      SHM1 <SOS|RES|MED|FOD|SHL|OTH> <lat>,<lng> P<people> [F<E|C|M>] <text>
#   Keyword:  HELP <place> <message>
# ---------------------------------------------------------------------------
CODE_CAT = {"RES": "rescue", "MED": "medical", "FOD": "food", "SHL": "shelter", "OTH": "general", "SOS": "rescue"}
_APP_RE = re.compile(r"^SHM1\s+(SOS|RES|MED|FOD|SHL|OTH)\s+(-?\d{1,2}(?:\.\d+)?),(-?\d{1,3}(?:\.\d+)?)(?:\s+P(\d+))?(?:\s+F([ECM]+))?\s*(.*)$", re.I)
_KEY_RE = re.compile(r"^(HELP|SOS|SAHAYAM)\b[\s,:.-]*(.*)$", re.I)


def parse_sms(body: str) -> dict:
    raw = (body or "").strip()
    m = _APP_RE.match(raw)
    if m:
        code = m.group(1).upper()
        lat, lng = float(m.group(2)), float(m.group(3))
        f = (m.group(5) or "").upper()
        located = in_kerala(lat, lng)
        return {
            "ok": True, "format": "app", "sos": code == "SOS", "category": CODE_CAT[code],
            "lat": lat, "lng": lng, "located": located, "people": int(m.group(4) or 1),
            "flags": {"elderly": "E" in f, "children": "C" in f, "medical": "M" in f},
            "text": m.group(6) or "", "place": district_of(lat, lng) if located else None,
        }
    m = _KEY_RE.match(raw)
    if m:
        rest = m.group(2) or ""
        low = rest.lower()
        place = find_place(rest)
        if re.search(r"medic|insulin|dialysis|doctor|hospital|pregnan|labou?r|injur|bleed", low):
            cat = "medical"
        elif re.search(r"food|water|milk|hungry|drink", low):
            cat = "food"
        elif re.search(r"shelter|camp|evacuat|house damaged", low):
            cat = "shelter"
        else:
            cat = "rescue"
        pm = re.search(r"(\d+)\s*(people|persons|members|of us|family|children|kids)", low) or re.search(r"family of\s*(\d+)", low)
        return {
            "ok": True, "format": "keyword", "sos": m.group(1).upper() == "SOS", "category": cat,
            "lat": place[1] if place else None, "lng": place[2] if place else None,
            "located": place is not None, "place": place[0] if place else None,
            "people": int(pm.group(1)) if pm else 1, "text": rest,
            "flags": {
                "elderly": bool(re.search(r"elder|old|grand|aged|amma", low)),
                "children": bool(re.search(r"child|kid|baby|infant", low)),
                "medical": bool(re.search(r"sick|medic|insulin|dialysis|pregnan|wheelchair|bedridden|oxygen", low)),
            },
        }
    return {"ok": False, "error": "Not a Sahayam message"}


REPLY_NO_PLACE = "Sahayam: We couldn't find your place. Reply HELP <your place> <problem>, e.g. HELP Aluva water in house."
REPLY_INVALID = "Sahayam: To ask for help, send HELP <your place> <problem>. Emergency: call 112."


def reply_for_status(req_id: int, status: str, vol_first: str = "") -> str:
    rid = f"R-{req_id}"
    return {
        "Pending": f"Sahayam: Request {rid} received. Keep your phone on.",
        "Assigned": f"Sahayam: Volunteer {vol_first} assigned to {rid}.".replace("  ", " "),
        "En Route": "Sahayam: Help is on the way. Stay where you are.",
        "On Site": "Sahayam: The volunteer has reached your location.",
        "Resolved": f"Sahayam: {rid} is closed. Send HELP again if you need more help.",
    }.get(status, "")
