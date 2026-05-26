#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/insta_reels_downloads}"
BRANCH="${BRANCH:-refactor-instagram-services}"
IMAGE="${IMAGE:-insta_reels_downloads:refactor}"
CONTAINER="${CONTAINER:-insta_reels_downloads}"
ENV_FILE="${ENV_FILE:-$APP_DIR/.env}"
PORT="${PORT:-9081}"
OLD_IMAGE=""

cd "$APP_DIR"

if sudo docker inspect "$CONTAINER" >/dev/null 2>&1; then
    OLD_IMAGE="$(sudo docker inspect -f '{{.Config.Image}}' "$CONTAINER")"
fi

sudo git fetch origin
sudo git checkout "$BRANCH"
sudo git pull origin "$BRANCH"

sudo cp "$ENV_FILE" "$ENV_FILE.backup.$(date +%Y%m%d%H%M%S)"

if sudo docker buildx version >/dev/null 2>&1; then
    sudo DOCKER_BUILDKIT=1 docker build --force-rm . -t "$IMAGE"
else
    echo "Docker buildx is not installed; using legacy docker builder."
    sudo DOCKER_BUILDKIT=0 docker build --force-rm . -t "$IMAGE"
fi

if ! grep -q '^APP_ENV=' "$ENV_FILE"; then
    echo 'APP_ENV=production' | sudo tee -a "$ENV_FILE" >/dev/null
fi

if ! grep -q '^APP_ENCRYPTION_KEY=' "$ENV_FILE"; then
    echo "APP_ENCRYPTION_KEY is missing in $ENV_FILE. Add the shared key before deploy."
    exit 1
fi

sudo docker rm -f insta_reels_downloads_migrate 2>/dev/null || true
sudo docker run --rm \
    --name insta_reels_downloads_migrate \
    --entrypoint python3 \
    --env-file "$ENV_FILE" \
    -e FORCE_SECRET_SEED=1 \
    "$IMAGE" \
    /app/scripts/migrate-secrets.py

sudo sed -i \
    '/^APIFY_TOKEN=/d;/^SMTP_USER=/d;/^SMTP_PASS=/d;/^ALERT_EMAIL_TO=/d;/^GEMINI_API_KEY=/d;/^GROQ_API_KEY=/d;/^RAPIDAPI_KEY=/d' \
    "$ENV_FILE"

restart_old_container() {
    if [ -n "$OLD_IMAGE" ]; then
        sudo docker rm -f "$CONTAINER" 2>/dev/null || true
        sudo docker run -d \
            --restart unless-stopped \
            --name "$CONTAINER" \
            --env-file "$ENV_FILE" \
            -p "$PORT:8000" \
            "$OLD_IMAGE" >/dev/null
    fi
}

sudo docker stop "$CONTAINER" 2>/dev/null || true
sudo docker rm "$CONTAINER" 2>/dev/null || true

if ! sudo docker run -d \
    --restart unless-stopped \
    --name "$CONTAINER" \
    --env-file "$ENV_FILE" \
    -p "$PORT:8000" \
    "$IMAGE" >/dev/null; then
    restart_old_container
    exit 1
fi

sleep 10

if [ "$(sudo docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || echo false)" != "true" ]; then
    sudo docker logs --tail 100 "$CONTAINER" || true
    restart_old_container
    exit 1
fi

sudo docker rm -f insta_reels_downloads_migrate 2>/dev/null || true
sudo docker image prune -f

sudo docker logs --tail 100 "$CONTAINER"
