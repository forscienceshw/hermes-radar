"""手动上传原论文：落盘、入库、提取页数，且重复上传不会多出一条论文。"""
import asyncio
import io
import os
import unittest
import uuid

from pypdf import PdfWriter
from starlette.requests import Request

from app.api import db, upload_item_paper
from app.paths import DATA_DIR
from app.services import purge
from app.storage import paper_folder


def _pdf_bytes(pages: int = 1) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _request(payload: bytes) -> Request:
    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request({"type": "http", "method": "POST",
                    "headers": [(b"content-length", str(len(payload)).encode())]}, receive)


class PaperUploadTest(unittest.TestCase):
    def setUp(self):
        self.item_id = f"test-paper-{uuid.uuid4().hex[:8]}"
        db.execute("""INSERT INTO items(id,kind,external_key,url,canonical_url,discovered_at,title)
                      VALUES(?,?,?,?,?,?,?)""",
                   (self.item_id, "article", self.item_id, "https://example.com/p",
                    "https://example.com/p", "2026-11-27T00:00:00+00:00", "手动上传测试"))

    def tearDown(self):
        papers = db.query("SELECT id,doi FROM papers p JOIN item_papers ip ON p.id=ip.paper_id WHERE ip.item_id=?",
                          (self.item_id,))
        db.execute("DELETE FROM item_papers WHERE item_id=?", (self.item_id,))
        for paper in papers:
            db.execute("DELETE FROM papers WHERE id=?", (paper["id"],))
            folder = paper_folder(paper["doi"])
            if folder.is_dir() and str(folder).startswith(str(DATA_DIR)):
                purge.tree(folder)
        db.execute("DELETE FROM items WHERE id=?", (self.item_id,))

    def _upload(self, payload: bytes, filename: str = "paper.pdf", paper_id: str = ""):
        return asyncio.run(upload_item_paper(self.item_id, _request(payload),
                                             filename=filename, paper_id=paper_id))

    def test_upload_saves_pdf_and_links_item(self):
        result = self._upload(_pdf_bytes(2))
        self.assertEqual(result["pages"], 2)
        self.assertTrue(os.path.exists(result["path"]))
        paper = db.one("SELECT status,local_path FROM papers WHERE id=?", (result["paper_id"],))
        self.assertEqual(paper["status"], "downloaded")
        link = db.one("SELECT evidence FROM item_papers WHERE item_id=? AND paper_id=?",
                      (self.item_id, result["paper_id"]))
        self.assertEqual(link["evidence"], "手动上传")

    def test_reupload_replaces_same_row(self):
        first = self._upload(_pdf_bytes(1))
        second = self._upload(_pdf_bytes(1), paper_id=first["paper_id"])
        self.assertEqual(first["paper_id"], second["paper_id"])
        count = db.one("SELECT COUNT(*) n FROM item_papers WHERE item_id=?", (self.item_id,))
        self.assertEqual(count["n"], 1)

    def test_rejects_non_pdf(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            self._upload(b"hello, not a pdf")
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
