#!/usr/bin/env bash
# Momir Pocket Printer setup for Raspberry Pi OS.
# Installs dependencies, binds the Bluetooth printer to /dev/rfcomm0 at boot,
# and installs a systemd service that starts the web app on boot.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$PROJECT_DIR/src/config.ini"
SERVICE_NAME="momir-pocket-printer"
RFCOMM_SERVICE_NAME="momir-rfcomm"
RUN_USER="${SUDO_USER:-$USER}"

if [[ $EUID -ne 0 ]]; then
    echo "Please run with sudo: sudo ./setup.sh"
    exit 1
fi

get_config() {
    # get_config SECTION key -> value
    awk -F' *= *' -v section="[$1]" -v key="$2" '
        $0 == section { in_section=1; next }
        /^\[/ { in_section=0 }
        in_section && $1 == key { print $2; exit }
    ' "$CONFIG_FILE"
}

CONNECTION_MODE="$(get_config PRINTER connection_mode)"
BT_MAC="$(get_config PRINTER bluetooth_mac)"

echo "==> Installing system packages..."
apt-get update -qq
apt-get install -y -qq git python3-venv python3-pip bluez libopenjp2-7

echo "==> Creating Python virtual environment..."
sudo -u "$RUN_USER" python3 -m venv "$PROJECT_DIR/.venv"
sudo -u "$RUN_USER" "$PROJECT_DIR/.venv/bin/pip" install --quiet -r "$PROJECT_DIR/requirements.txt"

echo "==> Disabling Wi-Fi power saving (causes stalls/timeouts on Pi Zero)..."
if [[ -d /etc/NetworkManager/conf.d ]]; then
    cat > /etc/NetworkManager/conf.d/momir-wifi-powersave.conf <<'EOF'
# Installed by momir-pocket-printer setup.sh. Wi-Fi power saving causes
# intermittent connection stalls on Pi Zero W hardware; 2 = disable.
[connection]
wifi.powersave = 2
EOF
    nmcli general reload 2>/dev/null || systemctl reload NetworkManager 2>/dev/null || true
fi
PS_IFACE="$(get_config WIFI ap_interface)"
iw "${PS_IFACE:-wlan0}" set power_save off 2>/dev/null || true

echo "==> Installing hotspot helper (phone-toggleable hotspot)..."
install -m 755 "$PROJECT_DIR/scripts/momir-hotspot" /usr/local/bin/momir-hotspot
echo "MOMIR_CONFIG=$CONFIG_FILE" > /etc/momir-pocket-printer.env

echo "==> Installing network rescue watchdog..."
cat > /etc/systemd/system/momir-net-fallback.service <<EOF
[Unit]
Description=Raise Momir rescue hotspot when no Wi-Fi is connected

[Service]
Type=oneshot
ExecStart=/usr/local/bin/momir-hotspot fallback
EOF
cat > /etc/systemd/system/momir-net-fallback.timer <<EOF
[Unit]
Description=Periodic check for Wi-Fi connectivity (Momir rescue hotspot)

[Timer]
OnBootSec=2min
OnUnitActiveSec=2min

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now momir-net-fallback.timer

echo "==> Installing momir CLI and login banner..."
install -m 755 "$PROJECT_DIR/scripts/momir" /usr/local/bin/momir
if [[ -d /etc/update-motd.d ]]; then
    install -m 755 "$PROJECT_DIR/scripts/momir-motd" /etc/update-motd.d/50-momir
fi
# Allow the app user to run exactly this helper as root, nothing else.
echo "$RUN_USER ALL=(root) NOPASSWD: /usr/local/bin/momir-hotspot" \
    > /etc/sudoers.d/momir-pocket-printer
chmod 440 /etc/sudoers.d/momir-pocket-printer

AP_ENABLED="$(get_config WIFI ap_enabled | tr '[:upper:]' '[:lower:]')"
if [[ "$AP_ENABLED" == "true" ]]; then
    if ! command -v nmcli >/dev/null; then
        echo "!! nmcli not found. Hotspot mode needs NetworkManager (default on Raspberry Pi OS)."
        exit 1
    fi
    echo "==> Starting Wi-Fi hotspot..."
    echo "    NOTE: if you are SSHed in over Wi-Fi, this will drop your connection."
    echo "    The Pi will then be at http://10.42.0.1:8080 on the hotspot network."
    /usr/local/bin/momir-hotspot on || echo "!! Hotspot will start on next boot."
else
    # Leave any existing profile in place but inactive; the host can toggle
    # the hotspot from the web app's host panel at any time.
    if command -v nmcli >/dev/null; then
        /usr/local/bin/momir-hotspot off || true
    fi
fi

echo "==> Installing app service..."
cat > "/etc/systemd/system/$SERVICE_NAME.service" <<EOF
[Unit]
Description=Momir Pocket Printer web app
After=network-online.target $([[ "$CONNECTION_MODE" == "bluetooth" ]] && echo "$RFCOMM_SERVICE_NAME.service")
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$PROJECT_DIR/src
ExecStart=$PROJECT_DIR/.venv/bin/python $PROJECT_DIR/src/app.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME.service"

if [[ "$CONNECTION_MODE" == "bluetooth" ]]; then
    if [[ -z "$BT_MAC" || "$BT_MAC" == "00:00:00:00:00:00" ]]; then
        cat <<'EOF'
!! bluetooth_mac is not set in src/config.ini.

   Find your printer's MAC address first:
     1. Turn the printer on.
     2. Run: bluetoothctl
     3. In the prompt: scan on
     4. Look for a device named PT-210 (or similar) and note its MAC.
     5. Still in bluetoothctl: pair <MAC>   (PIN is usually 0000 or 1234)
        then: trust <MAC>, then: quit
     6. Put the MAC in src/config.ini under [PRINTER] bluetooth_mac.
     7. Re-run: sudo ./setup.sh

   No printer yet? Everything else is installed and the web app is
   already running - open http://<pi-hostname>.local:8080 to see it
   (printing errors until the printer is paired). You can pre-download
   the card database now with: momir update
EOF
        exit 1
    fi

    echo "==> Installing rfcomm binding service for $BT_MAC..."
    cat > "/etc/systemd/system/$RFCOMM_SERVICE_NAME.service" <<EOF
[Unit]
Description=Bind PT-210 thermal printer to /dev/rfcomm0
After=bluetooth.service
Requires=bluetooth.service

[Service]
Type=simple
ExecStart=/usr/bin/rfcomm connect 0 $BT_MAC 1
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now "$RFCOMM_SERVICE_NAME.service"
elif [[ "$CONNECTION_MODE" == "usb" ]]; then
    echo "==> USB mode: granting $RUN_USER access to USB printers (plugdev)..."
    usermod -aG plugdev "$RUN_USER" || true
    VENDOR_ID="$(get_config PRINTER vendor_id | sed 's/^0x//')"
    PRODUCT_ID="$(get_config PRINTER product_id | sed 's/^0x//')"
    if [[ -n "$VENDOR_ID" && "$VENDOR_ID" != "0000" ]]; then
        cat > /etc/udev/rules.d/99-momir-printer.rules <<EOF
SUBSYSTEM=="usb", ATTRS{idVendor}=="$VENDOR_ID", ATTRS{idProduct}=="$PRODUCT_ID", MODE="0666"
EOF
        udevadm control --reload-rules
        udevadm trigger
    else
        echo "!! Set vendor_id/product_id in src/config.ini (from lsusb), then re-run setup."
    fi
fi

PORT="$(get_config APP listen_port)"
echo ""
if [[ "$AP_ENABLED" == "true" ]]; then
    echo "Done. Join Wi-Fi '$(get_config WIFI ap_ssid)' and open http://10.42.0.1:${PORT:-8080}"
    echo "(or tap 'Print Wi-Fi join ticket' in the app to print a scannable QR receipt)."
else
    IP_ADDR="$(hostname -I | awk '{print $1}')"
    echo "Done. Open http://${IP_ADDR:-<pi-address>}:${PORT:-8080} on your phone."
fi
echo "Logs: sudo journalctl -u $SERVICE_NAME.service -f"
