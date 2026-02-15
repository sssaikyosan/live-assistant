# 配信アシスタント (Skills + CLI)

ライブ配信を AI がアシストするシステム。
エージェントは Skill を読み、I/O は CLI 経由で操作する。

## アーキテクチャ

```
┌──────────────────────┐  CLI/HTTP  ┌──────────────────────────┐
│   Claude Code Agent  │◄──────────►│ live-assistant service    │
│ + SKILL.md           │           │ src/live_cli.py + HTTP API│
└──────────────────────┘            └──────────┬───────────────┘
                                               │
                ┌──────────────────────────────┼──────────────────┐
                │                              │                  │
         ┌──────▼──────┐              ┌────────▼──────┐  ┌───────▼──────┐
         │  わんコメ    │              │  VOICEVOX     │  │  OBS         │
         │  :11180 (WS) │              │  :50021       │  │  :4455 (WS)  │
         │  コメント受信│              │  音声合成     │  │  画面取得    │
         └─────────────┘              └───────────────┘  └──────────────┘
```

## 必要なもの

- Python 3.11+
- [Claude Code](https://claude.ai/code) (エージェント実行環境)
- [VOICEVOX](https://voicevox.hiroshiba.jp/) (音声合成エンジン)
- [わんコメ (OneComme)](https://onecomme.com/) (マルチプラットフォームコメントビューア)
- [OBS Studio](https://obsproject.com/) (配信ソフト、WebSocket API で画面取得)

## セットアップ

### 1. Python 環境

Python 3.11+ が必要。CUDA 対応 GPU がある場合は faster-whisper が GPU を使用する。

```bash
pip install -e .
```

これにより `live-assistant` コマンドがグローバルに使えるようになる。

### 2. 外部サービス

1. **VOICEVOX** を起動 (`localhost:50021`)
2. **わんコメ** を起動し、配信URLを接続 (`localhost:11180`)
3. **OBS** を起動し、WebSocket サーバーを有効化 (`localhost:4455`)
   - ツール → WebSocket サーバー設定 → WebSocket サーバーを有効にする

### 3. 設定

`config.yaml` で各サービスの接続先やパラメータを調整する。

| セクション | キー | デフォルト | 説明 |
|-----------|------|-----------|------|
| `voicevox` | `url` | `http://localhost:50021` | VOICEVOX エンジンURL |
| | `speaker_id` | `1` | 話者ID（1=ずんだもん ノーマル） |
| `vad` | `speech_threshold` | `0.5` | VAD確率の閾値 |
| | `silence_duration` | `1.5` | 沈黙何秒で発話終了とみなすか |
| | `min_speech_sec` | `0.0` | 最短発話長（未満はスキップ） |
| | `max_speech_sec` | `30` | 最大バッファ秒数（超過で強制転写） |
| | `pre_buffer_sec` | `0.5` | プリバッファ（発話冒頭の切れ防止） |
| `obs` | `host` | `127.0.0.1` | OBS WebSocket ホスト |
| | `port` | `4455` | OBS WebSocket ポート |
| | `password` | `""` | OBS WebSocket パスワード（空=認証なし） |
| `onecomme` | `enabled` | `true` | わんコメからのコメント受信 |
| | `host` | `127.0.0.1` | わんコメ ホスト |
| | `port` | `11180` | わんコメ ポート |
| `whisper` | `model` | `large-v3` | faster-whisper モデル (tiny/base/small/medium/large-v3) |
| | `language` | `ja` | 認識言語 |
| | `device` | `cuda` | 推論デバイス。GPU がない場合は `cpu` に変更 |
| | `compute_type` | `auto` | 計算精度 |
| | `beam_size` | `1` | ビームサーチ幅 |
| | `no_speech_threshold` | `0.8` | 無音判定の閾値 |

> **CPU環境の推奨設定**: `model: small`, `device: cpu`, `compute_type: int8`

### 4. OBS ブラウザソース (立ち絵オーバーレイ)

1. URL: `http://localhost:50700/overlay/`
2. 幅・高さを配信解像度に合わせて設定する

## 使用方法

### 1. サーバー起動

配信前に手動でサーバーを起動する。

```bash
live-assistant serve
```

サーバーは起動すると同時に以下のバックグラウンドタスクを自動開始：
- マイク音声の録音・VAD・文字起こし
- わんコメ経由のコメント受信

### 2. 配信アシスタント開始

サーバーが起動した状態で、Claude Code で `/live-assistant` スキルを実行する。エージェントが以下を自動実行：
1. `live-assistant status` でサーバー起動を確認
2. メインループ — `wait` → 応答 → `speak` → ループ

### 3. 配信終了

配信終了後、サーバーを手動で停止する。

```bash
# Windows の場合
tasklist | grep python  # プロセスIDを確認
taskkill //PID <PID> //F

# Linux/Mac の場合
pkill -f "live-assistant serve"
```

または、サーバーを起動したターミナルで `Ctrl+C` を押す。

## 他プロジェクトからの利用

`pip install -e .` により `live-assistant` コマンドがどのディレクトリからでも使える。
さらに SKILL.md をユーザーレベルに配置すると、任意のプロジェクトで `/live-assistant` スキルを呼び出せる。

```bash
# Windows
mkdir %USERPROFILE%\.claude\skills\live-assistant
copy .claude\skills\live-assistant\SKILL.md %USERPROFILE%\.claude\skills\live-assistant\SKILL.md
```

## Agent Compatibility

- **Claude Code**: `.claude/skills/live-assistant/SKILL.md` またはユーザーレベルの `~/.claude/skills/live-assistant/SKILL.md` を使用

## CLI コマンド

| コマンド | 説明 |
|--------|------|
| `live-assistant serve` | サービス起動 |
| `live-assistant wait --timeout-sec 15` | コメント/マイクイベント待機 |
| `live-assistant speak "..."` | VOICEVOX 読み上げ |
| `live-assistant status` | 稼働状態確認 |
| `live-assistant activity "..."` | 稼働状況をオーバーレイに表示 |

オーバーレイへのHTML表示は `overlay/dynamic-state.json` にJSONを書き込むことで行う（サーバーがファイル変更を自動検知して配信画面に反映）。

## 外部サービス・素材の利用について

本リポジトリのコードは MIT License で提供する。
実行時に読み込む外部サービス・素材には個別のライセンス/規約が適用される。

- [VOICEVOX](https://voicevox.hiroshiba.jp/) — 利用規約: https://voicevox.hiroshiba.jp/term/
- [ずんだもん](https://zunko.jp/) — ガイドライン: https://zunko.jp/guideline.html
- `overlay/sprites/` の立ち絵画像 — [坂本アヒル氏の素材](https://seiga.nicovideo.jp/seiga/im10788496) 
