"""把 public/ 发布到 GitHub Pages 的 gh-pages 分支（Python 3.10+ 标准库）。

用法：
    python update_daily.py
    python scripts/publish_pages.py --owner <用户名> --repo <仓库名>

令牌从环境变量 GH_TOKEN 读取（需要 repo 权限），也可用 --token-file 指向本机文件。
脚本只上传 public/ 下的静态文件，不会提交内部快照、日志或令牌。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = ["index.html", "style.css", "dashboard.js", "window.js", "api/history.json"]


def api(method: str, path: str, token: str, payload=None, base: str = "https://api.github.com"):
    request = urllib.request.Request(
        base + path,
        method=method,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "etf-dashboard-pages",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read()
            return response.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:300]
        raise SystemExit(f"{method} {path} -> {error.code}: {detail}") from None


def get_ref(token: str, path: str, base: str):
    try:
        _status, ref = api("GET", path, token, base=base)
        return ref["object"]["sha"]
    except SystemExit as error:
        if "-> 404" in str(error):
            return None
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="发布 public/ 到 GitHub Pages")
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--public", type=Path, default=ROOT / "public")
    parser.add_argument("--token-file", type=Path, help="存放令牌的文件；默认读 GH_TOKEN")
    parser.add_argument("--base-url", default="https://api.github.com", help="GitHub API 地址")
    parser.add_argument("--message", help="提交说明，默认含数据截至日")
    args = parser.parse_args()

    token = os.environ.get("GH_TOKEN", "").strip()
    if args.token_file:
        token = args.token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit("缺少令牌：请设置 GH_TOKEN 或使用 --token-file")

    repo_path = f"/repos/{args.owner}/{args.repo}"
    api("GET", repo_path, token, base=args.base_url)

    entries = []
    for name in FILES:
        content = (args.public / name).read_bytes()
        status, blob = api("POST", f"{repo_path}/git/blobs", token, {
            "content": base64.b64encode(content).decode("ascii"), "encoding": "base64"}, base=args.base_url)
        entries.append({"path": name, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    status, blob = api("POST", f"{repo_path}/git/blobs", token, {
        "content": "", "encoding": "base64"}, base=args.base_url)
    entries.append({"path": ".nojekyll", "mode": "100644", "type": "blob", "sha": blob["sha"]})

    status, tree = api("POST", f"{repo_path}/git/trees", token, {"tree": entries}, base=args.base_url)
    meta = json.loads((args.public / "api" / "history.json").read_text(encoding="utf-8"))["meta"]
    message = args.message or f"发布看板快照：数据截至 {meta['through']}（{meta['record_count']} 个交易日）"
    parent = get_ref(token, f"{repo_path}/git/ref/heads/gh-pages", args.base_url)
    parents = [parent] if parent else []
    status, commit = api("POST", f"{repo_path}/git/commits", token,
                         {"message": message, "tree": tree["sha"], "parents": parents}, base=args.base_url)
    if parents:
        api("PATCH", f"{repo_path}/git/refs/heads/gh-pages", token,
            {"sha": commit["sha"], "force": False}, base=args.base_url)
    else:
        api("POST", f"{repo_path}/git/refs", token,
            {"ref": "refs/heads/gh-pages", "sha": commit["sha"]}, base=args.base_url)

    print(f"已发布 {len(entries)} 个文件到 {args.owner}/{args.repo} 的 gh-pages 分支")
    print(f"提交：{commit['sha'][:10]}，数据截至 {meta['through']}，{meta['record_count']} 个交易日")
    print(f"页面：https://{args.owner}.github.io/{args.repo}/")
    print("GitHub Pages 通常 1–2 分钟完成构建；若页面仍是旧数据，等待构建完成或强制刷新。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
