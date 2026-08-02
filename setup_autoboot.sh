#!/bin/bash
# setup_autoboot.sh — make the weather frame start automatically at boot.
#
# Run ON the Pi (as the desktop user, e.g. necco):
#   cd ~/dev/weather-frame && git pull && bash setup_autoboot.sh
#   sudo reboot
#
# After the reboot, check it worked with:
#   bash ~/dev/weather-frame/setup_autoboot.sh --check
#
# The project directory is wherever this script lives — no path is
# assumed, so any clone location works.
#
# What it does:
#   - diagnoses the environment loudly (user, paths, python deps, session)
#   - retires earlier autostart attempts (systemd unit, old autostart lines,
#     including any with a wrong hardcoded /home/pi path)
#   - installs a clean launch line into ~/.config/labwc/autostart that
#     relaunches the app if it crashes and logs to /tmp/weather-frame.log
# Safe to re-run any time (idempotent; backs up the autostart file first).

set -u

# the project is wherever this script itself lives
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$APP_DIR/weather_frame.py"
AUTOSTART="$HOME/.config/labwc/autostart"
APPLOG="/tmp/weather-frame.log"
MARKER="# weather-frame (managed by setup_autoboot.sh)"

say()  { printf '\n=== %s\n' "$*"; }
ok()   { printf '  OK   %s\n' "$*"; }
warn() { printf '  WARN %s\n' "$*"; }
fail() { printf '  FAIL %s\n' "$*"; PROBLEMS=$((PROBLEMS + 1)); }
PROBLEMS=0

if [ "$(uname)" != "Linux" ]; then
    echo "This script configures a Raspberry Pi — run it on the Pi, not here."
    exit 1
fi

# ---------------------------------------------------------------- --check
if [ "${1:-}" = "--check" ]; then
    say "post-reboot check"
    if pgrep -af weather_frame.py; then
        ok "the app is running"
    else
        fail "app not running"
        echo "  --- last lines of $APPLOG (why it died):"
        tail -10 "$APPLOG" 2>/dev/null || echo "  (no log — autostart never launched it)"
    fi
    exit 0
fi

say "1. who and where"
echo "  user: $(whoami)    home: $HOME"
if [ "$(whoami)" = "pi" ]; then
    warn "user is 'pi' — fine, but this setup was built for 'necco'"
elif [ "$(whoami)" != "necco" ]; then
    warn "user is '$(whoami)', expected 'necco' — continuing with \$HOME=$HOME"
else
    ok "user necco confirmed"
fi

say "2. project files"
echo "  project dir (from script location): $APP_DIR"
if [ -f "$APP" ]; then
    ok "$APP"
else
    fail "$APP not found next to this script — run the copy inside the repo"
fi
[ -f "$APP_DIR/slides.json" ] && ok "slides.json present" || fail "slides.json missing"

say "3. python and required packages"
echo "  $(python3 --version 2>&1) at $(command -v python3)"
for mod in pygame PIL requests; do
    if python3 -c "import $mod" 2>/dev/null; then
        ok "python3 -c 'import $mod'"
    else
        fail "python module '$mod' missing (sudo apt install python3-pygame python3-pil python3-requests)"
    fi
done
for mod in vosk sounddevice; do
    python3 -c "import $mod" 2>/dev/null && ok "optional voice dep '$mod'" \
        || warn "optional voice dep '$mod' missing (voice control will stay off)"
done

say "4. app sanity (imports + config parse, no window opened)"
SANITY_OUT=$(python3 - <<PYEOF 2>&1
import sys, os
sys.path.insert(0, "$APP_DIR")
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
import weather_frame
from pathlib import Path
cfg, slides = weather_frame.load_config(Path("$APP_DIR/slides.json"))
print(f"config OK: {len(slides)} slides, voice {'on' if cfg['voice']['enabled'] else 'off'}")
PYEOF
)
SANITY_RC=$?
printf '%s\n' "$SANITY_OUT" | sed 's/^/  /'
if [ "$SANITY_RC" -eq 0 ]; then
    ok "weather_frame.py imports and config parses"
else
    fail "the app itself errors — fix that before autostart can work"
fi

say "5. desktop session"
echo "  XDG_CURRENT_DESKTOP=${XDG_CURRENT_DESKTOP:-<unset>}  XDG_SESSION_TYPE=${XDG_SESSION_TYPE:-<unset>}  (unset is normal over SSH)"
pgrep -a labwc >/dev/null 2>&1 && ok "labwc compositor is running" \
    || warn "labwc not currently running (normal if headless right now)"
if grep -q '^autologin-user=' /etc/lightdm/lightdm.conf 2>/dev/null; then
    ok "desktop autologin: $(grep '^autologin-user=' /etc/lightdm/lightdm.conf)"
else
    warn "could not confirm desktop autologin — if the wall shows a login screen after reboot, run: sudo raspi-config -> System -> Boot/Auto Login -> Desktop Autologin"
fi

say "5b. freeing the microphone from PipeWire/WirePlumber"
# WirePlumber claims every ALSA device at session start and only
# releases (suspends) them after ~5 s with no audio clients — which
# kept the USB mic busy/invisible to PortAudio at boot (kernel showed
# the card; PortAudio saw zero inputs; a stop/start gap > 5 s freed
# it). This frame plays no audio, so PipeWire has no job here.
if systemctl --user is-active --quiet pipewire 2>/dev/null || \
   systemctl --user is-enabled --quiet pipewire.socket 2>/dev/null; then
    systemctl --user disable --now wireplumber pipewire pipewire-pulse \
        pipewire.socket pipewire-pulse.socket >/dev/null 2>&1
    ok "PipeWire/WirePlumber user services disabled — nothing claims the mic now"
    echo "  (re-enable if you ever want system audio:  systemctl --user"
    echo "   enable --now pipewire pipewire-pulse wireplumber)"
    echo "  (the desktop volume applet may show an error icon — harmless)"
else
    ok "PipeWire already inactive"
fi

say "5c. hunting rival launchers (the double-instance check)"
# A leftover launcher (systemd unit, crontab @reboot, XDG autostart,
# old clone) can start a SECOND app instance at boot that grabs the
# mic and answers the wake word invisibly. The app now enforces
# single-instance, but rivals should still be removed at the source.
CRON=$(crontab -l 2>/dev/null | grep -Ei 'weather|python' || true)
if [ -n "$CRON" ]; then
    warn "crontab entries that may launch the app — remove via 'crontab -e':"
    echo "$CRON" | sed 's/^/  | /'
else
    ok "crontab: no app entries"
fi
UNITS=$(systemctl --user list-unit-files 2>/dev/null | grep -i weather || true)
if [ -n "$UNITS" ]; then
    warn "systemd user unit(s) found: $UNITS"
    systemctl --user disable --now weather-frame >/dev/null 2>&1
    ok "weather-frame unit disabled"
else
    ok "systemd user units: none"
fi
XDG_ENTRIES=$(ls "$HOME/.config/autostart/" 2>/dev/null | grep -Ei 'weather|frame' || true)
if [ -n "$XDG_ENTRIES" ]; then
    warn "XDG autostart entries: $XDG_ENTRIES — remove from ~/.config/autostart/"
else
    ok "XDG autostart: clean"
fi
if [ -d "$HOME/weather-frame" ] && [ "$APP_DIR" != "$HOME/weather-frame" ]; then
    warn "old clone exists at ~/weather-frame — delete it: rm -rf ~/weather-frame"
else
    ok "no stray old clone"
fi
RUNNING=$(pgrep -af weather_frame.py || true)
if [ -n "$RUNNING" ]; then
    echo "  currently running instances (paths reveal which clone launched them):"
    echo "$RUNNING" | sed 's/^/  | /'
fi

say "6. retiring earlier autostart attempts"
if systemctl --user disable --now weather-frame >/dev/null 2>&1; then
    ok "systemd user unit disabled"
else
    ok "no systemd user unit active (nothing to disable)"
fi
if [ -f "$AUTOSTART" ]; then
    BAK="$AUTOSTART.bak.$(date +%Y%m%d%H%M%S)"
    cp "$AUTOSTART" "$BAK"
    ok "backed up existing autostart to $BAK"
    echo "  --- previous contents:"
    sed 's/^/  | /' "$AUTOSTART"
    grep -vE 'weather[-_]frame|import-environment WAYLAND_DISPLAY' "$AUTOSTART" > "$AUTOSTART.tmp" || true
    mv "$AUTOSTART.tmp" "$AUTOSTART"
    ok "removed old weather-frame lines (other lines kept)"
fi

say "7. installing the launch line"
mkdir -p "$(dirname "$AUTOSTART")"
{
    echo "$MARKER"
    echo "while true; do [ -e /tmp/weather-frame.stop ] || /usr/bin/python3 \"$APP\" >>$APPLOG 2>&1; sleep 5; done &"
} >> "$AUTOSTART"
ok "wrote $AUTOSTART:"
sed 's/^/  | /' "$AUTOSTART"

say "result"
if [ "$PROBLEMS" -eq 0 ]; then
    echo "  All checks passed. Now:   sudo reboot"
    echo "  After reboot, verify:     bash $APP_DIR/setup_autoboot.sh --check"
else
    echo "  $PROBLEMS problem(s) above marked FAIL — fix those first, then re-run this script."
fi
