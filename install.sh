#!/bin/bash
# HP Thermal Control Applet — install script
# Run as: sudo bash install.sh [--user USERNAME]

set -e
APPLET_DIR="$(cd "$(dirname "$0")" && pwd)"
APPLET_USER="${SUDO_USER:-$USER}"

while [[ "$1" == --* ]]; do
    case "$1" in
        --user) APPLET_USER="$2"; shift 2 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

USER_HOME=$(eval echo "~$APPLET_USER")

echo "=== HP Thermal Control Applet installer ==="
echo "Installing for user: $APPLET_USER"
echo ""

# ── 1. Kernel modules ─────────────────────────────────────────────────────────
echo "[1/4] Checking kernel modules..."

REQUIRED_MODULES=(hp_wmi_sensors hp_bioscfg intel_rapl_common intel_rapl_msr)
MISSING=()

for mod in "${REQUIRED_MODULES[@]}"; do
    if modinfo "$mod" &>/dev/null; then
        if ! lsmod | grep -q "^${mod} \|^${mod}\t"; then
            echo "  Loading: $mod"
            modprobe "$mod" && echo "  ✓ $mod loaded" || echo "  ✗ $mod failed to load"
        else
            echo "  ✓ $mod (already loaded)"
        fi
    else
        echo "  ✗ $mod — NOT AVAILABLE on this kernel (some features may not work)"
        MISSING+=("$mod")
    fi
done

# Persist modules (not needed in initrd — loaded after systemd-modules-load)
cat > /etc/modules-load.d/hp-thermal.conf << MEOF
# HP Thermal Control Applet — required modules
hp_wmi_sensors
hp_bioscfg
intel_rapl_common
intel_rapl_msr
MEOF
echo "  Module autoload config: /etc/modules-load.d/hp-thermal.conf"

if [[ ${#MISSING[@]} -gt 0 ]]; then
    echo ""
    echo "  WARNING: Missing modules: ${MISSING[*]}"
    echo "  Some features may not work. Check your kernel version and config."
fi

echo ""

# ── 2. Privileged helper + polkit ─────────────────────────────────────────────
echo "[2/5] Installing privileged helper and polkit policy..."
install -m 755 "$APPLET_DIR/hp-thermal-helper" /usr/local/sbin/hp-thermal-helper
echo "  ✓ /usr/local/sbin/hp-thermal-helper"
install -m 644 "$APPLET_DIR/com.hp.thermal.policy" /usr/share/polkit-1/actions/com.hp.thermal.policy
echo "  ✓ /usr/share/polkit-1/actions/com.hp.thermal.policy"

echo ""

# ── 3. Install applet script ──────────────────────────────────────────────────
echo "[3/5] Installing applet script, icon and desktop entry..."
install -m 755 "$APPLET_DIR/hp_fan_control.py" /usr/local/bin/hp-thermal-applet

# Icon
install -d /usr/share/icons/hicolor/scalable/apps
install -m 644 "$APPLET_DIR/hp-thermal-applet.svg" /usr/share/icons/hicolor/scalable/apps/hp-thermal-applet.svg
gtk-update-icon-cache -f -t /usr/share/icons/hicolor 2>/dev/null || true

# System .desktop file — makes applet appear in GNOME app launcher
cat > /usr/share/applications/hp-thermal-applet.desktop << DESKTOP
[Desktop Entry]
Type=Application
Name=HP Thermal Monitor
GenericName=Thermal Control
Comment=HP ZBook thermal control — RAPL, turbo boost and fan settings
Exec=hp-thermal-applet
Icon=hp-thermal-applet
Categories=System;Settings;HardwareSettings;
Keywords=thermal;fan;cpu;temperature;hp;zbook;
StartupNotify=false
NoDisplay=false
DESKTOP
echo "  ✓ /usr/local/bin/hp-thermal-applet"
echo "  ✓ /usr/share/icons/hicolor/scalable/apps/hp-thermal-applet.svg"
echo "  ✓ /usr/share/applications/hp-thermal-applet.desktop"

echo ""

# ── 4. Python dependencies ────────────────────────────────────────────────────
echo "[4/5] Checking Python dependencies..."
MISSING_PY=()
for pkg in "gi" "gi.repository.AppIndicator3" "gi.repository.Secret"; do
    python3 -c "import $pkg" 2>/dev/null && echo "  ✓ $pkg" || MISSING_PY+=("$pkg")
done
if [[ ${#MISSING_PY[@]} -gt 0 ]]; then
    echo "  Installing missing Python packages..."
    apt-get install -y python3-gi gir1.2-appindicator3-0.1 gir1.2-secret-1 2>/dev/null \
        || echo "  WARNING: Could not auto-install. Install manually: python3-gi gir1.2-appindicator3-0.1 gir1.2-secret-1"
fi

echo ""

# ── 5. Autostart desktop entry ────────────────────────────────────────────────
echo "[5/5] Installing autostart entry for $APPLET_USER..."
AUTOSTART_DIR="$USER_HOME/.config/autostart"
mkdir -p "$AUTOSTART_DIR"
cat > "$AUTOSTART_DIR/hp-thermal-applet.desktop" << DESKTOP
[Desktop Entry]
Type=Application
Name=HP Thermal Monitor
Comment=HP ZBook thermal control system tray applet
Exec=hp-thermal-applet
Icon=hp-thermal-applet
StartupNotify=false
X-GNOME-Autostart-enabled=true
DESKTOP
chown "$APPLET_USER:" "$AUTOSTART_DIR/hp-thermal-applet.desktop"
echo "  ✓ $AUTOSTART_DIR/hp-thermal-applet.desktop"

echo ""
echo "=== Installation complete ==="
echo ""
echo "To start now (as $APPLET_USER):"
echo "  hp-thermal-applet &"
