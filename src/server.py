"""配信アシスタントのバックエンドサービス.

コメント受信(わんコメ経由)、VOICEVOX音声合成、配信画面取得(OBS WebSocket)、
マイク音声認識(VAD+faster-whisper)を提供する。
"""

from __future__ import annotations

import asyncio
import io
import json
import uuid

import logging
import logging.handlers
import sys
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import os

import aiohttp
import httpx
import numpy as np
import pygame
import sounddevice as sd
import yaml
from aiohttp import web

# --- ロギング設定 (STDIOトランスポートのため stderr に出力 + ファイルにも出力) ---
_log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
_stderr_handler = logging.StreamHandler(sys.stderr)
_stderr_handler.setFormatter(_log_formatter)
_file_handler = logging.handlers.RotatingFileHandler(
    str(Path(__file__).resolve().parent.parent / "server.log"),
    encoding="utf-8",
    maxBytes=1_000_000,  # 1MB
    backupCount=1,       # 最大1つのバックアップ (server.log.1)
)
_file_handler.setFormatter(_log_formatter)
logging.basicConfig(level=logging.INFO, handlers=[_stderr_handler, _file_handler])
logger = logging.getLogger(__name__)

# --- 設定読み込み ---
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """プロジェクトルートの .env ファイルを環境変数に読み込む。"""
    env_path = _PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def _load_config() -> dict:
    _load_dotenv()
    config_path = _PROJECT_ROOT / "config.yaml"
    if config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


# --- アプリケーションコンテキスト ---
@dataclass
class AppContext:
    """lifespan で初期化され、ツールから参照されるアプリケーション状態。"""

    event_queue: asyncio.Queue[dict] = field(default_factory=asyncio.Queue)
    http_runner: web.AppRunner | None = None
    config: dict = field(default_factory=dict)
    total_comments: int = 0
    last_comment_time: float = 0.0
    start_time: float = field(default_factory=time.time)
    pygame_initialized: bool = False
    mic_task: asyncio.Task | None = None
    onecomme_task: asyncio.Task | None = None
    mic_vad_state: str = "IDLE"  # "IDLE" | "SPEAKING" | "TRAILING"
    whisper_model: Any = None
    history: list[dict] = field(default_factory=list)
    _history_max: int = 20
    sse_clients: list[web.StreamResponse] = field(default_factory=list)
    _audio_cache: dict[str, bytes] = field(default_factory=dict)
    memory_dir: Path = field(default_factory=lambda: _PROJECT_ROOT / "memory")
    _speak_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


# --- HTTPサーバー (コメント受信) ---


async def _broadcast_sse(ctx: AppContext, event_type: str, data: str) -> None:
    """全SSEクライアントにイベントを送信する。切断済みクライアントは除去する。"""
    dead: list[web.StreamResponse] = []
    payload = f"event: {event_type}\ndata: {data}\n\n"
    for client in ctx.sse_clients:
        try:
            await client.write(payload.encode("utf-8"))
        except (ConnectionResetError, ConnectionError, Exception):
            dead.append(client)
    for d in dead:
        ctx.sse_clients.remove(d)


def _create_http_app(ctx: AppContext) -> web.Application:
    _overlay_dir = _PROJECT_ROOT / "overlay"
    app = web.Application()

    async def handle_overlay_events(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse()
        resp.content_type = "text/event-stream"
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["Access-Control-Allow-Origin"] = "*"
        await resp.prepare(request)
        ctx.sse_clients.append(resp)
        logger.info("[overlay] SSEクライアント接続 (total=%d)", len(ctx.sse_clients))
        try:
            while True:
                await asyncio.sleep(15)
                await resp.write(b": keepalive\n\n")
        except (asyncio.CancelledError, ConnectionResetError, ConnectionError):
            pass
        finally:
            if resp in ctx.sse_clients:
                ctx.sse_clients.remove(resp)
            logger.info("[overlay] SSEクライアント切断 (total=%d)", len(ctx.sse_clients))
        return resp

    async def handle_overlay_audio(request: web.Request) -> web.Response:
        audio_id = request.match_info["audio_id"]
        wav_data = ctx._audio_cache.pop(audio_id, None)
        if wav_data is None:
            return web.Response(status=404, text="Not found")
        return web.Response(
            body=wav_data,
            content_type="audio/wav",
            headers={"Access-Control-Allow-Origin": "*"},
        )

    async def handle_overlay_static(request: web.Request) -> web.FileResponse:
        rel_path = request.match_info.get("path", "index.html") or "index.html"
        file_path = _overlay_dir / rel_path
        # パストラバーサル防止
        try:
            file_path.resolve().relative_to(_overlay_dir.resolve())
        except ValueError:
            return web.Response(status=403, text="Forbidden")
        if not file_path.is_file():
            return web.Response(status=404, text="Not found")
        return web.FileResponse(file_path)

    def _to_bool(value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
        if isinstance(value, (int, float)):
            return bool(value)
        return default

    async def _read_json_body(request: web.Request) -> dict[str, Any]:
        if not request.can_read_body:
            return {}
        try:
            payload = await request.json()
        except Exception:
            return {}
        if isinstance(payload, dict):
            return payload
        return {}

    async def handle_api_wait(request: web.Request) -> web.Response:
        payload = await _read_json_body(request)
        raw_timeout = payload.get("timeout_sec", request.query.get("timeout_sec", 30))
        try:
            timeout_sec = max(0, int(raw_timeout))
        except (TypeError, ValueError):
            timeout_sec = 30
        include_history = _to_bool(
            payload.get("include_history", request.query.get("include_history", False)),
            default=False,
        )
        result = await _wait_for_comments_impl(
            app_ctx=ctx,
            timeout_sec=timeout_sec,
            include_history=include_history,
        )
        return web.json_response(result)

    async def handle_api_speak(request: web.Request) -> web.Response:
        payload = await _read_json_body(request)
        text = str(payload.get("text", "")).strip()
        if not text:
            return web.json_response(
                {"error": "text は必須です"},
                status=400,
            )
        result = await _speak_impl(ctx, text)
        return web.json_response({"result": result})

    async def handle_api_status(request: web.Request) -> web.Response:
        return web.json_response(_get_stream_status_impl(ctx))

    async def handle_api_start_stream(request: web.Request) -> web.Response:
        context_content = _start_stream_impl(ctx)
        return web.json_response({"context": context_content})

    async def handle_api_save_note(request: web.Request) -> web.Response:
        payload = await _read_json_body(request)
        key = str(payload.get("key", "")).strip()
        if not key:
            return web.json_response({"error": "key は必須です"}, status=400)
        content = str(payload.get("content", ""))
        try:
            result = _save_note_impl(ctx, key, content)
            return web.json_response({"result": result})
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)

    async def handle_api_load_note(request: web.Request) -> web.Response:
        key = request.query.get("key", "").strip()
        if not key:
            payload = await _read_json_body(request)
            key = str(payload.get("key", "")).strip()
        if not key:
            return web.json_response({"error": "key は必須です"}, status=400)
        try:
            content = _load_note_impl(ctx, key)
            return web.json_response({"content": content})
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)

    async def handle_api_screenshot(request: web.Request) -> web.Response:
        image_data = _take_screenshot_jpeg(ctx.config)
        screenshot_path = _PROJECT_ROOT / "screenshot.jpg"
        screenshot_path.write_bytes(image_data)
        return web.json_response({"path": str(screenshot_path)})

    async def handle_api_overlay_html(request: web.Request) -> web.Response:
        """オーバーレイに動的HTMLを注入する。"""
        payload = await _read_json_body(request)
        html_content = payload.get("html", "")
        css_content = payload.get("css", "")
        sse_data: dict[str, Any] = {}
        if html_content is not None:
            sse_data["html"] = html_content
        if css_content:
            sse_data["css"] = css_content
        await _broadcast_sse(ctx, "html", json.dumps(sse_data))
        return web.json_response({"result": "ok"})

    async def handle_api_overlay_custom(request: web.Request) -> web.Response:
        """オーバーレイに任意のSSEイベントを送信する。"""
        payload = await _read_json_body(request)
        event_type = payload.get("event", "")
        data = payload.get("data", {})
        if not event_type:
            return web.json_response({"error": "event は必須です"}, status=400)
        await _broadcast_sse(ctx, event_type, json.dumps(data))
        return web.json_response({"result": "ok"})

    async def handle_healthz(request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "now": time.time()})

    app.router.add_get("/healthz", handle_healthz)
    app.router.add_post("/api/wait", handle_api_wait)
    app.router.add_post("/api/speak", handle_api_speak)
    app.router.add_get("/api/status", handle_api_status)
    app.router.add_post("/api/start_stream", handle_api_start_stream)
    app.router.add_post("/api/save_note", handle_api_save_note)
    app.router.add_get("/api/load_note", handle_api_load_note)
    app.router.add_post("/api/load_note", handle_api_load_note)
    app.router.add_get("/api/screenshot", handle_api_screenshot)
    app.router.add_post("/api/overlay/html", handle_api_overlay_html)
    app.router.add_post("/api/overlay/event", handle_api_overlay_custom)
    app.router.add_get("/overlay/events", handle_overlay_events)
    app.router.add_get("/overlay/audio/{audio_id}", handle_overlay_audio)
    async def handle_overlay_redirect(request: web.Request) -> web.Response:
        raise web.HTTPFound("/overlay/")

    app.router.add_get("/overlay/{path:.*}", handle_overlay_static)
    app.router.add_get("/overlay", handle_overlay_redirect)
    return app


async def _enqueue_comment(ctx: AppContext, text: str, source_label: str = "comment") -> None:
    """コメントをイベントキューに追加する。"""
    logger.info("コメント受信 [%s]: %s", source_label, text)
    ctx.total_comments += 1
    ctx.last_comment_time = time.time()

    await ctx.event_queue.put({
        "text": text,
        "time": time.time(),
        "number": ctx.total_comments,
        "source": "comment",
    })


# --- バックグラウンドマイク録音 (VAD + STT) ---

async def _onecomme_loop(ctx: AppContext) -> None:
    """わんコメ (OneComme) WebSocket APIに接続し、コメントを受信する。"""
    onecomme_config = ctx.config.get("onecomme", {})
    host = onecomme_config.get("host", "127.0.0.1")
    port = onecomme_config.get("port", 11180)
    url = f"ws://{host}:{port}/sub"

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(url) as ws:
                    logger.info("[onecomme] 接続完了: %s", url)

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                payload = json.loads(msg.data)
                            except json.JSONDecodeError:
                                continue

                            event_type = payload.get("type", "")

                            if event_type in ("comments", "connected"):
                                # data.comments にコメント配列がある
                                data_obj = payload.get("data", {})
                                comment_list = data_obj.get("comments", []) if isinstance(data_obj, dict) else []
                                if isinstance(comment_list, list):
                                    for wrapper in comment_list:
                                        if not isinstance(wrapper, dict):
                                            continue
                                        # wrapper = {service, name, data: {comment, name, ...}}
                                        inner = wrapper.get("data", {})
                                        if not isinstance(inner, dict):
                                            continue
                                        text = inner.get("comment", "")
                                        name = inner.get("screenName", "") or inner.get("name", "")
                                        if text and event_type == "comments":
                                            display = text  # ユーザー名はコメント自体に含まれることが多い
                                            logger.info("[onecomme] コメント: %s (name=%s)", text, name)
                                            await _enqueue_comment(ctx, text, "onecomme")

                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
        except asyncio.CancelledError:
            logger.info("[onecomme] 接続停止")
            return
        except Exception:
            logger.exception("[onecomme] 接続エラー、5秒後に再接続")
            await asyncio.sleep(5)


def _do_transcribe(model: Any, audio: np.ndarray, language: str, beam_size: int) -> dict:
    """faster-whisperで文字起こしする (スレッドプール用の同期関数)。

    Returns:
        dict with "text" and "no_speech_prob" (最大値)
    """
    segments, _info = model.transcribe(
        audio, language=language, beam_size=beam_size, vad_filter=False,
    )
    texts = []
    max_no_speech_prob = 0.0
    for seg in segments:
        texts.append(seg.text)
        if seg.no_speech_prob > max_no_speech_prob:
            max_no_speech_prob = seg.no_speech_prob
    return {"text": "".join(texts).strip(), "no_speech_prob": max_no_speech_prob}


async def _transcribe_and_enqueue(ctx: AppContext, speech_buf: list[np.ndarray]) -> None:
    """speech_bufを結合してfaster-whisperで文字起こしし、event_queueに追加する。"""
    audio = np.concatenate(speech_buf).astype(np.float32)
    duration = len(audio) / 16000

    vad_config = ctx.config.get("vad", {})
    min_speech_sec = vad_config.get("min_speech_sec", 0.5)
    if duration < min_speech_sec:
        logger.info("[whisper] 短すぎる音声をスキップ (%.1f秒 < %.1f秒)", duration, min_speech_sec)
        return

    if ctx.whisper_model is None:
        logger.warning("[whisper] モデル未ロード、スキップ")
        return

    whisper_config = ctx.config.get("whisper", {})
    language = whisper_config.get("language", "ja")
    beam_size = whisper_config.get("beam_size", 5)

    logger.info("[whisper] 文字起こし開始 (%.1f秒の音声)", duration)
    t0 = time.time()

    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, _do_transcribe, ctx.whisper_model, audio, language, beam_size,
        )
    except Exception:
        logger.exception("[whisper] 文字起こしエラー")
        return

    text = result["text"]
    no_speech_prob = result["no_speech_prob"]
    elapsed = time.time() - t0
    logger.info("[whisper] 文字起こし完了 (%.1f秒, no_speech=%.2f): '%s'", elapsed, no_speech_prob, text)

    if not text:
        logger.info("[whisper] 空テキスト、スキップ")
        return

    # no_speech_prob が高い場合はノイズと判断 (閾値は config で調整可能)
    no_speech_threshold = whisper_config.get("no_speech_threshold", 0.8)
    if no_speech_prob > no_speech_threshold:
        logger.info("[whisper] no_speech_prob=%.2f > %.2f、ノイズ判定で除外: '%s'", no_speech_prob, no_speech_threshold, text)
        return

    # 短い音声(1.5秒未満)から長いテキスト(20文字以上)が出た場合は疑わしい
    if duration < 1.5 and len(text) > 20:
        logger.info("[whisper] 短音声+長テキスト除外: '%s' (%.1f秒)", text, duration)
        return

    event = {
        "text": text,
        "time": time.time(),
        "source": "mic",
        "duration_sec": round(duration, 1),
    }
    await ctx.event_queue.put(event)


async def _mic_loop_with_restart(ctx: AppContext) -> None:
    """mic_loopをクラッシュ時に自動再起動するラッパー。"""
    while True:
        try:
            await _continuous_mic_loop(ctx)
            return  # 正常終了 (CancelledError経由)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[mic] マイクループがクラッシュ、5秒後に再起動")
            await asyncio.sleep(5)


async def _continuous_mic_loop(ctx: AppContext) -> None:
    """バックグラウンドで常時録音し、VADベースで発話区間を検出・文字起こしする。"""
    from silero_vad_lite import SileroVAD

    sample_rate = 16000
    vad = SileroVAD(sample_rate)
    frame_samples = vad.window_size_samples  # 512 at 16kHz = 32ms

    vad_config = ctx.config.get("vad", {})
    speech_threshold = vad_config.get("speech_threshold", 0.5)
    silence_duration = vad_config.get("silence_duration", 2.0)
    max_speech_sec = vad_config.get("max_speech_sec", 30)

    # プリバッファ: IDLE中の直近フレームを保持し、発話冒頭の切れを防ぐ
    pre_buffer_sec = vad_config.get("pre_buffer_sec", 0.3)
    pre_buffer_frames = max(1, int(pre_buffer_sec * sample_rate / frame_samples))
    ring_buf: deque[np.ndarray] = deque(maxlen=pre_buffer_frames)

    audio_queue: asyncio.Queue[np.ndarray] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def _audio_callback(indata: np.ndarray, _frames: int, _time_info: Any, status: Any) -> None:
        if status:
            logger.debug("sounddevice status: %s", status)
        loop.call_soon_threadsafe(audio_queue.put_nowait, indata.copy())

    logger.info(
        "Silero VAD 初期化完了 (threshold=%.2f, silence=%.1fs, max=%.0fs)",
        speech_threshold, silence_duration, max_speech_sec,
    )
    logger.info("バックグラウンドマイク録音開始 (VADベース)")

    stream = sd.InputStream(
        samplerate=sample_rate,
        channels=1,
        dtype="float32",
        blocksize=frame_samples,
        callback=_audio_callback,
    )
    stream.start()

    # ステートマシン: IDLE → SPEAKING → TRAILING → IDLE
    state = "IDLE"
    speech_buf: list[np.ndarray] = []
    speech_start_time = 0.0
    silence_start_time = 0.0
    frame_count = 0
    last_heartbeat = time.time()
    _HEARTBEAT_INTERVAL = 30

    try:
        while True:
            try:
                frame = await audio_queue.get()
            except asyncio.CancelledError:
                break

            frame_count += 1
            frame_flat = frame.flatten()
            now = time.time()

            prob = vad.process(memoryview(frame_flat.data))

            if state == "IDLE":
                if prob >= speech_threshold:
                    state = "SPEAKING"
                    ctx.mic_vad_state = state
                    speech_buf = list(ring_buf) + [frame_flat]
                    ring_buf.clear()
                    speech_start_time = now
                    logger.info("[mic] 発話開始検出 (prob=%.3f, pre_buf=%d frames)", prob, len(speech_buf) - 1)
                else:
                    ring_buf.append(frame_flat)

            elif state == "SPEAKING":
                speech_buf.append(frame_flat)
                elapsed = now - speech_start_time

                if elapsed >= max_speech_sec:
                    logger.info("[mic] 最大バッファ超過 (%.1f秒), 強制文字起こし", elapsed)
                    await _transcribe_and_enqueue(ctx, speech_buf)
                    speech_buf = []
                    ring_buf.clear()
                    state = "IDLE"
                    ctx.mic_vad_state = state
                elif prob < speech_threshold:
                    state = "TRAILING"
                    ctx.mic_vad_state = state
                    silence_start_time = now

            elif state == "TRAILING":
                speech_buf.append(frame_flat)

                if prob >= speech_threshold:
                    state = "SPEAKING"
                    ctx.mic_vad_state = state
                elif now - silence_start_time >= silence_duration:
                    speech_dur = now - speech_start_time
                    logger.info("[mic] 発話終了検出 (発話%.1f秒, 無音%.1f秒)", speech_dur, silence_duration)
                    await _transcribe_and_enqueue(ctx, speech_buf)
                    speech_buf = []
                    ring_buf.clear()
                    state = "IDLE"
                    ctx.mic_vad_state = state

            # 定期的にハートビートログ
            if now - last_heartbeat >= _HEARTBEAT_INTERVAL:
                logger.info(
                    "[mic-heartbeat] state=%s, frames=%d, queue=%d, stream_active=%s",
                    state, frame_count, audio_queue.qsize(), stream.active,
                )
                last_heartbeat = now

    finally:
        stream.stop()
        stream.close()
        logger.info("[mic] ストリーム閉じました (total frames=%d)", frame_count)


# --- Lifespan ---


@asynccontextmanager
async def app_lifespan() -> AsyncIterator[AppContext]:
    """HTTPサーバーをバックグラウンドで起動し、終了時にクリーンアップする。"""
    config = _load_config()
    ctx = AppContext(config=config)

    # メモリディレクトリの初期化
    ctx.memory_dir.mkdir(parents=True, exist_ok=True)
    logger.info("メモリディレクトリ: %s", ctx.memory_dir)

    # pygame.mixer を事前初期化 (マイク録音開始前に行うことで sounddevice との干渉を防ぐ)
    try:
        pygame.mixer.init(frequency=24000, size=-16, channels=1)
        ctx.pygame_initialized = True
        logger.info("pygame.mixer 初期化完了")
    except Exception:
        logger.exception("pygame.mixer の初期化に失敗")

    # faster-whisper モデルをロード
    whisper_config = config.get("whisper", {})
    try:
        from faster_whisper import WhisperModel
        ctx.whisper_model = WhisperModel(
            whisper_config.get("model", "small"),
            device=whisper_config.get("device", "cpu"),
            compute_type=whisper_config.get("compute_type", "int8"),
        )
        logger.info("Whisperモデルロード完了: %s", whisper_config.get("model", "small"))
    except Exception:
        logger.exception("Whisperモデルのロードに失敗")

    # HTTPサーバー起動 (CLI API + overlay)
    host = "127.0.0.1"
    port = 50700
    app = _create_http_app(ctx)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    # ポートがTIME_WAITで解放待ちの場合にリトライする
    for _retry in range(15):
        try:
            await site.start()
            break
        except OSError as e:
            if e.errno in (10048, 10013) and _retry < 14:
                logger.warning("ポート %d がまだ使用中、2秒後にリトライ (%d/15)", port, _retry + 1)
                await asyncio.sleep(2)
                site = web.TCPSite(runner, host, port)
            else:
                raise
    ctx.http_runner = runner
    logger.info("HTTPサーバー起動: %s:%d", host, port)

    # バックグラウンドマイク録音タスク起動 (クラッシュ時自動再起動付き)
    ctx.mic_task = asyncio.create_task(_mic_loop_with_restart(ctx))
    logger.info("バックグラウンドマイク録音タスク起動")

    # わんコメ (OneComme) 接続タスク起動 (有効な場合のみ)
    if config.get("onecomme", {}).get("enabled", False):
        ctx.onecomme_task = asyncio.create_task(_onecomme_loop(ctx))
        logger.info("わんコメ接続タスク起動")

    try:
        yield ctx
    finally:
        for task in (ctx.mic_task, ctx.onecomme_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if ctx.http_runner is not None:
            await ctx.http_runner.cleanup()
        if ctx.pygame_initialized:
            pygame.mixer.quit()
        logger.info("サーバーシャットダウン完了")


# --- コア実装 ---


async def _wait_for_comments_impl(
    app_ctx: AppContext,
    timeout_sec: int = 30,
    include_history: bool = False,
) -> dict[str, Any]:
    """コメントやマイク音声の文字起こしが届くまで待機する (ロングポーリング)。"""
    q = app_ctx.event_queue
    results: list[dict] = []

    def _drain_queue() -> None:
        """キューから溜まっているものを全て取得する。"""
        while not q.empty():
            try:
                results.append(q.get_nowait())
            except asyncio.QueueEmpty:
                break

    deadline = time.time() + timeout_sec

    # まずキューに溜まっているものを全て取得
    _drain_queue()

    if not results:
        # キューが空なら最初の1件が届くまで待つ
        remaining = deadline - time.time()
        if remaining > 0:
            try:
                item = await asyncio.wait_for(q.get(), timeout=remaining)
                results.append(item)
            except asyncio.TimeoutError:
                pass

            # 追加で届いているものも取得
            _drain_queue()

    # 履歴スナップショット (要求された場合のみ)
    history_snapshot = list(app_ctx.history) if include_history else None

    # 新規アイテムを履歴に追加
    for item in results:
        app_ctx.history.append(item)
    # 履歴の上限を維持
    if len(app_ctx.history) > app_ctx._history_max:
        app_ctx.history = app_ctx.history[-app_ctx._history_max:]

    text_result: dict[str, Any] = {"new": results}
    if history_snapshot is not None:
        text_result["history"] = history_snapshot

    return text_result


async def _speak_impl(app_ctx: AppContext, text: str) -> str:
    """VOICEVOXで音声合成してスピーカーから再生する。再生完了まで待つ。"""
    async with app_ctx._speak_lock:
        return await _speak_impl_locked(app_ctx, text)


async def _speak_impl_locked(app_ctx: AppContext, text: str) -> str:
    """speak の排他ロック内で実行される本体。"""
    # 配信者が発話中なら最大5秒待ってIDLEになるのを待つ
    for _ in range(50):  # 50 * 0.1s = 5s
        if app_ctx.mic_vad_state == "IDLE":
            break
        await asyncio.sleep(0.1)
    else:
        return f"BLOCKED: 配信者が発話中です (state={app_ctx.mic_vad_state})"

    voicevox_config = app_ctx.config.get("voicevox", {})
    base_url = voicevox_config.get("url", "http://localhost:50021").rstrip("/")
    speaker_id = voicevox_config.get("speaker_id", 1)

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # 1. audio_query
            resp = await client.post(
                f"{base_url}/audio_query",
                params={"text": text, "speaker": speaker_id},
            )
            resp.raise_for_status()
            query = resp.json()

            # 2. synthesis
            resp = await client.post(
                f"{base_url}/synthesis",
                params={"speaker": speaker_id},
                json=query,
            )
            resp.raise_for_status()
            wav_data = resp.content
    except Exception as e:
        logger.warning("VOICEVOX音声合成に失敗: %s", e)
        return f"音声合成に失敗しました: {e}"

    # pygame で再生
    try:
        if not app_ctx.pygame_initialized:
            pygame.mixer.init(frequency=24000, size=-16, channels=1)
            app_ctx.pygame_initialized = True

        sound = pygame.mixer.Sound(io.BytesIO(wav_data))

        # SSE: 字幕表示 + 口パク用音声URL送信
        audio_id = uuid.uuid4().hex[:12]
        app_ctx._audio_cache[audio_id] = wav_data
        await _broadcast_sse(app_ctx, "subtitle", json.dumps({"text": text}))
        await _broadcast_sse(app_ctx, "speak", json.dumps({"audioUrl": f"/overlay/audio/{audio_id}"}))

        sound.play()
        await asyncio.sleep(sound.get_length() + 0.3)

        # 字幕クリア
        await _broadcast_sse(app_ctx, "subtitle", json.dumps({"text": ""}))
    except Exception as e:
        logger.warning("音声再生に失敗: %s", e)
        return f"音声再生に失敗しました: {e}"

    return f"読み上げ完了: {text}"


def _take_screenshot_jpeg(config: dict | None = None) -> bytes:
    """OBS WebSocket API経由で配信画面のスクリーンショットを取得する。"""
    import base64

    obs_config = (config or {}).get("obs", {})
    obs_host = obs_config.get("host", "127.0.0.1")
    obs_port = obs_config.get("port", 4455)
    obs_password = obs_config.get("password", "")

    import obsws_python as obs
    kwargs: dict[str, Any] = {"host": obs_host, "port": obs_port}
    if obs_password:
        kwargs["password"] = obs_password
    cl = obs.ReqClient(**kwargs, timeout=5)
    # 現在のプログラムシーン名を取得
    scene_resp = cl.get_current_program_scene()
    scene_name = scene_resp.scene_name
    resp = cl.get_source_screenshot(
        name=scene_name,
        img_format="jpg",
        width=1024,
        height=576,
        quality=70,
    )
    cl.disconnect()
    # レスポンスからbase64画像データを取得
    img_data = resp.image_data
    if img_data.startswith("data:"):
        img_data = img_data.split(",", 1)[1]
    return base64.b64decode(img_data)


def _get_stream_status_impl(app_ctx: AppContext) -> dict[str, Any]:
    """配信の現在の状態を返す。"""
    now = time.time()

    elapsed_since_last = (
        now - app_ctx.last_comment_time if app_ctx.last_comment_time > 0 else None
    )
    uptime = now - app_ctx.start_time

    # マイクタスクの状態確認
    mic_status = "not_started"
    if app_ctx.mic_task is not None:
        if app_ctx.mic_task.done():
            exc = app_ctx.mic_task.exception() if not app_ctx.mic_task.cancelled() else None
            mic_status = f"dead (exception: {exc})" if exc else "dead (finished/cancelled)"
        else:
            mic_status = "running"

    return {
        "total_comments": app_ctx.total_comments,
        "seconds_since_last_comment": (
            round(elapsed_since_last, 1) if elapsed_since_last is not None else None
        ),
        "uptime_seconds": round(uptime, 1),
        "pending_comments_in_queue": app_ctx.event_queue.qsize(),
        "mic_task_status": mic_status,
        "mic_vad_state": app_ctx.mic_vad_state,
    }


def _validate_note_key(key: str) -> None:
    """ノートのkeyを検証し、不正なパスを拒否する。"""
    if ".." in key:
        raise ValueError(f"keyに '..' を含めることはできません: {key}")
    if key.startswith("/") or key.startswith("\\"):
        raise ValueError(f"keyは絶対パスにできません: {key}")


def _resolve_note_path(app_ctx: AppContext, key: str) -> Path:
    _validate_note_key(key)
    file_path = (app_ctx.memory_dir / f"{key}.md").resolve()
    # パストラバーサル防止: 解決後のパスが memory_dir 配下であることを確認
    if not str(file_path).startswith(str(app_ctx.memory_dir.resolve())):
        raise ValueError(f"不正なkeyです: {key}")
    return file_path


def _start_stream_impl(app_ctx: AppContext) -> str:
    """配信開始時に context 読み込み + topics リセットを行う。"""
    # context.md を読み込む
    context_file = _resolve_note_path(app_ctx, "context")
    context_content = ""
    if context_file.is_file():
        context_content = context_file.read_text(encoding="utf-8")
    logger.info("[start_stream] context.md loaded (%d bytes)", len(context_content))

    # topics.md をリセット
    topics_file = _resolve_note_path(app_ctx, "topics")
    topics_file.parent.mkdir(parents=True, exist_ok=True)
    topics_file.write_text("", encoding="utf-8")
    logger.info("[start_stream] topics.md をリセットしました")

    return context_content if context_content else "(前回の配信ログはありません)"


def _save_note_impl(app_ctx: AppContext, key: str, content: str) -> str:
    """memory/{key}.md にcontentを書き込む（上書き）。"""
    file_path = _resolve_note_path(app_ctx, key)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    logger.info("[memory] save_note: %s (%d bytes)", key, len(content))
    return f"保存しました: memory/{key}.md ({len(content)} bytes)"


def _load_note_impl(app_ctx: AppContext, key: str) -> str:
    """memory/{key}.md を読み込んで内容を返す。"""
    file_path = _resolve_note_path(app_ctx, key)
    if not file_path.is_file():
        return ""
    content = file_path.read_text(encoding="utf-8")
    logger.info("[memory] load_note: %s (%d bytes)", key, len(content))
    return content


