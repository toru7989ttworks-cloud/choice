# SiteSearch

指定したサイトだけを検索できるアプリ（iOS / Android 対応）

## 構成

```
site-search/
├── backend/    # Python FastAPI サーバー
└── frontend/   # Expo (React Native) アプリ
```

---

## セットアップ

### 1. Node.js のインストール（まだの場合）

WSL2 / Ubuntu:
```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs
```

### 2. バックエンドの起動

```bash
cd site-search/backend
pip3 install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

### 3. フロントエンドのセットアップ

```bash
cd site-search/frontend
npm install
npm start
```

ターミナルに QR コードが表示されます。
**Expo Go** アプリ（iOS / Android）で QR コードを読み取ると実機で動作します。

---

## スマホから接続する場合

`frontend/components/config.ts` の `API_URL` を
PC の IP アドレスに変更してください：

```ts
export const API_URL = "http://192.168.1.100:8000";  // PCのIPアドレス
```

PC の IP 確認コマンド（WSL2）:
```bash
ip route show | grep -i default | awk '{ print $3}'
# または Windows側で: ipconfig
```

---

## 使い方

1. 「サイト管理」ボタンからサイトを追加
2. ホーム画面でキーワードを入力して検索
3. 検索モード:
   - **リアルタイム**: そのつど指定サイトにアクセスして検索（常に最新）
   - **インデックス**: 事前にクロールした内容から検索（高速・オフライン可）
4. インデックス検索を使う場合は「サイト管理」画面の ↺ ボタンでクロール実行
