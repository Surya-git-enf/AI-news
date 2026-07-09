#!/bin/bash

echo "🚀 Booting Modular AI God-Mode with 9Router..."

# ==========================================
# Load Local Environment Variables First
# ==========================================
if [ -f .env ]; then
    echo "📂 Loading .env variables..."
    set -a
    source .env
    set +a
fi

if [ ! -f .env.example ]; then
    cp .env .env.example 2>/dev/null || touch .env.example
fi

# ==========================================
# Install Core Dependencies
# ==========================================
echo "📦 Installing core tools..."

# Clean old anthropic installations
rm -rf "$(npm root -g)/@anthropic-ai" 2>/dev/null || true

# Install 9Router and Claude Code globally
npm install -g 9router @anthropic-ai/claude-code

# ==========================================
# Sync Claude Skills
# ==========================================
echo "🧠 Syncing Claude Skills..."

rm -rf ~/.claude/skills
mkdir -p ~/.claude/skills

TEMP_DIR=$(mktemp -d)

if git clone --quiet --depth 1 \
  https://github.com/Surya-git-enf/Claude-skills.git "$TEMP_DIR"; then

    find "$TEMP_DIR" -type f -iname "*.md" \
      -exec cp {} ~/.claude/skills/ \;

    echo "✅ Skills synced successfully"
else
    echo "❌ Failed to sync skills"
fi

rm -rf "$TEMP_DIR"

# ==========================================
# Configure Environment Variables
# ==========================================
# Point Claude to the 9Router endpoint
export ANTHROPIC_BASE_URL="http://127.0.0.1:20128/v1"
# Map your NVIDIA API Key (or fall back to 9Router's default auth)
export ANTHROPIC_AUTH_TOKEN="${NVIDIA_API_KEY:-123456}"
export ANTHROPIC_API_KEY="${NVIDIA_API_KEY:-123456}"

grep -qxF 'export ANTHROPIC_BASE_URL="http://127.0.0.1:20128/v1"' ~/.bashrc || \
echo 'export ANTHROPIC_BASE_URL="http://127.0.0.1:20128/v1"' >> ~/.bashrc

# ==========================================
# Restart 9Router Proxy
# ==========================================
echo "⚡ Starting 9Router..."

# Kill any old python proxy or existing 9router instances
pkill -f python || true
pkill -f fcc-server || true
pkill -f 9router || true

# Boot 9Router in the background
9router > proxy.log 2>&1 &
sleep 5

# ==========================================
# Health Check
# ==========================================
if curl -s http://127.0.0.1:20128 >/dev/null; then
    echo "✅ 9Router server running on port 20128"
else
    echo "❌ 9Router startup failed"
    cat proxy.log
fi

# ==========================================
# Launch Claude
# ==========================================
echo "🚀 Launching Claude..."

chmod +x "$(which claude 2>/dev/null)" 2>/dev/null || true

npx -y @anthropic-ai/claude-code \
  --continue \
  --dangerously-skip-permissions

