FROM python:3.9-slim

ENV DEBIAN_FRONTEND=noninteractive

# Install Chromium + deps + fonts (IMPORTANT for headless fingerprint)
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg unzip curl ca-certificates \
    chromium chromium-driver \
    fonts-liberation fonts-dejavu-core fonts-dejavu-extra \
    libglib2.0-0 libnss3 libfontconfig1 \
    libx11-6 libx11-xcb1 libxcb1 libxcomposite1 \
    libxcursor1 libxdamage1 libxi6 libxtst6 \
    libxrandr2 libasound2 \
    libatk1.0-0 libatk-bridge2.0-0 \
    libgtk-3-0 libgbm1 \
    libpango-1.0-0 libpangocairo-1.0-0 \
    libatspi2.0-0 libcups2 libdrm2 libxfixes3 \
    tor torsocks netcat-openbsd net-tools \
    && rm -rf /var/lib/apt/lists/*


# -----------------------
# Tor configuration
# -----------------------
RUN echo "ControlPort 9051\n\
SocksPort 9050\n\
HashedControlPassword 16:E3712241ADB403A6603A241FBA8C8D1C1B9730D4BC35EEE6763958AA1D\n\
CookieAuthentication 0" > /etc/tor/torrc


# Display (for headless Chromium)
ENV DISPLAY=:99


# -----------------------
# App setup
# -----------------------
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .


# -----------------------
# Expose Tor ports
# -----------------------
EXPOSE 9050 9051


# -----------------------
# Start services
# -----------------------
CMD sh -c "tor & python3 -m api & python3 /app/api/scheduler.py"
