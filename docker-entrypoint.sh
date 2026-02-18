#!/bin/bash
set -e

echo "===== Preparing Tor directories ====="

mkdir -p /var/lib/tor
chown -R debian-tor:debian-tor /var/lib/tor
chmod 700 /var/lib/tor

echo "===== Writing torrc ====="

cat > /etc/tor/torrc <<EOF
SocksPort 127.0.0.1:9050
ControlPort 127.0.0.1:9051
CookieAuthentication 1
DataDirectory /var/lib/tor
MaxCircuitDirtiness 30
Log notice stdout
EOF

echo "===== Starting Tor ====="

# start as correct user
su -s /bin/sh debian-tor -c "tor" &
TOR_PID=$!

echo "Waiting for Tor bootstrap..."

until nc -z 127.0.0.1 9050; do
  sleep 1
done

sleep 8

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
