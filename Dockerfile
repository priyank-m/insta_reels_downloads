FROM python:3.9-slim

# Install Tor and dependencies
RUN apt-get update && apt-get install -y tor torsocks curl && rm -rf /var/lib/apt/lists/*

# Create and configure the Tor config file
RUN echo "ControlPort 9051\nSocksPort 9050\nHashedControlPassword 16:E3712241ADB403A6603A241FBA8C8D1C1B9730D4BC35EEE6763958AA1D\nCookieAuthentication 0" > /etc/tor/torrc

# Expose the required ports
EXPOSE 9050 9051

# Copy the Python script and dependencies
WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir python-multipart
COPY . /app/

# Start Tor in the background and run the Python API module
CMD tor & python3 -m api