"""配信アシスタントを Skills + CLI で操作するためのCLI."""

from __future__ import annotations

import argparse
import asyncio
import json
import io
import sys
from pathlib import Path
from typing import Any

import subprocess
import time as _time

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:50700"


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _request(
    base_url: str,
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    timeout: float = 60.0,
) -> httpx.Response:
    url = f"{base_url.rstrip('/')}{path}"
    request_kwargs: dict[str, Any] = {}
    if json_body is not None:
        request_kwargs["json"] = json_body
    with httpx.Client(timeout=timeout) as client:
        resp = client.request(method=method, url=url, **request_kwargs)
    resp.raise_for_status()
    return resp


def _is_service_running(base_url: str) -> bool:
    """サービスが起動しているか確認する。"""
    try:
        with httpx.Client(timeout=2) as client:
            resp = client.get(f"{base_url.rstrip('/')}/healthz")
            return resp.status_code == 200
    except Exception:
        return False


def _ensure_service(base_url: str, timeout_sec: int = 30) -> bool:
    """サービスが未起動なら自動でバックグラウンド起動する。

    Returns:
        True if service is running (or was started), False if failed.
    """
    if _is_service_running(base_url):
        return True

    print("サービス未起動 → バックグラウンドで起動中...", file=sys.stderr)
    project_root = Path(__file__).resolve().parent.parent
    # 新しいプロセスとしてserveを起動 (デタッチ)
    proc = subprocess.Popen(
        [sys.executable, "-m", "src.live_cli", "serve"],
        cwd=str(project_root),
        stdout=subprocess.DEVNULL,
        stderr=open(str(project_root / "server.log"), "a"),
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "DETACHED_PROCESS", 0),
    )
    print(f"サービスプロセス起動 (PID={proc.pid})", file=sys.stderr)

    # 起動を待つ
    deadline = _time.time() + timeout_sec
    while _time.time() < deadline:
        _time.sleep(1)
        if _is_service_running(base_url):
            print("サービス起動完了!", file=sys.stderr)
            return True
    print("サービス起動タイムアウト", file=sys.stderr)
    return False


def _cmd_serve(_args: argparse.Namespace) -> int:
    from .server import app_lifespan

    async def _run_forever() -> None:
        async with app_lifespan():
            await asyncio.Event().wait()

    try:
        asyncio.run(_run_forever())
    except KeyboardInterrupt:
        pass
    return 0


def _cmd_wait(args: argparse.Namespace) -> int:
    resp = _request(
        args.base_url,
        "POST",
        "/api/wait",
        json_body={
            "timeout_sec": args.timeout_sec,
            "include_history": args.include_history,
        },
        timeout=max(float(args.timeout_sec) + 5.0, 30.0),
    )
    _print_json(resp.json())
    return 0


def _cmd_speak(args: argparse.Namespace) -> int:
    body: dict[str, Any] = {"text": args.text}
    if args.sync:
        body["sync"] = True
    if args.speed is not None:
        body["speed_scale"] = args.speed
    resp = _request(
        args.base_url,
        "POST",
        "/api/speak",
        json_body=body,
    )
    data = resp.json()
    print(data.get("result", ""))
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    resp = _request(args.base_url, "GET", "/api/status")
    _print_json(resp.json())
    return 0


def _cmd_start_stream(args: argparse.Namespace) -> int:
    # サービスが未起動なら自動起動
    if not _ensure_service(args.base_url):
        print("サービスの起動に失敗しました", file=sys.stderr)
        return 1
    resp = _request(args.base_url, "POST", "/api/start_stream", json_body={})
    data = resp.json()
    print(data.get("context", ""))
    screenshot_path = data.get("screenshot_path")
    if screenshot_path:
        print(f"\nscreenshot_path: {screenshot_path}")
    return 0


def _cmd_save_note(args: argparse.Namespace) -> int:
    resp = _request(
        args.base_url,
        "POST",
        "/api/save_note",
        json_body={"key": args.key, "content": args.content},
    )
    data = resp.json()
    print(data.get("result", ""))
    return 0


def _cmd_load_note(args: argparse.Namespace) -> int:
    resp = _request(
        args.base_url,
        "POST",
        "/api/load_note",
        json_body={"key": args.key},
    )
    data = resp.json()
    print(data.get("content", ""))
    return 0



def _cmd_activity(args: argparse.Namespace) -> int:
    resp = _request(
        args.base_url,
        "POST",
        "/api/activity",
        json_body={"text": args.text},
    )
    print(resp.json().get("result", ""))
    return 0


def _cmd_generate_image(args: argparse.Namespace) -> int:
    workflow_path = Path(args.workflow)
    if not workflow_path.is_file():
        print(f"ワークフローファイルが見つかりません: {args.workflow}", file=sys.stderr)
        return 1
    workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    try:
        resp = _request(
            args.base_url,
            "POST",
            "/api/generate_image",
            json_body={"workflow": workflow},
            timeout=120.0,
        )
        _print_json(resp.json())
    finally:
        # 使用後にワークフローファイルを自動削除
        try:
            workflow_path.unlink()
        except OSError:
            pass
    return 0


def _cmd_overlay_html(args: argparse.Namespace) -> int:
    resp = _request(
        args.base_url,
        "POST",
        "/api/overlay/html",
        json_body={"html": args.html, "css": args.css or ""},
    )
    print(resp.json().get("result", ""))
    return 0



def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="配信アシスタント CLI")
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"サービスURL (default: {DEFAULT_BASE_URL})",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="配信アシスタントサービスを起動")
    serve.set_defaults(func=_cmd_serve)

    wait = subparsers.add_parser("wait", help="コメント/マイクイベントを待機")
    wait.add_argument("--timeout-sec", type=int, default=30)
    wait.add_argument("--include-history", action="store_true")
    wait.set_defaults(func=_cmd_wait)

    speak = subparsers.add_parser("speak", help="VOICEVOXで読み上げ")
    speak.add_argument("text")
    speak.add_argument("--sync", action="store_true", help="再生完了まで待つ")
    speak.add_argument("--speed", type=float, default=None, help="読み上げ速度 (1.0=通常)")
    speak.set_defaults(func=_cmd_speak)

    status = subparsers.add_parser("status", help="配信状態を表示")
    status.set_defaults(func=_cmd_status)

    start_stream = subparsers.add_parser("start-stream", help="配信開始初期化")
    start_stream.set_defaults(func=_cmd_start_stream)

    save_note = subparsers.add_parser("save-note", help="memory/{key}.md に保存")
    save_note.add_argument("key")
    save_note.add_argument("content")
    save_note.set_defaults(func=_cmd_save_note)

    load_note = subparsers.add_parser("load-note", help="memory/{key}.md を表示")
    load_note.add_argument("key")
    load_note.set_defaults(func=_cmd_load_note)

    activity = subparsers.add_parser("activity", help="稼働状況をオーバーレイに表示")
    activity.add_argument("text", help="稼働状況テキスト (空文字でクリア)")
    activity.set_defaults(func=_cmd_activity)

    gen_image = subparsers.add_parser("generate-image", help="ComfyUIで画像生成")
    gen_image.add_argument("workflow", help="ComfyUI ワークフローJSONファイルのパス")
    gen_image.set_defaults(func=_cmd_generate_image)

    overlay_html = subparsers.add_parser("overlay-html", help="オーバーレイに動的HTML注入")
    overlay_html.add_argument("html", help="HTMLコンテンツ (空文字でクリア)")
    overlay_html.add_argument("--css", default="", help="追加CSSスタイル")
    overlay_html.set_defaults(func=_cmd_overlay_html)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Ensure stdout can handle Unicode on Windows (cp932 workaround)
    if sys.stdout.encoding and sys.stdout.encoding.lower().replace("-", "") != "utf8":
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except httpx.HTTPStatusError as e:
        detail = ""
        try:
            detail = f" {e.response.text}"
        except Exception:
            pass
        print(f"HTTP error: {e.response.status_code}.{detail}", file=sys.stderr)
        return 1
    except httpx.RequestError as e:
        print(f"Request error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
