"""
First-run demo data: the same scenario the app's offline demo mode shows
(Aluva hotspot, SMS requests, a flagged junk request, a duplicate, an idle
claim, a blocked number and a live flood warning), so the control room has
something meaningful on screen from the first launch.

Delete sahayam_v2.db to start fresh.
"""
from auth import hash_password
from database import Alert, AuditLog, HelpRequest, Reporter, SmsLog, Subscription, User, now_ms
from geo import district_of

MIN = 60_000
DAY = 86_400_000

USERS = [
    # (username, password, role, name, phone, district, skills, status, lat, lng)
    ("admin", "admin123", "admin", "Control Room, Ernakulam", "4842000000", "ernakulam", [], "active", None, None),
    ("arjun", "arjun123", "volunteer", "Arjun Menon", "9847100001", "ernakulam", ["boat", "firstaid"], "active", 10.1075, 76.3516),
    ("divya", "divya123", "volunteer", "Divya Pillai", "9847100002", "ernakulam", ["medical", "driving"], "active", 9.9816, 76.2999),
    ("rahul", "rahul123", "volunteer", "Rahul Varma", "9847100003", "alappuzha", ["boat", "swim"], "active", 9.3833, 76.4333),
    ("meera", "meera123", "volunteer", "Meera Joseph", "9847100004", "thrissur", ["cooking", "firstaid"], "pending", 10.5276, 76.2144),
]

# (lat, lng, priority, name, phone, desc, channel, age_min, assigned_username, status, extra)
REQUESTS = [
    (10.1075, 76.3516, "Emergency", "Radhamani K.", "9847000001", "Water entering house fast, elderly mother trapped on second floor, cannot swim.", "app", 3, "arjun", "En Route", {}),
    (9.3833, 76.4333, "Emergency", None, "9847000002", "Pregnant woman in labour, roads flooded, cannot reach hospital.", "sms", 6, "rahul", "Assigned", {"sms_body": "SHM1 MED 9.3833,76.4333 P2 FM Pregnant woman in labour roads flooded", "sms_format": "app"}),
    (9.9816, 76.2999, "High", "Joseph Mathew", "9847000003", "Wheelchair user cannot evacuate alone, water rising slowly.", "app", 14, "divya", "On Site", {}),
    (10.1100, 76.3550, "High", None, "9847000004", "Diabetic patient out of insulin, area cut off by floodwater.", "sms", 22, None, "Pending", {"sms_body": "HELP Aluva diabetic patient no insulin area cut off", "sms_format": "keyword", "approx_location": True}),
    (10.1040, 76.3480, "High", "Shaji P.", "9847000009", "Family of five on terrace, ground floor fully under water.", "app", 9, None, "Pending", {"people": 5}),
    (10.5276, 76.2144, "Medium", None, "9847000005", "Need dry food and clean water for family of four.", "sms", 40, None, "Pending", {"sms_body": "HELP Thrissur need food and water family of 4", "sms_format": "keyword", "approx_location": True, "people": 4}),
    (9.3900, 76.4400, "Medium", "Anitha Raj", "9847000006", "House compound flooded, main house still dry for now.", "app", 55, None, "Pending", {}),
    (9.8500, 77.0500, "Low", "Ravi S.", "9847000007", "Tree fallen across the road near the school.", "app", 95, None, "Resolved", {"outcome": "helped"}),
    (9.9800, 76.3000, "High", "Suresh Nair", "9847000008", "Two children stuck on first floor, water still rising.", "app", 2, None, "Pending", {"has_children": True}),
    (9.9312, 76.2673, "Low", None, "9847000013", "test test hello", "app", 4, None, "Pending", {"flags": ["junk", "lowtrust"], "needs_verification": True, "orig_priority": "Medium"}),
    (10.1046, 76.3486, "High", "Bindu S.", "9847000010", "Family of five stuck on terrace, ground floor under water.", "app", 5, None, "Pending", {"flags": ["dup"], "dup_of": 104, "people": 5}),
    (9.9700, 76.2850, "Medium", "Thomas K.", "9847000011", "Elderly couple need help moving out of the ground floor.", "app", 19, "divya", "Assigned", {"has_elderly": True}),
]
FLOW = ["Assigned", "En Route", "On Site", "Resolved"]
SCORES = {"Emergency": 92, "High": 68, "Medium": 42, "Low": 20}
LABELS = {"Emergency": "critical", "High": "high", "Medium": "medium", "Low": "low"}


def seed(db):
    now = now_ms()
    ids = {}
    for i, (un, pw, role, name, phone, district, skills, status, lat, lng) in enumerate(USERS, start=1):
        u = User(id=i, username=un, password_hash=hash_password(pw), role=role, name=name, phone=phone,
                 district=district, skills=skills, status=status, available=status == "active", lat=lat, lng=lng)
        db.add(u)
        ids[un] = str(i)
    db.flush()

    for n, (lat, lng, pri, name, phone, desc, channel, age, assignee, status, extra) in enumerate(REQUESTS):
        created = now - age * MIN
        history = [{"status": "Pending", "at": created}]
        upto = FLOW.index(status) if status in FLOW else -1
        step = min(MIN, age * MIN // (upto + 2)) if upto >= 0 else 0
        for i in range(upto + 1):
            history.append({"status": FLOW[i], "at": created + (i + 1) * step})
        r = HelpRequest(
            id=100 + n, name=name, phone=phone, reporter_key=phone, channel=channel, description=desc,
            category="rescue", people=extra.get("people", 1), lat=lat, lng=lng, district_id=district_of(lat, lng),
            urgency_score=SCORES[pri], urgency_label=LABELS[pri], ml_confidence=0.8, priority=pri,
            status=status, assigned_to=ids.get(assignee) if assignee else None, history=history,
            flags=extra.get("flags", []), dup_of=extra.get("dup_of"),
            needs_verification=extra.get("needs_verification", False), orig_priority=extra.get("orig_priority"),
            sms_body=extra.get("sms_body"), sms_format=extra.get("sms_format"),
            approx_location=extra.get("approx_location", False), outcome=extra.get("outcome"),
            has_children=extra.get("has_children", False), has_elderly=extra.get("has_elderly", False),
            created_at=created, updated_at=created,
        )
        db.add(r)

    db.add_all([
        Reporter(key="9847000013", trust=0.18, false_reports=3, dup=1, notfound=1, updated_at=now - 2 * DAY),
        Reporter(key="9847000666", trust=0.05, false_reports=5, notfound=2, updated_at=now - DAY, blocked=True,
                 blocked_by="Control Room, Ernakulam", blocked_at=now - DAY, block_note="5 hoax SOS alerts in one night"),
        Reporter(key="9847000001", trust=0.75, genuine=2, verified=True, updated_at=now - 20 * DAY),
    ])
    db.add(AuditLog(at=now - DAY, by="Control Room, Ernakulam", action="Blocked number", target="+91 9847000666",
                    note="5 hoax SOS alerts in one night"))
    db.add_all([
        SmsLog(at=now - 22 * MIN, direction="in", from_number="+91 98470 00004",
               body="HELP Aluva diabetic patient no insulin area cut off", result="created", request_id=103, format="keyword"),
        SmsLog(at=now - 12 * MIN, direction="in", from_number="+91 98470 00013", body="help pls water coming inside",
               result="noplace", reply="Sahayam: We couldn't find your place. Reply HELP <your place> <problem>, e.g. HELP Aluva water in house."),
    ])
    db.add(Alert(district_id="ernakulam", level="Warning", at=now - 40 * MIN, sent_to=3, issued_by="Control Room, Ernakulam",
                 en="WARNING for Ernakulam: Move valuables and documents up high, and be ready to go to a relief camp. Areas hit before: Aluva, North Paravur. Need help? Call 112 or SMS HELP <place> to +91 00000 00000.",
                 ml="Ernakulam - മുന്നറിയിപ്പ്: വിലപ്പെട്ട സാധനങ്ങളും രേഖകളും ഉയരത്തിലേക്ക് മാറ്റുക. ക്യാമ്പിലേക്ക് പോകാൻ തയ്യാറായിരിക്കുക. മുൻപ് ബാധിച്ച പ്രദേശങ്ങൾ: Aluva, North Paravur. സഹായത്തിന് 112 വിളിക്കുക."))
    db.add_all([Subscription(phone=p, district_id=d) for p, d in [
        ("9847200001", "ernakulam"), ("9847200002", "ernakulam"), ("9847200003", "ernakulam"),
        ("9847200004", "alappuzha"), ("9847200005", "wayanad")]])
    db.commit()
