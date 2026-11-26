"""更新/发布流程的离线自测（Python 3.10+ 标准库 unittest）。

覆盖：快照校验规则、只追加、发布与备份、互斥锁、源站未齐时保留旧版、漏跑补齐。
运行：python tests/test_update_daily.py -v
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import update_daily as updater  # noqa: E402

SEED = json.loads((ROOT / "data" / "history.json").read_text(encoding="utf-8"))
LATEST = SEED["records"][-1]["date"]
LATEST_DATE = date.fromisoformat(LATEST)
NEXT_TRADING_DAY = "2026-11-26"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Workspace:
    """为每个用例准备独立的内部快照、对外 JSON、日志与取数桩。"""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.work = base / "current" / "history.json"
        self.published = base / "www" / "api" / "history.json"
        self.logs = base / "logs"
        self.backups = base / "backups"
        self.refresh = base / "stub_refresh.py"
        self.work.parent.mkdir(parents=True)
        self.published.parent.mkdir(parents=True)
        self.work.write_text(json.dumps(SEED, ensure_ascii=False), encoding="utf-8")

    def publish_seed(self, records=None) -> None:
        data = {"indices": SEED["indices"], "records": records or SEED["records"]}
        self.published.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def stub(self, body: str) -> None:
        self.refresh.write_text(body, encoding="utf-8")

    def args(self, *extra: str) -> list[str]:
        return [
            "--work", str(self.work), "--published", str(self.published),
            "--log-dir", str(self.logs), "--backup-dir", str(self.backups),
            "--refresh", str(self.refresh), "--retry-wait", "0", "--keep-backups", "5",
            *extra,
        ]

    def events(self) -> list[dict]:
        path = self.logs / "update.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class ValidateTests(unittest.TestCase):
    def test_seed_is_valid(self):
        self.assertEqual(updater.validate_history(copy.deepcopy(SEED), LATEST_DATE, None), LATEST)

    def test_rejects_changed_history(self):
        data = copy.deepcopy(SEED)
        data["records"][10]["values"][0][1] += 1
        prior = {"indices": SEED["indices"], "records": SEED["records"]}
        with self.assertRaisesRegex(updater.ValidationError, "改写|不符"):
            updater.validate_history(data, LATEST_DATE, prior)

    def test_accepts_refetch_rounding(self):
        # 重新取同一交易日可能出现 1e-8 级别的求和舍入差异，应允许发布。
        data = copy.deepcopy(SEED)
        prior = copy.deepcopy(SEED)
        prior["records"][10]["values"][0][1] += 1e-8
        self.assertEqual(updater.validate_history(data, LATEST_DATE, prior), LATEST)

    def test_rejects_malformed_published(self):
        prior = {"indices": SEED["indices"], "records": copy.deepcopy(SEED["records"])}
        prior["records"][3]["values"] = [[1, 2]]
        with self.assertRaisesRegex(updater.ValidationError, "结构不符"):
            updater.validate_history(copy.deepcopy(SEED), LATEST_DATE, prior)

    def test_rejects_wrong_change(self):
        data = copy.deepcopy(SEED)
        data["records"][-1]["values"][3][0] += 0.5
        with self.assertRaisesRegex(updater.ValidationError, "日度变动"):
            updater.validate_history(data, LATEST_DATE, None)

    def test_rejects_non_positive_size(self):
        data = copy.deepcopy(SEED)
        data["records"][5]["values"][2][1] = 0
        with self.assertRaisesRegex(updater.ValidationError, "总规模"):
            updater.validate_history(data, LATEST_DATE, None)

    def test_rejects_weekend_and_order(self):
        data = copy.deepcopy(SEED)
        data["records"][7]["date"] = "2026-01-17"  # 周六
        with self.assertRaisesRegex(updater.ValidationError, "交易日"):
            updater.validate_history(data, LATEST_DATE, None)
        data = copy.deepcopy(SEED)
        data["records"][9]["date"] = data["records"][8]["date"]
        with self.assertRaisesRegex(updater.ValidationError, "递增"):
            updater.validate_history(data, LATEST_DATE, None)

    def test_rejects_future_record(self):
        data = copy.deepcopy(SEED)
        with self.assertRaisesRegex(updater.ValidationError, "截止日"):
            updater.validate_history(data, date(2026, 11, 20), None)

    def test_rejects_wrong_indices(self):
        data = copy.deepcopy(SEED)
        data["indices"][0], data["indices"][1] = data["indices"][1], data["indices"][0]
        with self.assertRaisesRegex(updater.ValidationError, "八指数"):
            updater.validate_history(data, LATEST_DATE, None)

    def test_rejects_shrinking_history(self):
        data = copy.deepcopy(SEED)
        data["records"] = data["records"][:-1]
        prior = {"indices": SEED["indices"], "records": SEED["records"]}
        with self.assertRaisesRegex(updater.ValidationError, "少于已发布"):
            updater.validate_history(data, LATEST_DATE, prior)


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ws = Workspace(Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def test_publish_only_writes_meta_and_backup(self):
        self.ws.publish_seed(SEED["records"][:-1])
        before = digest(self.ws.published)
        code = updater.main(self.ws.args("--publish-only"))
        self.assertEqual(code, 0)
        published = json.loads(self.ws.published.read_text(encoding="utf-8"))
        self.assertEqual(len(published["records"]), len(SEED["records"]))
        self.assertEqual(published["meta"]["through"], SEED["records"][-1]["date"])
        self.assertEqual(published["meta"]["record_count"], len(SEED["records"]))
        self.assertNotIn("last_members", published)
        backups = list(self.ws.backups.glob("history-*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(before, digest(backups[0]), "备份应是发布前的上一版")
        self.assertNotEqual(before, digest(self.ws.published), "发布后内容应已变化")
        self.assertTrue(any(event["status"] == "published" for event in self.ws.events()))
        last_run = json.loads((self.ws.work.parent / "last_run.json").read_text(encoding="utf-8"))
        self.assertEqual(last_run["latest"], SEED["records"][-1]["date"])
        self.assertFalse((self.ws.work.parent / ".update.lock").exists())

    def test_lock_is_exclusive(self):
        self.ws.publish_seed()
        before = digest(self.ws.published)
        lock = self.ws.work.parent / ".update.lock"
        lock.write_text(json.dumps({"pid": 999999}), encoding="utf-8")
        code = updater.main(self.ws.args("--publish-only"))
        self.assertEqual(code, 0)
        self.assertEqual(before, digest(self.ws.published))
        self.assertTrue(any(event["status"] == "skipped_busy" for event in self.ws.events()))
        self.assertTrue(lock.exists(), "他人持有的锁不应被删除")

    def test_stale_lock_is_reclaimed(self):
        import os
        import time
        self.ws.publish_seed(SEED["records"][:-1])
        lock = self.ws.work.parent / ".update.lock"
        lock.write_text(json.dumps({"pid": 1}), encoding="utf-8")
        old = time.time() - 7200 - 10
        os.utime(lock, (old, old))
        code = updater.main(self.ws.args("--publish-only", "--lock-stale", "60"))
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(self.ws.published.read_text(encoding="utf-8"))["records"]), len(SEED["records"]))
        self.assertFalse(lock.exists())

    def test_failure_keeps_previous_version(self):
        self.ws.publish_seed()
        before = digest(self.ws.published)
        self.ws.stub("import sys\nprint('模拟：深交所份额尚未发布', file=sys.stderr)\nsys.exit(1)\n")
        code = updater.main(self.ws.args("--through", LATEST, "--retries", "1"))
        self.assertEqual(code, 1)
        self.assertEqual(before, digest(self.ws.published), "失败时不得改动对外 JSON")
        events = self.ws.events()
        self.assertEqual(events[-1]["status"], "error")
        self.assertIn("深交所份额尚未发布", events[-1]["reason"])
        log_text = (self.ws.logs / "update.log").read_text(encoding="utf-8")
        self.assertIn("保留上一版", log_text)
        self.assertFalse((self.ws.work.parent / ".update.lock").exists())

    def test_missing_dates_are_filled_in_one_run(self):
        records = SEED["records"]
        self.ws.publish_seed(records[:-1])
        self.ws.work.write_text(json.dumps({**SEED, "records": records[:-2]}, ensure_ascii=False), encoding="utf-8")
        prepared = self.ws.base / "filled.json"
        prepared.write_text(json.dumps(SEED, ensure_ascii=False), encoding="utf-8")
        self.ws.stub(
            "import shutil, sys\n"
            f"target = sys.argv[sys.argv.index('--output') + 1]\n"
            f"shutil.copyfile(r'{prepared}', target)\n"
            f"print('模拟补齐 {records[-2]['date']}、{records[-1]['date']}')\n"
        )
        code = updater.main(self.ws.args("--through", records[-1]["date"], "--retries", "1"))
        self.assertEqual(code, 0)
        published = json.loads(self.ws.published.read_text(encoding="utf-8"))
        self.assertEqual(len(published["records"]), len(records))
        dates = [row["date"] for row in published["records"]]
        self.assertEqual(len(dates), len(set(dates)), "不得出现重复交易日")
        self.assertEqual(dates, [row["date"] for row in records])

    def test_weekend_run_skips_without_touching_data(self):
        self.ws.publish_seed()
        before = digest(self.ws.published)
        code = updater.main(self.ws.args("--today", "2026-11-22"))
        self.assertEqual(code, 0)
        self.assertEqual(before, digest(self.ws.published))
        self.assertTrue(any(event["status"] == "skipped_holiday" for event in self.ws.events()))

    def test_calendar_failure_errors_after_retries(self):
        self.ws.publish_seed()
        before = digest(self.ws.published)
        with mock.patch.object(updater, "trading_day", side_effect=RuntimeError("模拟日历故障")):
            code = updater.main(self.ws.args("--today", NEXT_TRADING_DAY, "--retries", "2"))
        self.assertEqual(code, 1)
        self.assertEqual(before, digest(self.ws.published), "无法确认交易日时不得发布")
        event = self.ws.events()[-1]
        self.assertEqual(event["status"], "error")
        self.assertIn("交易日", event["reason"])
        self.assertIn("模拟日历故障", event["reason"])

    def test_calendar_retry_recovers(self):
        self.ws.publish_seed(SEED["records"][:-1])
        self.ws.stub("print('没有待补齐的交易日')\n")
        with mock.patch.object(updater, "trading_day", side_effect=[RuntimeError("模拟一次失败"), True]):
            code = updater.main(self.ws.args("--today", NEXT_TRADING_DAY, "--retries", "2"))
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(self.ws.published.read_text(encoding="utf-8"))["records"]), len(SEED["records"]))

    def test_readback_mismatch_is_reported(self):
        self.ws.publish_seed(SEED["records"][:-1])
        with mock.patch.object(updater, "http_readback", return_value=(False, "看板地址回读的记录与写入内容不一致：模拟")):
            code = updater.main(self.ws.args("--publish-only", "--readback-url", "http://127.0.0.1:1/api/history"))
        self.assertEqual(code, 1, "回读不一致应返回非零码以便告警")
        self.assertEqual(len(json.loads(self.ws.published.read_text(encoding="utf-8"))["records"]), len(SEED["records"]))
        event = self.ws.events()[-1]
        self.assertEqual(event["status"], "readback_mismatch")
        self.assertIn("回读", event["readback"])
        self.assertIn("已写入", (self.ws.logs / "update.log").read_text(encoding="utf-8"))

    def test_idempotent_repeat_run(self):
        self.ws.stub(
            "import sys\n"
            "print('没有待补齐的交易日')\n"
        )
        code = updater.main(self.ws.args("--through", LATEST, "--retries", "1"))
        self.assertEqual(code, 0)
        published = json.loads(self.ws.published.read_text(encoding="utf-8"))
        self.assertEqual(len(published["records"]), len(SEED["records"]))
        first = published["records"]
        code = updater.main(self.ws.args("--through", LATEST, "--retries", "1"))
        self.assertEqual(code, 0)
        again = json.loads(self.ws.published.read_text(encoding="utf-8"))
        self.assertEqual(again["records"], first, "重复执行不得重复追加或改写记录")


class CalendarTests(unittest.TestCase):
    def test_trading_day_calendar(self):
        try:
            self.assertFalse(updater.trading_day(date(2026, 11, 22)), "周日应为休市")
            self.assertFalse(updater.trading_day(date(2026, 10, 1)), "国庆应为休市")
            self.assertTrue(updater.trading_day(LATEST_DATE), f"{LATEST} 应为交易日")
        except (RuntimeError, OSError, KeyError) as error:
            self.skipTest(f"无法连接深交所交易日历：{error}")


if __name__ == "__main__":
    unittest.main()
