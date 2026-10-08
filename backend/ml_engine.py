"""
ML-Based Urgency Triage Engine
==============================

Combines three signals into one urgency score (0-100):

1. Text severity classifier -- TF-IDF + Logistic Regression trained on a
   small seed dataset of request descriptions labelled by severity.
   In production this would be retrained on real, growing request logs;
   here it ships with a synthetic seed set so the prototype works
   out of the box.

2. Rule-based factors -- vulnerable-population flags (elderly, children,
   medical need) that a text model alone tends to under-weight.

3. Hotspot density -- DBSCAN clustering of open requests' coordinates,
   so a request inside a dense cluster of other requests (a likely
   disaster epicentre) is nudged up in priority.

final_score = max(0.55 * text_severity + 0.30 * rule_score + 0.15 * hotspot_boost,
                  0.8 * text_severity)
plus safety floors: known life-threatening phrases >= 82 (critical), serious
phrases >= 58 (high). See classify().
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np
from sklearn.cluster import DBSCAN
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline

# ---------------------------------------------------------------------------
# 1. Seed training data for the text severity classifier
# ---------------------------------------------------------------------------
# Labels: 0=low, 1=medium, 2=high, 3=critical
SEED_TEXTS = [
    # critical (3) -- immediate danger to life
    ("water entering house fast, family trapped on roof, cannot swim", 3),
    ("elderly mother collapsed, not breathing properly, need ambulance now", 3),
    ("landslide hit our house, people buried under debris", 3),
    ("flood water rising rapidly, three children stuck on first floor", 3),
    ("boat capsized nearby, people struggling in water need rescue immediately", 3),
    ("husband having chest pain, house surrounded by water, no way out", 3),
    ("baby trapped, water up to neck level, please send boat urgently", 3),
    ("wall collapsed on neighbour, bleeding heavily, need emergency help", 3),
    ("stranded on rooftop since morning, water still rising, six people", 3),
    ("pregnant woman in labour, roads flooded, cannot reach hospital", 3),
    # high (2) -- serious, time sensitive but not immediate death risk
    ("water entered ground floor, elderly parents alone, need evacuation today", 2),
    ("diabetic patient out of insulin, area cut off by floodwater", 2),
    ("house partially flooded, we can move upstairs but scared to stay night", 2),
    ("no drinking water since two days, small children at home", 2),
    ("power line fell near house, afraid it will catch fire", 2),
    ("elderly father needs dialysis tomorrow, roads blocked by landslide", 2),
    ("family of five stuck, water waist high, need boat before night", 2),
    ("wheelchair user cannot evacuate alone, water rising slowly", 2),
    # medium (1) -- needs help soon but stable
    ("need dry food and clean water for family of four", 1),
    ("house compound flooded, main house still dry for now", 1),
    ("lost some belongings in water, need temporary shelter tonight", 1),
    ("mobile network down, want to inform relatives we are safe", 1),
    ("need mosquito nets and basic medicines for children", 1),
    ("small leak in roof during rain, house otherwise fine", 1),
    ("need transport to relief camp, roads slippery but passable", 1),
    ("cattle stranded in flooded field, need help moving them", 1),
    # low (0) -- informational / non-urgent requests
    ("want to volunteer for relief work this weekend", 0),
    ("asking if relief camp near town hall is still open", 0),
    ("checking status of road repair after last week rain", 0),
    ("want to donate clothes and food, where to drop off", 0),
    ("requesting update on flood forecast for next few days", 0),
    ("looking for information on nearest relief shelter location", 0),
    ("all safe at home, just confirming registration for records", 0),
    ("want to know how to sandbag my compound wall", 0),
]

_LABEL_NAMES = {0: "low", 1: "medium", 2: "high", 3: "critical"}
_LABEL_BASE_SCORE = {0: 15, 1: 40, 2: 65, 3: 90}

# Keywords that strongly imply life-threatening danger; used as a rule-based
# backstop so the classifier's mistakes on short/unusual text don't let a
# critical case slip through.
_CRITICAL_KEYWORDS = [
    "trapped", "drowning", "not breathing", "unconscious", "bleeding heavily",
    "collapsed", "buried", "capsized", "chest pain", "labour", "labor",
    "rising rapidly", "cannot swim", "roof and water", "can't swim", "swept away",
    "stuck on roof", "stuck on the roof", "on the roof", "on roof", "not breathing",
]
# Serious but not immediately life-threatening: never below "high"
_HIGH_KEYWORDS = [
    "insulin", "dialysis", "wheelchair", "cannot evacuate", "can't evacuate", "diabetic",
    "pregnant", "cut off", "oxygen", "ventilator", "infant", "newborn", "bedridden",
    "injured", "bleeding", "landslide", "water entering", "water entered",
]


@dataclass
class TriageResult:
    urgency_score: float
    urgency_label: str
    ml_confidence: float
    text_severity: float
    rule_score: float
    hotspot_boost: float


class UrgencyTriageEngine:
    def __init__(self):
        self.pipeline = Pipeline([
            ("tfidf", TfidfVectorizer(ngram_range=(1, 2), min_df=1, stop_words="english")),
            ("clf", LogisticRegression(max_iter=1000, class_weight="balanced")),
        ])
        self._fit()

    def _fit(self):
        texts = [t for t, _ in SEED_TEXTS]
        labels = [l for _, l in SEED_TEXTS]
        self.pipeline.fit(texts, labels)

    # -- text severity --------------------------------------------------
    def _text_severity(self, description: str) -> tuple[float, float]:
        """Returns (severity_0_100, confidence_0_1)."""
        proba = self.pipeline.predict_proba([description])[0]
        classes = self.pipeline.named_steps["clf"].classes_
        # expected score = weighted average of each class's base score
        expected = sum(proba[i] * _LABEL_BASE_SCORE[c] for i, c in enumerate(classes))
        confidence = float(np.max(proba))

        lowered = description.lower()
        if any(kw in lowered for kw in _CRITICAL_KEYWORDS):
            expected = max(expected, 88)
            confidence = max(confidence, 0.9)

        return float(expected), confidence

    # -- rule-based vulnerable-population factors ------------------------
    @staticmethod
    def _rule_score(has_elderly: bool, has_children: bool, has_medical_need: bool) -> float:
        score = 0.0
        if has_elderly:
            score += 30
        if has_children:
            score += 30
        if has_medical_need:
            score += 40
        return min(score, 100.0)

    # -- hotspot boost, filled in by classify_and_score() ----------------

    def classify(
        self,
        description: str,
        has_elderly: bool = False,
        has_children: bool = False,
        has_medical_need: bool = False,
        hotspot_boost: float = 0.0,
    ) -> TriageResult:
        text_severity, confidence = self._text_severity(description)
        rule_score = self._rule_score(has_elderly, has_children, has_medical_need)

        final = 0.55 * text_severity + 0.30 * rule_score + 0.15 * hotspot_boost
        # Baseline: vulnerability flags and hotspots should only ever RAISE a score.
        # Without this, a request with no flags is capped at 55% of its text
        # severity (a family with no food and water would rank "low").
        final = max(final, 0.8 * text_severity)
        # Safety floors. The weighted formula caps a text-only request near 48
        # (0.55 x 88), which would rank "trapped on the roof" below food requests.
        # Life-threatening phrases therefore never score below critical, and
        # serious ones never below high. The model can raise a score, never
        # push a known danger phrase down.
        lowered = description.lower()
        if any(kw in lowered for kw in _CRITICAL_KEYWORDS):
            final = max(final, 82.0)
        elif any(kw in lowered for kw in _HIGH_KEYWORDS):
            final = max(final, 58.0)
        final = float(max(0.0, min(100.0, final)))

        if final >= 80:
            label = "critical"
        elif final >= 55:
            label = "high"
        elif final >= 30:
            label = "medium"
        else:
            label = "low"

        return TriageResult(
            urgency_score=round(final, 1),
            urgency_label=label,
            ml_confidence=round(confidence, 2),
            text_severity=round(text_severity, 1),
            rule_score=round(rule_score, 1),
            hotspot_boost=round(hotspot_boost, 1),
        )


# ---------------------------------------------------------------------------
# 2. DBSCAN hotspot detection
# ---------------------------------------------------------------------------
def find_hotspots(points: list[dict], eps_km: float = 1.5, min_samples: int = 3) -> list[dict]:
    """
    points: list of {"id": int, "lat": float, "lng": float}
    Returns list of clusters: {"cluster_id": int, "size": int, "center_lat": .., "center_lng": .., "request_ids": [...]}

    Uses a simple equirectangular approximation (fine at city/district scale)
    to convert eps_km into degrees so we don't need a haversine metric.
    """
    valid = [p for p in points if p.get("lat") is not None and p.get("lng") is not None]
    if len(valid) < min_samples:
        return []

    coords = np.array([[p["lat"], p["lng"]] for p in valid])
    # ~111km per degree latitude; longitude scaled by cos(latitude)
    mean_lat_rad = np.radians(coords[:, 0].mean())
    lat_scale = 111.0
    lng_scale = 111.0 * max(np.cos(mean_lat_rad), 0.1)
    scaled = np.column_stack([coords[:, 0] * lat_scale, coords[:, 1] * lng_scale])

    db = DBSCAN(eps=eps_km, min_samples=min_samples).fit(scaled)
    labels = db.labels_

    clusters = []
    for cluster_id in sorted(set(labels)):
        if cluster_id == -1:
            continue  # noise, not a hotspot
        idxs = [i for i, l in enumerate(labels) if l == cluster_id]
        members = [valid[i] for i in idxs]
        clusters.append({
            "cluster_id": int(cluster_id),
            "size": len(members),
            "center_lat": float(np.mean([m["lat"] for m in members])),
            "center_lng": float(np.mean([m["lng"] for m in members])),
            "request_ids": [m["id"] for m in members],
        })
    clusters.sort(key=lambda c: -c["size"])
    return clusters


def hotspot_boost_for_point(lat: float | None, lng: float | None, hotspots: list[dict]) -> float:
    """0-100 boost: 100 if the point sits inside the largest hotspot, scaled down for smaller ones, 0 if in none."""
    if lat is None or lng is None or not hotspots:
        return 0.0
    max_size = max(h["size"] for h in hotspots) if hotspots else 1
    for h in hotspots:
        # cheap containment check: within ~1.5km of cluster center
        d_lat = (lat - h["center_lat"]) * 111.0
        d_lng = (lng - h["center_lng"]) * 111.0 * max(np.cos(np.radians(lat)), 0.1)
        dist_km = float(np.hypot(d_lat, d_lng))
        if dist_km <= 1.5:
            return 100.0 * (h["size"] / max_size)
    return 0.0


engine = UrgencyTriageEngine()
