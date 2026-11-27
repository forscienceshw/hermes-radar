"""智能收藏建议：只认精读分类/标签里真的出现过的方向名，宁缺毋滥。"""
import unittest
import uuid

from app.api import db
from app.services.collect_suggest import suggest


class CollectSuggestTest(unittest.TestCase):
    def setUp(self):
        self.item_id = f"test-suggest-{uuid.uuid4().hex[:8]}"
        self.created_collections = []
        db.execute("""INSERT INTO items(id,kind,external_key,url,canonical_url,discovered_at,title,
                      summary,analysis_json)
                      VALUES(?,?,?,?,?,?,?,?,?)""",
                   (self.item_id, "article", self.item_id, "https://example.com/s",
                    "https://example.com/s", "2026-11-27T00:00:00+00:00",
                    "微流控工程化囊泡用于光动力治疗",
                    "用微流控做均一脂质囊泡，红光触发内质网氧化爆发。",
                    '{"category":"纳米医学与肿瘤放疗免疫","tags":["微流控","光动力治疗","内质网应激"]}'))

    def tearDown(self):
        db.execute("DELETE FROM collection_entries WHERE entry_id=?", (self.item_id,))
        for cid in self.created_collections:
            db.execute("DELETE FROM collections WHERE id=?", (cid,))
        db.execute("DELETE FROM items WHERE id=?", (self.item_id,))

    def _collection(self, name: str) -> str:
        row = db.one("SELECT id FROM collections WHERE scope='library' AND name=?", (name,))
        if row:
            return row["id"]
        cid = f"test-col-{uuid.uuid4().hex[:8]}"
        db.execute("""INSERT INTO collections(id,name,scope,created_at,updated_at)
                      VALUES(?,?,'library','2026-11-27T00:00:00+00:00','2026-11-27T00:00:00+00:00')""",
                   (cid, name))
        self.created_collections.append(cid)
        return cid

    def test_matches_topic_name_from_analysis(self):
        cid = self._collection("微流控")
        row = db.one("SELECT * FROM items WHERE id=?", (self.item_id,))
        result = suggest(db, row, [])
        names = [item["name"] for item in result["suggestions"]]
        self.assertIn("微流控", names)
        self.assertEqual(result["suggestions"][0]["id"], cid)

    def test_skips_generic_catch_all_collections(self):
        self._collection("实验")
        row = db.one("SELECT * FROM items WHERE id=?", (self.item_id,))
        names = [item["name"] for item in suggest(db, row, [])["suggestions"]]
        self.assertNotIn("实验", names)

    def test_excludes_collections_already_containing_item(self):
        cid = self._collection("微流控")
        row = db.one("SELECT * FROM items WHERE id=?", (self.item_id,))
        names = [item["name"] for item in suggest(db, row, [cid])["suggestions"]]
        self.assertNotIn("微流控", names)

    def test_at_most_two_and_second_must_be_strong(self):
        self._collection("微流控")
        self._collection("光动力治疗")
        self._collection("蛋白质设计")
        row = db.one("SELECT * FROM items WHERE id=?", (self.item_id,))
        suggestions = suggest(db, row, [])["suggestions"]
        self.assertLessEqual(len(suggestions), 2)
        for item in suggestions:
            self.assertGreaterEqual(item["score"], 3.0)

    def test_reports_names_already_holding_the_item(self):
        """全部命中项都收藏过了，也要把名字报回去——界面才能说「已经在里面了」。"""
        cid = self._collection("微流控")
        row = db.one("SELECT * FROM items WHERE id=?", (self.item_id,))
        result = suggest(db, row, [cid])
        self.assertEqual(result["suggestions"], [])
        self.assertIn("微流控", result["already"])

    def test_falls_back_to_item_category_without_analysis(self):
        """还没精读的条目：用抓取时打的 category / tags 兜底，否则永远「没找到」。"""
        plain_id = f"test-suggest-plain-{uuid.uuid4().hex[:8]}"
        db.execute("""INSERT INTO items(id,kind,external_key,url,canonical_url,discovered_at,title,
                      category,tags_json)
                      VALUES(?,?,?,?,?,?,?,?,?)""",
                   (plain_id, "article", plain_id, "https://example.com/p",
                    "https://example.com/p", "2026-11-27T00:00:00+00:00",
                    "微流控芯片上的单细胞分选", "微流控", '["单细胞"]'))
        try:
            cid = self._collection("微流控")
            row = db.one("SELECT * FROM items WHERE id=?", (plain_id,))
            names = [item["name"] for item in suggest(db, row, [])["suggestions"]]
            self.assertEqual(names, ["微流控"])
            self.assertEqual(suggest(db, row, [cid])["already"], ["微流控"])
        finally:
            db.execute("DELETE FROM collection_entries WHERE entry_id=?", (plain_id,))
            db.execute("DELETE FROM items WHERE id=?", (plain_id,))


if __name__ == "__main__":
    unittest.main()
