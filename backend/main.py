import sqlite3
import re
import socket
import ipaddress
import urllib.request
import urllib.parse
import html
import json
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager
from pathlib import Path
from curl_cffi import requests as cffi_requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from typing import Optional

import os
DB_PATH = os.environ.get("DB_PATH", "sites.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT NOT NULL UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER)")
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    version = row["version"] if row else 0

    if version < 2:
        conn.execute("DROP TABLE IF EXISTS pages")
        conn.execute("""
            CREATE VIRTUAL TABLE pages USING fts5(
                site_id UNINDEXED,
                url,
                title,
                content,
                tokenize='trigram'
            )
        """)
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version (version) VALUES (2)")
        version = 2

    if version < 3:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE
            )
        """)
        conn.execute("ALTER TABLE sites ADD COLUMN group_id INTEGER REFERENCES groups(id) ON DELETE SET NULL")
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version (version) VALUES (3)")
        version = 3

    if version < 4:
        conn.execute("""
            CREATE TABLE sites_v4 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                group_id INTEGER REFERENCES groups(id) ON DELETE SET NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("INSERT INTO sites_v4 (id, name, url, group_id, created_at) SELECT id, name, url, group_id, created_at FROM sites")
        conn.execute("DROP TABLE sites")
        conn.execute("ALTER TABLE sites_v4 RENAME TO sites")
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version (version) VALUES (4)")

    if version < 5:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site_id INTEGER REFERENCES sites(id) ON DELETE CASCADE,
                url TEXT NOT NULL,
                title TEXT NOT NULL,
                published_at TEXT DEFAULT '',
                fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE TABLE IF NOT EXISTS topic_reads (url TEXT PRIMARY KEY)")
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version (version) VALUES (5)")
        version = 5

    if version < 6:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS read_later (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL DEFAULT '',
                site_name TEXT NOT NULL DEFAULT '',
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version (version) VALUES (6)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    if version < 7:
        # user_token を各テーブルに追加
        for stmt in [
            "ALTER TABLE sites ADD COLUMN user_token TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE groups ADD COLUMN user_token TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE topics ADD COLUMN user_token TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE read_later ADD COLUMN user_token TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE topic_reads ADD COLUMN user_token TEXT NOT NULL DEFAULT ''",
        ]:
            try: conn.execute(stmt)
            except: pass
        # settings テーブルを再作成（PRIMARY KEY を (user_token, key) に変更）
        conn.execute("CREATE TABLE IF NOT EXISTS settings_v7 (user_token TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL DEFAULT '', PRIMARY KEY (user_token, key))")
        conn.execute("INSERT OR IGNORE INTO settings_v7 (user_token, key, value) SELECT '', key, value FROM settings")
        conn.execute("DROP TABLE settings")
        conn.execute("ALTER TABLE settings_v7 RENAME TO settings")
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version (version) VALUES (7)")

    if version < 8:
        # groups.name の UNIQUE を (user_token, name) 複合ユニークに変更
        conn.execute("""
            CREATE TABLE groups_v8 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_token TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL,
                UNIQUE(user_token, name)
            )
        """)
        conn.execute("INSERT OR IGNORE INTO groups_v8 (id, user_token, name) SELECT id, user_token, name FROM groups")
        conn.execute("DROP TABLE groups")
        conn.execute("ALTER TABLE groups_v8 RENAME TO groups")
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version (version) VALUES (8)")

    conn.commit()
    conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Choice API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

from fastapi import Depends

def get_token(request: Request) -> str:
    token = request.headers.get("X-User-Token", "").strip()
    if not token or len(token) < 16:
        raise HTTPException(401, "ユーザートークンが必要です")
    return token

HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Choice">
<meta name="theme-color" content="#1a1a2e">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon-192.png">
<title>Choice</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html { overflow-x: clip; min-height: 100%; }
  body { font-family: -apple-system, sans-serif; background: #f0f4f8; color: #333; overflow-x: clip; min-height: 100vh; }
  .header {
    background: #1a1a2e; color: #fff;
    padding: env(safe-area-inset-top, 20px) 16px 16px;
    padding-top: calc(env(safe-area-inset-top, 20px) + 12px);
    display: flex; align-items: center; justify-content: space-between;
    position: fixed; top: 0; left: 0; right: 0; z-index: 100;
  }
  .header h1 { font-size: 20px; font-weight: bold; }
  .tab-btn {
    background: rgba(255,255,255,0.15); border: none; color: #fff;
    padding: 6px 14px; border-radius: 20px; font-size: 13px; cursor: pointer;
  }
  .tab-btn.active { background: #4a90d9; }
  .container { padding: 6px 12px 12px; max-width: 600px; margin: 0 auto; width: 100%; overflow-x: hidden; }

  /* Search */
  .search-box {
    display: flex; align-items: center; background: #fff;
    border-radius: 14px; padding: 10px 14px; gap: 8px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.08); margin-bottom: 10px;
  }
  .search-box input {
    flex: 1; border: none; outline: none; font-size: 16px; color: #333;
  }
  .mode-row {
    display: flex; align-items: center; gap: 8px; margin-bottom: 12px;
    font-size: 13px; color: #666;
  }
  .seg-ctrl {
    display: flex; background: #e8ecf0; border-radius: 10px; padding: 2px; gap: 2px;
  }
  .seg-btn {
    border: none; background: none; padding: 6px 12px; border-radius: 8px;
    font-size: 13px; color: #666; cursor: pointer; white-space: nowrap;
  }
  .seg-btn.active {
    background: #fff; color: #1a1a2e; font-weight: 600;
    box-shadow: 0 1px 3px rgba(0,0,0,0.12);
  }
  .search-btn {
    margin-left: auto; background: #1a1a2e; color: #fff;
    border: none; padding: 8px 20px; border-radius: 20px;
    font-size: 14px; font-weight: bold; cursor: pointer;
  }

  /* Cards */
  .card {
    background: #fff; border-radius: 14px; padding: 12px;
    margin-bottom: 8px; box-shadow: 0 2px 6px rgba(0,0,0,0.06);
    cursor: pointer; text-decoration: none; display: block; color: inherit;
    width: 100%; overflow: hidden; min-width: 0;
  }
  .card:active { opacity: 0.8; }
  .card-site { font-size: 11px; color: #4a90d9; font-weight: 600; margin-bottom: 3px; text-transform: uppercase; display:flex; align-items:center; gap:4px; }
  .favicon { width:14px; height:14px; border-radius:2px; flex-shrink:0; }
  .card-title { font-size: 15px; font-weight: bold; color: #1a1a2e; margin-bottom: 4px; word-break: break-all; overflow-wrap: anywhere; }
  .card-excerpt { font-size: 13px; color: #555; line-height: 1.4; margin-bottom: 4px; word-break: break-all; overflow-wrap: anywhere; }
  .card-url { font-size: 11px; color: #aaa; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .empty { text-align: center; padding: 32px 0; color: #bbb; font-size: 15px; }
  .empty-icon { font-size: 40px; margin-bottom: 8px; }

  /* Sites */
  .form-card { background: #fff; border-radius: 14px; padding: 10px 12px; margin-bottom: 6px; box-shadow: 0 2px 6px rgba(0,0,0,0.06); }
  .form-card h2 { font-size: 15px; font-weight: 700; color: #1a1a2e; margin-bottom: 6px; }
  .form-card input {
    width: 100%; border: 1px solid #dde3ec; border-radius: 10px;
    padding: 10px 12px; font-size: 16px; color: #333;
    margin-bottom: 10px; outline: none; background: #f9fbfc;
  }
  input, select, textarea { font-size: 16px; }
  .add-btn {
    width: 100%; background: #1a1a2e; color: #fff;
    border: none; padding: 12px; border-radius: 10px;
    font-size: 15px; font-weight: bold; cursor: pointer;
  }
  .site-item {
    background: #fff; border-radius: 12px; padding: 14px;
    margin-bottom: 8px; display: flex; align-items: center;
    box-shadow: 0 1px 4px rgba(0,0,0,0.05);
  }
  .site-info { flex: 1; }
  .site-name { font-size: 15px; font-weight: 700; color: #1a1a2e; }
  .site-url { font-size: 12px; color: #888; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 200px; }
  .site-actions { display: flex; gap: 14px; }
  .crawl-btn, .del-btn { background: none; border: none; font-size: 20px; cursor: pointer; padding: 4px; }
  .list-header { font-size: 13px; color: #888; font-weight: 600; padding: 4px 4px 8px; text-transform: uppercase; letter-spacing: 0.5px; }
  .hint { text-align: center; font-size: 12px; color: #bbb; padding: 16px; }
  .loading { text-align: center; padding: 24px; color: #888; }
  .setting-label { font-size: 12px; color: #888; display: block; margin-bottom: 4px; margin-top: 6px; }
  .setting-hint { font-size: 12px; color: #aaa; line-height: 1.5; margin-bottom: 8px; }
  .api-status { display: inline-block; font-size: 12px; padding: 3px 10px; border-radius: 10px; margin-left: 8px; }
  .api-status.ok { background: #e6f4ea; color: #2e7d32; }
  .api-status.ng { background: #fce8e6; color: #c62828; }
  .step-box { background: #f0f4f8; border-radius: 10px; padding: 10px 12px; font-size: 13px; line-height: 1.6; color: #444; margin-bottom: 8px; }
  .group-row { display:flex; gap:8px; overflow-x:auto; padding:0 2px 10px; scrollbar-width:none; margin-bottom:2px; }
  .group-row::-webkit-scrollbar { display:none; }
  .group-chip { flex-shrink:0; background:#e8ecf0; border:none; border-radius:20px; padding:6px 16px; font-size:13px; color:#666; cursor:pointer; white-space:nowrap; }
  .group-chip.active { background:#1a1a2e; color:#fff; font-weight:600; }
  .site-group-badge { display:inline-block; font-size:11px; color:#fff; background:#4a90d9; border-radius:8px; padding:1px 7px; margin-top:3px; }
  .folder-header { display:flex; align-items:center; gap:8px; padding:10px 12px; background:#fff; border-radius:12px; margin-bottom:4px; box-shadow:0 1px 4px rgba(0,0,0,0.06); cursor:pointer; user-select:none; }
  .folder-icon { font-size:18px; }
  .folder-name { flex:1; font-size:15px; font-weight:700; color:#1a1a2e; }
  .folder-count { font-size:12px; color:#aaa; }
  .folder-arrow { font-size:12px; color:#aaa; transition:transform .2s; }
  .folder-arrow.open { transform:rotate(90deg); }
  .folder-body { margin-bottom:10px; padding-left:12px; border-left:2px solid #e8ecf0; margin-left:10px; }
  .folder-site-item { display:flex; align-items:center; padding:10px 10px; background:#fff; border-radius:10px; margin-bottom:4px; box-shadow:0 1px 3px rgba(0,0,0,0.04); }
  .folder-site-item .site-info { flex:1; min-width:0; }
  .folder-site-item .site-actions { display:flex; gap:6px; align-items:center; }
  .page { display: none; }
  .page.active { display: block; }
  .tab-bar {
    position: fixed; bottom: 0; left: 0; right: 0; z-index: 500;
    background: #fff; border-top: 1px solid #e8ecf0;
    display: flex; padding-bottom: env(safe-area-inset-bottom, 0);
    box-shadow: 0 -2px 10px rgba(0,0,0,0.06);
  }
  .tab {
    flex: 1; display: flex; flex-direction: column; align-items: center;
    padding: 10px; border: none; background: none; cursor: pointer;
    color: #aaa; font-size: 10px; gap: 3px;
  }
  .tab.active { color: #1a1a2e; }
  .tab-icon { font-size: 22px; }
  .main-content {
    padding-top: calc(env(safe-area-inset-top, 20px) + 54px);
    padding-bottom: calc(70px + env(safe-area-inset-bottom, 0));
  }
  .quick-btn {
    background: #fff; border: 1px solid #dde3ec; border-radius: 20px;
    padding: 7px 14px; font-size: 13px; color: #444; cursor: pointer;
  }
  .quick-btn:active { background: #f0f4f8; }
  .badge {
    background: #e27d60; color: #fff; border-radius: 10px;
    font-size: 10px; padding: 1px 5px; margin-left: 4px;
  }
  /* ── Dark mode ───────────────────────────────────────── */
  [data-theme=dark] body { background:#111320; color:#d0d5e8; }
  [data-theme=dark] .header { background:#0d0f1e; }
  [data-theme=dark] .search-box { background:#1c1f35; box-shadow:0 2px 8px rgba(0,0,0,0.4); }
  [data-theme=dark] .search-box input { color:#d0d5e8; background:transparent; }
  [data-theme=dark] .seg-ctrl { background:#151828; }
  [data-theme=dark] .seg-btn { color:#8890b0; }
  [data-theme=dark] .seg-btn.active { background:#1c1f35; color:#d0d5e8; box-shadow:0 1px 3px rgba(0,0,0,0.4); }
  [data-theme=dark] .card { background:#1c1f35; }
  [data-theme=dark] .card-title { color:#d0d5e8; }
  [data-theme=dark] .card-excerpt { color:#8890b0; }
  [data-theme=dark] .card-url { color:#505870; }
  [data-theme=dark] .form-card { background:#1c1f35; }
  [data-theme=dark] .form-card h2 { color:#d0d5e8; }
  [data-theme=dark] .form-card input,
  [data-theme=dark] .form-card select { background:#151828; color:#d0d5e8; border-color:#2a2d45; }
  [data-theme=dark] .add-btn { background:#2a3060; }
  [data-theme=dark] .tab-bar { background:#1c1f35; border-top-color:#2a2d45; }
  [data-theme=dark] .tab { color:#505870; }
  [data-theme=dark] .tab.active { color:#d0d5e8; }
  [data-theme=dark] .site-item { background:#1c1f35; }
  [data-theme=dark] .site-name { color:#d0d5e8; }
  [data-theme=dark] .site-url { color:#7880a0; }
  [data-theme=dark] .list-header { color:#7880a0; }
  [data-theme=dark] .hint { color:#505870; }
  [data-theme=dark] .loading { color:#7880a0; }
  [data-theme=dark] .empty { color:#7880a0; }
  [data-theme=dark] .setting-label { color:#7880a0; }
  [data-theme=dark] .setting-hint { color:#505870; }
  [data-theme=dark] .step-box { background:#151828; color:#8890b0; }
  [data-theme=dark] .quick-btn { background:#1c1f35; border-color:#2a2d45; color:#8890b0; }
  [data-theme=dark] .group-chip { background:#1c1f35; color:#8890b0; }
  [data-theme=dark] .group-chip.active { background:#2a3060; color:#fff; }
  [data-theme=dark] .folder-header { background:#1c1f35; }
  [data-theme=dark] .folder-name { color:#d0d5e8; }
  [data-theme=dark] .folder-count { color:#505870; }
  [data-theme=dark] .folder-arrow { color:#505870; }
  [data-theme=dark] .folder-body { border-left-color:#2a2d45; }
  [data-theme=dark] .folder-site-item { background:#1c1f35; }
  [data-theme=dark] .api-status.ok { background:#1a3028; color:#4caf50; }
  [data-theme=dark] .api-status.ng { background:#301820; color:#ef5350; }
  [data-theme=dark] #preset-overlay { background:#111320; }
  [data-theme=dark] #browser-overlay { background:#0d0f1e; }
  [data-theme=dark] #search-history { color:#8890b0; }
  [data-theme=dark] select { background:#1c1f35; color:#d0d5e8; border-color:#2a2d45; }
</style>
</head>
<body>

<div id="ptr-indicator" style="position:fixed;top:-56px;left:0;right:0;z-index:1000;height:56px;display:flex;align-items:center;justify-content:center;gap:8px;background:#1a1a2e;color:#fff;font-size:13px;transition:top 0.15s ease">
  <span id="ptr-icon" style="display:inline-block;font-size:18px;transition:transform 0.2s">↓</span>
  <span id="ptr-label">引っ張って更新</span>
</div>

<div class="header">
  <h1>Choice</h1>
</div>

<div class="main-content">
  <!-- Search page -->
  <div class="page active" id="page-search">
    <div class="container">
      <div style="height:4px"></div>
      <div class="search-box">
        <span>🔍</span>
        <input type="search" id="query" data-i18n="search_ph" placeholder="キーワードを入力..." onkeydown="if(event.key==='Enter')doSearch()">
        <button onclick="clearSearch()" id="clear-btn" style="background:none;border:none;font-size:18px;color:#aaa;display:none">✕</button>
        <button onclick="doSearch()" data-i18n="search_btn" style="background:#1a1a2e;color:#fff;border:none;padding:8px 18px;border-radius:20px;font-size:14px;font-weight:bold;cursor:pointer;white-space:nowrap;flex-shrink:0">検索</button>
      </div>
      <div class="mode-row" style="margin-bottom:8px">
        <div class="seg-ctrl">
          <button class="seg-btn active" id="mode-web" onclick="setMode('web')" data-i18n="mode_web">🌐 Web</button>
          <button class="seg-btn" id="mode-index" onclick="setMode('index')" data-i18n="mode_index">📚 インデックス</button>
        </div>
      </div>
      <div style="font-size:11px;color:#aaa;padding:0 2px 4px" data-i18n="group_filter_label">対象グループ</div>
      <div class="group-row" id="group-filter-row">
        <button class="group-chip active" id="group-chip-all" onclick="setGroupFilter(null)" data-i18n="group_all">すべて</button>
      </div>
      <div id="search-history" style="margin-bottom:10px;display:none"></div>
      <div id="results"></div>
    </div>
  </div>

  <!-- Sites page (サイト追加 + サイト管理 統合) -->
  <div class="page" id="page-sites">
    <div class="container">
      <div style="height:4px"></div>
      <div class="search-box">
        <span>🌐</span>
        <input type="text" id="explore-input" data-i18n="explore_ph" placeholder="URLまたはキーワードを入力..." onkeydown="if(event.key==='Enter')exploreGo()">
        <button onclick="clearExplore()" id="explore-clear-btn" style="background:none;border:none;font-size:18px;color:#aaa;display:none">✕</button>
      </div>
      <div style="display:flex;gap:8px;margin-bottom:12px">
        <button onclick="exploreGo()" class="search-btn" data-i18n="open_btn" style="margin-left:0">開く</button>
        <button onclick="addFromInput()" data-i18n="add_from_btn" style="background:#e27d60;color:#fff;border:none;padding:8px 16px;border-radius:20px;font-size:13px;font-weight:bold;cursor:pointer">＋追加</button>
        <button onclick="openPresets()" style="background:#4a90d9;color:#fff;border:none;padding:8px 14px;border-radius:20px;font-size:13px;font-weight:bold;cursor:pointer;white-space:nowrap">🔍 ジャンル</button>
        <button onclick="pasteFromClipboard()" style="background:#f0f4f8;color:#555;border:1px solid #dde3ec;padding:8px 14px;border-radius:20px;font-size:13px;font-weight:bold;cursor:pointer;white-space:nowrap">📋</button>
      </div>
      <div id="explore-results"></div>
      <div id="site-search-results"></div>
      <div class="list-header" id="sites-header" data-i18n="sites_label">登録済みサイト</div>
      <div id="sites-list"></div>
      <div class="hint" data-i18n="crawl_hint">↺ ボタン: インデックス検索用にクロール</div>

      <div class="form-card" style="margin-top:4px">
        <h2 data-i18n="group_mgmt_title">📁 グループ管理</h2>
        <div style="display:flex;gap:8px;margin-bottom:10px">
          <input type="text" id="group-name-input" data-i18n="group_name_ph" placeholder="グループ名" style="flex:1;border:1px solid #dde3ec;border-radius:10px;padding:9px 12px;font-size:16px;outline:none;background:#f9fbfc">
          <button onclick="addGroup()" style="background:#1a1a2e;color:#fff;border:none;padding:9px 16px;border-radius:10px;font-size:14px;font-weight:bold;cursor:pointer;white-space:nowrap" data-i18n="group_add_btn">追加</button>
        </div>
        <div id="groups-list"></div>
      </div>

      <div class="form-card" style="margin-top:4px">
        <h2 data-i18n="shortcut_title">⚡ iOS ショートカットで追加（推奨）</h2>
        <p style="font-size:13px;color:#666;margin-bottom:12px;line-height:1.7" data-i18n="shortcut_intro">
          一度設定すれば、Safari の 共有ボタン → ショートカット名 をタップするだけで追加できます。
        </p>
        <div style="background:#f0f4f8;border-radius:10px;padding:12px;font-size:13px;line-height:1.8;color:#444;margin-bottom:12px">
          <span data-i18n-html="shortcut_steps_pre"><b>設定手順</b><br>
          1. iPhone の「ショートカット」アプリを開く<br>
          2. 右上の <b>＋</b> をタップ<br>
          3. 「アクションを追加」→「URL を開く」を選択<br>
          4. URL 欄に以下を入力:</span>
          <div style="background:#fff;border-radius:6px;padding:8px;margin:6px 0;font-family:monospace;font-size:12px;word-break:break-all" id="shortcut-url"></div>
          <span data-i18n-html="shortcut_steps_post">5. 「ショートカット名」を <b>Choice に追加</b> にする<br>
          6. 右上の <b>完了</b> をタップ<br>
          7. ショートカットの詳細 →「共有シートに表示」をオン</span>
        </div>
        <p style="font-size:12px;color:#aaa;line-height:1.6" data-i18n="shortcut_tip">
          ※ 使い方: Safari でサイトを開く → 共有ボタン →「Choice に追加」→ 名前を確認して追加
        </p>
      </div>

      <div class="form-card" style="margin-top:4px">
        <h2 data-i18n="clipboard_title">📋 クリップボードから追加</h2>
        <p style="font-size:13px;color:#666;margin-bottom:12px;line-height:1.6" data-i18n="clipboard_desc">
          Safari でサイトを開いて URL をコピー → ここに戻って下のボタンをタップ
        </p>
        <button class="add-btn" onclick="pasteFromClipboard()" style="background:#4a90d9" data-i18n="clipboard_btn">
          📋 コピーした URL を貼り付け
        </button>
      </div>
    </div>
  </div>
</div>

  <!-- Topics / Read Later page -->
  <div class="page" id="page-topics">
    <div class="container" style="padding-top:6px">
      <div style="background:#fff;border-radius:12px;box-shadow:0 2px 8px rgba(0,0,0,0.06);padding:8px;margin-bottom:8px">
        <div class="seg-ctrl" style="width:100%">
          <button class="seg-btn active" style="flex:1" id="content-seg-topics" onclick="showContentSeg('topics')" data-i18n="topics_tab">📰 トピック</button>
          <button class="seg-btn" style="flex:1" id="content-seg-later" onclick="showContentSeg('later')" data-i18n="later_tab">📌 あとで読む</button>
        </div>
      </div>
      <div id="topics-section">
        <div style="display:flex;gap:8px;margin-bottom:8px;align-items:center">
          <select id="topic-group-select" style="flex:1;border:1px solid #dde3ec;border-radius:10px;padding:9px 12px;font-size:16px;color:#333;background:#fff;outline:none">
            <option value="" data-i18n="topics_all">すべて</option>
          </select>
          <button onclick="refreshTopics()" id="topic-refresh-btn" style="background:#1a1a2e;color:#fff;border:none;padding:9px 16px;border-radius:10px;font-size:14px;font-weight:bold;cursor:pointer;white-space:nowrap" data-i18n="topics_refresh_btn">🔄 更新</button>
        </div>
        <div id="topic-results"></div>
      </div>
      <div id="later-section" style="display:none">
        <div id="later-results"></div>
      </div>
    </div>
  </div>

  <!-- Settings page -->
  <div class="page" id="page-settings">
    <div class="container">
      <div style="height:2px"></div>

      <div class="form-card" id="license-card">
        <h2>🔑 ライセンス
          <span id="license-badge" style="font-size:11px;font-weight:700;padding:2px 8px;border-radius:10px;margin-left:8px"></span>
        </h2>
        <div id="license-free-section">
          <p style="font-size:13px;color:#888;margin-bottom:10px">プレミアムキーを入力するとすべての機能が使えます。</p>
          <input type="text" id="license-key-input" placeholder="XXXXXXXX-XXXX-XXXX-XXXX" style="font-size:16px;font-family:monospace;letter-spacing:1px">
          <button class="add-btn" onclick="activateLicense()" style="margin-top:8px">認証する</button>
          <p id="license-error" style="font-size:12px;color:#e53935;margin-top:6px;display:none"></p>
        </div>
        <div id="license-premium-section" style="display:none">
          <p style="font-size:13px;color:#4caf50;margin-bottom:10px">✅ プレミアム版が有効です。</p>
          <p id="license-key-hint" style="font-size:12px;color:#aaa;font-family:monospace;margin-bottom:10px"></p>
          <button onclick="deactivateLicense()" style="background:none;border:1px solid #dde3ec;color:#888;padding:6px 14px;border-radius:10px;font-size:13px;cursor:pointer">ライセンスを解除</button>
        </div>
      </div>

      <div class="form-card">
        <h2 data-i18n="api_title">🔍 検索 API
          <span class="api-status" id="api-status"></span>
        </h2>
        <div class="seg-ctrl" style="width:100%;margin-bottom:10px">
          <button class="seg-btn" style="flex:1" id="provider-yahoo"  onclick="setProvider('yahoo')">DuckDuckGo</button>
          <button class="seg-btn" style="flex:1" id="provider-google" onclick="setProvider('google')">Google</button>
          <button class="seg-btn" style="flex:1" id="provider-brave"  onclick="setProvider('brave')">Brave</button>
        </div>

        <!-- Brave -->
        <div id="pane-brave">
          <div class="step-box" data-i18n-html="brave_steps">
            <b>無料 2,000回/月</b><br>
            1. <b>brave.com/search/api</b> を開く<br>
            2. 「Get Started for Free」→ アカウント作成<br>
            3. 表示された API キーをコピー
          </div>
          <span class="setting-label" data-i18n="api_key_label">API キー</span>
          <input type="text" id="setting-brave-key" placeholder="BSA...">
        </div>

        <!-- Google CSE -->
        <div id="pane-google" style="display:none">
          <div class="step-box" data-i18n-html="google_steps">
            <b>無料 100回/日</b><br>
            <b>① API キー</b><br>
            1. console.cloud.google.com を開く<br>
            2. 「APIとサービス」→「認証情報」→「APIキーを作成」<br><br>
            <b>② 検索エンジン ID</b><br>
            1. programmablesearchengine.google.com を開く<br>
            2. 新規作成 →「ウェブ全体を検索」ON → IDをコピー
          </div>
          <span class="setting-label" data-i18n="api_key_label">API キー</span>
          <input type="text" id="setting-google-key" placeholder="AIzaSy...">
          <span class="setting-label" data-i18n="google_cx_label">検索エンジン ID（cx）</span>
          <input type="text" id="setting-google-cx" placeholder="a1b2c3...">
        </div>

        <!-- DuckDuckGo -->
        <div id="pane-yahoo" style="display:none">
          <div class="step-box" style="color:#666" id="yahoo-note" data-i18n-html="yahoo_note">
            APIキー不要。DuckDuckGo 経由で検索します。<br>
            スニペット（説明文）は表示されません。
          </div>
        </div>

        <button class="add-btn" id="save-btn" onclick="saveSettings()" data-i18n="save_btn" style="margin-top:4px">保存する</button>
      </div>

      <div class="form-card">
        <h2 data-i18n="lang_title">🌐 表示言語</h2>
        <div class="seg-ctrl" style="width:100%;justify-content:stretch">
          <button class="seg-btn" style="flex:1" id="lang-ja" onclick="setLang('ja')">日本語</button>
          <button class="seg-btn" style="flex:1" id="lang-en" onclick="setLang('en')">English</button>
          <button class="seg-btn" style="flex:1" id="lang-zh" onclick="setLang('zh')">中文</button>
        </div>
      </div>

      <div class="form-card">
        <h2 data-i18n="darkmode_title">🌙 ダークモード</h2>
        <div class="seg-ctrl" style="width:100%;justify-content:stretch">
          <button class="seg-btn active" style="flex:1" id="darkmode-off" onclick="setDarkMode('off')" data-i18n="darkmode_off">オフ</button>
          <button class="seg-btn" style="flex:1" id="darkmode-on" onclick="setDarkMode('on')" data-i18n="darkmode_on">オン</button>
        </div>
      </div>

      <div class="form-card">
        <h2 data-i18n="crawl_auto_title">🔄 サイト追加時の自動クロール</h2>
        <p class="setting-hint" data-i18n="crawl_auto_hint">「する」にするとサイト追加時に自動でインデックスを作成します。時間がかかる場合があります。</p>
        <div class="seg-ctrl" style="width:100%;justify-content:stretch">
          <button class="seg-btn" style="flex:1" id="autocrawl-off" onclick="setAutoCrawl('off')" data-i18n="crawl_auto_off">しない</button>
          <button class="seg-btn" style="flex:1" id="autocrawl-on"  onclick="setAutoCrawl('on')"  data-i18n="crawl_auto_on">する</button>
        </div>
      </div>

      <div class="form-card">
        <h2>🔗 デバイス同期</h2>
        <p style="font-size:13px;color:#888;margin-bottom:10px">同期URLを別のデバイスで開くとデータを引き継げます。</p>
        <button class="add-btn" onclick="copySyncUrl()" style="margin-bottom:12px">同期URLをコピー</button>
        <p style="font-size:12px;color:#aaa;margin-bottom:6px">別デバイスから復元する場合はトークンを貼り付け：</p>
        <input type="text" id="sync-token-input" placeholder="トークンを貼り付け（32文字以上）" style="font-family:monospace;font-size:16px">
        <button class="add-btn" onclick="applySyncToken()" style="margin-top:8px">このトークンで復元</button>
      </div>

      <div class="form-card" style="background:#f9fbfc">
        <p style="font-size:13px;color:#888;line-height:1.7" id="api-note-footer" data-i18n="api_note_footer">
          APIキーはこのサーバー内にのみ保存されます。
        </p>
      </div>
    </div>
  </div>

<!-- プリセットオーバーレイ -->
<div id="preset-overlay" style="display:none;position:fixed;inset:0;z-index:850;background:#1a1a2e;flex-direction:column">
  <div style="height:env(safe-area-inset-top,20px);flex-shrink:0"></div>
  <div style="padding:8px 12px;display:flex;align-items:center;gap:8px;flex-shrink:0">
    <button onclick="closePresets()" style="background:none;border:none;color:#fff;font-size:15px;padding:4px 8px;cursor:pointer;white-space:nowrap">✕ 閉じる</button>
    <span style="flex:1;color:#fff;font-weight:bold;font-size:16px">🔍 サイトを探す</span>
  </div>
  <div style="overflow-y:auto;flex:1;padding:12px;background:#f0f4f8" id="preset-list"></div>
</div>

<!-- 全画面ブラウザオーバーレイ -->
<div id="browser-overlay" style="display:none;position:fixed;inset:0;z-index:900;background:#1a1a2e;flex-direction:column">
  <!-- safe-area スペーサー: ノッチ・Dynamic Island の高さを確保 -->
  <div style="height:env(safe-area-inset-top,20px);flex-shrink:0"></div>
  <!-- ヘッダー -->
  <div id="browser-header" style="display:flex;align-items:center;gap:6px;padding:8px 10px;flex-shrink:0">
    <button onclick="closeBrowser()" data-i18n="close_btn" style="background:none;border:none;color:#fff;font-size:15px;padding:4px 8px;cursor:pointer;white-space:nowrap">✕ 閉じる</button>
    <div id="browser-url" style="flex:1;background:rgba(255,255,255,0.15);border-radius:10px;padding:6px 10px;font-size:12px;color:#ddd;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></div>
    <button id="browser-later-btn" onclick="saveBrowserPageForLater()" style="background:none;border:none;color:#ddd;font-size:20px;padding:4px 2px;cursor:pointer;flex-shrink:0" title="あとで読む">📌</button>
    <button id="browser-add-btn" onclick="addBrowserSite()" data-i18n="add_site_btn" style="background:#e27d60;border:none;color:#fff;font-size:13px;font-weight:bold;padding:6px 12px;border-radius:16px;cursor:pointer;white-space:nowrap">＋追加</button>
  </div>
  <iframe id="browser-frame" src="about:blank" style="flex:1;width:100%;border:none;background:#fff" sandbox="allow-scripts allow-same-origin allow-forms allow-popups"></iframe>
</div>

<div class="tab-bar">
  <button class="tab active" id="tab-search" onclick="showPage('search')">
    <span class="tab-icon">🔍</span>
    <span data-i18n="tab_search">検索</span>
  </button>
  <button class="tab" id="tab-sites" onclick="showPage('sites')">
    <span class="tab-icon">🌐</span>
    <span data-i18n="tab_sites">サイト管理</span>
  </button>
  <button class="tab" id="tab-topics" onclick="showPage('topics')">
    <span class="tab-icon">📰</span>
    <span data-i18n="tab_topics">トピック</span>
  </button>
  <button class="tab" id="tab-settings" onclick="showPage('settings')">
    <span class="tab-icon">⚙️</span>
    <span data-i18n="tab_settings">設定</span>
  </button>
</div>

<script>
const API = '';
let searchMode = 'web';
let currentGroupId = null;  // null = すべて

// ── グループフィルター（検索タブ）────────────────────────
async function loadGroupChips(groups) {
  if (!groups) groups = await api('/groups');
  const row = document.getElementById('group-filter-row');
  const allBtn = document.getElementById('group-chip-all');
  [...row.children].forEach(el => { if (el.id !== 'group-chip-all') el.remove(); });
  groups.forEach(g => {
    const btn = document.createElement('button');
    btn.className = 'group-chip' + (currentGroupId === g.id ? ' active' : '');
    btn.id = 'group-chip-' + g.id;
    btn.textContent = g.name;
    btn.onclick = () => setGroupFilter(g.id);
    row.appendChild(btn);
  });
  allBtn.textContent = t('group_all');
}

function setGroupFilter(groupId) {
  currentGroupId = groupId;
  document.querySelectorAll('.group-chip').forEach(el => el.classList.remove('active'));
  const active = groupId ? document.getElementById('group-chip-' + groupId) : document.getElementById('group-chip-all');
  if (active) active.classList.add('active');
}

// ── グループ管理（サイト管理タブ）───────────────────────
async function loadGroupsList() {
  const groups = await api('/groups');
  const el = document.getElementById('groups-list');
  if (!groups.length) {
    el.innerHTML = `<div style="font-size:13px;color:#bbb;padding:8px 0">${t('no_groups')}</div>`;
  } else {
    el.innerHTML = groups.map(g => `
      <div style="display:flex;align-items:center;padding:8px 0;border-bottom:1px solid #f0f4f8">
        <span style="flex:1;font-size:14px;color:#333">${esc(g.name)}</span>
        <span style="font-size:12px;color:#aaa;margin-right:8px">${g.site_count}${t('sites_unit')}</span>
        <button onclick="renameGroup(${g.id},'${esc(g.name)}')" style="background:none;border:none;font-size:16px;cursor:pointer;color:#ccc;margin-right:2px">✏️</button>
        <button onclick="deleteGroup(${g.id},'${esc(g.name)}')" style="background:none;border:none;font-size:18px;cursor:pointer;color:#ccc">🗑️</button>
      </div>`).join('');
  }
  await loadGroupChips(groups);
}

async function addGroup() {
  const input = document.getElementById('group-name-input');
  const name = input.value.trim();
  if (!name) return;
  const res = await api('/groups', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name})});
  if (res.detail) { alert(res.detail); return; }
  input.value = '';
  await loadGroupsList();
}

async function renameGroup(id, currentName) {
  const newName = prompt(t('rename_group_prompt'), currentName);
  if (!newName || newName.trim() === currentName) return;
  const res = await api('/groups/' + id, {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name: newName.trim()})});
  if (res.detail) { alert(res.detail); return; }
  await loadGroupsList();
  loadSites();
}

async function deleteGroup(id, name) {
  if (!confirm(t('group_delete_confirm'))) return;
  await api('/groups/' + id, {method:'DELETE'});
  if (currentGroupId === id) setGroupFilter(null);
  loadGroupsList();
  loadSites();
}

function setMode(mode) {
  searchMode = mode;
  ['web','index'].forEach(m => document.getElementById('mode-'+m)?.classList.remove('active'));
  document.getElementById('mode-' + mode)?.classList.add('active');
}

function getOrCreateToken() {
  const sync = new URLSearchParams(location.search).get('sync');
  if (sync && sync.length >= 16) {
    localStorage.setItem('ch_token', sync);
    history.replaceState(null, '', location.pathname);
  }
  let t = localStorage.getItem('ch_token');
  if (!t || t.length < 16) {
    t = 'xxxxxxxxxxxx4xxxyxxxxxxxxxxxxxxx'.replace(/[xy]/g, c => {
      const r = Math.random() * 16 | 0;
      return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
    });
    localStorage.setItem('ch_token', t);
  }
  return t;
}
const USER_TOKEN = getOrCreateToken();

async function api(path, opts={}) {
  opts.headers = Object.assign({'X-User-Token': USER_TOKEN}, opts.headers || {});
  const res = await fetch(API + path, opts);
  if (!res.ok) {
    let msg = `エラー (${res.status})`;
    try { const j = await res.json(); msg = j.detail || j.message || msg; } catch(_) {}
    throw new Error(msg);
  }
  return res.json();
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function getFavicon(url) {
  try { return 'https://www.google.com/s2/favicons?domain=' + new URL(url).hostname + '&sz=32'; }
  catch(e) { return ''; }
}

function showPage(name) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  document.getElementById('tab-' + name).classList.add('active');
  window.scrollTo(0, 0);
  localStorage.setItem('ch_page', name);
  if (name === 'search') renderSearchHistory();
  if (name === 'sites') { loadSites(); loadGroupsList(); }
  if (name === 'topics') loadTopics();
  if (name === 'settings') loadSettings();
}

// ── i18n ──────────────────────────────────────────────
const T = {
  ja: {
    tab_search:'検索', tab_explore:'サイト追加', tab_sites:'サイト管理', tab_topics:'トピック', tab_settings:'設定',
    search_ph:'キーワードを入力...', search_btn:'検索',
    mode_web:'🌐 Web', mode_index:'📚 インデックス',
    loading:'🔍 検索中...', no_results:'結果が見つかりませんでした', error_msg:'エラーが発生しました',
    add_site_title:'検索対象サイトを追加', name_ph:'サイト名', url_ph:'URL',
    add_btn:'追加する', sites_label:'登録済みサイト', crawl_hint:'↺ ボタン: インデックス用にクロール',
    close_btn:'✕ 閉じる', add_site_btn:'＋追加',
    explore_ph:'URLまたはキーワードを入力...', open_btn:'開く', add_from_btn:'＋追加',
    api_title:'🔍 検索 API', lang_title:'🌐 表示言語',
    save_btn:'保存する', saving:'保存中...', saved:'保存しました',
    api_ok:'設定済み', api_ng:'未設定',
    key_ph:'APIキーを入力してください', key_cx_ph:'APIキーと検索エンジンIDを両方入力してください',
    no_api_note:'APIキー未設定の場合は DuckDuckGo 経由で検索します（スニペットなし）。APIキーはこのサーバー内にのみ保存されます。',
    crawl_auto_title:'🔄 サイト追加時の自動クロール', crawl_auto_hint:'「する」にするとサイト追加時に自動でインデックスを作成します。時間がかかる場合があります。', crawl_auto_off:'しない', crawl_auto_on:'する',
    adding:'追加中...', adding_crawl:'追加＆クロール中...',
    group_filter_label:'対象グループ', group_all:'すべて', group_mgmt_title:'📁 グループ管理', group_name_ph:'グループ名', group_add_btn:'追加', group_none:'グループなし', group_delete_confirm:'このグループを削除しますか？（サイトはグループなしになります）',
    explore_open_sites:'登録済みサイトを開く', preset_title:'📦 プリセットから追加', preset_desc:'カテゴリを選んでサイトをまとめて登録できます。', preset_btn:'📦 プリセットを見る',
    shortcut_title:'⚡ iOS ショートカットで追加（推奨）', clipboard_title:'📋 クリップボードから追加', clipboard_btn:'📋 コピーした URL を貼り付け',
    topics_tab:'📰 トピック', later_tab:'📌 あとで読む', topics_all:'すべて', topics_refresh_btn:'🔄 更新', topics_refreshing:'取得中...',
    darkmode_title:'🌙 ダークモード', darkmode_off:'オフ', darkmode_on:'オン',
    api_key_label:'API キー', google_cx_label:'検索エンジン ID（cx）', api_note_footer:'APIキーはこのサーバー内にのみ保存されます。',
    no_sites:'サイトがまだ登録されていません', no_explore_sites:'登録済みサイトはありません', no_groups:'グループがまだありません', sites_unit:'サイト',
    no_topics:'まだコンテンツがありません', topics_push_refresh:'更新ボタンを押してください', already_read:'✓ 既読',
    history_label:'最近の検索', history_clear:'クリア', later_empty:'あとで読むリストは空です', title_save_later:'あとで読む',
    crawl_failed:'クロールに失敗しました', add_failed:'追加に失敗しました',
    alert_name_url:'名前とURLを入力してください', alert_url_required:'URLを入力してください', alert_url_invalid:'正しいURLを入力してください',
    alert_copy_url_first:'URLをコピーしてから試してください', alert_paste_done:'URLを貼り付けました。サイト名を確認して「追加する」を押してください。',
    prompt_paste_url:'URLを貼り付けてください:', rename_group_prompt:'グループ名を変更',
    preset_adding:'追加中...', preset_added:'✓ 追加済み', preset_error:'エラー', preset_loading:'読み込み中...',
    confirm_delete_site:'「{0}」を削除しますか？', confirm_crawl:'「{0}」をクロールしますか？\\n（最大30ページ、少し時間がかかります）', crawl_done:'クロール完了: {0}ページをインデックスしました',
    explore_howto:'<b style="color:#1a1a2e">💡 使い方</b><br>① URL またはキーワードを入力して「開く」<br>② ページを確認して「＋追加」でサイトを登録<br>③ 登録済みサイトはグループ別に一覧表示されます<br>④ グループの追加・管理は「サイト管理」タブから行えます',
    brave_steps:'<b>無料 2,000回/月</b><br>1. <b>brave.com/search/api</b> を開く<br>2. 「Get Started for Free」→ アカウント作成<br>3. 表示された API キーをコピー',
    google_steps:'<b>無料 100回/日</b><br><b>① API キー</b><br>1. console.cloud.google.com を開く<br>2. 「APIとサービス」→「認証情報」→「APIキーを作成」<br><br><b>② 検索エンジン ID</b><br>1. programmablesearchengine.google.com を開く<br>2. 新規作成 →「ウェブ全体を検索」ON → IDをコピー',
    yahoo_note:'APIキー不要。DuckDuckGo 経由で検索します。<br>スニペット（説明文）は表示されません。',
    shortcut_intro:'一度設定すれば、Safari の 共有ボタン → ショートカット名 をタップするだけで追加できます。',
    shortcut_steps_pre:'<b>設定手順</b><br>1. iPhone の「ショートカット」アプリを開く<br>2. 右上の <b>＋</b> をタップ<br>3. 「アクションを追加」→「URL を開く」を選択<br>4. URL 欄に以下を入力:',
    shortcut_steps_post:'5. 「ショートカット名」を <b>Choice に追加</b> にする<br>6. 右上の <b>完了</b> をタップ<br>7. ショートカットの詳細 →「共有シートに表示」をオン',
    shortcut_tip:'※ 使い方: Safari でサイトを開く → 共有ボタン →「Choice に追加」→ 名前を確認して追加',
    clipboard_desc:'Safari でサイトを開いて URL をコピー → ここに戻って下のボタンをタップ',
    preset_overlay_title:'📦 プリセットから追加',
  },
  en: {
    tab_search:'Search', tab_explore:'Add Site', tab_sites:'Sites', tab_topics:'Topics', tab_settings:'Settings',
    search_ph:'Enter keyword...', search_btn:'Search',
    mode_web:'🌐 Web', mode_index:'📚 Index',
    loading:'🔍 Searching...', no_results:'No results found', error_msg:'An error occurred',
    add_site_title:'Add Search Site', name_ph:'Site name', url_ph:'URL',
    add_btn:'Add', sites_label:'Registered Sites', crawl_hint:'↺ button: Crawl for index',
    close_btn:'✕ Close', add_site_btn:'＋Add',
    explore_ph:'Enter URL or keyword...', open_btn:'Open', add_from_btn:'＋Add',
    api_title:'🔍 Search API', lang_title:'🌐 Display Language',
    save_btn:'Save', saving:'Saving...', saved:'Saved',
    api_ok:'Configured', api_ng:'Not set',
    key_ph:'Please enter an API key', key_cx_ph:'Please enter both API key and Engine ID',
    no_api_note:'Without an API key, DuckDuckGo is used (no snippets). API keys are stored on this server only.',
    crawl_auto_title:'🔄 Auto-crawl on Site Add', crawl_auto_hint:'When ON, newly added sites are crawled automatically. This may take a moment.', crawl_auto_off:'Off', crawl_auto_on:'On',
    adding:'Adding...', adding_crawl:'Adding & crawling...',
    group_filter_label:'Search group', group_all:'All', group_mgmt_title:'📁 Group Management', group_name_ph:'Group name', group_add_btn:'Add', group_none:'No group', group_delete_confirm:'Delete this group? (Sites will become ungrouped)',
    explore_open_sites:'Open registered sites', preset_title:'📦 Add from Presets', preset_desc:'Select categories to register sites in bulk.', preset_btn:'📦 Browse Presets',
    shortcut_title:'⚡ Add via iOS Shortcut (Recommended)', clipboard_title:'📋 Add from Clipboard', clipboard_btn:'📋 Paste Copied URL',
    topics_tab:'📰 Topics', later_tab:'📌 Read Later', topics_all:'All', topics_refresh_btn:'🔄 Refresh', topics_refreshing:'Loading...',
    darkmode_title:'🌙 Dark Mode', darkmode_off:'Off', darkmode_on:'On',
    api_key_label:'API Key', google_cx_label:'Search Engine ID (cx)', api_note_footer:'API keys are stored on this server only.',
    no_sites:'No sites registered yet', no_explore_sites:'No registered sites', no_groups:'No groups yet', sites_unit:' site(s)',
    no_topics:'No content yet', topics_push_refresh:'Press the refresh button', already_read:'✓ Read',
    history_label:'Recent searches', history_clear:'Clear', later_empty:'Read Later list is empty', title_save_later:'Read Later',
    crawl_failed:'Crawl failed', add_failed:'Failed to add',
    alert_name_url:'Enter name and URL', alert_url_required:'Enter a URL', alert_url_invalid:'Enter a valid URL',
    alert_copy_url_first:'Copy a URL first', alert_paste_done:'URL pasted. Check the site name and tap "Add".',
    prompt_paste_url:'Paste URL:', rename_group_prompt:'Rename group',
    preset_adding:'Adding...', preset_added:'✓ Added', preset_error:'Error', preset_loading:'Loading...',
    confirm_delete_site:'Delete "{0}"?', confirm_crawl:'Crawl "{0}"?\\n(Up to 30 pages, may take a moment)', crawl_done:'Done: indexed {0} pages',
    explore_howto:'<b style="color:#1a1a2e">💡 How to Use</b><br>① Enter a URL or keyword and tap "Open"<br>② Check the page and tap "＋Add" to register<br>③ Registered sites are listed by group<br>④ Manage groups in the "Sites" tab',
    brave_steps:'<b>Free 2,000/month</b><br>1. Open <b>brave.com/search/api</b><br>2. "Get Started for Free" → Create account<br>3. Copy the API key shown',
    google_steps:'<b>Free 100/day</b><br><b>① API Key</b><br>1. Open console.cloud.google.com<br>2. "APIs & Services" → "Credentials" → "Create API Key"<br><br><b>② Search Engine ID</b><br>1. Open programmablesearchengine.google.com<br>2. Create new → Enable "Search the entire web" → Copy ID',
    yahoo_note:'No API key required. Searches via DuckDuckGo.<br>Snippets are not shown.',
    shortcut_intro:'Once set up, just tap Share → Shortcut name in Safari to add sites.',
    shortcut_steps_pre:'<b>Setup steps</b><br>1. Open the "Shortcuts" app on iPhone<br>2. Tap <b>＋</b> in the top right<br>3. "Add Action" → select "Open URL"<br>4. Enter the following in the URL field:',
    shortcut_steps_post:'5. Set shortcut name to <b>Add to Choice</b><br>6. Tap <b>Done</b> in the top right<br>7. Shortcut details → enable "Show in Share Sheet"',
    shortcut_tip:'※ Usage: Open site in Safari → Share → "Add to Choice" → Confirm name and add',
    clipboard_desc:'Open a site in Safari and copy the URL → come back here and tap the button below',
    preset_overlay_title:'📦 Add from Presets',
  },
  zh: {
    tab_search:'搜索', tab_explore:'添加网站', tab_sites:'网站管理', tab_topics:'话题', tab_settings:'设置',
    search_ph:'输入关键词...', search_btn:'搜索',
    mode_web:'🌐 网络', mode_index:'📚 索引',
    loading:'🔍 搜索中...', no_results:'未找到结果', error_msg:'发生错误',
    add_site_title:'添加搜索网站', name_ph:'网站名称', url_ph:'网址',
    add_btn:'添加', sites_label:'已注册网站', crawl_hint:'↺ 按钮：爬取索引',
    close_btn:'✕ 关闭', add_site_btn:'＋添加',
    explore_ph:'输入网址或关键词...', open_btn:'打开', add_from_btn:'＋添加',
    api_title:'🔍 搜索 API', lang_title:'🌐 显示语言',
    save_btn:'保存', saving:'保存中...', saved:'已保存',
    api_ok:'已设置', api_ng:'未设置',
    key_ph:'请输入API密钥', key_cx_ph:'请输入API密钥和搜索引擎ID',
    no_api_note:'未设置API密钥时通过Yahoo Japan搜索（无摘要）。API密钥仅保存在此服务器上。',
    crawl_auto_title:'🔄 添加网站时自动爬取', crawl_auto_hint:'开启后，添加网站时将自动创建索引。可能需要一些时间。', crawl_auto_off:'关闭', crawl_auto_on:'开启',
    adding:'添加中...', adding_crawl:'添加并爬取中...',
    group_filter_label:'搜索分组', group_all:'全部', group_mgmt_title:'📁 分组管理', group_name_ph:'分组名称', group_add_btn:'添加', group_none:'无分组', group_delete_confirm:'删除此分组？（网站将变为无分组）',
    explore_open_sites:'打开已注册网站', preset_title:'📦 从预设添加', preset_desc:'选择分类批量注册网站。', preset_btn:'📦 浏览预设',
    shortcut_title:'⚡ 通过iOS快捷方式添加（推荐）', clipboard_title:'📋 从剪贴板添加', clipboard_btn:'📋 粘贴复制的URL',
    topics_tab:'📰 话题', later_tab:'📌 稍后阅读', topics_all:'全部', topics_refresh_btn:'🔄 刷新', topics_refreshing:'获取中...',
    darkmode_title:'🌙 深色模式', darkmode_off:'关闭', darkmode_on:'开启',
    api_key_label:'API 密钥', google_cx_label:'搜索引擎 ID（cx）', api_note_footer:'API密钥仅保存在此服务器上。',
    no_sites:'尚无已注册网站', no_explore_sites:'没有已注册网站', no_groups:'暂无分组', sites_unit:'个网站',
    no_topics:'暂无内容', topics_push_refresh:'请按刷新按钮', already_read:'✓ 已读',
    history_label:'最近搜索', history_clear:'清除', later_empty:'稍后阅读列表为空', title_save_later:'稍后阅读',
    crawl_failed:'爬取失败', add_failed:'添加失败',
    alert_name_url:'请输入名称和URL', alert_url_required:'请输入URL', alert_url_invalid:'请输入有效URL',
    alert_copy_url_first:'请先复制URL', alert_paste_done:'已粘贴URL。请确认网站名后点击"添加"。',
    prompt_paste_url:'请粘贴URL：', rename_group_prompt:'修改分组名',
    preset_adding:'添加中...', preset_added:'✓ 已添加', preset_error:'错误', preset_loading:'加载中...',
    confirm_delete_site:'删除"{0}"？', confirm_crawl:'爬取"{0}"？\\n（最多30页，可能需要一些时间）', crawl_done:'完成：已索引{0}页',
    explore_howto:'<b style="color:#1a1a2e">💡 使用方法</b><br>① 输入URL或关键词，点击"打开"<br>② 确认页面后点击"＋添加"注册网站<br>③ 已注册网站按分组列出<br>④ 在"网站管理"标签中管理分组',
    brave_steps:'<b>免费 2,000次/月</b><br>1. 打开 <b>brave.com/search/api</b><br>2. "Get Started for Free" → 创建账户<br>3. 复制显示的API密钥',
    google_steps:'<b>免费 100次/天</b><br><b>① API 密钥</b><br>1. 打开 console.cloud.google.com<br>2. "API 和服务" → "凭据" → "创建 API 密钥"<br><br><b>② 搜索引擎 ID</b><br>1. 打开 programmablesearchengine.google.com<br>2. 新建 → 启用"搜索整个网络" → 复制 ID',
    yahoo_note:'无需API密钥。通过Yahoo Japan搜索。<br>不显示摘要。',
    shortcut_intro:'设置完成后，只需在Safari中点击共享 → 快捷方式名称即可添加。',
    shortcut_steps_pre:'<b>设置步骤</b><br>1. 打开iPhone的"快捷指令"应用<br>2. 点击右上角 <b>＋</b><br>3. "添加操作" → 选择"打开URL"<br>4. 在URL栏输入以下内容：',
    shortcut_steps_post:'5. 将快捷指令名称设为 <b>添加到Choice</b><br>6. 点击右上角 <b>完成</b><br>7. 快捷指令详情 → 启用"在共享表单中显示"',
    shortcut_tip:'※ 使用方法：在Safari中打开网站 → 共享 →"添加到Choice" → 确认名称后添加',
    clipboard_desc:'在Safari中打开网站并复制URL → 返回此处点击下方按钮',
    preset_overlay_title:'📦 从预设添加',
  }
};
let appLang = 'ja';

function t(key) { return (T[appLang] || T.ja)[key] || key; }
function tf(key, ...args) { return args.reduce((s, a, i) => s.replace('{' + i + '}', a), t(key)); }

function applyTranslations() {
  document.querySelectorAll('[data-i18n]').forEach(el => {
    const key = el.getAttribute('data-i18n');
    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') el.placeholder = t(key);
    else el.textContent = t(key);
  });
  document.querySelectorAll('[data-i18n-html]').forEach(el => {
    el.innerHTML = t(el.getAttribute('data-i18n-html'));
  });
}

// ── 言語 ──────────────────────────────────────────────
async function setLang(lang) {
  appLang = lang;
  applyLangButtons(lang);
  applyTranslations();
  await api('/settings/app_lang', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({value:lang})});
}

function applyLangButtons(lang) {
  ['ja','en','zh'].forEach(l => document.getElementById('lang-'+l)?.classList.remove('active'));
  const map = {'ja':'lang-ja','en':'lang-en','zh':'lang-zh'};
  document.getElementById(map[lang] || 'lang-ja')?.classList.add('active');
}

// ── ダークモード ───────────────────────────────────────
let darkMode = false;

function applyDarkMode(val) {
  darkMode = val === 'on';
  document.documentElement.setAttribute('data-theme', darkMode ? 'dark' : 'light');
  document.getElementById('darkmode-off')?.classList.toggle('active', !darkMode);
  document.getElementById('darkmode-on')?.classList.toggle('active', darkMode);
}

async function setDarkMode(val) {
  applyDarkMode(val);
  await api('/settings/dark_mode', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({value:val})});
}

// ── 自動クロール ───────────────────────────────────────
async function setAutoCrawl(val) {
  autoCrawl = val;
  applyAutoCrawlUI(val);
  await api('/settings/auto_crawl', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({value:val})});
}

function applyAutoCrawlUI(val) {
  document.getElementById('autocrawl-off')?.classList.toggle('active', val !== 'on');
  document.getElementById('autocrawl-on')?.classList.toggle('active', val === 'on');
}

// ── API プロバイダー ────────────────────────────────────
let currentProvider = 'yahoo';
let autoCrawl = 'off';

async function setProvider(provider) {
  currentProvider = provider;
  applyProviderUI(provider);
  await api('/settings/search_provider', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({value:provider})});
}

function applyProviderUI(provider) {
  ['brave','google','yahoo'].forEach(p => {
    document.getElementById('provider-'+p)?.classList.remove('active');
    const pane = document.getElementById('pane-'+p);
    if (pane) pane.style.display = 'none';
  });
  document.getElementById('provider-'+provider)?.classList.add('active');
  const pane = document.getElementById('pane-'+provider);
  if (pane) pane.style.display = '';
  const saveBtn = document.getElementById('save-btn');
  if (saveBtn) saveBtn.style.display = provider === 'yahoo' ? 'none' : '';
}

// ── 設定 読み込み / 保存 ────────────────────────────────
async function loadSettings() {
  const [braveKey, googleKey, googleCx, providerData, langData, autoCrawlData, darkModeData, licenseData] = await Promise.all([
    api('/settings/brave_api_key'), api('/settings/google_api_key'),
    api('/settings/google_cx'),    api('/settings/search_provider'),
    api('/settings/app_lang'),     api('/settings/auto_crawl'),
    api('/settings/dark_mode'),    api('/license/status'),
  ]);
  currentProvider = providerData.value || 'yahoo';
  appLang = langData.value || 'ja';
  applyProviderUI(currentProvider);
  applyLangButtons(appLang);
  autoCrawl = autoCrawlData.value || 'off';
  applyAutoCrawlUI(autoCrawl);
  applyDarkMode(darkModeData.value || 'off');
  applyTranslations();

  const mk = v => v ? v.slice(0,4)+'****'+v.slice(-4) : '';
  document.getElementById('setting-brave-key').placeholder  = mk(braveKey.value)  || 'BSA...';
  document.getElementById('setting-google-key').placeholder = mk(googleKey.value) || 'AIzaSy...';
  document.getElementById('setting-google-cx').placeholder  = googleCx.value ? googleCx.value.slice(0,4)+'****' : 'a1b2c3...';
  ['setting-brave-key','setting-google-key','setting-google-cx'].forEach(id => {
    document.getElementById(id).value = '';
  });
  updateApiStatus(currentProvider, braveKey.value, googleKey.value, googleCx.value);
  applyLicenseUI(licenseData);
}

function applyLicenseUI(data) {
  const isPremium = data && data.status === 'premium';
  const badge = document.getElementById('license-badge');
  badge.textContent = isPremium ? 'PREMIUM' : 'FREE';
  badge.style.background = isPremium ? '#4caf50' : '#e0e0e0';
  badge.style.color = isPremium ? '#fff' : '#888';
  document.getElementById('license-free-section').style.display    = isPremium ? 'none' : '';
  document.getElementById('license-premium-section').style.display = isPremium ? '' : 'none';
  if (isPremium && data.key_hint) {
    document.getElementById('license-key-hint').textContent = data.key_hint;
  }
}

async function activateLicense() {
  const key = document.getElementById('license-key-input').value.trim();
  const errEl = document.getElementById('license-error');
  errEl.style.display = 'none';
  if (!key) { errEl.textContent = 'キーを入力してください'; errEl.style.display = ''; return; }
  try {
    const res = await api('/license/activate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({key}),
    });
    applyLicenseUI(res);
    document.getElementById('license-key-input').value = '';
  } catch(e) {
    errEl.textContent = e.message || '認証に失敗しました';
    errEl.style.display = '';
  }
}

function copySyncUrl() {
  const url = location.origin + location.pathname + '?sync=' + USER_TOKEN;
  navigator.clipboard.writeText(url).then(() => alert('同期URLをコピーしました')).catch(() => {
    prompt('この URL をコピーしてください:', url);
  });
}

function applySyncToken() {
  const t = document.getElementById('sync-token-input').value.trim();
  if (!t || t.length < 16) { alert('トークンが短すぎます'); return; }
  if (!confirm('トークンを切り替えます。現在のデータは表示されなくなります（トークンを控えておけば戻せます）。続けますか？')) return;
  localStorage.setItem('ch_token', t);
  location.reload();
}

async function deactivateLicense() {
  if (!confirm('ライセンスを解除しますか？')) return;
  const res = await api('/license', {method: 'DELETE'});
  applyLicenseUI(res);
}

async function saveSettings() {
  const btn = document.getElementById('save-btn');
  const origText = btn.textContent;
  btn.textContent = t('saving'); btn.disabled = true;
  try {
    const saves = [];
    if (currentProvider === 'brave') {
      const key = document.getElementById('setting-brave-key').value.trim();
      if (!key) { alert(t('key_ph')); return; }
      saves.push(api('/settings/brave_api_key',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({value:key})}));
      const el = document.getElementById('setting-brave-key');
      el.value = ''; el.placeholder = key.slice(0,4)+'****'+key.slice(-4);
    } else if (currentProvider === 'google') {
      const key = document.getElementById('setting-google-key').value.trim();
      const cx  = document.getElementById('setting-google-cx').value.trim();
      if (!key || !cx) { alert(t('key_cx_ph')); return; }
      saves.push(api('/settings/google_api_key',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({value:key})}));
      saves.push(api('/settings/google_cx',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({value:cx})}));
      document.getElementById('setting-google-key').value=''; document.getElementById('setting-google-key').placeholder=key.slice(0,4)+'****'+key.slice(-4);
      document.getElementById('setting-google-cx').value='';  document.getElementById('setting-google-cx').placeholder=cx.slice(0,4)+'****';
    }
    await Promise.all(saves);
    alert(t('saved'));
    updateApiStatus(currentProvider, true, true, true);
  } catch(e) {
    alert(t('error_msg'));
  } finally {
    btn.textContent = origText; btn.disabled = false;
  }
}

function updateApiStatus(provider, braveKey, googleKey, googleCx) {
  const el = document.getElementById('api-status');
  if (!el) return;
  const ok = provider==='yahoo' || (provider==='brave'&&braveKey) || (provider==='google'&&googleKey&&googleCx);
  el.textContent = t(ok ? 'api_ok' : 'api_ng');
  el.className = 'api-status '+(ok ? 'ok' : 'ng');
}

async function doSearch() {
  const q = document.getElementById('query').value.trim();
  if (!q) return;
  document.getElementById('clear-btn').style.display = '';
  document.getElementById('search-history').style.display = 'none';
  saveSearchHistory(q);

  if (searchMode === 'web') {
    await doSearchWeb(q);
    return;
  }

  const el = document.getElementById('results');
  el.innerHTML = `<div class="loading">${t('loading')}</div>`;
  try {
    const data = await api('/search', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({query: q, group_id: currentGroupId})
    });
    if (!data.results || data.results.length === 0) {
      el.innerHTML = `<div class="empty"><div class="empty-icon">🔍</div>${t('no_results')}</div>`;
      return;
    }
    el.innerHTML = data.results.map(r => {
      const saved = readLaterUrls.has(r.url);
      return `<div class="card" onclick="openBrowser('${esc(r.url)}',false,'${esc(r.title)}','${esc(r.site_name)}')">
        <div class="card-site"><img src="${getFavicon(r.url)}" class="favicon" onerror="this.style.display='none'">${esc(r.site_name)}</div>
        <div class="card-title">${esc(r.title)}</div>
        <div class="card-excerpt">${esc(r.excerpt)}</div>
        <div style="display:flex;align-items:center">
          <div class="card-url" style="flex:1">${esc(r.url)}</div>
          <button onclick="event.stopPropagation();saveForLater('${esc(r.url)}','${esc(r.title)}','${esc(r.site_name)}')"
            data-later="${esc(r.url)}"
            style="background:none;border:none;font-size:15px;cursor:pointer;padding:0 4px;color:${saved?'#4a90d9':'#ccc'}"
            title="${t('title_save_later')}">${saved?'✓':'📌'}</button>
        </div>
      </div>`;
    }).join('');
  } catch(e) {
    el.innerHTML = `<div class="empty"><div class="empty-icon">⚠️</div>${t('error_msg')}</div>`;
  }
}

async function doSearchWeb(q) {
  const el = document.getElementById('results');
  el.innerHTML = `<div class="loading">${t('loading')}</div>`;
  try {
    const data = await api('/search/web', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({query: q, group_id: currentGroupId})
    });
    if (!data.results || data.results.length === 0) {
      el.innerHTML = `<div class="empty"><div class="empty-icon">🔍</div>${t('no_results')}</div>`;
      return;
    }
    el.innerHTML = data.results.map(r => {
      const saved = readLaterUrls.has(r.url);
      return `<div class="card" onclick="openBrowser('${esc(r.url)}',false,'${esc(r.title)}','${esc(r.site_name)}')">
        <div class="card-site"><img src="${getFavicon(r.url)}" class="favicon" onerror="this.style.display='none'">${esc(r.site_name)}</div>
        <div class="card-title">${esc(r.title)}</div>
        ${r.excerpt ? '<div class="card-excerpt">' + esc(r.excerpt) + '</div>' : ''}
        <div style="display:flex;align-items:center">
          <div class="card-url" style="flex:1">${esc(r.url)}</div>
          <button onclick="event.stopPropagation();saveForLater('${esc(r.url)}','${esc(r.title)}','${esc(r.site_name)}')"
            data-later="${esc(r.url)}"
            style="background:none;border:none;font-size:15px;cursor:pointer;padding:0 4px;color:${saved?'#4a90d9':'#ccc'}"
            title="${t('title_save_later')}">${saved?'✓':'📌'}</button>
        </div>
      </div>`;
    }).join('');
  } catch(e) {
    el.innerHTML = `<div class="empty"><div class="empty-icon">⚠️</div>${t('error_msg')}</div>`;
  }
}

function clearSearch() {
  document.getElementById('query').value = '';
  document.getElementById('results').innerHTML = '';
  document.getElementById('clear-btn').style.display = 'none';
  renderSearchHistory();
}

const folderOpen = {};  // group_id → bool (開閉状態)

async function loadSites() {
  const [sitesData, groupsData] = await Promise.all([api('/sites'), api('/groups')]);
  sites = sitesData;
  const header = document.getElementById('sites-header');
  header.textContent = `${t('sites_label')} (${sites.length})`;
  const el = document.getElementById('sites-list');
  if (sites.length === 0) {
    el.innerHTML = `<div class="empty"><div class="empty-icon">🌐</div>${t('no_sites')}</div>`;
    return;
  }

  const groupOpts = `<option value="">-</option>` +
    groupsData.map(g => `<option value="${g.id}">${esc(g.name)}</option>`).join('');

  // グループごとにサイトを分類
  const grouped = {};   // group_id(or 'none') → {name, sites[]}
  groupsData.forEach(g => { grouped[g.id] = {name: g.name, sites: []}; });
  grouped['none'] = {name: t('group_none'), sites: []};
  sites.forEach(s => {
    const key = s.group_id || 'none';
    if (!grouped[key]) grouped[key] = {name: s.group_name || t('group_none'), sites: []};
    grouped[key].sites.push(s);
  });

  function siteItem(s) {
    const sel = `<select onchange="changeSiteGroup(${s.id},this.value)" style="font-size:12px;border:1px solid #dde3ec;border-radius:8px;padding:3px 6px;background:#f9fbfc;color:#555;max-width:84px">
      ${groupOpts.replace(`value="${s.group_id || ''}"`, `value="${s.group_id || ''}" selected`)}
    </select>`;
    return `<div class="folder-site-item">
      <div class="site-info">
        <div class="site-name" style="font-size:14px;display:flex;align-items:center;gap:5px"><img src="${getFavicon(s.url)}" class="favicon" onerror="this.style.display='none'">${esc(s.name)}</div>
        <div class="site-url" style="font-size:11px">${s.url}</div>
      </div>
      <div class="site-actions">
        ${sel}
        <button class="crawl-btn" onclick="crawlSite(${s.id},'${esc(s.name)}')" title="クロール">↺</button>
        <button class="del-btn" onclick="deleteSite(${s.id},'${esc(s.name)}')" title="削除">🗑️</button>
      </div>
    </div>`;
  }

  function folder(key, {name, sites: ss}) {
    if (!ss.length) return '';
    if (folderOpen[key] === undefined) folderOpen[key] = false;
    const open = folderOpen[key];
    const icon = key === 'none' ? '📂' : '📁';
    return `<div>
      <div class="folder-header" onclick="toggleFolder('${key}')">
        <span class="folder-icon">${icon}</span>
        <span class="folder-name">${esc(name)}</span>
        <span class="folder-count">${ss.length}${t('sites_unit')}</span>
        <span class="folder-arrow ${open ? 'open' : ''}">›</span>
      </div>
      <div class="folder-body" id="folder-body-${key}" style="display:${open ? '' : 'none'}">
        ${ss.map(siteItem).join('')}
      </div>
    </div>`;
  }

  // グループあり → グループなし の順で表示
  const parts = groupsData.map(g => folder(g.id, grouped[g.id]));
  parts.push(folder('none', grouped['none']));
  el.innerHTML = parts.join('');
}

function toggleFolder(key) {
  folderOpen[key] = !folderOpen[key];
  const body = document.getElementById('folder-body-' + key);
  const arrow = body?.previousElementSibling?.querySelector('.folder-arrow');
  if (body) body.style.display = folderOpen[key] ? '' : 'none';
  if (arrow) arrow.classList.toggle('open', folderOpen[key]);
}

async function changeSiteGroup(siteId, groupId) {
  await api('/sites/' + siteId, {
    method: 'PATCH',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({group_id: groupId ? parseInt(groupId) : null})
  });
  loadSites();
  loadGroupChips();
}

async function searchForSites() {
  const q = document.getElementById('site-search-input').value.trim();
  if (!q) return;
  const btn = document.getElementById('site-search-btn');
  const el = document.getElementById('site-search-results');
  btn.textContent = '検索中...';
  btn.disabled = true;
  el.innerHTML = `<div class="loading" style="padding:16px 0">${t('loading')}</div>`;
  try {
    const data = await api('/search-sites?q=' + encodeURIComponent(q));
    if (!data.results.length) {
      el.innerHTML = `<div class="empty" style="padding:20px 0"><div class="empty-icon">🔍</div>${t('no_results')}</div>`;
      return;
    }
    el.innerHTML = data.results.map((r, i) => `
      <div class="card" onclick="openBrowser('${esc(r.url)}',false,'${esc(r.title)}','${esc(r.site_name)}')" style="cursor:pointer">
        <div class="card-site"><img src="${getFavicon(r.url)}" class="favicon" onerror="this.style.display='none'">${esc(r.site_name)}</div>
        <div class="card-title">${esc(r.title)}</div>
        ${r.excerpt ? `<div class="card-excerpt">${esc(r.excerpt)}</div>` : ''}
        <div style="display:flex;align-items:center;margin-top:6px;gap:8px">
          <div class="card-url" style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(r.url)}</div>
          <button id="found-btn-${i}"
            onclick="event.stopPropagation();addFoundSite('${esc(r.site_url)}','${esc(r.site_name)}',${i})"
            style="border:none;border-radius:16px;font-size:13px;font-weight:bold;cursor:pointer;padding:5px 10px;white-space:nowrap;flex-shrink:0;${r.is_registered?'background:#e8f5e9;color:#2e7d32;pointer-events:none':'background:#e27d60;color:#fff'}"
            title="${r.is_registered?'登録済み':'サイトを登録'}">${r.is_registered?'✓':'＋追加'}</button>
        </div>
      </div>`).join('');
  } catch(e) {
    el.innerHTML = `<div class="empty" style="padding:20px 0"><div class="empty-icon">⚠️</div>${t('error_msg')}</div>`;
  } finally {
    btn.textContent = '検索';
    btn.disabled = false;
  }
}

async function addFoundSite(siteUrl, siteName, idx, btnEl) {
  const btn = idx !== null ? document.getElementById('found-btn-' + idx) : btnEl;
  btn.style.opacity = '0.5';
  btn.style.pointerEvents = 'none';
  try {
    const searchWord = document.getElementById('site-search-input').value.trim();
    let groupId = null;
    if (searchWord) {
      const grp = await api('/groups/find-or-create', {method:'POST',
        headers:{'Content-Type':'application/json'}, body: JSON.stringify({name: searchWord})});
      groupId = grp.id;
    }
    await api('/sites', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name: siteName, url: siteUrl, group_id: groupId})});
    btn.textContent = '✓';
    btn.style.color = '#4a90d9';
    btn.style.opacity = '1';
    btn.title = '登録済み';
    loadSites();
    loadGroupChips();
  } catch(e) {
    btn.style.opacity = '1';
    btn.style.pointerEvents = 'auto';
    alert(e.message);
  }
}

async function deleteSite(id, name) {
  if (!confirm(tf('confirm_delete_site', name))) return;
  await api('/sites/' + id, {method: 'DELETE'});
  loadSites();
}

async function crawlSite(id, name) {
  if (!confirm(tf('confirm_crawl', name))) return;
  try {
    const data = await api('/crawl/' + id, {method: 'POST'});
    alert(tf('crawl_done', data.indexed));
  } catch(e) { alert(t('crawl_failed')); }
}

let browserCurrentUrl = '';
let browserCurrentTitle = '';
let browserCurrentSiteName = '';

function openBrowser(url, showAdd = true, title = '', siteName = '') {
  browserCurrentUrl = url;
  browserCurrentTitle = title;
  browserCurrentSiteName = siteName;
  document.getElementById('browser-url').textContent = url;
  document.getElementById('browser-frame').src = '/proxy?url=' + encodeURIComponent(url);
  document.getElementById('browser-add-btn').style.display = showAdd ? '' : 'none';
  const laterBtn = document.getElementById('browser-later-btn');
  laterBtn.textContent = '📌';
  laterBtn.style.color = readLaterUrls.has(url) ? '#4a90d9' : '#ddd';
  const ov = document.getElementById('browser-overlay');
  ov.style.display = 'flex';
}

function closeBrowser() {
  document.getElementById('browser-overlay').style.display = 'none';
  document.getElementById('browser-frame').src = 'about:blank';
  browserCurrentUrl = '';
  browserCurrentTitle = '';
  browserCurrentSiteName = '';
}

// iframe 内のリンククリック・タイトル取得をハンドル
window.addEventListener('message', function(e) {
  if (!e.data) return;
  if (e.data.type === 'nav' && e.data.url) {
    browserCurrentUrl = e.data.url;
    browserCurrentTitle = '';
    document.getElementById('browser-url').textContent = e.data.url;
    document.getElementById('browser-frame').src = '/proxy?url=' + encodeURIComponent(e.data.url);
    const laterBtn = document.getElementById('browser-later-btn');
    laterBtn.textContent = '📌';
    laterBtn.style.color = readLaterUrls.has(e.data.url) ? '#4a90d9' : '#ddd';
  }
  if (e.data.type === 'title' && e.data.title) {
    browserCurrentTitle = e.data.title;
  }
});

async function saveBrowserPageForLater() {
  if (!browserCurrentUrl) return;
  const title = browserCurrentTitle || browserCurrentUrl;
  await saveForLater(browserCurrentUrl, title, browserCurrentSiteName);
  const btn = document.getElementById('browser-later-btn');
  btn.textContent = '✅';
  setTimeout(() => { btn.textContent = '📌'; btn.style.color = '#4a90d9'; }, 1200);
}

async function addBrowserSite() {
  if (!browserCurrentUrl) return;
  const btn = document.getElementById('browser-add-btn');
  try {
    const u = new URL(browserCurrentUrl);
    const name = browserCurrentTitle
      ? browserCurrentTitle.replace(/[|｜–—].*$/, '').trim().slice(0, 40) || u.hostname.replace('www.', '')
      : u.hostname.replace('www.', '');
    btn.textContent = '追加中...';
    btn.disabled = true;
    await api('/sites', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name, url: u.origin, group_id: null})});
    btn.textContent = '✓ 追加済み';
    btn.style.background = '#2e7d32';
    loadSites();
    loadGroupChips();
    setTimeout(() => { btn.textContent = t('add_site_btn'); btn.style.background = '#e27d60'; btn.disabled = false; }, 2000);
  } catch(e) {
    btn.textContent = t('add_site_btn');
    btn.style.background = '#e27d60';
    btn.disabled = false;
    alert(e.message);
  }
}

async function exploreGo() {
  const val = document.getElementById('explore-input').value.trim();
  if (!val) return;
  if (val.startsWith('http')) {
    openBrowser(val);
    return;
  }
  document.getElementById('explore-clear-btn').style.display = '';
  const el = document.getElementById('explore-results');
  el.innerHTML = `<div class="loading">${t('loading')}</div>`;
  try {
    const data = await api('/search/explore', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({query: val})
    });
    if (!data.results || data.results.length === 0) {
      el.innerHTML = `<div class="empty"><div class="empty-icon">🌐</div>${t('no_results')}</div>`;
      return;
    }
    el.innerHTML = data.results.map((r, i) => {
      const origin = (() => { try { return new URL(r.url).origin; } catch(_) { return r.url; } })();
      const siteN = (() => { try { return new URL(r.url).hostname.replace('www.',''); } catch(_) { return r.url; } })();
      return `
      <div class="card" onclick="openBrowser('${esc(r.url)}')">
        <div class="card-title">${esc(r.title)}</div>
        ${r.excerpt ? `<div class="card-excerpt">${esc(r.excerpt)}</div>` : ''}
        <div style="display:flex;align-items:center;margin-top:6px;gap:8px">
          <div class="card-url" style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(r.url)}</div>
          <button onclick="event.stopPropagation();addFoundSite('${esc(origin)}','${esc(siteN)}',null,this)"
            style="border:none;border-radius:16px;font-size:13px;font-weight:bold;cursor:pointer;padding:5px 10px;white-space:nowrap;flex-shrink:0;background:#e27d60;color:#fff">＋追加</button>
        </div>
      </div>`;
    }).join('');
  } catch(e) {
    el.innerHTML = `<div class="empty"><div class="empty-icon">⚠️</div>${t('error_msg')}</div>`;
  }
}

function clearExplore() {
  document.getElementById('explore-input').value = '';
  document.getElementById('explore-clear-btn').style.display = 'none';
  document.getElementById('explore-results').innerHTML = '';
}

function addFromInput() {
  let val = document.getElementById('explore-input').value.trim();
  if (!val) { alert(t('alert_url_required')); return; }
  if (!val.startsWith('http')) val = 'https://' + val;
  try {
    new URL(val);
    openBrowser(val);
  } catch(e) { alert(t('alert_url_invalid')); }
}

const exploreFolderOpen = {};

async function loadExploreSites() {
  const [sitesData, groupsData] = await Promise.all([api('/sites'), api('/groups')]);
  const el = document.getElementById('explore-sites');
  if (!sitesData.length) {
    el.innerHTML = `<div class="empty" style="padding:20px 0"><div class="empty-icon">🌐</div>${t('no_explore_sites')}</div>`;
    return;
  }

  // グループ分類
  const grouped = {};
  groupsData.forEach(g => { grouped[g.id] = {name: g.name, sites: []}; });
  grouped['none'] = {name: t('group_none'), sites: []};
  sitesData.forEach(s => {
    const key = s.group_id || 'none';
    if (!grouped[key]) grouped[key] = {name: s.group_name || t('group_none'), sites: []};
    grouped[key].sites.push(s);
  });

  function siteCard(s) {
    return `<div class="card" onclick="openBrowser('${esc(s.url)}')" style="display:flex;align-items:center;gap:12px;padding:11px 14px;margin-bottom:4px;cursor:pointer">
      <img src="${getFavicon(s.url)}" style="width:20px;height:20px;border-radius:3px;flex-shrink:0" onerror="this.style.display='none'">
      <div style="flex:1;min-width:0">
        <div class="card-title" style="font-size:14px">${esc(s.name)}</div>
        <div class="card-url">${s.url}</div>
      </div>
      <span style="color:#ccc;font-size:18px">›</span>
    </div>`;
  }

  function folder(key, {name, sites: ss}) {
    if (!ss.length) return '';
    if (exploreFolderOpen[key] === undefined) exploreFolderOpen[key] = false;
    const open = exploreFolderOpen[key];
    const icon = key === 'none' ? '📂' : '📁';
    return `<div>
      <div class="folder-header" onclick="toggleExploreFolder('${key}')">
        <span class="folder-icon">${icon}</span>
        <span class="folder-name">${esc(name)}</span>
        <span class="folder-count">${ss.length}${t('sites_unit')}</span>
        <span class="folder-arrow ${open ? 'open' : ''}">›</span>
      </div>
      <div class="folder-body" id="explore-folder-body-${key}" style="display:${open ? '' : 'none'}">
        ${ss.map(siteCard).join('')}
      </div>
    </div>`;
  }

  const parts = groupsData.map(g => folder(g.id, grouped[g.id]));
  parts.push(folder('none', grouped['none']));
  el.innerHTML = parts.join('');
}

function toggleExploreFolder(key) {
  exploreFolderOpen[key] = !exploreFolderOpen[key];
  const body = document.getElementById('explore-folder-body-' + key);
  const arrow = body?.previousElementSibling?.querySelector('.folder-arrow');
  if (body) body.style.display = exploreFolderOpen[key] ? '' : 'none';
  if (arrow) arrow.classList.toggle('open', exploreFolderOpen[key]);
}

// ショートカット用 URL を表示
(function() {
  const el = document.getElementById('shortcut-url');
  if (el) el.textContent = location.origin + '/?add=' + encodeURIComponent('{入力された URL}');
})();

// ?add=URL パラメータで自動入力（initApp完了後に実行）
function handleAddParam() {
  const params = new URLSearchParams(location.search);
  const addUrl = params.get('add');
  if (addUrl) {
    try { new URL(addUrl); openBrowser(addUrl); } catch(e) {}
  }
}

async function pasteFromClipboard() {
  try {
    const text = await navigator.clipboard.readText();
    if (!text.startsWith('http')) {
      alert(t('alert_copy_url_first'));
      return;
    }
    new URL(text);
    openBrowser(text);
  } catch(e) {
    // クリップボードアクセス拒否された場合は手動貼り付けを促す
    const text = prompt(t('prompt_paste_url'));
    if (text && text.startsWith('http')) {
      try { new URL(text); openBrowser(text); } catch(e2) {}
    }
  }
}

// ── トピック ──────────────────────────────────────────
async function loadTopicGroups() {
  const groups = await api('/groups');
  const sel = document.getElementById('topic-group-select');
  sel.innerHTML = `<option value="">${t('topics_all')}</option>` +
    groups.map(g => `<option value="${g.id}">${esc(g.name)}</option>`).join('');
}

async function loadTopics() {
  await loadTopicGroups();
  const groupId = document.getElementById('topic-group-select').value;
  const el = document.getElementById('topic-results');
  el.innerHTML = `<div class="loading">${t('loading')}</div>`;
  const params = groupId ? `?group_id=${groupId}` : '';
  const data = await api('/topics' + params);
  if (!data.length) {
    el.innerHTML = `<div class="empty"><div class="empty-icon">📰</div>${t('no_topics')}<br><span style="font-size:13px;color:#bbb">${t('topics_push_refresh')}</span></div>`;
    return;
  }
  el.innerHTML = data.map(r => {
    const saved = readLaterUrls.has(r.url);
    return `<div class="card" id="topic-card-${r.id}" onclick="openTopicUrl('${esc(r.url)}',${r.id},'${esc(r.url)}')"
      style="opacity:${r.is_read ? '0.5' : '1'}">
      <div class="card-site" style="display:flex;align-items:center;gap:6px">
        <img src="${getFavicon(r.url)}" class="favicon" onerror="this.style.display='none'">
        ${r.is_read ? `<span style="color:#4caf50;font-size:11px">${t('already_read')}</span>` : ''}
        ${esc(r.site_name)}
      </div>
      <div class="card-title">${esc(r.title)}</div>
      <div style="display:flex;align-items:center">
        <div class="card-url" style="flex:1">${esc(r.url)}</div>
        <button onclick="event.stopPropagation();saveForLater('${esc(r.url)}','${esc(r.title)}','${esc(r.site_name)}')"
          data-later="${esc(r.url)}"
          style="background:none;border:none;font-size:15px;cursor:pointer;padding:0 4px;color:${saved?'#4a90d9':'#ccc'}"
          title="${t('title_save_later')}">${saved?'✓':'📌'}</button>
      </div>
    </div>`;
  }).join('');
}

function openTopicUrl(url, topicId, topicUrl) {
  openBrowser(url, false);
  markTopicRead(topicId, topicUrl);
}

async function markTopicRead(topicId, url) {
  await api('/topics/read', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({url})});
  const card = document.getElementById('topic-card-' + topicId);
  if (card) {
    card.style.opacity = '0.5';
    const site = card.querySelector('.card-site');
    if (site && !site.querySelector('.read-mark')) {
      const mark = document.createElement('span');
      mark.className = 'read-mark';
      mark.style.cssText = 'color:#4caf50;font-size:11px';
      mark.textContent = t('already_read');
      site.prepend(mark);
    }
  }
}

async function refreshTopics() {
  const btn = document.getElementById('topic-refresh-btn');
  btn.textContent = t('topics_refreshing');
  btn.disabled = true;
  const groupId = document.getElementById('topic-group-select').value;
  const el = document.getElementById('topic-results');
  el.innerHTML = `<div class="loading">${t('loading')}</div>`;
  try {
    const res = await api('/topics/refresh', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({group_id: groupId ? parseInt(groupId) : null})
    });
    await loadTopics();
  } catch(e) {
    el.innerHTML = `<div class="empty"><div class="empty-icon">⚠️</div>${t('error_msg')}</div>`;
  } finally {
    btn.textContent = t('topics_refresh_btn');
    btn.disabled = false;
  }
}

// ── プリセット ────────────────────────────────────────
let _genres = [];

async function openPresets() {
  document.getElementById('preset-overlay').style.display = 'flex';
  const el = document.getElementById('preset-list');
  if (_genres.length === 0) {
    el.innerHTML = `<div class="loading">${t('preset_loading')}</div>`;
    _genres = await api('/genres');
  }
  renderGenres();
}

function closePresets() {
  document.getElementById('preset-overlay').style.display = 'none';
}

function renderGenres() {
  const el = document.getElementById('preset-list');
  el.innerHTML = `
    <p style="font-size:13px;color:#888;margin:0 4px 14px;line-height:1.6">気になるジャンルをタップするとChoiceがサイトを探します。</p>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
      ${_genres.map(g => `
        <button onclick="discoverSites('${g.id}')"
          style="background:#fff;border:1.5px solid #e0e0e0;border-radius:14px;padding:16px 10px;font-size:15px;cursor:pointer;text-align:center;word-break:break-word;line-height:1.4;color:#1a1a2e;font-weight:600;box-shadow:0 1px 4px rgba(0,0,0,.06)">
          ${esc(g.label)}
        </button>`).join('')}
    </div>`;
}

let _currentGenreLabel = '';

async function discoverSites(genreId) {
  const genre = _genres.find(g => g.id === genreId);
  _currentGenreLabel = genre ? genre.label : '';
  const el = document.getElementById('preset-list');
  el.innerHTML = `
    <button onclick="renderGenres()" style="background:none;border:none;color:#4a90d9;font-size:14px;cursor:pointer;padding:0 0 12px;display:flex;align-items:center;gap:4px">‹ ジャンル一覧</button>
    <div style="font-size:17px;font-weight:700;color:#1a1a2e;margin-bottom:12px">${genre ? esc(genre.label) : ''}</div>
    <div class="loading">${t('preset_loading')}</div>`;
  try {
    const data = await api('/discover?genre=' + genreId);
    renderDiscoverResults(data);
  } catch(e) {
    el.querySelector('.loading').textContent = '取得に失敗しました';
  }
}

function renderDiscoverResults(data) {
  const el = document.getElementById('preset-list');
  const header = el.querySelector('button').outerHTML + el.querySelectorAll('div')[0].outerHTML;
  if (!data.results || data.results.length === 0) {
    el.innerHTML = header + `<div class="empty"><div class="empty-icon">🔍</div>サイトが見つかりませんでした</div>`;
    return;
  }
  el.innerHTML = header + data.results.map((s, i) => `
    <div style="display:flex;align-items:flex-start;gap:10px;padding:12px 0;border-bottom:1px solid #f0f4f8">
      <img src="${getFavicon(s.url)}" style="width:24px;height:24px;border-radius:4px;margin-top:2px;flex-shrink:0" onerror="this.style.display='none'">
      <div style="flex:1;min-width:0">
        <div style="font-size:14px;font-weight:600;color:#1a1a2e;word-break:break-word">${esc(s.name)}</div>
        <div style="font-size:11px;color:#4a90d9;word-break:break-all;margin:2px 0">${esc(s.url)}</div>
        ${s.description ? `<div style="font-size:12px;color:#888;line-height:1.5;word-break:break-word">${esc(s.description)}</div>` : ''}
      </div>
      <button id="disc-btn-${i}" onclick="addDiscoveredSite('${esc(s.url)}','${esc(s.name)}',${i})"
        style="flex-shrink:0;border:none;border-radius:18px;padding:7px 14px;font-size:13px;font-weight:bold;cursor:pointer;white-space:nowrap;
          ${s.is_registered ? 'background:#e8f5e9;color:#2e7d32;pointer-events:none' : 'background:#1a1a2e;color:#fff'}">
        ${s.is_registered ? '登録済み' : '＋ 登録'}
      </button>
    </div>`).join('');
}

async function addDiscoveredSite(url, name, idx) {
  const btn = document.getElementById('disc-btn-' + idx);
  btn.textContent = '追加中...';
  btn.style.opacity = '0.6';
  btn.style.pointerEvents = 'none';
  try {
    let group_id = null;
    if (_currentGenreLabel) {
      const g = await api('/groups/find-or-create', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({name: _currentGenreLabel})});
      group_id = g.id;
    }
    await api('/sites', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name, url, group_id})});
    btn.textContent = '登録済み';
    btn.style.background = '#e8f5e9';
    btn.style.color = '#2e7d32';
    btn.style.opacity = '1';
    loadSites();
    loadGroupChips();
  } catch(e) {
    btn.textContent = 'エラー';
    btn.style.background = '#ffebee';
    btn.style.color = '#c62828';
    btn.style.opacity = '1';
    btn.style.pointerEvents = 'auto';
  }
}

// ── 検索履歴 ──────────────────────────────────────────
const HISTORY_MAX = 15;

function saveSearchHistory(q) {
  let hist = JSON.parse(localStorage.getItem('ch_history') || '[]');
  hist = hist.filter(x => x !== q);
  hist.unshift(q);
  if (hist.length > HISTORY_MAX) hist.length = HISTORY_MAX;
  localStorage.setItem('ch_history', JSON.stringify(hist));
  renderSearchHistory();
}

function renderSearchHistory() {
  const hist = JSON.parse(localStorage.getItem('ch_history') || '[]');
  const el = document.getElementById('search-history');
  if (!el) return;
  if (!hist.length) { el.style.display = 'none'; return; }
  el.style.display = '';
  el.innerHTML = `<div style="display:flex;align-items:center;margin-bottom:6px">
    <span style="font-size:12px;color:#aaa;flex:1">${t('history_label')}</span>
    <button onclick="clearSearchHistory()" style="font-size:12px;color:#aaa;background:none;border:none;cursor:pointer">${t('history_clear')}</button>
  </div>
  <div style="display:flex;flex-wrap:wrap;gap:6px">
    ${hist.map(q => `<button class="quick-btn" onclick="useHistory('${q.replace(/'/g,"\\'")}')">${esc(q)}</button>`).join('')}
  </div>`;
}

function useHistory(q) {
  document.getElementById('query').value = q;
  doSearch();
}

function clearSearchHistory() {
  localStorage.removeItem('ch_history');
  renderSearchHistory();
}

// ── あとで読む ────────────────────────────────────────
let readLaterUrls = new Set();

async function loadReadLater() {
  const data = await api('/read-later');
  readLaterUrls = new Set(data.map(r => r.url));
  return data;
}

async function saveForLater(url, title, siteName) {
  await api('/read-later', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({url, title, site_name: siteName})});
  readLaterUrls.add(url);
  document.querySelectorAll('[data-later="' + CSS.escape(url) + '"]').forEach(btn => {
    btn.textContent = '✓'; btn.style.color = '#4a90d9';
  });
}

async function removeFromLater(id) {
  await api('/read-later/' + id, {method:'DELETE'});
  await renderLaterList();
}

async function renderLaterList() {
  const el = document.getElementById('later-results');
  if (!el) return;
  el.innerHTML = `<div class="loading">${t('loading')}</div>`;
  const data = await loadReadLater();
  if (!data.length) {
    el.innerHTML = `<div class="empty"><div class="empty-icon">📌</div>${t('later_empty')}</div>`;
    return;
  }
  el.innerHTML = data.map(r => `
    <div class="card" style="display:flex;align-items:flex-start;gap:8px;cursor:default">
      <div style="flex:1;min-width:0" onclick="openBrowser('${esc(r.url)}',false)" style="cursor:pointer">
        ${r.site_name ? `<div class="card-site"><img src="${getFavicon(r.url)}" class="favicon" onerror="this.style.display='none'">${esc(r.site_name)}</div>` : ''}
        <div class="card-title">${esc(r.title)}</div>
        <div class="card-url">${esc(r.url)}</div>
      </div>
      <button onclick="removeFromLater(${r.id})" style="background:none;border:none;font-size:18px;color:#ccc;cursor:pointer;padding:2px;flex-shrink:0">🗑️</button>
    </div>`).join('');
}

function showContentSeg(seg) {
  ['topics','later'].forEach(s => {
    document.getElementById('content-seg-' + s)?.classList.toggle('active', s === seg);
    const sec = document.getElementById(s === 'topics' ? 'topics-section' : 'later-section');
    if (sec) sec.style.display = s === seg ? '' : 'none';
  });
  if (seg === 'later') renderLaterList();
}

async function initApp() {
  const [langData, autoCrawlData, darkModeData] = await Promise.all([
    api('/settings/app_lang'),
    api('/settings/auto_crawl'),
    api('/settings/dark_mode'),
  ]);
  appLang = langData.value || 'ja';
  autoCrawl = autoCrawlData.value || 'off';
  applyLangButtons(appLang);
  applyTranslations();
  applyDarkMode(darkModeData.value || 'off');
  loadSites();
  renderSearchHistory();
  loadReadLater().catch(() => {});
  api('/topics/refresh', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({group_id:null})}).catch(()=>{});
  const lastPage = localStorage.getItem('ch_page');
  if (lastPage && document.getElementById('page-' + lastPage)) showPage(lastPage);
  handleAddParam();
}
initApp().catch(e => {
  document.body.innerHTML = `<div style="padding:40px;text-align:center;font-family:sans-serif">
    <div style="font-size:40px;margin-bottom:16px">⚠️</div>
    <div style="font-size:16px;color:#c62828;margin-bottom:8px">起動エラー</div>
    <div style="font-size:13px;color:#666;word-break:break-all">${e.message || e}</div>
    <button onclick="location.reload()" style="margin-top:24px;padding:10px 24px;background:#1a1a2e;color:#fff;border:none;border-radius:20px;font-size:14px;cursor:pointer">再読み込み</button>
  </div>`;
});

// プルトゥリフレッシュ
(function(){
  var startY = 0, pulling = false;
  var THRESHOLD = 80;
  var el = document.getElementById('ptr-indicator');
  var icon = document.getElementById('ptr-icon');
  var label = document.getElementById('ptr-label');

  document.addEventListener('touchstart', function(e){
    startY = e.touches[0].clientY;
    pulling = false;
  }, {passive:true});

  document.addEventListener('touchmove', function(e){
    var scrollTop = window.scrollY || document.documentElement.scrollTop;
    if(scrollTop > 0){ return; }
    var dy = e.touches[0].clientY - startY;
    if(dy <= 0){ return; }
    pulling = true;
    var ratio = Math.min(dy / THRESHOLD, 1);
    // -56px (hidden) → 0px (fully shown)
    el.style.top = (-56 + ratio * 56) + 'px';
    el.style.transition = 'none';
    if(dy >= THRESHOLD){
      icon.style.transform = 'rotate(180deg)';
      label.textContent = '離して更新';
    } else {
      icon.style.transform = 'rotate(0deg)';
      label.textContent = '引っ張って更新';
    }
  }, {passive:true});

  document.addEventListener('touchend', function(e){
    if(!pulling){ return; }
    pulling = false;
    var dy = e.changedTouches[0].clientY - startY;
    el.style.transition = 'top 0.2s ease';
    // スクロール位置の再チェックは省略（pullign フラグで上端から引いたことは保証済み）
    if(dy >= THRESHOLD){
      label.textContent = '更新中...';
      icon.textContent = '🔄';
      icon.style.transform = 'rotate(0deg)';
      setTimeout(function(){ location.reload(); }, 300);
    } else {
      el.style.top = '-56px';
    }
  }, {passive:true});
})();
</script>
</body>
</html>"""


class SiteCreate(BaseModel):
    name: str
    url: str
    group_id: Optional[int] = None


class SearchRequest(BaseModel):
    query: str
    group_id: Optional[int] = None


def fetch_page(url: str) -> tuple[str, str, str]:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; Choice/1.0)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            body = resp.read().decode(charset, errors="replace")
    except Exception as e:
        raise RuntimeError(f"Failed to fetch {url}: {e}")

    title_match = re.search(r"<title[^>]*>(.*?)</title>", body, re.IGNORECASE | re.DOTALL)
    title = html.unescape(title_match.group(1).strip()) if title_match else url

    text = re.sub(r"<script[^>]*>.*?</script>", " ", body, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()

    return title, text, body


def fetch_rss(site_url: str) -> list[dict]:
    UA = "Mozilla/5.0 (compatible; Choice/1.0)"
    try:
        req = urllib.request.Request(site_url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=10) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            body = resp.read().decode(charset, errors="replace")
    except Exception:
        return []

    # HTMLからRSSリンクを探す
    rss_url = None
    m = re.search(r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*href=["\']([^"\']+)["\']', body, re.IGNORECASE)
    if not m:
        m = re.search(r'<link[^>]+href=["\']([^"\']+)["\'][^>]*type=["\']application/(?:rss|atom)\+xml["\']', body, re.IGNORECASE)
    if m:
        href = m.group(1)
        parsed = urllib.parse.urlparse(site_url)
        rss_url = href if href.startswith("http") else f"{parsed.scheme}://{parsed.netloc}{href}"

    # 一般的なパスを試す
    if not rss_url:
        parsed = urllib.parse.urlparse(site_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        for path in ["/feed", "/rss", "/atom.xml", "/feed.xml", "/rss.xml", "/index.xml"]:
            try:
                r = urllib.request.Request(base + path, headers={"User-Agent": UA})
                with urllib.request.urlopen(r, timeout=5) as resp:
                    ct = resp.headers.get_content_type() or ""
                    if "xml" in ct or "rss" in ct:
                        rss_url = base + path
                        break
            except Exception:
                continue

    if not rss_url:
        return []

    try:
        r = urllib.request.Request(rss_url, headers={"User-Agent": UA})
        with urllib.request.urlopen(r, timeout=10) as resp:
            xml_body = resp.read()
        root = ET.fromstring(xml_body)
        items = []
        # RSS 2.0
        for item in root.findall(".//item")[:10]:
            title = (item.findtext("title") or "").strip()
            link  = (item.findtext("link")  or "").strip()
            pub   = (item.findtext("pubDate") or "").strip()
            if title and link:
                items.append({"title": title, "url": link, "published_at": pub})
        # Atom
        if not items:
            ns = "http://www.w3.org/2005/Atom"
            for entry in root.findall(f".//{{{ns}}}entry")[:10]:
                title    = (entry.findtext(f"{{{ns}}}title") or "").strip()
                link_el  = entry.find(f"{{{ns}}}link")
                link     = link_el.get("href", "") if link_el is not None else ""
                pub      = (entry.findtext(f"{{{ns}}}published") or entry.findtext(f"{{{ns}}}updated") or "").strip()
                if title and link:
                    items.append({"title": title, "url": link, "published_at": pub})
        return items
    except Exception:
        return []


def scrape_links(site_url: str) -> list[dict]:
    UA = "Mozilla/5.0 (compatible; Choice/1.0)"
    try:
        req = urllib.request.Request(site_url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=10) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            body = resp.read().decode(charset, errors="replace")
    except Exception:
        return []
    parsed = urllib.parse.urlparse(site_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    seen, items = set(), []
    for m in re.finditer(r'<a[^>]+href=["\']([^"\'#?][^"\']*)["\'][^>]*>(.*?)</a>', body, re.IGNORECASE | re.DOTALL):
        href = m.group(1).strip()
        text = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if not text or len(text) < 5 or len(text) > 120:
            continue
        full = href if href.startswith("http") else (base + href if href.startswith("/") else None)
        if not full or not full.startswith(base) or full == site_url or full in seen:
            continue
        seen.add(full)
        items.append({"title": text, "url": full, "published_at": ""})
        if len(items) >= 10:
            break
    return items


def fetch_site_topics(site: dict) -> list[dict]:
    items = fetch_rss(site["url"]) or scrape_links(site["url"])
    return [{"site_id": site["id"], "site_name": site["name"],
             "url": i["url"], "title": i["title"], "published_at": i.get("published_at", "")}
            for i in items[:10]]


def extract_links(base_url: str, body: str) -> list[str]:
    parsed = urllib.parse.urlparse(base_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    links = []
    for href in re.findall(r'href=["\']([^"\'#?]+)["\']', body, re.IGNORECASE):
        if href.startswith("http"):
            if href.startswith(base):
                links.append(href)
        elif href.startswith("/"):
            links.append(base + href)
    return list(set(links))


# ── License ──────────────────────────────────────────────
# Gumroad の Product Permalink。Gumroad 設定後に入力する。
GUMROAD_PRODUCT_ID = ""
FREE_SITE_LIMIT = 9999


def _get_license_status(conn, token: str) -> str:
    row = conn.execute("SELECT value FROM settings WHERE user_token=? AND key='license_status'", (token,)).fetchone()
    return row["value"] if row else "free"


@app.get("/license/status")
def license_status(token: str = Depends(get_token)):
    conn = get_db()
    status = _get_license_status(conn, token)
    key_row = conn.execute("SELECT value FROM settings WHERE user_token=? AND key='license_key'", (token,)).fetchone()
    conn.close()
    key = key_row["value"] if key_row else ""
    hint = (key[:4] + "-****-****") if len(key) > 4 else ""
    return {"status": status, "key_hint": hint}


@app.post("/license/activate")
def activate_license(body: dict, token: str = Depends(get_token)):
    key = (body.get("key") or "").strip().upper()
    if not key:
        raise HTTPException(400, "ライセンスキーを入力してください")

    if GUMROAD_PRODUCT_ID:
        data = urllib.parse.urlencode({
            "product_id": GUMROAD_PRODUCT_ID,
            "license_key": key,
            "increment_uses_count": "false",
        }).encode()
        try:
            req = urllib.request.Request(
                "https://api.gumroad.com/v2/licenses/verify",
                data=data,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
            if not result.get("success"):
                raise HTTPException(400, "無効なライセンスキーです")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, f"認証サーバーに接続できません: {str(e)[:60]}")
    else:
        if len(key) < 8:
            raise HTTPException(400, "無効なライセンスキーです")

    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO settings (user_token,key,value) VALUES (?,'license_key',?)", (token, key))
    conn.execute("INSERT OR REPLACE INTO settings (user_token,key,value) VALUES (?,'license_status','premium')", (token,))
    conn.commit()
    conn.close()
    return {"status": "premium"}


@app.delete("/license")
def deactivate_license(token: str = Depends(get_token)):
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO settings (user_token,key,value) VALUES (?,'license_key','')", (token,))
    conn.execute("INSERT OR REPLACE INTO settings (user_token,key,value) VALUES (?,'license_status','free')", (token,))
    conn.commit()
    conn.close()
    return {"status": "free"}


GENRE_LIST = [
    {"id":"pachinko", "label":"🎰 パチスロ・パチンコ", "query":"パチスロ パチンコ 攻略 情報 おすすめサイト"},
    {"id":"keiba",    "label":"🏇 競馬",               "query":"競馬 予想 攻略 情報 おすすめサイト"},
    {"id":"boat",     "label":"⛵ 競艇・競輪",          "query":"競艇 競輪 予想 情報 おすすめサイト"},
    {"id":"soccer",   "label":"⚽ サッカー",            "query":"サッカー ニュース 情報 おすすめサイト"},
    {"id":"baseball", "label":"⚾ 野球",               "query":"野球 ニュース 情報 おすすめサイト"},
    {"id":"martial",  "label":"🥊 格闘技",             "query":"格闘技 MMA ニュース 情報サイト"},
    {"id":"game",     "label":"🎮 ゲーム",             "query":"ゲーム 攻略 ニュース おすすめサイト"},
    {"id":"anime",    "label":"🎌 アニメ・マンガ",      "query":"アニメ マンガ ニュース 情報サイト"},
    {"id":"tech",     "label":"💻 IT・テック",          "query":"プログラミング エンジニア 技術情報 おすすめサイト"},
    {"id":"gadget",   "label":"📱 ガジェット",          "query":"ガジェット テック レビュー 情報サイト"},
    {"id":"news",     "label":"📰 ニュース",            "query":"ニュース 総合 情報サイト 日本"},
    {"id":"business", "label":"💼 ビジネス・経済",      "query":"ビジネス 経済 ニュース おすすめサイト"},
    {"id":"cooking",  "label":"🍳 料理・レシピ",        "query":"料理 レシピ 情報サイト おすすめ"},
    {"id":"health",   "label":"💪 健康・医療",          "query":"健康 医療 情報 おすすめサイト"},
    {"id":"travel",   "label":"✈️ 旅行",               "query":"旅行 観光 情報サイト おすすめ"},
    {"id":"fashion",  "label":"👗 ファッション",        "query":"ファッション トレンド 情報サイト"},
    {"id":"beauty",   "label":"💄 美容",               "query":"美容 コスメ スキンケア 情報サイト"},
    {"id":"career",   "label":"🏢 転職・就職",          "query":"転職 求人 就職 おすすめサイト"},
]

AD_BLOCK_DOMAINS = [
    "googlesyndication.com", "doubleclick.net", "googleadservices.com",
    "amazon-adsystem.com", "advertising.com", "adsafeprotected.com",
    "adnxs.com", "casalemedia.com", "rubiconproject.com", "pubmatic.com",
    "openx.net", "media.net", "moatads.com", "outbrain.com", "taboola.com",
    "criteo.com", "bidswitch.net", "smartadserver.com", "lijit.com",
]

INJECT_SCRIPT = """
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
ins.adsbygoogle,[data-ad-slot],[data-ad-client],[data-ad-unit-id],
.adsbygoogle,.ad-banner,.ad-container,.ad-slot,.ad-wrapper,.ad-block,
.advertisement,.banner-ad,.sponsored,.sponsor,.promo-banner,
[class*="adUnit"],[class*="ad-unit"],[class*="AdUnit"],
[id*="google_ads"],[id*="aswift"],[id*="ad_iframe"],
iframe[src*="doubleclick"],iframe[src*="googlesyndication"],
iframe[src*="amazon-adsystem"],iframe[src*="taboola"],
div[id^="div-gpt-ad"],div[class*="dfp-"]
{display:none!important;height:0!important;visibility:hidden!important}
img,video,table,pre{max-width:100%!important;height:auto!important}
</style>
<script>
(function(){
  // リンククリックを親に通知してプロキシ内でナビゲート
  document.addEventListener('click', function(e){
    var a = e.target.closest('a');
    if(a && a.href && !a.href.startsWith('javascript:') && !a.href.startsWith('mailto:')){
      e.preventDefault();
      window.parent.postMessage({type:'nav', url: a.href}, '*');
    }
  }, true);
  // フォーム送信も通知
  document.addEventListener('submit', function(e){
    e.preventDefault();
    var f = e.target;
    var url = f.action || location.href;
    if(f.method && f.method.toLowerCase()==='get'){
      var params = new URLSearchParams(new FormData(f)).toString();
      window.parent.postMessage({type:'nav', url: url + (url.includes('?')?'&':'?') + params}, '*');
    }
  }, true);
  // 動的に追加される広告要素も非表示
  var adSelectors = [
    'ins.adsbygoogle','[data-ad-slot]','[data-ad-client]',
    '[class*="adUnit"]','[id*="google_ads"]','[id*="aswift"]',
    'div[id^="div-gpt-ad"]'
  ].join(',');
  var obs = new MutationObserver(function(muts){
    muts.forEach(function(m){
      m.addedNodes.forEach(function(n){
        if(n.nodeType===1){
          if(n.matches && n.matches(adSelectors)) n.style.cssText='display:none!important';
          n.querySelectorAll && n.querySelectorAll(adSelectors).forEach(function(el){
            el.style.cssText='display:none!important';
          });
        }
      });
    });
  });
  document.addEventListener('DOMContentLoaded', function(){
    obs.observe(document.body, {childList:true, subtree:true});
  });
  // 画面幅に合わせて縮小（CSS zoom を使用して親ページのレイアウトに影響させない）
  var _fitLock = false;
  function autoFit(){
    if(_fitLock) return;
    var docW = Math.max(
      document.documentElement.scrollWidth,
      document.body ? document.body.scrollWidth : 0
    );
    var winW = window.innerWidth;
    if(docW > winW + 5){
      _fitLock = true;
      var scale = (winW / docW).toFixed(3);
      document.documentElement.style.zoom = scale;
      setTimeout(function(){ _fitLock = false; }, 400);
    }
  }
  var _fitTimer;
  function scheduleFit(){ clearTimeout(_fitTimer); _fitTimer = setTimeout(autoFit, 120); }

  document.addEventListener('DOMContentLoaded', autoFit);
  window.addEventListener('load', function(){
    autoFit();
    setTimeout(autoFit, 600);
    setTimeout(autoFit, 1800);
    setTimeout(autoFit, 4000);
    document.querySelectorAll('img').forEach(function(img){
      if(!img.complete) img.addEventListener('load', scheduleFit);
    });
    window.parent.postMessage({type:'title', title: document.title, url: location.href}, '*');
  });
  // 動的コンテンツ変化にも対応
  if(window.ResizeObserver){
    var ro = new ResizeObserver(scheduleFit);
    document.addEventListener('DOMContentLoaded', function(){
      if(document.body) ro.observe(document.body);
    });
  }
})();
</script>
"""

def _is_safe_proxy_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return False
        hostname = parsed.hostname or ''
        if not hostname:
            return False
        addr = socket.gethostbyname(hostname)
        ip = ipaddress.ip_address(addr)
        return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved)
    except Exception:
        return False


@app.get("/proxy")
def proxy(url: str):
    if not _is_safe_proxy_url(url):
        error_html = """<html><body style="font-family:-apple-system;padding:40px;text-align:center;color:#666">
        <p style="font-size:48px">🚫</p>
        <p style="font-size:16px;font-weight:bold">このURLにはアクセスできません</p>
        </body></html>"""
        return Response(content=error_html, media_type="text/html", status_code=403)
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            content_type = resp.headers.get_content_type() or "text/html"
            charset = resp.headers.get_content_charset() or "utf-8"
            body = resp.read()
    except Exception as e:
        msg = str(e)
        tip = "このサイトはアプリ内表示をブロックしています。Safari で直接開いてください。" if "403" in msg or "429" in msg or "SSL" in msg else "ページを読み込めませんでした。"
        error_html = f"""<html><body style="font-family:-apple-system;padding:40px;text-align:center;color:#666">
        <p style="font-size:48px">⚠️</p>
        <p style="font-size:16px;font-weight:bold;margin-bottom:12px">{tip}</p>
        <p style="font-size:12px;color:#aaa;margin-bottom:24px">{msg[:120]}</p>
        <a href="{url}" target="_blank" style="background:#1a1a2e;color:#fff;padding:12px 24px;border-radius:20px;text-decoration:none;font-size:14px">Safari で開く</a>
        </body></html>"""
        return Response(content=error_html, media_type="text/html")

    if "html" in content_type:
        text = body.decode(charset, errors="replace")
        base_tag = f'<base href="{url}">'
        # CSP と既存 viewport メタタグを除去
        text = re.sub(r'<meta[^>]*content-security-policy[^>]*>', '', text, flags=re.IGNORECASE)
        text = re.sub(r'<meta[^>]*name=["\']viewport["\'][^>]*/?>',  '', text, flags=re.IGNORECASE)
        # 既知の広告ドメインの script/iframe タグをサーバー側で除去
        for domain in AD_BLOCK_DOMAINS:
            text = re.sub(
                r'<script[^>]*src=["\'][^"\']*' + re.escape(domain) + r'[^"\']*["\'][^>]*>.*?</script>',
                '', text, flags=re.IGNORECASE | re.DOTALL
            )
            text = re.sub(
                r'<script[^>]*src=["\'][^"\']*' + re.escape(domain) + r'[^"\']*["\'][^>]*/?>',
                '', text, flags=re.IGNORECASE
            )
            text = re.sub(
                r'<iframe[^>]*src=["\'][^"\']*' + re.escape(domain) + r'[^"\']*["\'][^>]*>.*?</iframe>',
                '', text, flags=re.IGNORECASE | re.DOTALL
            )
        # base タグとスクリプトを注入
        if re.search(r'<head[^>]*>', text, re.IGNORECASE):
            text = re.sub(r'(<head[^>]*>)', r'\1' + base_tag + INJECT_SCRIPT, text, count=1, flags=re.IGNORECASE)
        else:
            text = base_tag + INJECT_SCRIPT + text
        body = text.encode("utf-8")
        content_type = "text/html; charset=utf-8"

    return Response(
        content=body,
        media_type=content_type,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/manifest.json")
def manifest():
    data = {
        "name": "Choice",
        "short_name": "Choice",
        "description": "好きなサイトだけをまとめてキーワード検索",
        "start_url": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#1a1a2e",
        "theme_color": "#1a1a2e",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    }
    return Response(content=json.dumps(data), media_type="application/manifest+json")


@app.get("/icon-{size}.png")
def icon(size: int):
    path = Path(__file__).parent / f"icon-{size}.png"
    if not path.exists():
        raise HTTPException(404)
    return Response(content=path.read_bytes(), media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(content=HTML, headers={"Cache-Control": "no-store, no-cache, must-revalidate"})


class SettingUpdate(BaseModel):
    value: str


@app.get("/settings/{key}")
def get_setting(key: str, token: str = Depends(get_token)):
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE user_token=? AND key=?", (token, key)).fetchone()
    conn.close()
    return {"value": row["value"] if row else ""}


@app.post("/settings/{key}")
def save_setting(key: str, body: SettingUpdate, token: str = Depends(get_token)):
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO settings (user_token, key, value) VALUES (?,?,?)", (token, key, body.value))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/groups")
def list_groups(token: str = Depends(get_token)):
    conn = get_db()
    rows = conn.execute("""
        SELECT g.id, g.name, COUNT(s.id) as site_count
        FROM groups g LEFT JOIN sites s ON s.group_id = g.id AND s.user_token=?
        WHERE g.user_token=?
        GROUP BY g.id ORDER BY g.name
    """, (token, token)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/topics")
def get_topics(group_id: Optional[int] = None, token: str = Depends(get_token)):
    conn = get_db()
    if group_id:
        rows = conn.execute("""
            SELECT t.id, t.url, t.title, t.published_at, t.fetched_at, s.name as site_name,
                   CASE WHEN tr.url IS NOT NULL THEN 1 ELSE 0 END as is_read
            FROM topics t
            JOIN sites s ON s.id = t.site_id
            LEFT JOIN topic_reads tr ON tr.url = t.url AND tr.user_token=?
            WHERE t.user_token=? AND s.group_id=?
            ORDER BY t.id DESC LIMIT 50
        """, (token, token, group_id)).fetchall()
    else:
        rows = conn.execute("""
            SELECT t.id, t.url, t.title, t.published_at, t.fetched_at, s.name as site_name,
                   CASE WHEN tr.url IS NOT NULL THEN 1 ELSE 0 END as is_read
            FROM topics t
            JOIN sites s ON s.id = t.site_id
            LEFT JOIN topic_reads tr ON tr.url = t.url AND tr.user_token=?
            WHERE t.user_token=?
            ORDER BY t.id DESC LIMIT 50
        """, (token, token)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/topics/refresh")
def refresh_topics(body: dict, token: str = Depends(get_token)):
    group_id = body.get("group_id")
    conn = get_db()
    if group_id:
        sites = conn.execute("SELECT * FROM sites WHERE user_token=? AND group_id=?", (token, group_id)).fetchall()
    else:
        sites = conn.execute("SELECT * FROM sites WHERE user_token=?", (token,)).fetchall()
    conn.close()
    if not sites:
        return {"ok": True, "count": 0}

    all_topics = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fetch_site_topics, dict(s)): dict(s) for s in sites}
        for future in as_completed(futures, timeout=30):
            try:
                all_topics.extend(future.result())
            except Exception:
                pass

    all_topics = all_topics[:50]
    conn = get_db()
    site_ids = [s["id"] for s in sites]
    conn.execute(f"DELETE FROM topics WHERE user_token=? AND site_id IN ({','.join('?'*len(site_ids))})", [token] + site_ids)
    for t in all_topics:
        conn.execute("INSERT INTO topics (user_token, site_id, url, title, published_at) VALUES (?,?,?,?,?)",
                     (token, t["site_id"], t["url"], t["title"], t.get("published_at", "")))
    conn.commit()
    conn.close()
    return {"ok": True, "count": len(all_topics)}


@app.post("/topics/read")
def mark_read(body: dict, token: str = Depends(get_token)):
    url = body.get("url", "")
    if not url:
        raise HTTPException(status_code=400, detail="url required")
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO topic_reads (user_token, url) VALUES (?,?)", (token, url))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/read-later")
def get_read_later(token: str = Depends(get_token)):
    conn = get_db()
    rows = conn.execute("SELECT * FROM read_later WHERE user_token=? ORDER BY added_at DESC", (token,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/read-later", status_code=201)
def add_read_later(body: dict, token: str = Depends(get_token)):
    url = (body.get("url") or "").strip()
    title = (body.get("title") or url).strip()
    site_name = (body.get("site_name") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="url required")
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO read_later (user_token, url, title, site_name) VALUES (?,?,?,?)", (token, url, title, site_name))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.delete("/read-later/{item_id}")
def delete_read_later(item_id: int, token: str = Depends(get_token)):
    conn = get_db()
    conn.execute("DELETE FROM read_later WHERE id=? AND user_token=?", (item_id, token))
    conn.commit()
    conn.close()
    return {"ok": True}


class GroupCreate(BaseModel):
    name: str

@app.post("/groups", status_code=201)
def create_group(body: GroupCreate, token: str = Depends(get_token)):
    conn = get_db()
    name = body.name.strip()
    existing = conn.execute("SELECT id FROM groups WHERE user_token=? AND name=?", (token, name)).fetchone()
    if existing:
        conn.close()
        raise HTTPException(status_code=409, detail="同じ名前のグループが存在します")
    conn.execute("INSERT INTO groups (user_token, name) VALUES (?,?)", (token, name))
    conn.commit()
    row = conn.execute("SELECT * FROM groups WHERE user_token=? AND name=?", (token, name)).fetchone()
    conn.close()
    return dict(row)

@app.post("/groups/find-or-create", status_code=200)
def find_or_create_group(body: dict, token: str = Depends(get_token)):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="グループ名を入力してください")
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM groups WHERE user_token=? AND name=?", (token, name)).fetchone()
        if row:
            return dict(row)
        conn.execute("INSERT OR IGNORE INTO groups (user_token, name) VALUES (?,?)", (token, name))
        conn.commit()
        row = conn.execute("SELECT * FROM groups WHERE user_token=? AND name=?", (token, name)).fetchone()
        return dict(row)
    finally:
        conn.close()


@app.patch("/groups/{group_id}")
def rename_group(group_id: int, body: dict, token: str = Depends(get_token)):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="グループ名を入力してください")
    conn = get_db()
    existing = conn.execute("SELECT id FROM groups WHERE user_token=? AND name=? AND id!=?", (token, name, group_id)).fetchone()
    if existing:
        conn.close()
        raise HTTPException(status_code=400, detail="同じ名前のグループが既に存在します")
    conn.execute("UPDATE groups SET name=? WHERE id=? AND user_token=?", (name, group_id, token))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/search-sites")
def search_sites_endpoint(q: str, token: str = Depends(get_token)):
    conn = get_db()
    def s(key): r = conn.execute("SELECT value FROM settings WHERE user_token=? AND key=?", (token, key)).fetchone(); return r["value"] if r else None
    _env_brave = os.environ.get("BRAVE_API_KEY", "")
    provider  = s("search_provider") or ("brave" if _env_brave else "yahoo")
    brave_key = s("brave_api_key") or _env_brave
    google_key = s("google_api_key")
    google_cx  = s("google_cx")
    registered_rows = conn.execute("SELECT url FROM sites WHERE user_token=?", (token,)).fetchall()
    conn.close()

    registered_origins = set()
    for r in registered_rows:
        try:
            registered_origins.add(urllib.parse.urlparse(r["url"]).netloc)
        except Exception:
            pass

    raw_results = []
    if provider == "brave" and brave_key:
        url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({"q": q, "count": 20, "search_lang": "ja", "country": "JP"})
        try:
            ureq = urllib.request.Request(url, headers={"Accept": "application/json", "Accept-Encoding": "gzip", "X-Subscription-Token": brave_key})
            with urllib.request.urlopen(ureq, timeout=10) as resp:
                data = json.loads(resp.read())
            for r in data.get("web", {}).get("results", []):
                raw_results.append({"url": r.get("url",""), "title": r.get("title",""), "description": r.get("description","")})
        except Exception:
            pass
    elif provider == "google" and google_key and google_cx:
        url = "https://www.googleapis.com/customsearch/v1?" + urllib.parse.urlencode({"key": google_key, "cx": google_cx, "q": q, "num": 10})
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read())
            for r in data.get("items", []):
                raw_results.append({"url": r.get("link",""), "title": r.get("title",""), "description": r.get("snippet","")})
        except Exception:
            pass
    else:
        try:
            sess = cffi_requests.Session(impersonate="chrome124")
            resp = sess.get("https://search.yahoo.co.jp/search", params={"p": q}, timeout=10)
            body = resp.text
            cards = re.findall(r'<div class="sw-Card Algo[^"]*">(.*?)(?=<div class="sw-Card Algo|</section>)', body, re.DOTALL)
            for card in cards[:30]:
                url_m   = re.search(r'<a href="(https://[^"]+)" class="sw-Card__titleInner', card)
                title_m = re.search(r'class="sw-Card__titleMain[^"]*"[^>]*>(.*?)</(?:h3|span|a|div)>', card, re.DOTALL)
                desc_m  = re.search(r'class="sw-Card__bodyItem[^"]*"[^>]*>(.*?)</(?:p|div)>', card, re.DOTALL)
                if url_m:
                    raw_results.append({
                        "url": url_m.group(1),
                        "title": html.unescape(re.sub(r"<[^>]+>", "", title_m.group(1))).strip() if title_m else "",
                        "description": html.unescape(re.sub(r"<[^>]+>", "", desc_m.group(1))).strip()[:120] if desc_m else ""
                    })
        except Exception:
            pass

    seen = set()
    results = []
    for r in raw_results:
        try:
            parsed = urllib.parse.urlparse(r["url"])
            origin = parsed.scheme + "://" + parsed.netloc
            netloc = parsed.netloc
        except Exception:
            continue
        if netloc in seen:
            continue
        seen.add(netloc)
        raw_title = r["title"]
        site_name = netloc.replace("www.", "")
        page_title = raw_title
        for sep in ["|", "｜", "–", "—"]:
            parts = raw_title.rsplit(sep, 1)
            if len(parts) == 2 and 1 < len(parts[1].strip()) < 30:
                site_name = parts[1].strip()
                page_title = parts[0].strip()
                break
        results.append({
            "url": r["url"],
            "site_url": origin,
            "title": page_title or raw_title,
            "site_name": site_name,
            "excerpt": r["description"],
            "is_registered": netloc in registered_origins,
        })
        if len(results) >= 10:
            break
    return {"results": results}


@app.get("/genres")
def get_genres():
    return GENRE_LIST


@app.get("/discover")
def discover(genre: str, token: str = Depends(get_token)):
    genre_item = next((g for g in GENRE_LIST if g["id"] == genre), None)
    if not genre_item:
        raise HTTPException(status_code=404, detail="ジャンルが見つかりません")

    conn = get_db()
    registered_rows = conn.execute("SELECT url FROM sites WHERE user_token=?", (token,)).fetchall()
    conn.close()
    registered_origins = set()
    for r in registered_rows:
        try:
            registered_origins.add(urllib.parse.urlparse(r["url"]).netloc)
        except Exception:
            pass

    try:
        sess = cffi_requests.Session(impersonate="chrome124")
        resp = sess.get("https://search.yahoo.co.jp/search",
                        params={"p": genre_item["query"]}, timeout=15)
        body = resp.text
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    cards = re.findall(
        r'<div class="sw-Card Algo[^"]*">(.*?)(?=<div class="sw-Card Algo|</section>)',
        body, re.DOTALL)

    seen = set()
    results = []
    for card in cards[:30]:
        url_m   = re.search(r'<a href="(https://[^"]+)" class="sw-Card__titleInner', card)
        title_m = re.search(r'class="sw-Card__titleMain[^"]*"[^>]*>(.*?)</(?:h3|span|a|div)>', card, re.DOTALL)
        desc_m  = re.search(r'class="sw-Card__bodyItem[^"]*"[^>]*>(.*?)</(?:p|div)>', card, re.DOTALL)
        if not url_m:
            continue
        try:
            parsed = urllib.parse.urlparse(url_m.group(1))
            origin = parsed.scheme + "://" + parsed.netloc
            netloc = parsed.netloc
        except Exception:
            continue
        if netloc in seen:
            continue
        seen.add(netloc)

        raw_title = html.unescape(re.sub(r"<[^>]+>", "", title_m.group(1))).strip() if title_m else ""
        name = netloc.replace("www.", "")
        for sep in ["|", "｜", "–", "—"]:
            parts = raw_title.rsplit(sep, 1)
            if len(parts) == 2 and 1 < len(parts[1].strip()) < 30:
                name = parts[1].strip()
                break

        description = html.unescape(re.sub(r"<[^>]+>", "", desc_m.group(1))).strip()[:120] if desc_m else ""

        results.append({
            "url": origin,
            "name": name,
            "description": description,
            "is_registered": netloc in registered_origins,
        })

    return {"genre_label": genre_item["label"], "results": results}


@app.delete("/groups/{group_id}")
def delete_group(group_id: int, token: str = Depends(get_token)):
    conn = get_db()
    conn.execute("UPDATE sites SET group_id=NULL WHERE group_id=? AND user_token=?", (group_id, token))
    conn.execute("DELETE FROM groups WHERE id=? AND user_token=?", (group_id, token))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/sites")
def list_sites(token: str = Depends(get_token)):
    conn = get_db()
    rows = conn.execute("""
        SELECT s.*, g.name as group_name
        FROM sites s LEFT JOIN groups g ON g.id = s.group_id
        WHERE s.user_token=?
        ORDER BY s.created_at DESC
    """, (token,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


class SiteUpdate(BaseModel):
    group_id: Optional[int] = None

@app.patch("/sites/{site_id}")
def update_site(site_id: int, body: SiteUpdate, token: str = Depends(get_token)):
    conn = get_db()
    conn.execute("UPDATE sites SET group_id=? WHERE id=? AND user_token=?", (body.group_id, site_id, token))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/sites", status_code=201)
def add_site(site: SiteCreate, token: str = Depends(get_token)):
    url = site.url.rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    conn = get_db()
    if _get_license_status(conn, token) != "premium":
        count = conn.execute("SELECT COUNT(*) as c FROM sites WHERE user_token=?", (token,)).fetchone()["c"]
        if count >= FREE_SITE_LIMIT:
            conn.close()
            raise HTTPException(403, f"無料版はサイトを{FREE_SITE_LIMIT}件まで登録できます。プレミアムにアップグレードしてください。")
    cur = conn.execute("INSERT INTO sites (user_token, name, url, group_id) VALUES (?,?,?,?)", (token, site.name, url, site.group_id))
    conn.commit()
    site_id = cur.lastrowid
    conn.close()

    conn2 = get_db()
    auto = conn2.execute("SELECT value FROM settings WHERE user_token=? AND key='auto_crawl'", (token,)).fetchone()
    conn2.close()
    if auto and auto["value"] == "on":
        _do_crawl(site_id)
    return {"id": site_id, "name": site.name, "url": url}


@app.delete("/sites/{site_id}")
def delete_site(site_id: int, token: str = Depends(get_token)):
    conn = get_db()
    conn.execute("DELETE FROM pages WHERE site_id=?", (site_id,))
    conn.execute("DELETE FROM sites WHERE id=? AND user_token=?", (site_id, token))
    conn.commit()
    conn.close()
    return {"ok": True}


def _do_crawl(site_id: int, max_pages: int = 30):
    conn = get_db()
    row = conn.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone()
    if not row:
        conn.close()
        return {"indexed": 0, "site_id": site_id}
    base_url = row["url"]
    conn.execute("DELETE FROM pages WHERE site_id=?", (site_id,))
    conn.commit()
    visited = set()
    queue = [base_url]
    indexed = 0
    while queue and indexed < max_pages:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        try:
            title, text, body = fetch_page(url)
            conn.execute("INSERT INTO pages (site_id, url, title, content) VALUES (?,?,?,?)", (site_id, url, title, text))
            conn.commit()
            indexed += 1
            for link in extract_links(base_url, body):
                if link not in visited:
                    queue.append(link)
        except Exception:
            continue
    conn.close()
    return {"indexed": indexed, "site_id": site_id}


@app.post("/crawl/{site_id}")
def crawl_site(site_id: int, max_pages: int = 30, token: str = Depends(get_token)):
    conn = get_db()
    row = conn.execute("SELECT id FROM sites WHERE id=? AND user_token=?", (site_id, token)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Site not found")
    return _do_crawl(site_id, max_pages)


def parse_search_query(query: str):
    terms = query.split()
    includes = [t for t in terms if not (t.startswith('-') and len(t) > 1)]
    excludes = [t[1:] for t in terms if t.startswith('-') and len(t) > 1]
    return includes, excludes


@app.post("/search")
def search(req: SearchRequest, token: str = Depends(get_token)):
    conn = get_db()
    if req.group_id:
        sites = conn.execute("SELECT * FROM sites WHERE user_token=? AND group_id=?", (token, req.group_id)).fetchall()
    else:
        sites = conn.execute("SELECT * FROM sites WHERE user_token=?", (token,)).fetchall()
    if not sites:
        conn.close()
        return {"results": [], "query": req.query, "count": 0}

    includes, excludes = parse_search_query(req.query)

    # trigram は3文字未満をサポートしないのでフィルタ
    words = [w for w in includes if len(w) >= 3]
    if not words:
        words = includes if includes else [req.query]

    site_id_list = [s["id"] for s in sites]
    placeholders = ",".join("?" * len(site_id_list))
    fts_query = " OR ".join(words)

    try:
        limit = 30 + len(excludes) * 15 if excludes else 30
        rows = conn.execute(
            f"""
            SELECT p.url, p.title, snippet(pages, 3, '【', '】', '...', 25) AS excerpt, p.site_id
            FROM pages p
            WHERE pages MATCH ? AND p.site_id IN ({placeholders})
            ORDER BY rank
            LIMIT {limit}
            """,
            [fts_query] + site_id_list,
        ).fetchall()
    except Exception:
        rows = []

    # 除外ワードでフィルター
    if excludes:
        rows = [r for r in rows if not any(
            ex.lower() in (r["title"] + " " + r["excerpt"]).lower()
            for ex in excludes
        )]

    rows = rows[:30]
    site_map = {s["id"]: s["name"] for s in sites}
    results = [
        {
            "url": row["url"],
            "title": row["title"],
            "excerpt": row["excerpt"],
            "site_name": site_map.get(row["site_id"], ""),
        }
        for row in rows
    ]

    conn.close()
    return {"results": results, "query": req.query, "count": len(results)}



@app.post("/search/web")
def search_web(req: SearchRequest, token: str = Depends(get_token)):
    conn = get_db()
    if req.group_id:
        sites = conn.execute("SELECT * FROM sites WHERE user_token=? AND group_id=?", (token, req.group_id)).fetchall()
    else:
        sites = conn.execute("SELECT * FROM sites WHERE user_token=?", (token,)).fetchall()
    def s(key): r = conn.execute("SELECT value FROM settings WHERE user_token=? AND key=?", (token, key)).fetchone(); return r["value"] if r else None
    _env_brave  = os.environ.get("BRAVE_API_KEY", "")
    provider    = s("search_provider") or ("brave" if _env_brave else "yahoo")
    brave_key   = s("brave_api_key") or _env_brave
    google_key  = s("google_api_key")
    google_cx   = s("google_cx")
    conn.close()

    if not sites:
        return {"results": [], "query": req.query, "count": 0}

    def fetch_brave(site):
        domain = urllib.parse.urlparse(site["url"]).netloc
        q = f"{req.query} site:{domain}"
        url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({"q": q, "count": 10, "search_lang": "ja", "country": "JP"})
        try:
            ureq = urllib.request.Request(url, headers={"Accept": "application/json", "Accept-Encoding": "gzip", "X-Subscription-Token": brave_key})
            with urllib.request.urlopen(ureq, timeout=10) as resp:
                data = json.loads(resp.read())
            return [{"url": r.get("url",""), "title": r.get("title",""), "excerpt": r.get("description",""), "site_name": site["name"]}
                    for r in data.get("web", {}).get("results", [])]
        except Exception:
            return []

    def fetch_google(site):
        domain = urllib.parse.urlparse(site["url"]).netloc
        q = f"{req.query} site:{domain}"
        url = "https://www.googleapis.com/customsearch/v1?" + urllib.parse.urlencode({"key": google_key, "cx": google_cx, "q": q, "num": 10})
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read())
            return [{"url": r.get("link",""), "title": r.get("title",""), "excerpt": r.get("snippet",""), "site_name": site["name"]}
                    for r in data.get("items", [])]
        except Exception:
            return []

    def fetch_ddg(site):
        domain = urllib.parse.urlparse(site["url"]).netloc
        q = f"{req.query} site:{domain}"
        try:
            s_sess = cffi_requests.Session(impersonate="chrome124")
            resp = s_sess.get("https://html.duckduckgo.com/html/", params={"q": q}, timeout=10,
                              headers={"Accept-Language": "ja,en;q=0.9"})
            body = resp.text
        except Exception:
            return []
        results = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', body, re.DOTALL)
        items = []
        for raw_url, raw_title in results[:10]:
            m = re.search(r'uddg=([^&"]+)', raw_url)
            if not m:
                continue
            actual_url = urllib.parse.unquote(m.group(1))
            if "duckduckgo.com/y.js" in actual_url:
                continue
            title = html.unescape(re.sub(r"<[^>]+>", "", raw_title)).strip()
            if title:
                items.append({"url": actual_url, "title": title, "excerpt": "", "site_name": site["name"]})
        return items

    def fetch_yahoo(site):
        domain = urllib.parse.urlparse(site["url"]).netloc
        q = f"{req.query} site:{domain}"
        try:
            s_sess = cffi_requests.Session(impersonate="chrome124")
            resp = s_sess.get("https://search.yahoo.co.jp/search", params={"p": q}, timeout=10)
            body = resp.text
        except Exception:
            return []
        cards = re.findall(r'<div class="sw-Card Algo[^"]*">(.*?)(?=<div class="sw-Card Algo|</section>)', body, re.DOTALL)
        items = []
        for card in cards[:10]:
            url_m   = re.search(r'<a href="(https://[^"]+)" class="sw-Card__titleInner', card)
            title_m = re.search(r'class="sw-Card__titleMain[^"]*"[^>]*>(.*?)</(?:h3|span|a|div)>', card, re.DOTALL)
            if not url_m or not title_m:
                continue
            items.append({"url": url_m.group(1), "title": html.unescape(re.sub(r"<[^>]+>","",title_m.group(1))).strip(), "excerpt": "", "site_name": site["name"]})
        return items

    if provider == "brave" and brave_key:
        fetch_fn = fetch_brave
    elif provider == "google" and google_key and google_cx:
        fetch_fn = fetch_google
    else:
        fetch_fn = fetch_ddg

    results = []
    with ThreadPoolExecutor(max_workers=min(len(sites), 8)) as ex:
        futures = {ex.submit(fetch_fn, site): site for site in sites}
        for fut in as_completed(futures):
            results.extend(fut.result())

    return {"results": results, "query": req.query, "count": len(results)}


@app.post("/search/explore")
def search_explore(req: SearchRequest, token: str = Depends(get_token)):
    conn = get_db()
    def sv(key): r = conn.execute("SELECT value FROM settings WHERE user_token=? AND key=?", (token, key)).fetchone(); return r["value"] if r else None
    _env_brave = os.environ.get("BRAVE_API_KEY", "")
    provider   = sv("search_provider") or ("brave" if _env_brave else "yahoo")
    brave_key  = sv("brave_api_key") or _env_brave
    google_key = sv("google_api_key")
    google_cx  = sv("google_cx")
    conn.close()

    results = []
    q = req.query

    if provider == "brave" and brave_key:
        url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({"q": q, "count": 20, "search_lang": "ja", "country": "JP"})
        try:
            ureq = urllib.request.Request(url, headers={"Accept": "application/json", "Accept-Encoding": "gzip", "X-Subscription-Token": brave_key})
            with urllib.request.urlopen(ureq, timeout=15) as resp:
                data = json.loads(resp.read())
            for r in data.get("web", {}).get("results", []):
                results.append({"url": r.get("url",""), "title": r.get("title",""), "excerpt": r.get("description","")})
        except Exception:
            pass

    elif provider == "google" and google_key and google_cx:
        url = "https://www.googleapis.com/customsearch/v1?" + urllib.parse.urlencode({"key": google_key, "cx": google_cx, "q": q, "num": 10})
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = json.loads(resp.read())
            for r in data.get("items", []):
                results.append({"url": r.get("link",""), "title": r.get("title",""), "excerpt": r.get("snippet","")})
        except Exception:
            pass

    else:
        try:
            s_sess = cffi_requests.Session(impersonate="chrome124")
            resp = s_sess.get("https://html.duckduckgo.com/html/", params={"q": q}, timeout=15,
                              headers={"Accept-Language": "ja,en;q=0.9"})
            body = resp.text
            for raw_url, raw_title in re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', body, re.DOTALL)[:20]:
                m = re.search(r'uddg=([^&"]+)', raw_url)
                if not m:
                    continue
                actual_url = urllib.parse.unquote(m.group(1))
                if "duckduckgo.com/y.js" in actual_url:
                    continue
                title = html.unescape(re.sub(r"<[^>]+>", "", raw_title)).strip()
                if title:
                    results.append({"url": actual_url, "title": title, "excerpt": ""})
        except Exception:
            pass

    return {"results": results, "query": req.query, "count": len(results)}
