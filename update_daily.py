"""宽基ETF看板每日发布脚本（Python 3.10+ 标准库，无需任何第三方依赖）。

职责：
1. 按北京时间判断当天是否为 A 股交易日（休市日跳过，周末不联网即可判定）。
2. 使用进程级互斥锁，避免定时任务与人工补跑并发写同一份数据。
3. 调用同目录的 refresh_public_etf.py，从公开源补齐快照之后的全部缺失交易日。
4. 严格校验快照：八指数固定顺序、日期递增、只追加、变动等于相邻规模差；
   历史值允许 1e-6 亿元以内的重复取数舍入差异，超过即拒绝发布。
5. 备份上一版对外 JSON，同目录临时文件 + 原子替换发布，随后回读核对。
6. 源站数据未齐、校验失败或断网时保留上一版，记录缺失日期与原因，退出码 1。

写入权限只给运行本脚本的账号：对外目录中的 api/history.json 由本脚本原子替换，
Web 服务器只读。脚本自身不访问任何需要账号的接口，也不读取或输出凭据。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener, install_opener, urlopen

CHINA = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from refresh_public_etf import INDICES, START, exchange_dates  # noqa: E402

SCHEMA_VERSION = 1
MAX_REASON = 1200
EXPECTED_INDICES = [{"code": code, "name": name} for code, name in INDICES]


class UpdateError(RuntimeError):
    """可以写进日志、面向运维的失败原因。"""


class ValidationError(UpdateError):
    """快照不符合已验收口径。"""


class LockBusy(UpdateError):
    """已有更新任务在运行。"""


# ---------------------------------------------------------------- 基础工具


def beijing_now() -> datetime:
    return datetime.now(CHINA)


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise UpdateError(f"文件不存在：{path}") from None
    except json.JSONDecodeError as exc:
        raise UpdateError(f"JSON 无法解析：{path}（{exc}）") from None


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def atomic_write(path: Path, payload: bytes) -> None:
    """在同一目录写临时文件后原子替换，避免读者看到半个文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with open(temporary, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            # Web 服务器只需读权限；显式设置，避免调用方 umask 过严导致 Nginx 读不到。
            with contextlib.suppress(OSError):
                os.chmod(path, 0o644)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


@contextlib.contextmanager
def update_lock(work_dir: Path, stale_seconds: int):
    """独占锁：锁文件存在且未过期则视为并发运行。"""
    work_dir.mkdir(parents=True, exist_ok=True)
    lock_path = work_dir / ".update.lock"
    payload = json.dumps({"pid": os.getpid(), "started_at": beijing_now().strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False)
    acquired = False
    for attempt in range(3):
        try:
            handle = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(handle, payload.encode("utf-8"))
            os.close(handle)
            acquired = True
            break
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except FileNotFoundError:
                continue  # 锁刚被其他进程释放，立即重试
            holder = "未知"
            with contextlib.suppress(OSError, json.JSONDecodeError):
                holder = json.loads(lock_path.read_text(encoding="utf-8")).get("pid", "未知")
            if age > stale_seconds and attempt == 0:
                with contextlib.suppress(FileNotFoundError):
                    lock_path.unlink()
                continue
            raise LockBusy(f"另一更新任务正在运行（pid={holder}，锁已存在 {int(age)} 秒）：{lock_path}") from None
    if not acquired:
        raise LockBusy(f"无法获取更新锁（锁文件频繁变化）：{lock_path}")
    try:
        yield
    finally:
        try:
            owner = json.loads(lock_path.read_text(encoding="utf-8")).get("pid")
        except (OSError, json.JSONDecodeError):
            owner = os.getpid()
        if owner == os.getpid():
            with contextlib.suppress(OSError):
                lock_path.unlink()


# ---------------------------------------------------------------- 交易日历


def trading_day(day: date) -> bool:
    """A 股交易日判断。周末直接返回 False，不依赖网络。"""
    if day.weekday() >= 5:
        return False
    return day in exchange_dates(day, day)


# ---------------------------------------------------------------- 校验


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_history(data: dict, through: date, prior: dict | None) -> str:
    """校验快照并返回最新数据日。任何一条不符合就抛 ValidationError。"""
    if not isinstance(data, dict):
        raise ValidationError("快照不是 JSON 对象")
    if data.get("indices") != EXPECTED_INDICES:
        raise ValidationError("八指数顺序或名称与已验收口径不一致")
    records = data.get("records")
    if not isinstance(records, list) or not records:
        raise ValidationError("快照没有交易日记录")
    previous = None
    for position, row in enumerate(records, 1):
        if not isinstance(row, dict) or not isinstance(row.get("date"), str):
            raise ValidationError(f"第 {position} 条记录缺少日期")
        stamp = row["date"]
        try:
            parsed = date.fromisoformat(stamp)
        except ValueError:
            raise ValidationError(f"第 {position} 条记录日期格式错误：{stamp}") from None
        if parsed.isoformat() != stamp or parsed.weekday() >= 5:
            raise ValidationError(f"第 {position} 条记录不是有效的交易日：{stamp}")
        if parsed < START:
            raise ValidationError(f"第 {position} 条记录早于数据起点 {START}：{stamp}")
        if parsed > through:
            raise ValidationError(f"快照包含截止日之后的记录：{stamp} > {through}")
        if previous and stamp <= previous["date"]:
            raise ValidationError(f"日期未严格递增：{previous['date']} 之后出现 {stamp}")
        values = row.get("values")
        if not isinstance(values, list) or len(values) != len(EXPECTED_INDICES):
            raise ValidationError(f"{stamp} 的指标组数不是 8")
        for index, point in enumerate(values):
            label = EXPECTED_INDICES[index]["name"]
            if not isinstance(point, list) or len(point) != 2 or not all(_is_number(v) for v in point):
                raise ValidationError(f"{stamp} {label} 的指标不是两个有限数值")
            change, size = point
            if size <= 0:
                raise ValidationError(f"{stamp} {label} 的总规模不为正：{size}")
            if previous:
                expected = size - previous["values"][index][1]
                if abs(change - expected) > 1e-6:
                    raise ValidationError(f"{stamp} {label} 的日度变动与相邻规模差不符：{change} vs {expected}")
        previous = row

    if prior is not None:
        prior_records = prior.get("records")
        if not isinstance(prior_records, list) or not prior_records:
            raise ValidationError("已发布 JSON 没有历史记录，无法核对只追加")
        if len(records) < len(prior_records):
            raise ValidationError(f"新快照记录数 {len(records)} 少于已发布 {len(prior_records)}")
        # 只允许在末尾追加交易日。历史值允许 1e-6 亿元以内的重复取数舍入差异，
        # 超过该幅度仍视为改写并拒绝发布。
        for position, (fresh, kept) in enumerate(zip(records, prior_records), 1):
            if not isinstance(kept, dict) or not isinstance(kept.get("values"), list) or len(kept["values"]) != 8 \
                    or not all(isinstance(point, list) and len(point) == 2 and all(_is_number(v) for v in point)
                               for point in kept["values"]):
                raise ValidationError(f"已发布 JSON 第 {position} 条结构不符，请检查线上数据文件")
            if fresh.get("date") != kept.get("date"):
                raise ValidationError(f"已发布历史第 {position} 条的日期被改写：{kept.get('date')} -> {fresh.get('date')}")
            for index, (a, b) in enumerate(zip(fresh["values"], kept["values"]), 1):
                for offset, (x, y) in enumerate(zip(a, b)):
                    if abs(x - y) > 1e-6:
                        field = "日度变动" if offset == 0 else "总规模"
                        raise ValidationError(
                            f"已发布历史 {fresh['date']} {EXPECTED_INDICES[index - 1]['name']} 的{field}被改写：{y} -> {x}"
                        )
    return records[-1]["date"]


# ---------------------------------------------------------------- 发布


def build_payload(data: dict, published_at: datetime) -> dict:
    records = data["records"]
    return {
        "indices": data["indices"],
        "records": records,
        "meta": {
            "schema_version": SCHEMA_VERSION,
            "published_at": published_at.strftime("%Y-%m-%d %H:%M"),
            "through": records[-1]["date"],
            "first_date": records[0]["date"],
            "record_count": len(records),
            "source": "上交所/深交所ETF份额 + 东方财富单位净值",
        },
    }


def prune_backups(backup_dir: Path, keep: int) -> None:
    if keep <= 0 or not backup_dir.is_dir():
        return
    files = sorted(backup_dir.glob("history-*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    for stale in files[keep:]:
        with contextlib.suppress(OSError):
            stale.unlink()


def publish(data: dict, published_path: Path, backup_dir: Path, keep_backups: int, now: datetime) -> dict:
    """备份上一版 → 原子写入 → 文件回读核对。"""
    payload = build_payload(data, now)
    body = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    backup = None
    if published_path.exists():
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"history-{now.strftime('%Y%m%dT%H%M%S')}.json"
        shutil.copy2(published_path, backup)
        prune_backups(backup_dir, keep_backups)
    atomic_write(published_path, body)
    readback = read_json(published_path)
    if canonical(readback.get("records")) != canonical(payload["records"]) or canonical(readback.get("indices")) != canonical(payload["indices"]):
        raise UpdateError(f"发布后回读与写入内容不一致：{published_path}")
    return {"records": payload["records"], "meta": payload["meta"], "backup": str(backup) if backup else None, "bytes": len(body)}


def http_readback(url: str, expected_records, timeout: float = 15.0) -> tuple[bool, str]:
    """通过看板地址回读，确认同源 GET 与写入内容一致。

    返回 (是否一致, 说明)。连接失败只记为警告（文件已经原子写入），内容不一致则视为失败。
    """
    request = Request(url, headers={"Cache-Control": "no-cache", "Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read()
    except (URLError, OSError) as exc:
        return True, f"看板地址回读未成功（不影响已写入文件）：{exc}"
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return False, f"看板地址回读不是 JSON：{url}"
    if canonical(data.get("records")) != canonical(expected_records):
        return False, f"看板地址回读的记录与写入内容不一致：{url}"
    return True, f"看板地址回读一致：{len(data['records'])} 个交易日，截至 {data['records'][-1]['date']}"


# ---------------------------------------------------------------- 日志


class RunLog:
    def __init__(self, log_dir: Path, now: datetime):
        log_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl = log_dir / "update.jsonl"
        self.text = log_dir / "update.log"
        self.now = now
        self.events: list[str] = []

    def line(self, level: str, message: str) -> None:
        stamp = beijing_now().strftime("%Y-%m-%d %H:%M:%S")
        text = f"[{stamp}] {level} {message}"
        print(text, flush=True)
        with open(self.text, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")
        self.events.append(message)

    def finish(self, event: dict) -> None:
        event = {"time": beijing_now().strftime("%Y-%m-%d %H:%M:%S"), **event}
        with open(self.jsonl, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- 主流程


def default_paths() -> dict:
    return {
        "work": ROOT / "data" / "current" / "history.json",
        "published": ROOT / "public" / "api" / "history.json",
        "log_dir": ROOT / "logs",
        "backup_dir": ROOT / "data" / "backups",
        "refresh": ROOT / "refresh_public_etf.py",
        "last_run": ROOT / "data" / "current" / "last_run.json",
    }


def parse_args(argv=None):
    paths = default_paths()
    parser = argparse.ArgumentParser(description="宽基ETF看板：按交易日补齐数据并原子发布只读 JSON")
    parser.add_argument("--through", type=date.fromisoformat, help="更新截止日（默认北京时间昨天），用于人工补跑")
    parser.add_argument("--today", type=date.fromisoformat, help="把某一天当作北京时间今天，仅用于测试")
    parser.add_argument("--work", type=Path, default=paths["work"], help="含 last_members 的内部快照")
    parser.add_argument("--published", type=Path, default=paths["published"], help="对外只读 JSON（网页读取）")
    parser.add_argument("--log-dir", type=Path, default=paths["log_dir"])
    parser.add_argument("--backup-dir", type=Path, default=paths["backup_dir"])
    parser.add_argument("--refresh", type=Path, default=paths["refresh"], help="公开源取数程序")
    parser.add_argument("--retries", type=int, default=3, help="源站未齐时的最多尝试次数")
    parser.add_argument("--retry-wait", type=float, default=120.0, help="两次尝试之间的等待秒数")
    parser.add_argument("--refresh-timeout", type=float, default=3600.0, help="单次取数超时秒数")
    parser.add_argument("--lock-stale", type=int, default=7200, help="超过该秒数的锁视为残留")
    parser.add_argument("--keep-backups", type=int, default=40, help="保留最近多少份对外 JSON 备份")
    parser.add_argument("--readback-url", help="可选：通过看板地址回读，例如 http://127.0.0.1:8080/api/history")
    parser.add_argument("--publish-only", action="store_true", help="不取数，只校验并发布当前内部快照")
    parser.add_argument("--direct", action="store_true", help="取数时忽略 HTTP(S)_PROXY 等代理变量，强制直连公开源")
    parser.add_argument("--force", action="store_true", help="非交易日也执行（人工补跑）")
    parser.add_argument("--dry-run", action="store_true", help="只校验，不写对外 JSON")
    return parser.parse_args(argv)


def run(args, log: RunLog) -> dict:
    now = args.today or beijing_now().date()
    through = args.through or (now - timedelta(days=1))
    if through > now - timedelta(days=1) and not args.today:
        raise UpdateError(f"截止日 {through} 不能晚于北京时间昨天")

    if not (args.publish_only or args.force or args.through):
        calendar_error = None
        for attempt in range(1, max(1, args.retries) + 1):
            try:
                if not trading_day(now):
                    log.line("INFO", f"{now} 不是 A 股交易日（休市），本次跳过")
                    return {"status": "skipped_holiday", "through": str(through), "attempts": 0}
                break
            except (RuntimeError, OSError, ValueError, KeyError) as error:
                calendar_error = error
                log.line("WARN", f"第 {attempt} 次读取 A 股交易日历失败：{error}")
                if attempt < args.retries:
                    time.sleep(min(max(args.retry_wait, 1), 30) * attempt)
        else:
            raise UpdateError(f"无法确认 {now} 是否为 A 股交易日，已保留上一版：{calendar_error}")

    prior = read_json(args.published) if args.published.exists() else None
    work = read_json(args.work)

    attempts = 0
    if not args.publish_only:
        last_error = "未知原因"
        for attempts in range(1, max(1, args.retries) + 1):
            log.line("INFO", f"第 {attempts}/{args.retries} 次尝试：补齐 {through} 及之前的数据")
            started = time.monotonic()
            command = [sys.executable, str(args.refresh), "--output", str(args.work)]
            if args.through:
                command += ["--through", str(args.through)]
            environment = os.environ.copy()
            if args.direct:
                for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                    environment.pop(key, None)
            try:
                result = subprocess.run(command, cwd=str(ROOT), capture_output=True, text=True,
                                        timeout=args.refresh_timeout, env=environment)
            except subprocess.TimeoutExpired:
                last_error = f"取数超时（>{int(args.refresh_timeout)} 秒）"
            else:
                for stream, name in ((result.stdout, "stdout"), (result.stderr, "stderr")):
                    for line in (stream or "").strip().splitlines()[-12:]:
                        log.line("INFO", f"  {name}: {line}")
                if result.returncode == 0:
                    log.line("INFO", f"取数完成，用时 {time.monotonic() - started:.1f} 秒")
                    break
                detail = (result.stderr or result.stdout or "").strip().splitlines()
                last_error = detail[-1] if detail else f"取数程序退出码 {result.returncode}"
            last_error = last_error[:MAX_REASON]
            log.line("WARN", f"第 {attempts} 次未成功：{last_error}")
            if attempts < args.retries:
                log.line("INFO", f"等待 {int(args.retry_wait)} 秒后重试")
                time.sleep(args.retry_wait)
        else:
            raise UpdateError(f"公开源数据未齐，已保留上一版：{last_error}")

        work = read_json(args.work)

    latest = validate_history(work, through if not args.publish_only else date.fromisoformat(work["records"][-1]["date"]), prior)
    log.line("INFO", f"快照校验通过：{len(work['records'])} 个交易日，最新 {latest}")

    same = prior is not None and canonical(prior.get("records")) == canonical(work["records"])
    if same and not args.publish_only:
        log.line("INFO", "对外 JSON 已是最新，无需重写")
        return {"status": "up_to_date", "through": str(through), "attempts": attempts, "latest": latest}

    if args.dry_run:
        log.line("INFO", "dry-run：未写入对外 JSON")
        return {"status": "dry_run", "through": str(through), "attempts": attempts, "latest": latest}

    result = publish(work, args.published, args.backup_dir, args.keep_backups, beijing_now())
    log.line("INFO", f"已发布 {args.published}（{result['bytes']} 字节，{result['meta']['record_count']} 个交易日，截至 {latest}）")
    summary = {"status": "published", "through": str(through), "attempts": attempts, "latest": latest,
               "published_at": result["meta"]["published_at"], "backup": result["backup"],
               "record_count": result["meta"]["record_count"]}
    if args.readback_url:
        matched, note = http_readback(args.readback_url, result["records"])
        summary["readback"] = note
        if matched:
            log.line("INFO", note)
        else:
            log.line("ERROR", f"对外 JSON 已写入，但{note}")
            summary["status"] = "readback_mismatch"
    return summary


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.direct:
        # 本进程的交易日历请求也走直连，与取数子进程保持一致。
        install_opener(build_opener(ProxyHandler({})))
    log = RunLog(args.log_dir, beijing_now())
    started = time.monotonic()
    try:
        with update_lock(args.work.parent, args.lock_stale):
            summary = run(args, log)
    except LockBusy as error:
        log.line("WARN", str(error))
        log.finish({"status": "skipped_busy", "reason": str(error), "duration_s": round(time.monotonic() - started, 1)})
        return 0
    except UpdateError as error:
        log.line("ERROR", f"本次未发布，保留上一版：{error}")
        log.finish({"status": "error", "reason": str(error), "duration_s": round(time.monotonic() - started, 1)})
        return 1
    except Exception as error:  # noqa: BLE001 - 兜底写日志，避免定时任务静默失败
        log.line("ERROR", f"意外失败，未发布：{type(error).__name__}: {error}")
        log.finish({"status": "error", "reason": f"{type(error).__name__}: {error}", "duration_s": round(time.monotonic() - started, 1)})
        return 1
    summary["duration_s"] = round(time.monotonic() - started, 1)
    summary["log"] = str(log.text)
    log.finish(summary)
    atomic_write(args.work.parent / "last_run.json", (json.dumps(summary, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return 1 if summary.get("status") == "readback_mismatch" else 0


if __name__ == "__main__":
    sys.exit(main())
