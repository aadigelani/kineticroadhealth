#!/usr/bin/env bash
# deploy.sh — One-shot setup + deploy for Kinetic Transport Tracker bridge
# Run once from your development machine after `gcloud auth login`.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── EDIT THESE ────────────────────────────────────────────────────────────────
PROJECT_ID="road-health-monitor-b6307"
REGION="asia-south1"               # closest GCP region to Chennai
SERVICE_NAME="ktt-bridge"
FIREBASE_DATABASE_URL="https://road-health-monitor-b6307-default-rtdb.asia-southeast1.firebasedatabase.app"
SERVICE_ACCOUNT_JSON="AIzaSyB6vPfn4_bNQ2_j97HBRIL2JpFs9PJl9L0"   # your Firebase service account key
# ─────────────────────────────────────────────────────────────────────────────

IMAGE="gcr.io/${road-health-monitor-b6307}/${SERVICE_NAME}"
SA_EMAIL="${SERVICE_NAME}-sa@${road-health-monitor-b6307}.iam.gserviceaccount.com"

echo "▶ Setting active project..."
gcloud config set project "${road-health-monitor-b6307}"

echo "▶ Enabling required APIs..."
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  secretmanager.googleapis.com \
  containerregistry.google  s.com \
  --quiet

# ── Service account ───────────────────────────────────────────────────────────
echo "▶ Creating service account ${SA_EMAIL}..."
gcloud iam service-accounts create "${SERVICE_NAME}-sa" \
  --display-name="KTT Bridge Service Account" \
  --quiet 2>/dev/null || echo "  (already exists, skipping)"

# Minimum IAM roles needed
for ROLE in \
  roles/firebase.admin \
  roles/secretmanager.secretAccessor \
  roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding "${road-health-monitor-b6307}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="${ROLE}" \
    --quiet
done

# ── Secrets ───────────────────────────────────────────────────────────────────
echo "▶ Storing Firebase service account in Secret Manager..."
gcloud secrets create firebase-service-account \
  --replication-policy=automatic \
  --quiet 2>/dev/null || echo "  (secret exists, adding new version)"
gcloud secrets versions add firebase-service-account \
  --data-file="${SERVICE_ACCOUNT_JSON}"

echo "▶ Storing Gemini API key..."
echo "  Enter your Gemini API key (input hidden):"
read -rs GEMINI_API_KEY
printf '%s' "${YOUR_GEMINI_API_KEY}" | \
  gcloud secrets create gemini-api-key --replication-policy=automatic \
    --data-file=- --quiet 2>/dev/null || \
  printf '%s' "${YOUR_GEMINI_API_KEY}" | \
  gcloud secrets versions add gemini-api-key --data-file=-

# ── Build & push Docker image ─────────────────────────────────────────────────
echo "▶ Building Docker image with Cloud Build..."
gcloud builds submit \
  --tag "${IMAGE}:latest" \
  --quiet

# ── Deploy to Cloud Run ───────────────────────────────────────────────────────
echo "▶ Deploying to Cloud Run (${REGION})..."
gcloud run deploy "${SERVICE_NAME}" \
  --image="${IMAGE}:latest" \
  --region="${REGION}" \
  --platform=managed \
  --no-allow-unauthenticated \
  --min-instances=1 \
  --max-instances=3 \
  --memory=512Mi \
  --cpu=1 \
  --timeout=3600 \
  --set-env-vars="FIREBASE_DATABASE_URL=${FIREBASE_DATABASE_URL}" \
  --set-secrets="/secrets/serviceAccount.json=firebase-service-account:latest,GEMINI_API_KEY=YOUR_GEMINI_API_KEY:latest" \
  --service-account="${SA_EMAIL}" \
  --quiet

echo ""
echo "✅ Deployment complete!"
echo "   Service URL (internal only):"
gcloud run services describe "${SERVICE_NAME}" \
  --region="${REGION}" \
  --format="value(status.url)"

echo ""
echo "📋 Stream live logs:"
echo "   gcloud beta run services logs tail ${SERVICE_NAME} --region=${REGION}"

# ── Cloud Build trigger (CI/CD on push to main) ───────────────────────────────
echo ""
echo "▶ Creating Cloud Build trigger on 'main' branch..."
gcloud builds triggers create github \
  --name="${SERVICE_NAME}-main" \
  --repo-name="kinetic-transport-tracker" \
  --repo-owner="YOUR_GITHUB_USERNAME" \
  --branch-pattern="^main$" \
  --build-config="cloudbuild.yaml" \
  --substitutions="_REGION=${REGION},_SERVICE_NAME=${SERVICE_NAME}" \
  --quiet 2>/dev/null || echo "  (trigger may already exist — check Cloud Console)"

echo ""
echo "🎉 All done. The bridge is live and listening to Firebase."
