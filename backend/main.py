"""
Sahayam API (v2).

Serves the app's data and the built frontend from one process:
    uvicorn main:app --host 0.0.0.0 --port 8000

Citizens never log in. Staff (control room + volunteers) use bearer tokens.
Citizens' own requests are recognised by the X-Device-Id header the app sends.
"""
from __future__ import annotations

import os
import random
import re
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth import create_session, destroy_session, get_user_id_for_token, hash_password, verify_password
from database import (Alert, AuditLog, HelpRequest, Reporter, SessionLocal, Setting, SmsLog, Subscription,
                      User, get_db, init_db, now_ms)
from geo import DISTRICTS, district_of, km_between
from ml_engine import engine as ml_engine, find_hotspots, hotspot_boost_for_point
from priority_queue import queue
from screening import (CLAIM_TIMEOUT_MIN, LABEL_TO_PRIORITY, LOW_TRUST, OPEN, OUTCOMES, PRIORITIES,
                       REPLY_INVALID, REPLY_NO_PLACE, TRUST_DEFAULT, clamp01, parse_sms, reply_for_status,
                       screen, trust_of)
from sms_provider import provider_configured, send_sms

VERSION = "2.0.0"
BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR.parent / "frontend"
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
MAX_MEDIA_BYTES = 100 * 1024 * 1024
ALERT_TTL_MS = 48 * 3600 * 1000
SMS_WEBHOOK_SECRET = os.environ.get("SMS_WEBHOOK_SECRET")  # optional shared secret for the gateway

app = FastAPI(title="Sahayam API", version=VERSION,
              description="ML-assisted, Malayalam-first disaster coordination platform for Kerala")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def rid(r_or_id) -> str | None:
    if r_or_id is None:
        return None
    return f"R-{r_or_id if isinstance(r_or_id, int) else r_or_id.id}"


def parse_rid(s: str) -> int:
    m = re.match(r"^R-?(\d+)$", str(s).strip(), re.I) or re.match(r"^(\d+)$", str(s).strip())
    if not m:
        raise HTTPException(404, "Request not found")
    return int(m.group(1))


def phone_key(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    return digits[-10:] if len(digits) >= 10 else (digits or None)


def ist_clock(ms: int | None = None) -> str:
    dt = datetime.utcfromtimestamp((ms or now_ms()) / 1000) + timedelta(hours=5, minutes=30)
    return dt.strftime("%H:%M")


def audit(db, by: str, action: str, target: str, note: str = ""):
    db.add(AuditLog(by=by, action=action, target=target, note=note))


def set_status(r: HelpRequest, status: str, note: str | None = None):
    if status == r.status:
        return
    r.status = status
    entry = {"status": status, "at": now_ms()}
    if note:
        entry["note"] = note
    r.history = list(r.history or []) + [entry]


def get_setting(db, key, default=None):
    s = db.get(Setting, key)
    return s.value if s else default


def put_setting(db, key, value):
    s = db.get(Setting, key)
    if s:
        s.value = value
    else:
        db.add(Setting(key=key, value=value))


def adjust_trust(db, key: str | None, delta: float, field: str | None = None):
    if not key:
        return
    rep = db.get(Reporter, key)
    if rep is None:
        rep = Reporter(key=key, trust=TRUST_DEFAULT, updated_at=now_ms())
        db.add(rep)
    rep.trust = clamp01(trust_of(rep) + delta)
    if field:
        setattr(rep, field, (getattr(rep, field) or 0) + 1)
    rep.updated_at = now_ms()


_hits: dict[str, deque] = defaultdict(deque)


def rate_limit(key: str, limit: int, window_s: int, message: str):
    now = time.time()
    q = _hits[key]
    while q and now - q[0] > window_s:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(429, message)
    q.append(now)


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------
def optional_user(authorization: str | None = Header(default=None), db: Session = Depends(get_db)) -> User | None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    uid = get_user_id_for_token(authorization.split(" ", 1)[1].strip())
    return db.get(User, uid) if uid else None


def current_user(user: User | None = Depends(optional_user)) -> User:
    if user is None:
        raise HTTPException(401, "Please sign in again.")
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "Only the control room can do this.")
    return user


def ser_user(u: User, full: bool) -> dict:
    d = {"id": str(u.id), "role": u.role, "name": u.name, "skills": u.skills or [], "district": u.district,
         "status": u.status, "available": bool(u.available)}
    if full:
        d.update({"username": u.username, "phone": u.phone, "lat": u.lat, "lng": u.lng})
    else:  # public: approximate base location only, no contact details
        d.update({"lat": round(u.lat, 2) if u.lat is not None else None, "lng": round(u.lng, 2) if u.lng is not None else None})
    return d


# ---------------------------------------------------------------------------
# serialisers
# ---------------------------------------------------------------------------
def ser_req(r: HelpRequest, staff: bool, device: str | None, users_by_id: dict) -> dict:
    mine = bool(device and r.device_id and device == r.device_id)
    base = {"id": rid(r), "priority": r.priority, "status": r.status, "createdAt": r.created_at,
            "districtId": r.district_id, "lat": r.lat, "lng": r.lng}
    if not (staff or mine):
        # Other people's requests: only what the queue-position counter needs.
        return {**base, "limited": True, "lat": None, "lng": None}
    d = {
        **base,
        "name": r.name, "phone": r.phone, "channel": r.channel, "desc": r.description, "category": r.category,
        "people": r.people, "sos": bool(r.sos), "has_elderly": bool(r.has_elderly), "has_children": bool(r.has_children),
        "has_medical": bool(r.has_medical_need), "approxLocation": bool(r.approx_location),
        "mlScore": round(r.urgency_score) if r.urgency_score is not None else None,
        "origPriority": r.orig_priority, "priorityOverridden": bool(r.priority_overridden),
        "assignedTo": r.assigned_to, "releasedFrom": r.released_from, "history": r.history or [],
        "flags": r.flags or [], "dupOf": rid(r.dup_of), "needsVerification": bool(r.needs_verification),
        "verifiedByCall": bool(r.verified_by_call), "verifiedPhone": bool(r.verified_phone),
        "outcome": r.outcome, "resolvedBy": r.resolved_by, "mergedInto": rid(r.merged_into),
        "reports": r.reports, "updates": r.updates, "media": r.media, "mediaPending": bool(r.media_pending),
        "smsFormat": r.sms_format, "needsReview": bool(r.needs_review), "detailsAdded": bool(r.details_added),
        "cancelledByCitizen": bool(r.cancelled_by_citizen), "mine": mine,
    }
    if staff:
        d.update({"reporterKey": r.reporter_key, "smsBody": r.sms_body})
    if mine and r.assigned_to and r.assigned_to in users_by_id:
        d["volunteerPhone"] = users_by_id[r.assigned_to].phone
    return d


def ser_rep(rep: Reporter) -> dict:
    return {"trust": rep.trust, "genuine": rep.genuine, "false": rep.false_reports, "dup": rep.dup,
            "notfound": rep.notfound, "verifiedCalls": rep.verified_calls, "verified": bool(rep.verified),
            "blocked": bool(rep.blocked), "blockedBy": rep.blocked_by, "blockedAt": rep.blocked_at,
            "blockNote": rep.block_note, "updatedAt": rep.updated_at}


# ---------------------------------------------------------------------------
# request creation (shared by app, SMS gateway and offline sync)
# ---------------------------------------------------------------------------
def create_help_request(db: Session, *, desc: str, category: str = "general", people: int = 1, sos: bool = False,
                        lat=None, lng=None, phone=None, name=None, device_id=None, channel="app",
                        elderly=False, children=False, medical=False, user_text=None, gps=None,
                        media_expected=False, needs_review=False, sms_body=None, sms_format=None,
                        approx=False, client_ref=None, language="ml"):
    open_reqs = db.query(HelpRequest).filter(HelpRequest.status.in_(OPEN)).all()
    clean = [r for r in open_reqs if not set(r.flags or []) & {"dup", "junk", "burst", "blocked"}]
    hotspots = find_hotspots([{"id": r.id, "lat": r.lat, "lng": r.lng} for r in clean])
    boost = hotspot_boost_for_point(lat, lng, hotspots)
    if sos:
        score, label, conf = 95.0, "critical", 1.0
    else:
        res = ml_engine.classify(desc, elderly, children, medical, boost)
        score, label, conf = res.urgency_score, res.urgency_label, res.ml_confidence
    now = now_ms()
    key = phone_key(phone) or (f"dev:{device_id}" if device_id else None)
    r = HelpRequest(
        name=name, phone=phone_key(phone), channel=channel, language=language, reporter_key=key,
        device_id=device_id, client_ref=client_ref, description=desc, category=category,
        people=max(1, int(people or 1)), sos=sos, has_elderly=elderly, has_children=children,
        has_medical_need=medical, media_pending=bool(media_expected), needs_review=needs_review,
        sms_body=sms_body, sms_format=sms_format, lat=lat, lng=lng, district_id=district_of(lat, lng),
        approx_location=approx, urgency_score=score, urgency_label=label, ml_confidence=conf,
        priority="Emergency" if sos else LABEL_TO_PRIORITY[label], status="Pending",
        history=[{"status": "Pending", "at": now}], flags=[], created_at=now, updated_at=now,
    )
    if channel in ("app", "sms") and key:
        recent = db.query(HelpRequest).filter(HelpRequest.created_at > now - 10 * 60_000).all()
        merge_into = screen(r, open_reqs, recent, db.get(Reporter, key), user_text, gps)
        if merge_into is not None:
            merge_into.description = f"{merge_into.description} [Update {ist_clock(now)}] {desc}"
            merge_into.updates = (merge_into.updates or 0) + 1
            if media_expected:
                merge_into.media_pending = True
            db.commit()
            return merge_into, True
    db.add(r)
    db.commit()
    db.refresh(r)
    queue.push(r.id, r.urgency_score, r.created_at / 1000)
    return r, False


def sweep_idle_claims(db: Session):
    """A volunteer who claims and doesn't move within CLAIM_TIMEOUT_MIN loses the request."""
    cutoff = now_ms() - CLAIM_TIMEOUT_MIN * 60_000
    changed = False
    for r in db.query(HelpRequest).filter(HelpRequest.status == "Assigned").all():
        last = (r.history or [{}])[-1].get("at", r.created_at)
        if last < cutoff and r.assigned_to:
            vol = db.get(User, int(r.assigned_to)) if r.assigned_to.isdigit() else None
            r.released_from = r.assigned_to
            r.assigned_to = None
            set_status(r, "Pending", note="released")
            audit(db, "System", "Released idle claim", rid(r), f"No update from {vol.name if vol else 'volunteer'} for {CLAIM_TIMEOUT_MIN} min")
            changed = True
    if changed:
        db.commit()


def notify_status(db, r: HelpRequest):
    """SMS the caller at each step when the request came in by SMS."""
    if r.channel != "sms" or not r.phone:
        return
    vol = db.get(User, int(r.assigned_to)) if (r.assigned_to or "").isdigit() else None
    msg = reply_for_status(r.id, r.status, vol.name.split()[0] if vol else "")
    if msg:
        send_sms(db, r.phone, msg, r.id)


# ---------------------------------------------------------------------------
# schemas
# ---------------------------------------------------------------------------
class RequestIn(BaseModel):
    desc: str
    category: str = "general"
    people: int = 1
    sos: bool = False
    lat: float | None = None
    lng: float | None = None
    phone: str | None = None
    name: str | None = None
    has_elderly: bool = False
    has_children: bool = False
    has_medical: bool = False
    userText: str | None = None
    gps: dict | None = None
    mediaExpected: bool = False
    needsReview: bool = False
    language: str = "ml"
    clientRef: str | None = None
    smsBody: str | None = None


class StaffUpdate(BaseModel):
    status: str | None = None
    priority: str | None = None
    assignedTo: str | None = None
    needsReview: bool | None = None


class OutcomeIn(BaseModel):
    outcome: str


class NoteIn(BaseModel):
    note: str | None = None


class DetailsIn(BaseModel):
    people: int | None = None
    has_elderly: bool = False
    has_children: bool = False
    has_medical: bool = False
    phone: str | None = None
    extra: str | None = None


class LoginIn(BaseModel):
    username: str
    password: str
    role: str | None = None


class RegisterIn(BaseModel):
    name: str
    phone: str
    district: str
    skills: list[str] = []
    username: str
    password: str


class UserStatusIn(BaseModel):
    status: str


class MeIn(BaseModel):
    available: bool | None = None
    lat: float | None = None
    lng: float | None = None


class SeverityIn(BaseModel):
    severity: str


class ConditionsIn(BaseModel):
    scenario: str | None = None
    conditions: dict | None = None


class AlertIn(BaseModel):
    districtId: str
    level: str
    en: str
    ml: str


class SubscribeIn(BaseModel):
    phone: str
    district: str


class OtpSendIn(BaseModel):
    phone: str


class OtpVerifyIn(BaseModel):
    phone: str
    code: str


# ---------------------------------------------------------------------------
# startup
# ---------------------------------------------------------------------------
@app.on_event("startup")
def on_startup():
    init_db()
    db = SessionLocal()
    try:
        if db.query(User).count() == 0:
            from seed_data import seed
            seed(db)
        for r in db.query(HelpRequest).filter(HelpRequest.status.in_(OPEN)).all():
            queue.push(r.id, r.urgency_score or 0, (r.created_at or now_ms()) / 1000)
    finally:
        db.close()


@app.get("/api/health")
def health():
    return {"ok": True, "version": VERSION, "smsProvider": "twilio" if provider_configured() else "outbox"}


# ---------------------------------------------------------------------------
# one polling endpoint the app calls every few seconds
# ---------------------------------------------------------------------------
@app.get("/api/state")
def state(user: User | None = Depends(optional_user), x_device_id: str | None = Header(default=None),
          db: Session = Depends(get_db)):
    sweep_idle_claims(db)
    staff = user is not None
    admin = staff and user.role == "admin"
    users = db.query(User).all()
    users_by_id = {str(u.id): u for u in users}
    reqs = db.query(HelpRequest).order_by(HelpRequest.created_at.desc()).limit(1000).all()
    out = {
        "serverTime": now_ms(),
        "requests": [ser_req(r, staff, x_device_id, users_by_id) for r in reqs],
        "users": [ser_user(u, admin or (staff and u.role == "volunteer")) for u in users
                  if admin or (u.role == "volunteer" and u.status == "active")],
        "alerts": [{"id": f"A{a.id}", "districtId": a.district_id, "level": a.level, "en": a.en, "ml": a.ml,
                    "at": a.at, "sentTo": a.sent_to} for a in
                   db.query(Alert).filter(Alert.at > now_ms() - 7 * 86_400_000).order_by(Alert.at.desc()).all()],
        "zones": get_setting(db, "zones", {}),
        "conditions": get_setting(db, "conditions"),
        "scenario": get_setting(db, "scenario"),
        "me": ser_user(user, True) if staff else None,
    }
    if admin:
        out["reporters"] = {r.key: ser_rep(r) for r in db.query(Reporter).all()}
        out["audit"] = [{"id": f"L{a.id}", "at": a.at, "by": a.by, "action": a.action, "target": a.target, "note": a.note}
                        for a in db.query(AuditLog).order_by(AuditLog.at.desc()).limit(200).all()]
        out["smsLog"] = [{"id": f"S{s.id}", "from": s.from_number, "body": s.body, "at": s.at, "result": s.result,
                          "requestId": rid(s.request_id), "reply": s.reply, "format": s.format}
                         for s in db.query(SmsLog).filter(SmsLog.direction == "in").order_by(SmsLog.at.desc()).limit(200).all()]
        out["subscriptions"] = [{"district": s.district_id} for s in db.query(Subscription).all()]
    return out


# ---------------------------------------------------------------------------
# citizen intake (open, rate-limited per network)
# ---------------------------------------------------------------------------
@app.post("/api/requests")
def create_request(payload: RequestIn, request: Request, x_device_id: str | None = Header(default=None),
                   db: Session = Depends(get_db)):
    # Generous: mobile carriers put many phones behind one IP address.
    rate_limit("req:" + client_ip(request), 40, 600,
               "Too many requests from this network right now. In an emergency, call 112.")
    r, merged = create_help_request(
        db, desc=payload.desc, category=payload.category, people=payload.people, sos=payload.sos,
        lat=payload.lat, lng=payload.lng, phone=payload.phone, name=payload.name, device_id=x_device_id,
        channel="app", elderly=payload.has_elderly, children=payload.has_children, medical=payload.has_medical,
        user_text=payload.userText, gps=payload.gps, media_expected=payload.mediaExpected,
        needs_review=payload.needsReview, client_ref=payload.clientRef, language=payload.language)
    users_by_id = {str(u.id): u for u in db.query(User).all()}
    return {**ser_req(r, False, r.device_id, users_by_id), "mergedFrom": merged}


def _own_request(db, request_id: str, device: str | None, user: User | None) -> HelpRequest:
    r = db.get(HelpRequest, parse_rid(request_id))
    if not r:
        raise HTTPException(404, "Request not found")
    if user is None and not (device and r.device_id == device):
        raise HTTPException(403, "This request was sent from another phone.")
    return r


@app.post("/api/requests/{request_id}/media")
async def upload_media(request_id: str, file: UploadFile = File(...), x_device_id: str | None = Header(default=None),
                       user: User | None = Depends(optional_user), db: Session = Depends(get_db)):
    r = _own_request(db, request_id, x_device_id, user)
    ctype = file.content_type or ""
    if not (ctype.startswith("video/") or ctype.startswith("audio/")):
        raise HTTPException(415, "Only video or audio files can be attached.")
    ext = (Path(file.filename or "").suffix or (".mp4" if ctype.startswith("video/") else ".m4a"))[:8]
    name = f"{r.id}_{uuid.uuid4().hex[:10]}{ext}"
    size = 0
    with open(UPLOAD_DIR / name, "wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_MEDIA_BYTES:
                out.close()
                (UPLOAD_DIR / name).unlink(missing_ok=True)
                raise HTTPException(413, "That file is too large (max 100 MB).")
            out.write(chunk)
    r.media = {"kind": "video" if ctype.startswith("video/") else "audio", "url": f"/uploads/{name}",
               "name": file.filename, "size": size, "type": ctype}
    r.media_pending = False
    db.commit()
    return {"ok": True, "media": r.media}


@app.post("/api/requests/{request_id}/details")
def add_details(request_id: str, payload: DetailsIn, x_device_id: str | None = Header(default=None),
                user: User | None = Depends(optional_user), db: Session = Depends(get_db)):
    r = _own_request(db, request_id, x_device_id, user)
    extra = [f"{payload.people} people" if payload.people and payload.people > 1 else None,
             "elderly" if payload.has_elderly else None, "children" if payload.has_children else None,
             "medical need" if payload.has_medical else None, payload.extra]
    extra = ", ".join(x for x in extra if x)
    if extra:
        r.description = f"{r.description} Details: {extra}."
    if payload.people:
        r.people = payload.people
    r.has_elderly = r.has_elderly or payload.has_elderly
    r.has_children = r.has_children or payload.has_children
    r.has_medical_need = r.has_medical_need or payload.has_medical
    if payload.phone and not r.phone:
        r.phone = phone_key(payload.phone)
    r.details_added = True
    db.commit()
    return {"ok": True}


@app.post("/api/requests/{request_id}/cancel")
def cancel_request(request_id: str, x_device_id: str | None = Header(default=None),
                   user: User | None = Depends(optional_user), db: Session = Depends(get_db)):
    r = _own_request(db, request_id, x_device_id, user)
    r.cancelled_by_citizen = True
    set_status(r, "Resolved")
    queue.remove(r.id)
    db.commit()
    return {"ok": True}


@app.post("/api/requests/sync")
def sync_offline(payload: RequestIn, request: Request, x_device_id: str | None = Header(default=None),
                 db: Session = Depends(get_db)):
    """The app's outbox: a request first sent as SMS while offline, now uploaded with full
    details. If the gateway already created it from the SMS, attach the details to that one."""
    rate_limit("req:" + client_ip(request), 40, 600, "Too many requests from this network right now.")
    users_by_id = {str(u.id): u for u in db.query(User).all()}
    if payload.clientRef:
        existing = db.query(HelpRequest).filter(HelpRequest.client_ref == payload.clientRef).first()
        if existing:
            return {**ser_req(existing, False, x_device_id, users_by_id), "mergedFrom": False}
    if payload.smsBody:
        match = db.query(HelpRequest).filter(HelpRequest.sms_body == payload.smsBody,
                                             HelpRequest.created_at > now_ms() - 3 * 3600_000).first()
        if match:
            match.description = payload.desc or match.description
            match.device_id = match.device_id or x_device_id
            match.client_ref = payload.clientRef
            match.media_pending = payload.mediaExpected
            if payload.phone and not match.phone:
                match.phone = phone_key(payload.phone)
            db.commit()
            return {**ser_req(match, False, x_device_id, users_by_id), "mergedFrom": False}
    r, merged = create_help_request(
        db, desc=payload.desc, category=payload.category, people=payload.people, sos=payload.sos,
        lat=payload.lat, lng=payload.lng, phone=payload.phone, device_id=x_device_id, channel="sms",
        elderly=payload.has_elderly, children=payload.has_children, medical=payload.has_medical,
        user_text=payload.userText, gps=payload.gps, media_expected=payload.mediaExpected,
        needs_review=payload.needsReview, sms_body=payload.smsBody, sms_format="app",
        client_ref=payload.clientRef, language=payload.language)
    return {**ser_req(r, False, x_device_id, users_by_id), "mergedFrom": merged}


# ---------------------------------------------------------------------------
# SMS gateway webhook (JSON {"from","body"}, or Twilio's form fields From/Body)
# ---------------------------------------------------------------------------
@app.post("/api/sms/incoming")
async def sms_incoming(request: Request, x_sms_secret: str | None = Header(default=None), db: Session = Depends(get_db)):
    if SMS_WEBHOOK_SECRET and x_sms_secret != SMS_WEBHOOK_SECRET and request.query_params.get("secret") != SMS_WEBHOOK_SECRET:
        raise HTTPException(403, "Bad gateway secret")
    twilio = "application/x-www-form-urlencoded" in (request.headers.get("content-type") or "")
    if twilio:
        form = await request.form()
        sender, body = form.get("From"), form.get("Body") or ""
    else:
        data = await request.json()
        sender, body = data.get("from"), data.get("body") or ""
    rate_limit("sms:" + str(sender), 20, 600, "Too many messages from this number.")
    parsed = parse_sms(body)
    log = SmsLog(direction="in", from_number=sender, body=body)
    result, reply, req = "invalid", REPLY_INVALID, None
    if parsed["ok"] and not parsed["located"]:
        result, reply = "noplace", REPLY_NO_PLACE
    elif parsed["ok"]:
        f = parsed["flags"]
        text = parsed["text"] or ("SOS alert sent by SMS" if parsed["sos"] else f"{parsed['category'].title()} help needed")
        req, merged = create_help_request(
            db, desc=text, category=parsed["category"], people=parsed["people"], sos=parsed["sos"],
            lat=parsed["lat"], lng=parsed["lng"], phone=sender, channel="sms", elderly=f["elderly"],
            children=f["children"], medical=f["medical"], user_text=parsed["text"] or None,
            sms_body=body, sms_format=parsed["format"], approx=parsed["format"] == "keyword")
        result = "merged" if merged else "created"
        reply = reply_for_status(req.id, "Pending")
        log.format = parsed["format"]
        log.request_id = req.id
    log.result = result
    if result != "created":
        log.reply = reply
    db.add(log)
    send_sms(db, sender, reply, req.id if req else None)
    db.commit()
    if twilio:
        safe = reply.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return Response(f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{safe}</Message></Response>',
                        media_type="application/xml")
    return {"ok": parsed["ok"], "result": result, "requestId": rid(req) if req else None, "reply": reply,
            "format": parsed.get("format")}


# ---------------------------------------------------------------------------
# optional phone verification (one-time code)
# ---------------------------------------------------------------------------
_OTPS: dict[str, tuple[str, float, int]] = {}


@app.post("/api/otp/send")
def otp_send(payload: OtpSendIn, request: Request, db: Session = Depends(get_db)):
    phone = phone_key(payload.phone)
    if not phone or len(phone) != 10:
        raise HTTPException(400, "Enter a 10-digit mobile number.")
    rate_limit("otp:" + phone, 3, 600, "Too many codes for this number. Wait 10 minutes.")
    rate_limit("otpip:" + client_ip(request), 10, 600, "Too many codes from this network. Wait 10 minutes.")
    code = f"{random.randint(1000, 9999)}"
    _OTPS[phone] = (code, time.time() + 300, 0)
    send_sms(db, phone, f"Sahayam verification code: {code}. It expires in 5 minutes.")
    db.commit()
    out = {"sent": True}
    if not provider_configured():
        out["demoCode"] = code  # no SMS provider yet: show the code in the app
    return out


@app.post("/api/otp/verify")
def otp_verify(payload: OtpVerifyIn, db: Session = Depends(get_db)):
    phone = phone_key(payload.phone)
    item = _OTPS.get(phone or "")
    if not item or time.time() > item[1]:
        raise HTTPException(400, "That code has expired. Send a new one.")
    code, exp, tries = item
    if tries >= 5:
        raise HTTPException(429, "Too many wrong tries. Send a new code.")
    if payload.code.strip() != code:
        _OTPS[phone] = (code, exp, tries + 1)
        raise HTTPException(400, "That code is wrong. Check the SMS and try again.")
    _OTPS.pop(phone, None)
    rep = db.get(Reporter, phone)
    if rep is None:
        rep = Reporter(key=phone, trust=TRUST_DEFAULT, updated_at=now_ms())
        db.add(rep)
    if not rep.verified:
        rep.verified = True
        rep.trust = clamp01(trust_of(rep) + 0.1)
        rep.updated_at = now_ms()
    db.commit()
    return {"verified": True}


# ---------------------------------------------------------------------------
# staff: login, registration, profiles
# ---------------------------------------------------------------------------
@app.post("/api/auth/login")
def login(payload: LoginIn, db: Session = Depends(get_db)):
    u = db.query(User).filter(User.username == payload.username.strip().lower()).first()
    if not u or not verify_password(payload.password, u.password_hash) or (payload.role and payload.role != u.role):
        raise HTTPException(401, "That username and password don't match. Check both and try again.")
    if u.status == "pending":
        raise HTTPException(403, "Your volunteer account is waiting for approval from the control room. You can sign in once it's approved.")
    if u.status == "rejected":
        raise HTTPException(403, "This account wasn't approved. Contact the district control room on 1077.")
    return {"token": create_session(u.id), "user": ser_user(u, True)}


@app.post("/api/auth/logout")
def logout(authorization: str | None = Header(default=None)):
    if authorization and authorization.lower().startswith("bearer "):
        destroy_session(authorization.split(" ", 1)[1].strip())
    return {"ok": True}


@app.get("/api/auth/me")
def me(user: User = Depends(current_user)):
    return ser_user(user, True)


@app.post("/api/auth/register")
def register(payload: RegisterIn, request: Request, db: Session = Depends(get_db)):
    rate_limit("reg:" + client_ip(request), 10, 3600, "Too many registrations from this network. Try again later.")
    username = payload.username.strip().lower()
    if not re.match(r"^[a-z0-9_.]{3,20}$", username):
        raise HTTPException(400, "Username: 3 to 20 letters, numbers, dots or underscores.")
    if len(payload.password) < 6:
        raise HTTPException(400, "Password needs at least 6 characters.")
    if payload.district not in DISTRICTS:
        raise HTTPException(400, "Choose the district you can help in.")
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(409, "That username is taken. Try another one.")
    lat, lng = DISTRICTS[payload.district]["center"]
    db.add(User(username=username, password_hash=hash_password(payload.password), role="volunteer",
                name=payload.name.strip(), phone=phone_key(payload.phone), district=payload.district,
                skills=payload.skills, status="pending", available=False, lat=lat, lng=lng))
    db.commit()
    return {"ok": True}


@app.patch("/api/me")
def update_me(payload: MeIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if payload.available is not None:
        user.available = payload.available
    if payload.lat is not None and payload.lng is not None:
        user.lat, user.lng = payload.lat, payload.lng
    db.commit()
    return ser_user(user, True)


@app.patch("/api/users/{user_id}")
def set_user_status(user_id: str, payload: UserStatusIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    u = db.get(User, int(user_id))
    if not u or u.role != "volunteer":
        raise HTTPException(404, "Volunteer not found")
    if payload.status not in ("active", "rejected", "pending"):
        raise HTTPException(400, "Unknown status")
    u.status = payload.status
    if payload.status == "active":
        u.available = True
    audit(db, admin.name, "Approved volunteer" if payload.status == "active" else "Rejected volunteer", u.name, u.username)
    db.commit()
    return ser_user(u, True)


# ---------------------------------------------------------------------------
# staff: dispatch
# ---------------------------------------------------------------------------
@app.patch("/api/requests/{request_id}")
def staff_update(request_id: str, payload: StaffUpdate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    r = db.get(HelpRequest, parse_rid(request_id))
    if not r:
        raise HTTPException(404, "Request not found")
    if user.role == "volunteer":
        if r.assigned_to != str(user.id):
            raise HTTPException(403, "This request is not assigned to you.")
        if payload.assignedTo is not None or payload.priority is not None:
            raise HTTPException(403, "Only the control room can reassign or change priority.")
        if payload.status == "Resolved":
            raise HTTPException(400, "Choose what happened when closing a request.")
    if payload.priority is not None:
        if payload.priority not in PRIORITIES:
            raise HTTPException(400, "Unknown priority")
        r.priority = payload.priority
        r.priority_overridden = True
        r.needs_review = False
    if payload.needsReview is not None:
        r.needs_review = payload.needsReview
    if payload.assignedTo is not None:
        vol = db.get(User, int(payload.assignedTo)) if payload.assignedTo.isdigit() else None
        if not vol or vol.role != "volunteer" or vol.status != "active":
            raise HTTPException(400, "Choose an active volunteer.")
        r.assigned_to = str(vol.id)
        set_status(r, "Assigned")
        notify_status(db, r)
    if payload.status is not None and payload.status != r.status:
        if payload.status not in ("Pending",) + OPEN[1:] + ("Resolved",):
            raise HTTPException(400, "Unknown status")
        set_status(r, payload.status)
        if payload.status == "Resolved":
            queue.remove(r.id)
        notify_status(db, r)
    db.commit()
    return {"ok": True}


@app.post("/api/requests/{request_id}/claim")
def claim(request_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.role != "volunteer":
        raise HTTPException(403, "Only volunteers claim requests.")
    if not user.available:
        raise HTTPException(400, "Switch to Available before claiming requests.")
    r = db.get(HelpRequest, parse_rid(request_id))
    if not r:
        raise HTTPException(404, "Request not found")
    if r.assigned_to:
        raise HTTPException(409, "Someone else has already claimed this request.")
    if "blocked" in (r.flags or []):
        raise HTTPException(403, "This request is under review by the control room.")
    r.assigned_to = str(user.id)
    set_status(r, "Assigned")
    notify_status(db, r)
    db.commit()
    return {"ok": True}


@app.post("/api/requests/{request_id}/outcome")
def outcome(request_id: str, payload: OutcomeIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    r = db.get(HelpRequest, parse_rid(request_id))
    if not r:
        raise HTTPException(404, "Request not found")
    if payload.outcome not in OUTCOMES:
        raise HTTPException(400, "Unknown outcome")
    if user.role == "volunteer" and r.assigned_to != str(user.id):
        raise HTTPException(403, "This request is not assigned to you.")
    delta, field = OUTCOMES[payload.outcome]
    r.outcome = payload.outcome
    r.resolved_by = user.name
    set_status(r, "Resolved")
    queue.remove(r.id)
    adjust_trust(db, r.reporter_key, delta, field)
    if payload.outcome in ("false", "notfound"):
        audit(db, user.name, "Marked false report" if payload.outcome == "false" else "Marked nobody found", rid(r), r.reporter_key or "")
    notify_status(db, r)
    db.commit()
    return {"ok": True}


@app.post("/api/requests/{request_id}/verify")
def verify(request_id: str, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    r = db.get(HelpRequest, parse_rid(request_id))
    if not r:
        raise HTTPException(404, "Request not found")
    r.flags = [f for f in (r.flags or []) if f == "dup"]
    r.needs_verification = False
    r.verified_by_call = True
    if r.orig_priority:
        r.priority = r.orig_priority
    adjust_trust(db, r.reporter_key, 0.05, "verified_calls")
    audit(db, admin.name, "Verified by call", rid(r), r.reporter_key or "")
    db.commit()
    return {"ok": True}


@app.post("/api/requests/{request_id}/merge")
def merge(request_id: str, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    r = db.get(HelpRequest, parse_rid(request_id))
    if not r or not r.dup_of:
        raise HTTPException(404, "Nothing to merge")
    tgt = db.get(HelpRequest, r.dup_of)
    if not tgt:
        raise HTTPException(404, "Original request not found")
    who = r.name or (f"+91 {r.phone}" if r.phone else "another caller")
    tgt.description = f"{tgt.description} [Also reported by {who}] {r.description}"
    tgt.reports = (tgt.reports or 1) + 1
    tgt.people = max(tgt.people or 1, r.people or 1)
    tgt.media = tgt.media or r.media
    r.outcome = "duplicate"
    r.merged_into = tgt.id
    r.flags = []
    r.needs_verification = False
    set_status(r, "Resolved")
    queue.remove(r.id)
    audit(db, admin.name, "Merged duplicate", rid(r), f"into {rid(tgt)}")
    db.commit()
    return {"ok": True}


@app.post("/api/requests/{request_id}/not-duplicate")
def not_duplicate(request_id: str, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    r = db.get(HelpRequest, parse_rid(request_id))
    if not r:
        raise HTTPException(404, "Request not found")
    audit(db, admin.name, "Not a duplicate", rid(r), f"was linked to {rid(r.dup_of)}")
    r.flags = [f for f in (r.flags or []) if f != "dup"]
    r.dup_of = None
    db.commit()
    return {"ok": True}


@app.post("/api/reporters/{key}/block")
def block(key: str, payload: NoteIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    rep = db.get(Reporter, key)
    if rep is None:
        rep = Reporter(key=key, trust=TRUST_DEFAULT)
        db.add(rep)
    rep.blocked, rep.blocked_by, rep.blocked_at, rep.block_note = True, admin.name, now_ms(), payload.note
    rep.updated_at = now_ms()
    for r in db.query(HelpRequest).filter(HelpRequest.reporter_key == key, HelpRequest.status.in_(OPEN)).all():
        r.flags = sorted(set(r.flags or []) | {"blocked"})
        r.needs_verification = True
    audit(db, admin.name, "Blocked number", key if key.startswith("dev:") else f"+91 {key}", payload.note or "")
    db.commit()
    return {"ok": True}


@app.post("/api/reporters/{key}/unblock")
def unblock(key: str, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    rep = db.get(Reporter, key)
    if not rep:
        raise HTTPException(404, "Number not found")
    rep.blocked = False
    rep.trust = max(rep.trust, LOW_TRUST)
    rep.updated_at = now_ms()
    for r in db.query(HelpRequest).filter(HelpRequest.reporter_key == key).all():
        r.flags = [f for f in (r.flags or []) if f != "blocked"]
    audit(db, admin.name, "Unblocked (appeal accepted)", key if key.startswith("dev:") else f"+91 {key}", "")
    db.commit()
    return {"ok": True}


@app.post("/api/requests/phone-call")
def log_phone_call(payload: RequestIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Control room logs a request that came in by phone call."""
    r, _ = create_help_request(db, desc=payload.desc, category=payload.category, lat=payload.lat, lng=payload.lng,
                               phone=payload.phone, name=payload.name, channel="call")
    if payload.smsBody in PRIORITIES:  # priority chosen by the operator
        r.priority = payload.smsBody
        r.priority_overridden = True
        db.commit()
    return {"ok": True, "id": rid(r)}


# ---------------------------------------------------------------------------
# control room: zones, forecast, alerts
# ---------------------------------------------------------------------------
@app.put("/api/zones/{district_id}")
def set_zone(district_id: str, payload: SeverityIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    if district_id not in DISTRICTS or payload.severity not in ("Critical", "Severe", "Moderate", "Low"):
        raise HTTPException(400, "Unknown district or level")
    zones = dict(get_setting(db, "zones", {}) or {})
    zones[district_id] = payload.severity
    put_setting(db, "zones", zones)
    db.commit()
    return {"ok": True}


@app.put("/api/risk/conditions")
def set_conditions(payload: ConditionsIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    if payload.conditions is not None:
        put_setting(db, "conditions", payload.conditions)
    put_setting(db, "scenario", payload.scenario or "custom")
    db.commit()
    return {"ok": True}


@app.post("/api/alerts")
def issue_alert(payload: AlertIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    if payload.districtId not in DISTRICTS or payload.level not in ("Watch", "Warning", "Severe"):
        raise HTTPException(400, "Unknown district or level")
    subs = db.query(Subscription).filter(Subscription.district_id == payload.districtId).all()
    a = Alert(district_id=payload.districtId, level=payload.level, en=payload.en, ml=payload.ml,
              sent_to=len(subs), issued_by=admin.name)
    db.add(a)
    for s in subs:
        send_sms(db, s.phone, payload.en[:300])
    audit(db, admin.name, f"Issued {payload.level} alert", DISTRICTS[payload.districtId]["name"], f"SMS to {len(subs)}")
    db.commit()
    return {"ok": True, "sentTo": len(subs)}


@app.post("/api/alerts/subscribe")
def subscribe(payload: SubscribeIn, request: Request, db: Session = Depends(get_db)):
    rate_limit("sub:" + client_ip(request), 20, 600, "Too many sign-ups from this network. Try again later.")
    phone = phone_key(payload.phone)
    if not phone or len(phone) != 10 or payload.district not in DISTRICTS:
        raise HTTPException(400, "Enter a 10-digit mobile number and choose a district.")
    if not db.query(Subscription).filter(Subscription.phone == phone, Subscription.district_id == payload.district).first():
        db.add(Subscription(phone=phone, district_id=payload.district))
        db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# kept from v1
# ---------------------------------------------------------------------------
@app.get("/api/hotspots")
def hotspots(db: Session = Depends(get_db)):
    pts = [{"id": r.id, "lat": r.lat, "lng": r.lng} for r in db.query(HelpRequest).filter(HelpRequest.status.in_(OPEN)).all()
           if not set(r.flags or []) & {"dup", "junk", "burst", "blocked"}]
    return find_hotspots(pts)


@app.get("/api/stats")
def stats(db: Session = Depends(get_db)):
    reqs = db.query(HelpRequest).all()
    by_p = {p: 0 for p in PRIORITIES}
    by_s: dict[str, int] = {}
    for r in reqs:
        by_p[r.priority] = by_p.get(r.priority, 0) + 1
        by_s[r.status] = by_s.get(r.status, 0) + 1
    return {"total": len(reqs), "by_priority": by_p, "by_status": by_s, "queue_length": len(queue),
            "flagged_open": sum(1 for r in reqs if r.status in OPEN and r.flags)}


app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
