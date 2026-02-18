#!/bin/bash
set -e

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

# Faster fresh circuits
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

su -s /bin/sh debian-tor -c "tor" &
TOR_PID=$!

echo "Waiting for Tor network..."

until curl -s --socks5-hostname 127.0.0.1:9050 https://check.torproject.org/api/ip | grep -q '"IsTor":true'; do
  sleep 2
done

echo "Tor fully bootstrapped"

# Warm up circuit (IMPORTANT)
printf 'AUTHENTICATE ""\r\nSIGNAL NEWNYM\r\nQUIT\r\n' | nc 127.0.0.1 9051 || true
sleep 20

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
