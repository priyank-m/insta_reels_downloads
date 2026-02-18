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

# Build fresh circuits frequently
MaxCircuitDirtiness 10
NewCircuitPeriod 5
CircuitBuildTimeout 15
LearnCircuitBuildTimeout 0

# Stability
ClientUseIPv6 0
SafeSocks 1

Log notice stdout
EOF

echo "===== Starting Tor ====="

# run tor in background as correct user
su -s /bin/sh debian-tor -c "tor" &
TOR_PID=$!

# ensure tor stops container if it crashes
( wait $TOR_PID && echo "Tor exited!" && exit 1 ) &

echo "Waiting for Tor network availability..."

# wait until Tor can reach the network and provide exit
for i in {1..120}; do
    if curl -s --socks5-hostname 127.0.0.1:9050 https://check.torproject.org/api/ip | grep -q '"IsTor":true'; then
        echo "Tor fully usable"
        break
    fi
    sleep 2
done

# fail if tor never bootstrapped
if ! curl -s --socks5-hostname 127.0.0.1:9050 https://check.torproject.org/api/ip | grep -q '"IsTor":true'; then
    echo "Tor failed to bootstrap"
    exit 1
fi

echo "Warming clean Tor circuit..."

# authenticate to control port using cookie
COOKIE_FILE="/var/lib/tor/control_auth_cookie"

for i in {1..30}; do
    if [ -f "$COOKIE_FILE" ]; then
        COOKIE=$(hexdump -ve '1/1 "%.2X"' "$COOKIE_FILE")
        printf "AUTHENTICATE %s\r\nSIGNAL NEWNYM\r\nQUIT\r\n" "$COOKIE" | nc 127.0.0.1 9051 || true
        break
    fi
    sleep 1
done

# allow new circuit to establish
sleep 20

echo "Tor Exit IP:"
curl --socks5-hostname 127.0.0.1:9050 https://api.ipify.org || true
echo

echo "===== Starting API ====="

python3 -m api &
API_PID=$!

python3 /app/api/scheduler.py &
SCHED_PID=$!

# keep container alive while any service runs
wait -n
exit $?
