FROM python:3.9-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV DISPLAY=:99

# -----------------------
# System + Chromium + Tor
# -----------------------
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
# App setup
# -----------------------
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# -----------------------
# Entrypoint
# -----------------------
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 9050 9051

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
