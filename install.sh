#!/usr/bin/env bash
# Puts `uptime-teesko` on PATH (~/.local/bin), pointing at this checkout.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HOME/.local/bin"
printf '#!/usr/bin/env bash\nexec python3 "%s" "$@"\n' "$HERE/uptime_teesko.py" > "$HOME/.local/bin/uptime-teesko"
chmod +x "$HOME/.local/bin/uptime-teesko" "$HERE/uptime_teesko.py"
echo "installed: ~/.local/bin/uptime-teesko → $HERE/uptime_teesko.py"
