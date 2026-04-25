# Kinetic Transport Tracker — Cloud Deployment Guide

## Prerequisites

- `gcloud` CLI installed and authenticated (`gcloud auth login`)
- Docker installed locally (only needed for local testing)
- A GCP project with billing enabled
- Firebase Realtime Database created in the same project
- A Gemini API key from [Google AI Studio](https://aistudio.google.com)

---

## Files in this package

| File | Purpose |
|---|---|
| `bridge.py` | The Python bridge application |
| `requirements.txt` | Python dependencies |
| `Dockerfile` | Two-stage build: slim runtime image |
| `.dockerignore` | Keeps secrets out of the image |
| `cloudbuild.yaml` | CI/CD: build → push → deploy on every `git push main` |
| `deploy.sh` | One-shot first-time setup script |
| `firebase.rules.json` | Realtime Database security rules |

---

## Step 1 — Configure `deploy.sh`

Open `deploy.sh` and set the three variables at the top:

```bash
PROJECT_ID="your-gcp-project-id"
REGION="asia-south1"          # or us-central1, europe-west1, etc.
SERVICE_ACCOUNT_JSON="./serviceAccount.json"
```

Download your Firebase service account key from:
**Firebase Console → Project Settings → Service Accounts → Generate new private key**

Save it as `serviceAccount.json` in the same directory. **Never commit this file.**

---

## Step 2 — Run the deploy script

```bash
chmod +x deploy.sh
./deploy.sh
```

This will:
1. Enable all required GCP APIs
2. Create a least-privilege service account
3. Store secrets in Secret Manager (never in environment variables or the image)
4. Build and push the Docker image via Cloud Build
5. Deploy to Cloud Run with `--min-instances=1` (keeps the Firebase listener alive)
6. Create a Cloud Build trigger for CI/CD on future `git push main`

---

## Step 3 — Apply Firebase security rules

In the Firebase Console → Realtime Database → Rules, paste the contents of
`firebase.rules.json`. This ensures:
- Only authenticated ESP32 devices can write to `/spikes`
- Only the bridge service account can write to `/alerts`
- The Flutter app can read `/alerts` when signed in

---

## Step 4 — Verify the deployment

Stream live logs to confirm the bridge is listening:

```bash
gcloud beta run services logs tail ktt-bridge --region=asia-south1
```

You should see:
```
[INFO] Firebase initialised — database: https://your-project.firebaseio.com
[INFO] Gemini model ready: gemini-1.5-flash
[INFO] Listening on /spikes — spike threshold: 1.5 g
```

---

## Cost estimate (at Google Solution Challenge scale)

| Service | Usage | Est. cost/month |
|---|---|---|
| Cloud Run | 1 min instance, ~720 hrs | ~$7–12 |
| Cloud Build | ~30 builds/month | Free tier |
| Secret Manager | 2 secrets, ~100 accesses | <$0.01 |
| Gemini 1.5 Flash | ~500 classifications/day | ~$1–3 |
| Firebase Realtime DB | Spark plan | Free |

**Total: ~$8–15/month** — well within Google's Solution Challenge credits.

---

## Local testing (without deploying)

```bash
# 1. Copy your service account and set env vars
export FIREBASE_DATABASE_URL="https://road-health-monitor-b6307-default-rtdb.asia-southeast1.firebasedatabase.app/"
export FIREBASE_SERVICE_ACCOUNT="./road-health-monitor-b6307-firebase-adminsdk-fbsvc-dd1666b432.json"
export GEMINI_API_KEY="YOUR_GEMINI_API_KEY"

# 2. Install deps
pip install -r requirements.txt

# 3. Run
python bridge.py

# 4. Simulate a spike by writing to Firebase manually:
# Firebase Console → Realtime Database → + Add node
# /spikes/test001 = { ts: 1720000000, lat: 13.08, lng: 80.27, az_peak: 2.1 }
```

---

## Troubleshooting

**Bridge exits immediately on Cloud Run**
Cloud Run terminates containers that don't bind to `$PORT`. The bridge's
`while True: time.sleep(60)` loop keeps the process alive. If it still exits,
check Cloud Run logs for Python import errors — usually a missing dependency.

**"PERMISSION_DENIED" from Firebase**
The Cloud Run service account needs `roles/firebase.admin`. Re-run `deploy.sh`
or grant it manually in IAM.

**Gemini returns non-JSON**
Temperature is set to 0.2, which is stable but not guaranteed. The bridge
retries 3 times. If failures persist, check your Gemini API quota in
[Google AI Studio](https://aistudio.google.com).
