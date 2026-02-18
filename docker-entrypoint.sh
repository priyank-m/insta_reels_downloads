#!/usr/bin/env bash
set -euo pipefail

echo "===== Preparing Tor directories ====="

mkdir -p /var/lib/tor
chown -R debian-tor:debian-tor /var/lib/tor
chmod 700 /var/lib/tor

echo "===== Writing torrc ====="

cat > /etc/tor/torrc <<'EOF'
SocksPort 127.0.0.1:9050 IsolateSOCKSAuth
ControlPort 127.0.0.1:9051
CookieAuthentication 1
DataDirectory /var/lib/tor

MaxCircuitDirtiness 10
NewCircuitPeriod 5
CircuitBuildTimeout 15
LearnCircuitBuildTimeout 0

ClientUseIPv6 0
SafeSocks 1

Log notice stdout
EOF

echo "===== Starting Tor ====="

su -s /bin/sh debian-tor -c "tor" &
TOR_PID=$!

echo "Waiting for Tor network..."

# wait until Tor exit works
for i in {1..180}; do
    if curl -s --socks5-hostname 127.0.0.1:9050 https://check.torproject.org/api/ip | grep -q '"IsTor":true'; then
        echo "Tor fully usable"
        break
    fi
    sleep 2
done

# fail if not ready
curl -s --socks5-hostname 127.0.0.1:9050 https://check.torproject.org/api/ip | grep -q '"IsTor":true' || {
    echo "Tor failed to bootstrap"
    exit 1
}

echo "Warming clean Tor circuit..."

COOKIE_FILE="/var/lib/tor/control_auth_cookie"

# wait until cookie exists
for i in {1..60}; do
    [ -f "$COOKIE_FILE" ] && break
    sleep 1
done

# convert cookie to hex using python (always available)
COOKIE=$(python3 - <<PY
with open("$COOKIE_FILE","rb") as f:
    print(f.read().hex().upper())
PY
)

printf "AUTHENTICATE %s\r\nSIGNAL NEWNYM\r\nQUIT\r\n" "$COOKIE" | nc 127.0.0.1 9051 || true

sleep 20

echo "Tor Exit IP:"
curl --socks5-hostname 127.0.0.1:9050 https://api.ipify.org || true
echo

echo "===== Starting API ====="

python3 -m api &
API_PID=$!

python3 /app/api/scheduler.py &
SCHED_PID=$!

wait -n
exit $?
