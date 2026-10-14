#!/bin/sh
# Serve 3ds/3DSStream.cia over HTTP on the LAN and show a QR code for
# FBI > Remote Install > Scan QR Code. Ctrl-C stops the server.
set -eu
PORT="${PORT:-8080}"
DIR="$(cd "$(dirname "$0")/../3ds" && pwd)"
CIA="3DSStream.cia"
[ -f "$DIR/$CIA" ] || { echo "Missing $DIR/$CIA — run 'make cia' in 3ds/ first." >&2; exit 1; }
IP="${IP:-$(ipconfig getifaddr en0 || ipconfig getifaddr en1)}"
URL="http://$IP:$PORT/$CIA"
command -v qrencode >/dev/null || { echo "Install qrencode: brew install qrencode" >&2; exit 1; }
echo "$URL"
qrencode -t ANSIUTF8 -m 2 "$URL"
qrencode -o "$DIR/build/cia_qr.png" -s 10 -m 2 "$URL" 2>/dev/null || true
cd "$DIR" && exec python3 -m http.server "$PORT" --bind 0.0.0.0
