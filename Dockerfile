FROM python:3.9-slim

# Install Chrome & dependencies
RUN apt-get update && apt-get install -y \
    wget gnupg unzip curl \
    libglib2.0-0 libnss3 libgconf-2-4 libfontconfig1 \
    libx11-6 libx11-xcb1 libxcb1 libxcomposite1 libxcursor1 \
    libxdamage1 libxi6 libxtst6 libxrandr2 libasound2 \
    libatk1.0-0 libatk-bridge2.0-0 libgtk-3-0 \
    libgbm1 libpango-1.0-0 libpangocairo-1.0-0 \
    libatspi2.0-0 libcups2 libdrm2 libxfixes3 \
    chromium chromium-driver tor torsocks netcat-openbsd net-tools systemctl \
    && rm -rf /var/lib/apt/lists/*

# Tor configuration
RUN echo "ControlPort 9051\nSocksPort 9050\nHashedControlPassword 16:E3712241ADB403A6603A241FBA8C8D1C1B9730D4BC35EEE6763958AA1D\nCookieAuthentication 0" > /etc/tor/torrc

# Set display env variable
ENV DISPLAY=:99

# Install Python dependencies
WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy app files
COPY . /app/

# Expose Tor ports
EXPOSE 9050 9051

# Start Tor and API
CMD tor & python3 -m api & python3 /app/api/scheduler.py
