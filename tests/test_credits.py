"""点数记账与模型选型：两件都出过"看起来对、算起来错"的事故。

第一个是免费模型被按 token 折算：`credit=0` 被当成"字段缺失"，一天 283 次
hy4-flash-free 记出 3082 点，把真实的 46 点完全淹掉。第二个是模型选型，
必须确认免费档真的排在最前、视频/补全/无标价的条目真的被排除——否则
"自动选最便宜的"会挑到一个根本不能做精读的模型。
"""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from app.services import credits, workbuddy_direct


class CreditAccountingTests(unittest.TestCase):
    def test_free_model_never_charges_points(self):
        charged, reference = credits._credit({"total_tokens": 10890, "credit": 0}, "hy4-flash-free")
        self.assertEqual(charged, 0.0)
        self.assertEqual(reference, 0.0)

    def test_free_model_without_credit_field_still_charges_nothing(self):
        # 名字里带 free 就够了，不该退回 total_tokens/1000 那套折算。
        charged, _ = credits._credit({"total_tokens": 5000}, "hy4free")
        self.assertEqual(charged, 0.0)

    def test_free_model_keeps_catalogue_price_as_reference(self):
        charged, reference = credits._credit(
            {"credit": 0, "original_credit": 0.42, "total_tokens": 9000}, "hy4free")
        self.assertEqual(charged, 0.0)
        self.assertEqual(reference, 0.42)

    def test_paid_model_uses_reported_credit(self):
        charged, reference = credits._credit({"credit": 0.19, "total_tokens": 9000}, "glm-5.3-flash")
        self.assertEqual(charged, 0.19)
        self.assertEqual(reference, 0.19)

    def test_paid_model_keeps_catalogue_price_as_reference(self):
        charged, reference = credits._credit(
            {"credit": 0.12, "original_credit": 0.19}, "glm-5.3-flash")
        self.assertEqual(charged, 0.12)
        self.assertEqual(reference, 0.19)

    def test_legacy_row_without_any_field_is_approximated(self):
        # 两个字段都不存在才是真的老数据，这时折算一次让图表不至于空白。
        charged, reference = credits._credit({"total_tokens": 2000}, "unknown-model")
        self.assertEqual(charged, 2.0)
        self.assertEqual(reference, 2.0)


class CreditSummaryTests(unittest.TestCase):
    def _summary(self, rows):
        today = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S")
        for row in rows:
            row.setdefault("time", today)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "llm-usage.jsonl"
            path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                            encoding="utf-8")
            with mock.patch.object(credits, "USAGE_LOG", path):
                return credits.summary(days=30)

    def test_free_calls_are_counted_but_never_charged(self):
        data = self._summary([
            {"model": "hy4-flash-free", "task": "analyze",
             "usage": {"total_tokens": 10_000, "credit": 0}},
            {"model": "glm-5.3-flash", "task": "analyze",
             "usage": {"total_tokens": 5_000, "credit": 0.2}},
        ])
        self.assertAlmostEqual(data["window_points"], 0.2, places=4)
        self.assertEqual(data["free_calls"], 1)
        self.assertEqual(data["free_tokens"], 10_000)
        self.assertEqual(data["today_tokens"], 15_000)

    def test_token_totals_are_reported_next_to_points(self):
        data = self._summary([
            {"model": "glm-5.3-flash", "task": "chat",
             "usage": {"total_tokens": 3_000, "credit": 0.1}},
        ])
        self.assertEqual(data["total_tokens"], 3_000)
        self.assertEqual(data["window_tokens"], 3_000)
        self.assertEqual(data["today_tokens"], 3_000)


class CheapestModelTests(unittest.TestCase):
    CATALOG = [
        {"id": "glm-5.3", "name": "GLM-5.3", "credits": "x0.69"},
        {"id": "hy4flash-free", "name": "Hy4 flash", "credits": "x0.0"},
        {"id": "glm-5.3-flash", "name": "GLM-5.3-Flash", "credits": "x0.08"},
        {"id": "hy4flash", "name": "Hy4 flash", "credits": "x0.03"},
        {"id": "kling-video-v3", "name": "Kling", "credits": "x0.01"},
        {"id": "default-1.1", "name": "Claude", "credits": ""},
        {"id": "fast-model", "name": "快速", "credits": "x0.15"},
        {"id": "deepseek-v3-0324-taco-completion", "name": "补全", "credits": "x0.02"},
    ]

    def setUp(self):
        workbuddy_direct._MODEL_CACHE.update({"at": 0.0, "rows": []})

    def _ranked(self):
        with mock.patch.object(workbuddy_direct, "client_model_catalog",
                               return_value=self.CATALOG):
            return workbuddy_direct.ranked_chat_models(force=True)

    def test_free_model_ranks_first(self):
        rows = self._ranked()
        self.assertEqual(rows[0]["id"], "hy4flash-free")
        self.assertEqual(rows[0]["price"], 0.0)

    def test_cheapest_falls_back_to_paid_when_no_free_entry(self):
        catalog = [row for row in self.CATALOG if row["id"] != "hy4flash-free"]
        with mock.patch.object(workbuddy_direct, "client_model_catalog", return_value=catalog):
            rows = workbuddy_direct.ranked_chat_models(force=True)
        self.assertEqual(rows[0]["id"], "hy4flash")

    def test_video_completion_and_alias_entries_are_excluded(self):
        ids = [row["id"] for row in self._ranked()]
        self.assertNotIn("kling-video-v3", ids)
        self.assertNotIn("deepseek-v3-0324-taco-completion", ids)
        self.assertNotIn("fast-model", ids)
        # 没标价的不能当"免费"，网关照样可能计费。
        self.assertNotIn("default-1.1", ids)

    def test_fallback_chain_skips_the_primary(self):
        with mock.patch.object(workbuddy_direct, "client_model_catalog",
                               return_value=self.CATALOG):
            chain = workbuddy_direct.fallback_models("hy4flash-free", 2)
        self.assertEqual(chain, ["hy4flash", "glm-5.3-flash"])

    def test_no_catalog_returns_none_instead_of_raising(self):
        with mock.patch.object(workbuddy_direct, "client_model_catalog", return_value=[]):
            self.assertIsNone(workbuddy_direct.cheapest_model())


if __name__ == "__main__":
    unittest.main()
