#!/bin/bash

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║     Ajay Doval — Setup Script            ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Check Homebrew ────────────────────────────────────────────────────────────
if ! command -v brew &>/dev/null; then
  echo "Installing Homebrew..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
else
  echo "✅ Homebrew found"
fi

# ── Install system dependencies ───────────────────────────────────────────────
echo ""
echo "Installing ffmpeg and yt-dlp..."
brew install ffmpeg yt-dlp

echo "✅ ffmpeg and yt-dlp installed"

# ── Install Python packages ───────────────────────────────────────────────────
echo ""
echo "Installing Python packages..."
pip3 install -r requirements.txt --break-system-packages

echo "✅ Python packages installed"

# ── Prompt for Gemini API key ─────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Get a free Gemini API key at: https://aistudio.google.com"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
read -p "Paste your Gemini API key: " GEMINI_KEY

if [ -n "$GEMINI_KEY" ]; then
  sed -i '' "s|GEMINI_KEY  = \".*\"|GEMINI_KEY  = \"$GEMINI_KEY\"|g" dashboard.py
  sed -i '' "s|ORM_API_KEY = \".*\"|ORM_API_KEY = \"$GEMINI_KEY\"|g" dashboard.py
  echo "✅ API key saved to dashboard.py"
else
  echo "⚠️  No key entered — add it manually in dashboard.py (GEMINI_KEY and ORM_API_KEY)"
fi

# ── Create output folders ─────────────────────────────────────────────────────
mkdir -p outputs inbox assets

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║     ✅ Setup complete!                   ║"
echo "║                                          ║"
echo "║     Run:  python3 dashboard.py           ║"
echo "║     Open: http://localhost:8080          ║"
echo "╚══════════════════════════════════════════╝"
echo ""
