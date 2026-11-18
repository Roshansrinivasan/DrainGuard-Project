"""
AI image-analysis module (integration-ready).

Right now NO model is connected, so analyze_image() returns a clearly-labelled SIMULATED result.

To plug in a real model, write a function that takes an image path and returns:
    {"categories": [{"label": "Plastic", "confidence": 0.91}, ...],
     "severity": "Low" | "Medium" | "High",
     "confidence": 0.88}            # overall confidence, or None if the model has none
and register it once at start-up:   ai_analysis.register_model(my_function)

Example (Ultralytics YOLO, not installed by default):
    from ultralytics import YOLO
    model = YOLO("models/drain_waste.pt")
    def my_function(path):
        res = model(path)[0]
        cats = [{"label": model.names[int(b.cls)], "confidence": float(b.conf)} for b in res.boxes]
        area = sum(float(b.xywhn[0][2] * b.xywhn[0][3]) for b in res.boxes)   # fraction of image covered
        sev = "High" if area > 0.4 else "Medium" if area > 0.15 else "Low"
        return {"categories": cats, "severity": sev, "confidence": max([c["confidence"] for c in cats], default=None)}
"""
import hashlib
import random

LABELS = ["Plastic", "Leaves", "Mud and soil", "Solid waste", "Other"]
ACTIONS = {
    "Low": "Monitor the drain; schedule routine cleaning.",
    "Medium": "Verify on site and schedule cleaning within 24 hours.",
    "High": "Treat as urgent: verify immediately and dispatch the cleaning unit or a crew.",
}
NOTICE = "SIMULATED RESULT - no computer-vision model is connected. These values are placeholders, not a real detection."
_model = None


def register_model(fn):
    global _model
    _model = fn


def analyze_image(path, declared_type=None):
    if _model is not None:
        try:
            return _normalise(_model(path))
        except Exception as exc:   # never let a model crash break report submission
            out = _simulated(path, declared_type)
            out["notice"] = f"Model error ({exc}); showing a SIMULATED result instead."
            return out
    return _simulated(path, declared_type)


def _normalise(raw):
    sev = raw.get("severity") if raw.get("severity") in ACTIONS else "Medium"
    cats = [{"label": str(c["label"]), "confidence": round(float(c["confidence"]), 3) if c.get("confidence") is not None else None}
            for c in raw.get("categories", [])]
    conf = raw.get("confidence")
    return {"mode": "model", "simulated": False, "categories": cats, "severity": sev,
            "confidence": round(float(conf), 3) if conf is not None else None,
            "suggested_action": ACTIONS[sev], "notice": None}


def _simulated(path, declared_type):
    with open(path, "rb") as f:
        seed = int(hashlib.sha256(f.read()).hexdigest()[:12], 16)   # same image -> same placeholder result
    rng = random.Random(seed)
    labels = [declared_type] if declared_type in LABELS else []
    for lab in rng.sample(LABELS, 2):
        if lab not in labels and len(labels) < 2:
            labels.append(lab)
    cats = [{"label": lab, "confidence": round(rng.uniform(0.55, 0.92), 2)} for lab in labels]
    sev = rng.choice(["Low", "Medium", "High"])
    return {"mode": "simulated", "simulated": True, "categories": cats, "severity": sev,
            "confidence": round(rng.uniform(0.6, 0.9), 2), "suggested_action": ACTIONS[sev], "notice": NOTICE}
