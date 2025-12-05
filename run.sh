#!/bin/bash
# Project Manager Agent - Startup Script
# Runs Flask server + ngrok tunnel + sets Telegram webhook

cd "$(dirname "$0")"

# Load environment
source ~/.zshrc 2>/dev/null

# Export required vars (fallback if not in zshrc)
export TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-7991164130:AAEQyJjDlaFS1U_36fVI5mVPiEohjlGQ6VM}"
export TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-8362761468}"
export XAI_API_KEY="${XAI_API_KEY_FAST:-xai-VVJKIvt5evCJsbSUuWtsYynjZbSYVSZvfUZpIR1jWq64aSlzRgyRXkub2kJbSAyWH4EYHm8G8I8xtbGN}"

echo "=========================================="
echo "  Project Manager Agent"
echo "=========================================="
echo ""

# Kill any existing processes on port 4000
lsof -ti:4000 | xargs kill -9 2>/dev/null

# Start Flask server in background
echo "[1/3] Starting Flask server on port 4000..."
python3 server.py &
SERVER_PID=$!
sleep 2

# Check server is running
if ! curl -s http://localhost:4000/ > /dev/null; then
    echo "ERROR: Server failed to start"
    exit 1
fi
echo "      Server running (PID: $SERVER_PID)"

# Start ngrok in background
echo "[2/3] Starting ngrok tunnel..."
ngrok http 4000 --log=stdout > /tmp/ngrok.log 2>&1 &
NGROK_PID=$!
sleep 3

# Get ngrok URL
NGROK_URL=$(curl -s http://localhost:4040/api/tunnels 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['tunnels'][0]['public_url'] if d.get('tunnels') else '')" 2>/dev/null)

if [ -z "$NGROK_URL" ]; then
    echo "ERROR: ngrok failed to start. Check /tmp/ngrok.log"
    kill $SERVER_PID 2>/dev/null
    exit 1
fi
echo "      Tunnel: $NGROK_URL"

# Set Telegram webhook
echo "[3/3] Setting Telegram webhook..."
WEBHOOK_RESULT=$(curl -s -X POST http://localhost:4000/webhook/set \
    -H "Content-Type: application/json" \
    -d "{\"url\": \"${NGROK_URL}/webhook\"}")

if echo "$WEBHOOK_RESULT" | grep -q '"ok":true'; then
    echo "      Webhook configured!"
else
    echo "ERROR: Failed to set webhook: $WEBHOOK_RESULT"
fi

echo ""
echo "=========================================="
echo "  READY!"
echo "=========================================="
echo ""
echo "  Telegram Bot: @Alan0130_bot"
echo "  Public URL:   $NGROK_URL"
echo "  Local:        http://localhost:4000"
echo "  ngrok UI:     http://localhost:4040"
echo ""
echo "  Send a message to the bot to test!"
echo ""
echo "  Press Ctrl+C to stop all services"
echo "=========================================="

# Trap to cleanup on exit
cleanup() {
    echo ""
    echo "Shutting down..."
    kill $SERVER_PID 2>/dev/null
    kill $NGROK_PID 2>/dev/null
    # Delete webhook on shutdown
    curl -s -X POST http://localhost:4000/webhook/delete > /dev/null 2>&1
    echo "Done."
    exit 0
}
trap cleanup SIGINT SIGTERM

# Wait for processes
wait $SERVER_PID
