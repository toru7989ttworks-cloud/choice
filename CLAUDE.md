# Choice アプリ

指定したサイトだけをキーワード検索できる Web アプリ。

## 構成

- `backend/main.py` — Python FastAPI（検索・クロール・プロキシ・HTML 配信）
- `backend/requirements.txt` — 依存パッケージ
- `backend/.venv/` — Python 仮想環境

フロントエンドは `main.py` の `HTML` 変数に直接埋め込み済み（別ファイルなし）。

## 起動方法

```bash
cd ~/site-search/backend
.venv/bin/uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

iPhone の Safari で `http://192.168.1.4:8000` を開く。

## 現在の機能

- **検索タブ**: 登録サイトをキーワード検索（リアルタイム / インデックス切替）
- **サイト探索タブ**: URL入力 → アプリ内全画面ブラウザで表示・追加
- **サイト管理タブ**: サイト追加（追加時に自動クロール）・削除・手動クロール・クリップボード追加

## 検索の仕組み

- **リアルタイム**: 登録 URL のトップページのみ取得して検索
- **インデックス**: 事前クロール済みページ（最大30件）を SQLite FTS5 で全文検索
- サイト追加時に自動クロールが走るのでインデックス検索がすぐ使える

## WSL2 ネットワーク設定（再起動時に必要な場合あり）

WSL2 IP が変わったら PowerShell（管理者）で再設定が必要:
```powershell
# WSL2 の IP 確認（WSL2 側で）: ip addr show eth0
netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=8000 connectaddress=<WSL2のIP> connectport=8000
```

## UI実装ルール（必ず守ること）

新しいUI要素・ページ・タブを追加・変更する際は以下を常に適用すること：

- テキスト要素: `word-break: break-all; overflow-wrap: anywhere`
- コンテナ: `overflow-x: hidden; width: 100%; box-sizing: border-box`
- 画像・テーブル・大きい要素: `max-width: 100%; height: auto`
- プロキシページの自動ズームは INJECT_SCRIPT に実装済み（追加不要）

Safari PWA（iPhone）での表示が前提のため、画面からのはみ出しと折り返し未対応は許容しない。

## 残課題

- サイト探索のブラウザでブロックされるサイトへの対応
- 検索精度のさらなる改善
