#!/bin/zsh
# Runs `jev_trader.py tick` every 30 minutes via launchd. The script itself decides
# whether the market is open and which tickers Jev scheduled for review.
# Uses an interactive zsh so TYPESAFE_API_KEY from ~/.zshrc is available.
set -e
DIR="${0:A:h}"
PLIST=~/Library/LaunchAgents/ai.typesafe.jev-trader.plist
UV="$(command -v uv)"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>ai.typesafe.jev-trader</string>
  <key>ProgramArguments</key><array>
    <string>/bin/zsh</string><string>-ic</string>
    <string>cd "$DIR" &amp;&amp; "$UV" run python jev_trader.py tick</string>
  </array>
  <key>StartInterval</key><integer>1800</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$DIR/state/tick.log</string>
  <key>StandardErrorPath</key><string>$DIR/state/tick.log</string>
</dict></plist>
PL
# Keep the Mac from idle-sleeping during US market hours (9:20am-4:25pm ET, weekdays) so reviews aren't missed.
# Times are in the Mac's local timezone; adjust if you're not on US Eastern.
AWAKE=~/Library/LaunchAgents/ai.typesafe.jev-trader-awake.plist
{
cat <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>ai.typesafe.jev-trader-awake</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/caffeinate</string><string>-i</string><string>-t</string><string>25500</string>
  </array>
  <key>StartCalendarInterval</key><array>
PL
for d in 1 2 3 4 5; do
  echo "    <dict><key>Weekday</key><integer>$d</integer><key>Hour</key><integer>9</integer><key>Minute</key><integer>20</integer></dict>"
done
cat <<PL
  </array>
</dict></plist>
PL
} > "$AWAKE"
launchctl unload "$AWAKE" 2>/dev/null || true
launchctl load "$AWAKE"

mkdir -p "$DIR/state"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "For wake-from-sleep before the open, run once:  sudo pmset repeat wakeorpoweron MTWRF 09:15:00"
echo "Scheduled. Logs: $DIR/state/tick.log   Remove with: launchctl unload $PLIST && rm $PLIST"
