#!/bin/bash
# frame.sh — start/stop/status for the wall display.
#
#   bash frame.sh stop     stop the app AND pause the auto-restart loop
#                          (stays off until "start" or the next reboot)
#   bash frame.sh start    resume — the app relaunches within ~5 s
#   bash frame.sh status   is it running? flag state + recent log
#
# Handy alias (one-time):
#   echo "alias frame='bash $HOME/dev/weather-frame/frame.sh'" >> ~/.bashrc
# then just: frame stop / frame start / frame status

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$APP_DIR/weather_frame.py"
STOP=/tmp/weather-frame.stop
APPLOG=/tmp/weather-frame.log

loop_running() {
    # the autostart loop's argv is "sh ~/.config/labwc/autostart";
    # a loop launched by "start" below contains the app path instead
    pgrep -f 'labwc/autostart' >/dev/null 2>&1 || \
        pgrep -f "weather-frame-loop" >/dev/null 2>&1
}

case "${1:-status}" in
  stop)
    touch "$STOP"
    if pkill -f "weather_frame.py"; then
        echo "stopped. Auto-restart is paused — resume with: frame.sh start"
    else
        echo "app was not running; auto-restart is now paused anyway"
    fi
    echo "(a reboot also clears the pause — the frame always comes back on power-cycle)"
    ;;
  start)
    rm -f "$STOP"
    if pgrep -f "weather_frame.py" >/dev/null; then
        echo "already running"
    elif loop_running; then
        echo "resuming — the loop relaunches the app within ~5 s"
    else
        nohup bash -c "exec -a weather-frame-loop bash -c 'while true; do [ -e $STOP ] || /usr/bin/python3 \"$APP\" >>$APPLOG 2>&1; sleep 5; done'" >/dev/null 2>&1 &
        echo "no restart loop was running — launched one; app starts within ~5 s"
    fi
    ;;
  status)
    if pgrep -af "weather_frame.py"; then
        echo "running"
    else
        echo "app: NOT running"
    fi
    [ -e "$STOP" ] && echo "auto-restart: PAUSED ($STOP present)" \
                   || echo "auto-restart: armed"
    loop_running && echo "restart loop: alive" || echo "restart loop: not running (reboot or 'frame.sh start' to launch)"
    echo "--- last log lines ($APPLOG):"
    tail -5 "$APPLOG" 2>/dev/null || echo "(no log yet)"
    ;;
  *)
    echo "usage: bash frame.sh {stop|start|status}"
    exit 1
    ;;
esac
