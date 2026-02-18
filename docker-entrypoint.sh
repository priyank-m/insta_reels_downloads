#!/bin/bash
set -e

echo "===== Starting Tor ====="

cat > /etc/tor/torrc <<EOF
SocksPort 0.0.0.0:9050
ControlPort 0.0.0.0:9051
CookieAuthentication 1
MaxCircuitDirtiness 30
DataDirectory /var/lib/tor
Log notice stdout
EOF

tor &
TOR_PID=$!

echo "Waiting for Tor bootstrap..."

# wait for port open
until nc -z 127.0.0.1 9050; do
  sleep 1
done

# wait for real circuit
sleep 8

echo "Tor is ready. Current IP:"
curl --socks5-hostname 127.0.0.1:9050 https://api.ipify.org || true
echo

echo "===== Starting API ====="

python3 -m api &
API_PID=$!

python3 /app/api/scheduler.py &
SCHED_PID=$!

# keep container alive & restart safe
wait -n
exit $?
