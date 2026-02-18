#!/bin/bash
set -e

echo "===== Preparing Tor directories ====="

mkdir -p /var/lib/tor
chown -R debian-tor:debian-tor /var/lib/tor
chmod 700 /var/lib/tor

echo "===== Writing torrc ====="

cat > /etc/tor/torrc <<'EOF'
SocksPort 0.0.0.0:9050 IsolateSOCKSAuth
ControlPort 127.0.0.1:9051
CookieAuthentication 1

DataDirectory /var/lib/tor

# Faster fresh circuits (important for scraping)
MaxCircuitDirtiness 10
NewCircuitPeriod 5
CircuitBuildTimeout 15
LearnCircuitBuildTimeout 0

# Reduce DNS leaks + reliability
ClientUseIPv6 0
SafeSocks 1

Log notice stdout
EOF

echo "===== Starting Tor ====="

su -s /bin/sh debian-tor -c "tor" &
TOR_PID=$!

echo "Waiting for Tor bootstrap (100%)..."

# wait until tor is FULLY ready (not just port open)
until grep -q "Bootstrapped 100%" <(timeout 1s cat /proc/$TOR_PID/fd/1 2>/dev/null || true); do
  sleep 1
done

sleep 5

echo "Tor connected. Exit IP:"
curl --socks5-hostname 127.0.0.1:9050 https://api.ipify.org || true
echo

echo "===== Starting API ====="

python3 -m api &
API_PID=$!

python3 /app/api/scheduler.py &
SCHED_PID=$!

wait -n
exit $?
