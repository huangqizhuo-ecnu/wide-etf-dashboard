"""本地预览服务器：静态页面 + 同源只读 /api/history（Python 3.10+ 标准库）。

生产环境用 Nginx 托管 public/ 并把 /api/history 指向同一份 JSON；本脚本只用于
本地验收，行为与线上一致：GET 只读、no-store、不列目录、不允许路径穿越。
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = ROOT / "public"
API_FILE = WEB_ROOT / "api" / "history.json"
MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}
SECURITY = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; object-src 'none'; base-uri 'none'",
}


def resolve(url_path: str) -> Path | None:
    path = unquote(url_path)
    if path in ("/", "/index.html"):
        return WEB_ROOT / "index.html"
    if path == "/api/history":
        return API_FILE
    candidate = (WEB_ROOT / path.lstrip("/")).resolve()
    if WEB_ROOT.resolve() not in candidate.parents or not candidate.is_file():
        return None
    return candidate


class Handler(BaseHTTPRequestHandler):
    server_version = "etf-dashboard"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802 - http.server 接口
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/") and parsed.path not in ("/api/history", "/api/history.json"):
            return self.send_json({"error": "not found"}, 404)
        target = resolve(parsed.path)
        if target is None or not target.is_file():
            return self.send_json({"error": "not found"}, 404)
        try:
            body = target.read_bytes()
        except OSError:
            return self.send_json({"error": "无法读取数据文件"}, 503)
        is_api = parsed.path.startswith("/api/")
        content_type = MIME.get(target.suffix.lower()) or mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store" if is_api else "no-cache")
        for key, value in SECURITY.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        target = resolve(parsed.path)
        if target is None or not target.is_file():
            return self.send_json({"error": "not found"}, 404, body=False)
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(target.suffix.lower()) or "application/octet-stream")
        self.send_header("Cache-Control", "no-store" if parsed.path.startswith("/api/") else "no-cache")
        for key, value in SECURITY.items():
            self.send_header(key, value)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        self.send_json({"error": "只读服务，不支持写入"}, 405, extra={"Allow": "GET, HEAD"})

    do_PUT = do_POST
    do_DELETE = do_POST
    do_PATCH = do_POST

    def send_json(self, payload: dict, status: int, body: bool = True, extra: dict | None = None) -> None:
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for key, value in SECURITY.items():
            self.send_header(key, value)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(data)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        sys.stderr.write("%s %s\n" % (self.address_string(), format % args))


def main() -> int:
    global WEB_ROOT, API_FILE
    parser = argparse.ArgumentParser(description="宽基ETF看板本地预览")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--web-root", type=Path, default=WEB_ROOT)
    args = parser.parse_args()
    WEB_ROOT = args.web_root.resolve()
    API_FILE = WEB_ROOT / "api" / "history.json"
    if not (WEB_ROOT / "index.html").is_file():
        print(f"缺少页面文件：{WEB_ROOT / 'index.html'}", file=sys.stderr)
        return 2
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"看板地址：http://{args.host}:{args.port}/", flush=True)
    print(f"数据地址：http://{args.host}:{args.port}/api/history", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
