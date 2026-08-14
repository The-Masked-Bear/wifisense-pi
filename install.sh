#!/usr/bin/env bash
#
# Install WiFi Sense so it starts automatically at boot.
#
#   ./install.sh              server only
#   ./install.sh --kiosk      server + full-screen browser on the Pi's display
#   ./install.sh --uninstall  remove both
#
# The server runs as a systemd *system* service, so it comes up at boot without
# anyone logging in.  The kiosk necessarily runs inside the graphical session,
# so it is hooked into labwc's autostart instead -- Raspberry Pi OS 13 uses
# labwc under Wayland, where the old X11 autostart tricks no longer apply.

set -euo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_NAME="${SUDO_USER:-$(id -un)}"
SERVICE=/etc/systemd/system/wifisense.service
LABWC_AUTOSTART="$HOME/.config/labwc/autostart"
KIOSK_MARK="# --- wifisense kiosk ---"
PORT=8080

blue() { printf '\033[36m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m  %s\n' "$*"; }

uninstall() {
  blue "Removing WiFi Sense autostart"
  if [ -f "$SERVICE" ]; then
    sudo systemctl disable --now wifisense.service >/dev/null 2>&1 || true
    sudo rm -f "$SERVICE"
    sudo systemctl daemon-reload
    ok "service removed"
  else
    warn "service was not installed"
  fi
  if [ -f "$LABWC_AUTOSTART" ] && grep -qF "$KIOSK_MARK" "$LABWC_AUTOSTART"; then
    # Delete from the marker to the matching end marker.
    sed -i "/$(printf '%s' "$KIOSK_MARK" | sed 's/[]\/$*.^[]/\\&/g')/,/# --- end wifisense kiosk ---/d" "$LABWC_AUTOSTART"
    ok "kiosk autostart removed"
  fi
  echo
  echo "Done."
  exit 0
}

[ "${1:-}" = "--uninstall" ] && uninstall

blue "Installing WiFi Sense from $PROJECT"

# --- sanity ----------------------------------------------------------------
if [ ! -x "$PROJECT/.venv/bin/python" ]; then
  echo "error: $PROJECT/.venv/bin/python not found."
  echo "Create it first:"
  echo "  python3 -m venv --system-site-packages '$PROJECT/.venv'"
  echo "  '$PROJECT/.venv/bin/pip' install scipy fastapi 'uvicorn[standard]' websockets pyserial cc1101"
  exit 1
fi
if ! "$PROJECT/.venv/bin/python" -c "import wifisense" 2>/dev/null; then
  ( cd "$PROJECT/pi" && "$PROJECT/.venv/bin/python" -c "import wifisense" ) >/dev/null 2>&1 \
    || { echo "error: the wifisense package does not import; run tools/validate_dsp.py first"; exit 1; }
fi
ok "virtualenv and package present"

if ! id -nG "$USER_NAME" | grep -qw dialout; then
  warn "$USER_NAME is not in the 'dialout' group; adding (takes effect next login)"
  sudo usermod -aG dialout "$USER_NAME"
fi

# --- server service --------------------------------------------------------
sed -e "s|__PROJECT__|$PROJECT|g" -e "s|__USER__|$USER_NAME|g" \
    "$PROJECT/systemd/wifisense.service.template" | sudo tee "$SERVICE" >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable wifisense.service >/dev/null
sudo systemctl restart wifisense.service
ok "wifisense.service installed and started"

# --- kiosk (optional) ------------------------------------------------------
if [ "${1:-}" = "--kiosk" ]; then
  mkdir -p "$(dirname "$LABWC_AUTOSTART")"
  touch "$LABWC_AUTOSTART"
  if grep -qF "$KIOSK_MARK" "$LABWC_AUTOSTART"; then
    warn "kiosk autostart already present, leaving it alone"
  else
    cat >> "$LABWC_AUTOSTART" <<EOF

$KIOSK_MARK
# Wait for the server to answer before opening the browser, otherwise Chromium
# shows its own error page and never retries.
(
  for _ in \$(seq 1 60); do
    curl -sf -m 2 http://localhost:$PORT/api/health >/dev/null 2>&1 && break
    sleep 1
  done
  # Stop the display blanking on a wall-mounted dashboard.
  wlr-randr --output "\$(wlr-randr 2>/dev/null | awk 'NR==1{print \$1}')" >/dev/null 2>&1 || true
  chromium --ozone-platform=wayland --kiosk --noerrdialogs --disable-infobars \\
           --check-for-update-interval=31536000 \\
           --app=http://localhost:$PORT/ >/dev/null 2>&1 &
) &
# --- end wifisense kiosk ---
EOF
    ok "kiosk autostart added to $LABWC_AUTOSTART"
  fi
fi

# --- report ----------------------------------------------------------------
echo
sleep 3
if systemctl is-active --quiet wifisense.service; then
  IP="$(hostname -I | awk '{print $1}')"
  blue "Running."
  echo "  local:   http://localhost:$PORT"
  [ -n "$IP" ] && echo "  network: http://$IP:$PORT"
  echo
  echo "  status:  systemctl status wifisense"
  echo "  logs:    journalctl -u wifisense -f"
  echo "  stop:    sudo systemctl stop wifisense"
else
  warn "service is not active; see: journalctl -u wifisense -n 40"
  exit 1
fi
