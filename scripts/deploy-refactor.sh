#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/insta_reels_downloads}"
BRANCH="${BRANCH:-refactor-instagram-services}"
IMAGE="${IMAGE:-insta_reels_downloads:refactor}"
CONTAINER="${CONTAINER:-insta_reels_downloads}"
ENV_FILE="${ENV_FILE:-$APP_DIR/.env}"
PORT="${PORT:-9081}"

cd "$APP_DIR"

sudo git fetch origin
sudo git checkout "$BRANCH"
sudo git pull origin "$BRANCH"

sudo cp "$ENV_FILE" "$ENV_FILE.backup.$(date +%Y%m%d%H%M%S)"

sudo docker build . -t "$IMAGE"

if ! grep -q '^APP_ENV=' "$ENV_FILE"; then
    echo 'APP_ENV=production' | sudo tee -a "$ENV_FILE" >/dev/null
fi

if ! grep -q '^APP_ENCRYPTION_KEY=' "$ENV_FILE"; then
    sudo docker run --rm --entrypoint python3 "$IMAGE" \
        -c "from cryptography.fernet import Fernet; print('APP_ENCRYPTION_KEY=' + Fernet.generate_key().decode())" \
        | sudo tee -a "$ENV_FILE" >/dev/null
fi

sudo docker run --rm \
    --env-file "$ENV_FILE" \
    "$IMAGE" \
    python3 /app/scripts/migrate-secrets.py

sudo sed -i \
    '/^APIFY_TOKEN=/d;/^SMTP_USER=/d;/^SMTP_PASS=/d;/^ALERT_EMAIL_TO=/d;/^GEMINI_API_KEY=/d;/^GROQ_API_KEY=/d;/^RAPIDAPI_KEY=/d' \
    "$ENV_FILE"

sudo docker stop "$CONTAINER" 2>/dev/null || true
sudo docker rm "$CONTAINER" 2>/dev/null || true

sudo docker run -d \
    --restart unless-stopped \
    --name "$CONTAINER" \
    --env-file "$ENV_FILE" \
    -p "$PORT:8000" \
    "$IMAGE"

sudo docker logs --tail 100 "$CONTAINER"
