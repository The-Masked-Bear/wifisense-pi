FROM python:3.13-slim

# Install only the core dependencies needed for the DSP and server.
# Radio dependencies (pyrf24, cc1101, spidev) are excluded because they require
# hardware-specific headers and are not strictly needed for the synthetic link
# or USB serial connections.
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt \
    && rm -rf /root/.cache/pip

# Copy the entire pi directory to preserve the relative path resolution
# used by the server to find the web assets.
COPY pi /app/pi
WORKDIR /app/pi

# Create a non-root user
RUN useradd -m -U appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

# Healthcheck hits the internal API. It returns 503 until the link is delivering
# data, and 200 when healthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -f http://localhost:8080/api/health || exit 1

# Default to the synthetic link so the container runs out-of-the-box with no hardware.
# To run with real hardware over USB:
#   docker run --device /dev/ttyUSB0 -p 8080:8080 ghcr.io/the-masked-bear/wifisense-pi python -m wifisense --link serial
CMD ["python", "-m", "wifisense", "--link", "synthetic"]
