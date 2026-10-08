"""
Database setup for Sahayam (v2).

SQLite keeps the prototype zero-config. Moving to PostgreSQL + PostGIS later
means changing SQLALCHEMY_DATABASE_URL (and optionally the lat/lng columns to
geometry) -- the models and API stay the same.

All timestamps are stored as epoch milliseconds (Integer) so they travel to the
app unchanged.
"""
import os
import time

from sqlalchemy import (Boolean, Column, Float, Integer, BigInteger, JSON, String, Text,
                        UniqueConstraint, create_engine)
from sqlalchemy.orm import declarative_base, sessionmaker

SQLALCHEMY_DATABASE_URL = os.environ.get("SAHAYAM_DB", "sqlite:///./sahayam_v2.db")

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False} if SQLALCHEMY_DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def now_ms() -> int:
    return int(time.time() * 1000)


class HelpRequest(Base):
    """One request for help, from any channel (app, SMS, phone call)."""
    __tablename__ = "requests"

    id = Column(Integer, primary_key=True, index=True)

    # who / how
    name = Column(String, nullable=True)
    phone = Column(String, nullable=True, index=True)
    channel = Column(String, default="app")          # app | sms | call
    language = Column(String, default="ml")
    reporter_key = Column(String, index=True)         # phone number, or "dev:<device id>"
    device_id = Column(String, nullable=True, index=True)
    client_ref = Column(String, nullable=True, index=True)

    # what
    description = Column(Text, nullable=False)
    category = Column(String, default="general")      # rescue | medical | food | shelter | general
    people = Column(Integer, default=1)
    sos = Column(Boolean, default=False)
    has_elderly = Column(Boolean, default=False)
    has_children = Column(Boolean, default=False)
    has_medical_need = Column(Boolean, default=False)
    media = Column(JSON, nullable=True)               # {kind, url, name, size, type}
    media_pending = Column(Boolean, default=False)
    needs_review = Column(Boolean, default=False)
    details_added = Column(Boolean, default=False)
    sms_body = Column(Text, nullable=True)
    sms_format = Column(String, nullable=True)

    # where
    lat = Column(Float, nullable=True)
    lng = Column(Float, nullable=True)
    district_id = Column(String, nullable=True, index=True)
    approx_location = Column(Boolean, default=False)

    # triage
    urgency_score = Column(Float, default=0.0)
    urgency_label = Column(String, default="medium")  # critical | high | medium | low (ML output)
    ml_confidence = Column(Float, default=0.0)
    priority = Column(String, default="Medium")       # Emergency | High | Medium | Low (what dispatch uses)
    orig_priority = Column(String, nullable=True)
    priority_overridden = Column(Boolean, default=False)

    # misuse screening
    flags = Column(JSON, default=list)                # dup | junk | lowtrust | burst | gps | blocked
    dup_of = Column(Integer, nullable=True)
    needs_verification = Column(Boolean, default=False)
    verified_by_call = Column(Boolean, default=False)
    verified_phone = Column(Boolean, default=False)
    reports = Column(Integer, default=1)
    updates = Column(Integer, default=0)
    merged_into = Column(Integer, nullable=True)

    # lifecycle
    status = Column(String, default="Pending")        # Pending | Assigned | En Route | On Site | Resolved
    assigned_to = Column(String, nullable=True)       # user id as string
    released_from = Column(String, nullable=True)
    outcome = Column(String, nullable=True)           # helped | safe | duplicate | notfound | false
    resolved_by = Column(String, nullable=True)
    cancelled_by_citizen = Column(Boolean, default=False)
    history = Column(JSON, default=list)              # [{status, at, note?}]
    created_at = Column(BigInteger, default=now_ms)
    updated_at = Column(BigInteger, default=now_ms, onupdate=now_ms)


class User(Base):
    """Admin (control room) or volunteer login. Citizens never log in."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    role = Column(String, nullable=False)             # admin | volunteer
    name = Column(String, nullable=False)
    phone = Column(String, nullable=True)
    district = Column(String, nullable=True)
    skills = Column(JSON, default=list)
    status = Column(String, default="active")         # active | pending | rejected
    available = Column(Boolean, default=True)
    lat = Column(Float, nullable=True)
    lng = Column(Float, nullable=True)
    created_at = Column(BigInteger, default=now_ms)


class Reporter(Base):
    """Trust record per phone number (or device when no phone was given)."""
    __tablename__ = "reporters"

    key = Column(String, primary_key=True)
    trust = Column(Float, default=0.6)
    genuine = Column(Integer, default=0)
    false_reports = Column(Integer, default=0)
    dup = Column(Integer, default=0)
    notfound = Column(Integer, default=0)
    verified_calls = Column(Integer, default=0)
    verified = Column(Boolean, default=False)
    blocked = Column(Boolean, default=False)
    blocked_by = Column(String, nullable=True)
    blocked_at = Column(BigInteger, nullable=True)
    block_note = Column(String, nullable=True)
    updated_at = Column(BigInteger, default=now_ms)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id = Column(Integer, primary_key=True)
    at = Column(BigInteger, default=now_ms)
    by = Column(String)
    action = Column(String)
    target = Column(String)
    note = Column(Text, nullable=True)


class SmsLog(Base):
    """Every SMS in or out. Outgoing messages sit here as an outbox when no
    SMS provider is configured."""
    __tablename__ = "sms_log"
    id = Column(Integer, primary_key=True)
    at = Column(BigInteger, default=now_ms)
    direction = Column(String, default="in")          # in | out
    from_number = Column(String, nullable=True)
    to_number = Column(String, nullable=True)
    body = Column(Text)
    result = Column(String, nullable=True)            # created | merged | noplace | invalid | queued | sent | failed
    request_id = Column(Integer, nullable=True)
    format = Column(String, nullable=True)
    reply = Column(Text, nullable=True)


class Alert(Base):
    __tablename__ = "alerts"
    id = Column(Integer, primary_key=True)
    district_id = Column(String, index=True)
    level = Column(String)                            # Watch | Warning | Severe
    en = Column(Text)
    ml = Column(Text)
    at = Column(BigInteger, default=now_ms)
    sent_to = Column(Integer, default=0)
    issued_by = Column(String, nullable=True)


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (UniqueConstraint("phone", "district_id"),)
    id = Column(Integer, primary_key=True)
    phone = Column(String, index=True)
    district_id = Column(String, index=True)
    created_at = Column(BigInteger, default=now_ms)


class Setting(Base):
    """Small key/value store: zone threat levels, forecast conditions."""
    __tablename__ = "settings"
    key = Column(String, primary_key=True)
    value = Column(JSON)


def init_db():
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
