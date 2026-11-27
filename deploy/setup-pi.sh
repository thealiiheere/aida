#!/usr/bin/env bash
# Sets up Aida on a Raspberry Pi (or any Debian-based Linux) to run on its own:
# the store, the Telegram chat and the phone line over Bluetooth, started at boot.
#
#   cd ~/Aida && bash deploy/setup-pi.sh
#
# Run it as the normal user (not root); it asks for sudo when it needs it. Running it
# again is safe. Fill in .env before or after; the services read it when they start.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
UNITS="$HOME/.config/systemd/user"

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as your normal user, not with sudo." >&2
    exit 1
fi

echo "== 1/6 Bluetooth hands-free and audio packages"
sudo apt-get update
sudo apt-get install -y bluez ofono pulseaudio-utils curl
if systemctl --user is-active --quiet pipewire 2>/dev/null || dpkg -s pipewire-pulse >/dev/null 2>&1; then
    # PipeWire (Raspberry Pi OS Bookworm and later): Bluetooth calls go through oFono.
    sudo apt-get install -y pipewire-pulse libspa-0.2-bluetooth wireplumber
    mkdir -p "$HOME/.config/wireplumber/wireplumber.conf.d" "$HOME/.config/wireplumber/bluetooth.lua.d"
    # WirePlumber 0.5 reads the first, 0.4 the second.
    cat > "$HOME/.config/wireplumber/wireplumber.conf.d/51-aida-ofono.conf" <<'EOF'
monitor.bluez.properties = {
  bluez5.hfphsp-backend = "ofono"
}
EOF
    cat > "$HOME/.config/wireplumber/bluetooth.lua.d/51-aida-ofono.lua" <<'EOF'
bluez_monitor.properties["bluez5.hfphsp-backend"] = "ofono"
EOF
else
    sudo apt-get install -y pulseaudio pulseaudio-module-bluetooth
fi
sudo systemctl enable --now bluetooth ofono
# oFono and BlueZ only answer these groups' members.
for group in bluetooth netdev; do
    if getent group "$group" >/dev/null; then
        sudo usermod -aG "$group" "$USER"
    fi
done

echo "== 2/6 uv"
if ! command -v uv >/dev/null && [ ! -x "$HOME/.local/bin/uv" ]; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"

echo "== 3/6 Python packages"
cd "$HERE"
"$UV" sync --group bluetooth

echo "== 4/6 .env"
if [ ! -f .env ]; then
    cp .env.example .env
    echo "Made .env from .env.example. Fill it in, then: systemctl --user restart aida-*"
fi

echo "== 5/6 Services that start at boot"
mkdir -p "$UNITS"
unit() {  # name, description, command, needs the store first?
    {
        echo "[Unit]"
        echo "Description=$2"
        if [ "$4" = yes ]; then
            echo "After=aida-store.service"
            echo "Wants=aida-store.service"
        fi
        echo
        echo "[Service]"
        echo "WorkingDirectory=$HERE"
        echo "ExecStart=$UV run --group bluetooth $3"
        echo "Restart=always"
        echo "RestartSec=5"
        echo
        echo "[Install]"
        echo "WantedBy=default.target"
    } > "$UNITS/$1.service"
}
unit aida-store "Aida store backend" "uvicorn app.main:app --host 127.0.0.1 --port 8000" no
unit aida-chat "Aida Telegram chat" "python -m chat" yes
unit aida-phone "Aida phone line over Bluetooth" "python -m voice.bluetooth" yes
# Keep the services running without anyone logged in.
sudo loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable aida-store aida-chat aida-phone

echo "== 6/6 Done"
cat <<EOF

Next:
  1. Restart once, so the new groups and audio settings apply:   sudo reboot
  2. Pair the phone:   bluetoothctl
       scan on                 (wait for the phone to appear)
       pair XX:XX:XX:XX:XX:XX
       trust XX:XX:XX:XX:XX:XX
       connect XX:XX:XX:XX:XX:XX
       quit
     On the phone, allow calls (and contacts) for this device.
  3. Check every key:   cd $HERE && $UV run python check.py
  4. Start everything:   systemctl --user restart aida-store aida-chat aida-phone
     See what the phone line is doing:   journalctl --user -u aida-phone -f
EOF
