# ── Stage 1: dependency builder ───────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# Install build tools for any C-extension deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip \
 && pip install --prefix=/install --no-cache-dir -r requirements.txt


# ── Stage 2: lean runtime image ───────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Non-root user for Cloud Run security best practices
RUN groupadd -r ktt && useradd -r -g ktt ktt

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY bridge.py .

# Cloud Run injects PORT; our bridge doesn't serve HTTP but we expose it
# so Cloud Run health-checks can reach a future /healthz endpoint.
ENV PORT=8080

# Firebase SDK uses this to find the service account JSON
# (value is overridden at deploy time via --set-secrets)
ENV FIREBASE_SERVICE_ACCOUNT=/secrets/serviceAccount.json

USER ktt

CMD ["python", "-u", "bridge.py"]
