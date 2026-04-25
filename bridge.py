"""
Kinetic Transport Tracker — Python Bridge
==========================================
Listens to Firebase Realtime DB for vibration spikes (Z-axis > 1.5g),
builds a structured payload, calls Gemini 2.0 Flash for classification,
and writes the result back to Firebase as a road-health alert.

SDG 11 / UN Decade of Sustainable Transport (2026-2035)
Google Solution Challenge 2026
"""

import os
import json
import time
import logging
import statistics
import threading
from datetime import datetime, timezone
from typing import Optional

import firebase_admin
from firebase_admin import credentials, db
import google.generativeai as genai

# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("ktt-bridge")

# --- Thresholds ---
SPIKE_THRESHOLD_G   = 1.5   # Z-axis acceleration in g-force
SNIPPET_WINDOW_S    = 2.0   # seconds of data to send to Gemini
ALERT_COOLDOWN_S    = 30    # minimum seconds between alerts for the same GPS cell
GRID_RESOLUTION_DEG = 0.001 # ~100m grid cell for deduplication

# --- Firebase paths ---
SPIKES_PATH = "/spikes"
ALERTS_PATH = "/alerts"

# --- Gemini model ---
GEMINI_MODEL = "models/gemini-2.0-flash"


# ─────────────────────────────────────────────
# Initialisation
# ─────────────────────────────────────────────

import os
import json
from firebase_admin import credentials

def init_firebase():
    database_url = os.environ.get("FIREBASE_DATABASE_URL")

    service_account_env = os.environ.get("FIREBASE_SERVICE_ACCOUNT")

    if service_account_env and service_account_env.strip().startswith("{"):
        # ✅ JSON string from Render
        service_account_info = json.loads(service_account_env)
        cred = credentials.Certificate(service_account_info)
    else:
        # ✅ Local file fallback
        cred = credentials.Certificate("serviceAccount.json")

    firebase_admin.initialize_app(cred, {
        "databaseURL": database_url
    })

    print("Firebase initialized")


def init_gemini() -> genai.GenerativeModel:
    """Initialise the Gemini client and return a model handle."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise EnvironmentError("Set GEMINI_API_KEY environment variable")
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(GEMINI_MODEL)
    log.info("Gemini model ready: %s", GEMINI_MODEL)
    return model


# ─────────────────────────────────────────────
# Deduplication — prevent alert storms
# ─────────────────────────────────────────────

class AlertDeduplicator:
    """Suppress repeated alerts within ALERT_COOLDOWN_S for the same GPS grid cell."""

    def __init__(self, cooldown_s: float = ALERT_COOLDOWN_S,
                 resolution: float = GRID_RESOLUTION_DEG) -> None:
        self._seen: dict[tuple, float] = {}
        self._cooldown   = cooldown_s
        self._resolution = resolution

    def _cell(self, lat: float, lng: float) -> tuple:
        return (
            round(lat / self._resolution) * self._resolution,
            round(lng / self._resolution) * self._resolution,
        )

    def should_alert(self, lat: float, lng: float) -> bool:
        cell = self._cell(lat, lng)
        now  = time.monotonic()
        if now - self._seen.get(cell, 0.0) >= self._cooldown:
            self._seen[cell] = now
            return True
        return False


# ─────────────────────────────────────────────
# Payload builder
# ─────────────────────────────────────────────

def extract_snippet(
    readings: list[dict],
    spike_ts: float,
    window_s: float = SNIPPET_WINDOW_S,
) -> list[dict]:
    """Return the 2-second window of readings centred on the spike timestamp."""
    half = window_s / 2
    return [r for r in readings if abs(r.get("ts", 0) - spike_ts) <= half]


def compute_statistics(snippet: list[dict]) -> dict:
    """Summarise a vibration snippet into scalar features for Gemini."""
    if not snippet:
        return {}

    az_vals = [r.get("az", 0.0) for r in snippet]
    ax_vals = [r.get("ax", 0.0) for r in snippet]
    ay_vals = [r.get("ay", 0.0) for r in snippet]
    gz_vals = [r.get("gz", 0.0) for r in snippet]

    def safe_stdev(vals):
        return statistics.stdev(vals) if len(vals) > 1 else 0.0

    return {
        "az_peak_g":    max(az_vals),
        "az_min_g":     min(az_vals),
        "az_mean_g":    statistics.mean(az_vals),
        "az_stdev":     safe_stdev(az_vals),
        "ax_peak_g":    max(ax_vals),
        "ay_peak_g":    max(ay_vals),
        "gz_peak_dps":  max(gz_vals),
        "sample_count": len(snippet),
    }


def build_gemini_prompt(
    stats: dict,
    lat: float,
    lng: float,
    speed_kmh: Optional[float],
    snippet: list[dict],
) -> str:
    """Construct the structured prompt sent to Gemini."""
    speed_str   = f"{speed_kmh:.1f} km/h" if speed_kmh is not None else "unknown"
    raw_preview = snippet[:40]

    return f"""
You are an AI road-health analyst embedded in the Kinetic Transport Tracker system.
A vibration spike was detected by an accelerometer mounted on a public transport vehicle.

## Sensor summary
- Location: latitude {lat:.6f}, longitude {lng:.6f}
- Vehicle speed: {speed_str}
- Z-axis peak: {stats.get('az_peak_g', 0):.3f} g  (threshold: {SPIKE_THRESHOLD_G} g)
- Z-axis min: {stats.get('az_min_g', 0):.3f} g
- Z-axis mean: {stats.get('az_mean_g', 0):.3f} g
- Z-axis std-dev: {stats.get('az_stdev', 0):.3f} g
- X-axis peak: {stats.get('ax_peak_g', 0):.3f} g
- Y-axis peak: {stats.get('ay_peak_g', 0):.3f} g
- Yaw rate peak: {stats.get('gz_peak_dps', 0):.1f} °/s
- Sample count (2 s window): {stats.get('sample_count', 0)}

## Raw waveform (az values in g, chronological)
{json.dumps([round(r.get('az', 0), 3) for r in raw_preview])}

## Classification task
Classify this event as exactly ONE of:
  - "pothole"          — sharp negative spike, short duration, asymmetric waveform
  - "speed_bump"       — gradual rise then fall, symmetric, moderate amplitude
  - "mechanical_fault" — repetitive oscillations not correlated with road surface

Respond ONLY with a valid JSON object. No markdown, no explanation outside the JSON.

{{
  "classification": "<pothole|speed_bump|mechanical_fault>",
  "confidence": <0.0-1.0>,
  "severity": "<low|medium|high|critical>",
  "reasoning": "<one concise sentence explaining the key signal features>",
  "recommended_action": "<one actionable sentence for city maintenance teams>"
}}
""".strip()


# ─────────────────────────────────────────────
# Gemini caller
# ─────────────────────────────────────────────

def classify_with_gemini(
    model: genai.GenerativeModel,
    prompt: str,
    max_retries: int = 2,
) -> Optional[dict]:
    """Send the prompt to Gemini and parse the JSON response."""
    generation_config = genai.types.GenerationConfig(
        temperature=0.2,
        max_output_tokens=512,
    )

    for attempt in range(1, max_retries + 1):
        try:
            response = model.generate_content(prompt, generation_config=generation_config)
            raw_text = response.text.strip()

            # Strip accidental markdown fences
            if raw_text.startswith("```"):
                raw_text = "\n".join(
                    line for line in raw_text.splitlines()
                    if not line.startswith("```")
                )

            result = json.loads(raw_text)

            required = {"classification", "confidence", "severity", "reasoning", "recommended_action"}
            if not required.issubset(result.keys()):
                raise ValueError(f"Missing keys in Gemini response: {result}")

            log.info(
                "Gemini classified: %s (confidence=%.2f, severity=%s)",
                result["classification"], result["confidence"], result["severity"],
            )
            return result

        except (json.JSONDecodeError, ValueError) as exc:
            log.warning("Attempt %d — parse error: %s", attempt, exc)
        except Exception as exc:
            log.warning("Attempt %d — API error: %s", attempt, exc)

        if attempt < max_retries:
            time.sleep(12)

    log.error("Gemini classification failed after %d attempts.", max_retries)
    return None


# ─────────────────────────────────────────────
# Fallback rule-based classifier
# ─────────────────────────────────────────────

def _fallback_classify(stats: dict) -> dict:
    """Simple threshold-based classifier used when Gemini is unavailable."""
    peak = stats.get("az_peak_g", 0)
    if peak > 2.5:
        return {
            "classification":     "pothole",
            "confidence":         0.8,
            "severity":           "high",
            "reasoning":          "High Z-axis spike typical of pothole impact",
            "recommended_action": "Inspect road surface for damage",
        }
    elif peak > 1.8:
        return {
            "classification":     "speed_bump",
            "confidence":         0.7,
            "severity":           "medium",
            "reasoning":          "Moderate symmetric spike suggests speed bump",
            "recommended_action": "Verify signage and markings",
        }
    else:
        return {
            "classification":     "mechanical_fault",
            "confidence":         0.6,
            "severity":           "low",
            "reasoning":          "Low irregular vibration not road-related",
            "recommended_action": "Check vehicle suspension",
        }


# ─────────────────────────────────────────────
# Firebase writer
# ─────────────────────────────────────────────

def _simple_geohash(lat: float, lng: float, precision: int = 6) -> str:
    """Minimal geohash — ~1.2 km precision at 6 chars. Swap for python-geohash in production."""
    BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"
    lat_range, lng_range = [-90.0, 90.0], [-180.0, 180.0]
    result, bits, bit_count = [], 0, 0
    is_lng = True

    while len(result) < precision:
        if is_lng:
            mid = (lng_range[0] + lng_range[1]) / 2
            if lng >= mid:
                bits = (bits << 1) | 1
                lng_range[0] = mid
            else:
                bits <<= 1
                lng_range[1] = mid
        else:
            mid = (lat_range[0] + lat_range[1]) / 2
            if lat >= mid:
                bits = (bits << 1) | 1
                lat_range[0] = mid
            else:
                bits <<= 1
                lat_range[1] = mid
        is_lng = not is_lng
        bit_count += 1
        if bit_count == 5:
            result.append(BASE32[bits])
            bits, bit_count = 0, 0

    return "".join(result)


def write_alert(
    spike_id: str,
    lat: float,
    lng: float,
    classification: dict,
    stats: dict,
) -> None:
    """
    Write the processed alert to /alerts/{spike_id}.
    Alert ID is always identical to spike_id — no random or timestamp suffixes.
    updated_at ensures Firebase always fires a change event on the frontend.
    """
    alert = {
        "spike_id":           spike_id,
        "timestamp_utc":      datetime.now(timezone.utc).isoformat(),
        "location": {
            "lat":     lat,
            "lng":     lng,
            "geohash": _simple_geohash(lat, lng),
        },
        "classification":     classification["classification"],
        "confidence":         classification["confidence"],
        "severity":           classification["severity"],
        "reasoning":          classification["reasoning"],
        "recommended_action": classification["recommended_action"],
        "sensor_stats":       stats,
        "processed_by":       "ktt-bridge-v1",
        "updated_at":         time.time(),
    }

    db.reference(f"{ALERTS_PATH}/{spike_id}").set(alert)
    log.info("Alert written → %s/%s", ALERTS_PATH, spike_id)


# ─────────────────────────────────────────────
# Firebase spike listener
# ─────────────────────────────────────────────

class SpikeListener:
    """
    Attaches a real-time Firebase listener to /spikes.

    Design decisions:
    - NO polling loop. The listener alone handles all events. Polling was the
      primary cause of duplicate processing.
    - Each spike is processed exactly once, guaranteed by two layers:
        1. In-session set (_seen): fast in-memory check, avoids redundant
           Firebase reads for spikes already handled this run.
        2. Firebase existence check: authoritative gate that survives restarts.
           If /alerts/{spike_id} already exists, we skip processing entirely.
    - The listener fires on a background thread, so _seen is guarded by a lock.
    """

    def __init__(self, model: genai.GenerativeModel) -> None:
        self._model = model
        self._dedup = AlertDeduplicator()
        self._seen: set[str] = set()
        self._lock  = threading.Lock()

    def start(self) -> None:
        log.info("Listening on %s — spike threshold: %.1f g", SPIKES_PATH, SPIKE_THRESHOLD_G)
        db.reference(SPIKES_PATH).listen(self._on_spike_event)
        # Keep the main thread alive; the listener runs on its own thread.
        while True:
            time.sleep(60)

    def _on_spike_event(self, event) -> None:
        """
        Called by Firebase SDK on every change under /spikes.

        Two event types:
          - Initial attach: path="/" with the full /spikes dict. We skip this
            entirely — historical spikes already have alerts, and new ones will
            arrive as individual child events.
          - Child added/changed: path="/<spike_id>" with the spike dict.
            This is the only case we process.
        """
        if event.data is None:
            return

        spike_id = event.path.strip("/")

        # Skip root dump (initial attach) and any nested field updates
        if not spike_id or "/" in spike_id:
            log.debug("Skipping root/nested event at path: %s", event.path)
            return

        if not isinstance(event.data, dict):
            return

        # Layer 1: in-session dedup (fast, no network call)
        with self._lock:
            if spike_id in self._seen:
                log.debug("Spike %s already seen this session — skipping.", spike_id)
                return
            self._seen.add(spike_id)

        log.info("Spike event received: %s", spike_id)
        self._handle_spike(spike_id, event.data)

    def _handle_spike(self, spike_id: str, spike: dict) -> None:
        """Process a single spike record end-to-end."""

        # ── 0. Idempotency — skip if alert already exists in Firebase ──────
        # Survives bridge restarts. If this spike was processed in a previous
        # run, /alerts/{spike_id} will already exist and we stop here.
        try:
            existing = db.reference(f"{ALERTS_PATH}/{spike_id}").get()
            if existing is not None:
                log.debug("Alert already exists for spike %s — skipping.", spike_id)
                return
        except Exception as exc:
            log.warning("Could not check existing alert for %s: %s — proceeding.", spike_id, exc)

        # ── 1. Extract fields ─────────────────────────────────────────────
        az_peak = (
            spike.get("peak_z_g") or
            spike.get("az_peak") or
            spike.get("az") or
            0.0
        )
        lat     = spike.get("lat") or spike.get("latitude")
        lng     = spike.get("lng") or spike.get("longitude")

        if lat is None or lng is None:
            log.warning("Spike %s missing GPS — skipping.", spike_id)
            return

        # ── 2. Threshold check ────────────────────────────────────────────
        if abs(az_peak) < SPIKE_THRESHOLD_G:
            log.debug("Spike %s below threshold (az=%.2fg) — skipping.", spike_id, az_peak)
            return

        # ── 3. GPS-cell deduplication ─────────────────────────────────────
        if not self._dedup.should_alert(lat, lng):
            log.debug("Spike %s suppressed (cooldown active for this GPS cell).", spike_id)
            return

        log.info("Processing spike %s — az=%.2fg @ (%.5f, %.5f)", spike_id, az_peak, lat, lng)

        # ── 4. Build vibration snippet ────────────────────────────────────
        readings: list[dict] = spike.get("readings", [])
        spike_ts: float      = spike.get("ts", time.time())
        snippet              = extract_snippet(readings, spike_ts)

        if not snippet:
            # Synthesise a single-sample snippet from the peak fields
            snippet = [{
                "ts": spike_ts,
                "az": az_peak,
                "ax": spike.get("ax", 0.0),
                "ay": spike.get("ay", 0.0),
                "gz": spike.get("gz", 0.0),
            }]

        # ── 5. Compute statistics ─────────────────────────────────────────
        stats = compute_statistics(snippet)
        speed = spike.get("speed_kmh")

        # ── 6. Classify ───────────────────────────────────────────────────
        prompt = build_gemini_prompt(stats, lat, lng, speed, snippet)
        result = classify_with_gemini(self._model, prompt)

        if result is None:
            log.warning("Gemini unavailable — using fallback classifier.")
            result = _fallback_classify(stats)

        # ── 7. Write alert to Firebase ────────────────────────────────────
        write_alert(spike_id, lat, lng, result, stats)


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

def main() -> None:
    log.info("=== Kinetic Transport Tracker — Python Bridge starting ===")
    init_firebase()
    model    = init_gemini()
    listener = SpikeListener(model)
    listener.start()


if __name__ == "__main__":
    main()

from flask import Flask
import threading

app = Flask(__name__)

@app.route("/")
def home():
    return "Backend running"

def run_worker():
    main()  # your existing function

if __name__ == "__main__":
    threading.Thread(target=run_worker).start()
    app.run(host="0.0.0.0", port=10000)
