import base64
import json
import unittest
from unittest.mock import patch

from app.services import workbuddy_direct as wd


def make_jwt(payload: dict) -> str:
    def part(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{part({'alg': 'RS256', 'typ': 'JWT'})}.{part(payload)}.c2lnbmF0dXJl"


class TokenParsingTest(unittest.TestCase):
    def test_reads_expiry_and_audience(self):
        token = make_jwt({"exp": 1797614439, "aud": "account", "sub": "u1"})
        self.assertEqual(wd.token_expiry(token), 1797614439)
        self.assertEqual(wd.token_audience(token), "account")

    def test_audience_list_is_flattened(self):
        token = make_jwt({"exp": 1, "aud": ["account", "api"]})
        self.assertEqual(wd.token_audience(token), "account,api")

    def test_malformed_token_is_not_fatal(self):
        self.assertEqual(wd.token_expiry("not-a-jwt"), 0.0)
        self.assertEqual(wd.token_audience("a.b"), "")


class ClientTokenCacheTest(unittest.TestCase):
    def setUp(self):
        wd.reset_cache()

    def tearDown(self):
        wd.reset_cache()

    def test_uses_cache_instead_of_rescanning(self):
        token = make_jwt({"exp": 9999999999, "aud": "account"})
        with patch.object(wd, "client_pids", return_value=[1234]), \
                patch.object(wd, "scan_tokens", return_value=[token]) as scan:
            self.assertEqual(wd.client_token(), token)
            self.assertEqual(wd.client_token(), token)
            self.assertEqual(scan.call_count, 1)

    def test_skips_expired_and_foreign_tokens(self):
        expired = make_jwt({"exp": 1000, "aud": "account"})
        other = make_jwt({"exp": 9999999999, "aud": "some-service"})
        good = make_jwt({"exp": 9999999999, "aud": "account"})
        with patch.object(wd, "client_pids", return_value=[1]), \
                patch.object(wd, "scan_tokens", return_value=[expired, other, good]):
            self.assertEqual(wd.client_token(), good)

    def test_raises_when_client_not_running(self):
        with patch.object(wd, "client_pids", return_value=[]), \
                patch.object(wd, "_load_cached_token", return_value=("", 0.0)):
            with self.assertRaises(wd.DirectUnavailable):
                wd.client_token()

    def test_falls_back_to_encrypted_copy_when_client_is_gone(self):
        """客户端关掉以后，上次加密存下的令牌还能顶一阵（28 天有效期）。"""
        saved = make_jwt({"exp": 9999999999, "aud": "account"})
        with patch.object(wd, "client_pids", return_value=[]), \
                patch.object(wd, "_load_cached_token", return_value=(saved, 9999999999)):
            self.assertEqual(wd.client_token(), saved)


class FakeResponse:
    def __init__(self, lines):
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class StreamParsingTest(unittest.IsolatedAsyncioTestCase):
    async def test_joins_content_and_keeps_usage(self):
        lines = [
            'data: {"model":"glm-5.3-flash","choices":[{"delta":{"reasoning_content":"想"}}]}',
            'data: {"choices":[{"delta":{"content":"{\\"ok\\":"}}]}',
            'data: {"choices":[{"delta":{"content":"true}"}}],"usage":{"total_tokens":9}}',
            "data: [DONE]",
        ]
        result = await wd._read_stream(FakeResponse(lines), None, 60.0, 0.0)
        self.assertEqual(result["content"], '{"ok":true}')
        self.assertEqual(result["reasoning"], "想")
        self.assertEqual(result["usage"], {"total_tokens": 9})
        self.assertEqual(result["model"], "glm-5.3-flash")

    async def test_in_stream_error_is_raised(self):
        lines = ['data: {"error":{"code":"login_required","message":"该能力需要登录后使用"}}']
        with self.assertRaises(RuntimeError) as ctx:
            await wd._read_stream(FakeResponse(lines), None, 60.0, 0.0)
        self.assertIn("login_required", str(ctx.exception))

    async def test_empty_content_is_an_error(self):
        lines = ['data: {"choices":[{"delta":{"reasoning_content":"只有思考"}}]}', "data: [DONE]"]
        with self.assertRaises(RuntimeError):
            await wd._read_stream(FakeResponse(lines), None, 60.0, 0.0)


if __name__ == "__main__":
    unittest.main()
