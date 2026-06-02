#!/usr/bin/env bash
#
# install.sh — Instal·la l'Archie: dependències + servei systemd d'usuari.
#
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/archie"
SERVICE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SERVICE_FILE="$SERVICE_DIR/archie.service"
BIN_DIR="$HOME/.local/bin"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
warn() { printf '\033[33m!! %s\033[0m\n' "$*"; }

bold "==> Instal·lant Archie"

# --------------------------------------------------------------------------- #
# 1. Dependències
# --------------------------------------------------------------------------- #
if command -v pacman >/dev/null 2>&1; then
  bold "==> Dependències (pacman)"
  required=(gtk4 gtk4-layer-shell python-gobject python-yaml)
  missing=()
  for pkg in "${required[@]}"; do
    pacman -Qq "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
  done
  if ((${#missing[@]})); then
    info "Instal·lant: ${missing[*]}"
    sudo pacman -S --needed --noconfirm "${missing[@]}"
  else
    info "Totes les dependències ja hi són."
  fi

  # Eines opcionals: si falten, els checks que les fan servir s'OMETEN
  # (no fallen). Cada paquet habilita el check entre parèntesis.
  declare -A optional=(
    [power-profiles-daemon]="cpu_governor / battery / power_profiles"
    [lm_sensors]="cpu_temp / thermal_paste"
    [pacman-contrib]="system_not_updated (checkupdates)"
    [bind]="dns_slow (dig)"
    [mesa-utils]="direct_rendering (glxinfo)"
    [wireless_tools]="wifi_power_save (iwconfig)"
  )
  for opt in "${!optional[@]}"; do
    pacman -Qq "$opt" >/dev/null 2>&1 || warn "(opcional) pacman -S $opt  → ${optional[$opt]}"
  done
  if ! pacman -Qq mbpfan >/dev/null 2>&1; then
    warn "(opcional, MacBook) yay -S mbpfan  → mbpfan_inactive (AUR)"
  fi
  # Cal un emulador de terminal perquè 'Arregla-ho' executi els fixos amb sudo.
  if ! command -v alacritty kitty foot ghostty wezterm konsole gnome-terminal xterm \
        >/dev/null 2>&1; then
    warn "(important) instal·la un terminal (p. ex. alacritty) per executar els fixos"
  fi
else
  warn "pacman no trobat. Instal·la manualment:"
  warn "  GTK4, gtk4-layer-shell, PyGObject, python-yaml (o usa el mini-parser inclòs)"
fi

# --------------------------------------------------------------------------- #
# 2. Còpia dels fitxers de l'app
# --------------------------------------------------------------------------- #
bold "==> Copiant l'app a $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
install -m 0755 "$SRC_DIR/archie.py"          "$INSTALL_DIR/archie.py"
install -m 0644 "$SRC_DIR/monitor.py"         "$INSTALL_DIR/monitor.py"
install -m 0644 "$SRC_DIR/archie_checks.yaml" "$INSTALL_DIR/archie_checks.yaml"

# Wrapper a ~/.local/bin perquè puguis fer: archie status | demo | check
bold "==> Instal·lant la comanda 'archie' a $BIN_DIR"
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/archie" <<EOF
#!/usr/bin/env bash
exec /usr/bin/python3 "$INSTALL_DIR/archie.py" "\$@"
EOF
chmod 0755 "$BIN_DIR/archie"
case ":$PATH:" in
  *":$BIN_DIR:"*) : ;;
  *) warn "$BIN_DIR no és al PATH; afegeix-lo per usar 'archie' directament." ;;
esac

# --------------------------------------------------------------------------- #
# 3. Servei systemd d'usuari
# --------------------------------------------------------------------------- #
bold "==> Instal·lant el servei d'usuari"
mkdir -p "$SERVICE_DIR"
sed "s|@INSTALL_DIR@|$INSTALL_DIR|g" "$SRC_DIR/archie.service" > "$SERVICE_FILE"

systemctl --user daemon-reload

# Importa l'entorn Wayland actual perquè pugui arrencar ja i en sessions futures
# que facin servir graphical-session.target (uwsm, etc.).
systemctl --user import-environment \
  WAYLAND_DISPLAY XDG_CURRENT_DESKTOP HYPRLAND_INSTANCE_SIGNATURE \
  DBUS_SESSION_BUS_ADDRESS XDG_RUNTIME_DIR PATH 2>/dev/null || true

systemctl --user enable archie.service
systemctl --user restart archie.service 2>/dev/null || systemctl --user start archie.service

# --------------------------------------------------------------------------- #
# 4. Resum
# --------------------------------------------------------------------------- #
echo
bold "✓ Archie instal·lat i en marxa"
info "Vista d'estat:   archie status"
info "Bombolla ara:    archie check"
info "Exemple/demo:    archie demo"
info "Servei (estat):  systemctl --user status archie.service"
info "Servei (logs):   journalctl --user -u archie.service -f"
info "Treure servei:   systemctl --user disable --now archie.service"
echo
if ! systemctl --user is-active --quiet graphical-session.target; then
  warn "graphical-session.target no està actiu (sembla que no uses uwsm)."
  warn "Perquè Archie arrenqui sol a cada login, afegeix a ~/.config/hypr/hyprland.conf:"
  echo
  echo "    exec-once = systemctl --user start archie.service"
  echo
fi
