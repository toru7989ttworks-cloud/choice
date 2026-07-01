#!/bin/bash
set -e

echo "=== SiteSearch セットアップ ==="
echo ""

# Node.js
if ! command -v node &> /dev/null; then
  echo "[1/4] Node.js をインストール中..."
  curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - > /dev/null 2>&1
  sudo apt-get install -y nodejs > /dev/null 2>&1
  echo "  Node.js $(node --version) をインストールしました"
else
  echo "[1/4] Node.js $(node --version) - 既にインストール済み"
fi

# python3-venv
echo "[2/4] Python 仮想環境をセットアップ中..."
sudo apt-get install -y python3-venv python3-full > /dev/null 2>&1
python3 -m venv backend/.venv
echo "  仮想環境を作成しました (backend/.venv)"

# Python deps in venv
echo "[3/4] Python パッケージをインストール中..."
backend/.venv/bin/pip install -r backend/requirements.txt -q
echo "  完了"

# Frontend deps
echo "[4/4] npm パッケージをインストール中..."
cd frontend && npm install --silent
cd ..
echo "  完了"

echo ""
echo "=== セットアップ完了 ==="
echo ""
echo "【起動方法】"
echo ""
echo "  ターミナル1（バックエンド）:"
echo "    cd ~/site-search/backend"
echo "    .venv/bin/uvicorn main:app --reload --host 0.0.0.0 --port 8000"
echo ""
echo "  ターミナル2（フロントエンド）:"
echo "    cd ~/site-search/frontend"
echo "    npm start"
echo ""
echo "  → スマホに Expo Go アプリを入れて QR コードを読み取る"
