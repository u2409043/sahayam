"""
Outgoing SMS.

With no provider configured (the default), every outgoing message is written
to the sms_log table as an outbox entry, so the demo shows exactly what would
be sent. To send real messages through Twilio, set:

    SMS_PROVIDER=twilio
    TWILIO_ACCOUNT_SID=ACxxxxxxxx
    TWILIO_AUTH_TOKEN=xxxxxxxx
    TWILIO_FROM=+1xxxxxxxxxx        (your Twilio number)

Indian providers (MSG91, Gupshup, Exotel) need DLT-registered templates for
bulk alerts; add a branch below in the same shape.
"""
import base64
import os
import urllib.parse
import urllib.request

from database import SmsLog

PROVIDER = os.environ.get("SMS_PROVIDER", "none").lower()


def provider_configured() -> bool:
    return PROVIDER == "twilio" and all(os.environ.get(k) for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM"))


def _to_e164(number: str) -> str:
    digits = "".join(ch for ch in number if ch.isdigit())
    if len(digits) == 10:
        return "+91" + digits
    return "+" + digits


def send_sms(db, to: str | None, body: str, request_id: int | None = None) -> str:
    """Queue or send one SMS. Returns the result: sent | failed | queued."""
    if not to:
        return "skipped"
    entry = SmsLog(direction="out", to_number=to, body=body, request_id=request_id, result="queued")
    if provider_configured():
        sid, token = os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]
        data = urllib.parse.urlencode({"To": _to_e164(to), "From": os.environ["TWILIO_FROM"], "Body": body}).encode()
        req = urllib.request.Request(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", data=data)
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{sid}:{token}".encode()).decode())
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                entry.result = "sent" if resp.status < 300 else "failed"
        except Exception:
            entry.result = "failed"
    db.add(entry)
    return entry.result
