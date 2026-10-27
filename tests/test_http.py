"""真正启动 HTTP 进程，验证保存、停止、重启和空库恢复。所有写入只在临时目录。"""
import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


class ProcessPersistenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.process = None

    def tearDown(self):
        self.stop()
        self.temp.cleanup()

    def stop(self):
        if self.process:
            self.process.terminate()
            self.process.wait(timeout=10)
            self.process = None

    def start(self, name, empty=False):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.base = "http://127.0.0.1:%s" % port
        args = [sys.executable, str(ROOT / "app.py"), "--port", str(port), "--db", str(Path(self.temp.name) / name)]
        if empty:
            args.append("--no-seed")
        self.process = subprocess.Popen(args, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if self.process.poll() is not None:
                self.fail("服务启动失败")
            try:
                return self.get("/api/health")
            except (OSError, URLError):
                time.sleep(0.05)
        self.fail("服务未在五秒内就绪")

    def get(self, path):
        with urlopen(self.base + path, timeout=5) as response:
            return json.load(response)

    def post(self, path, body, origin=None):
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        request = Request(self.base + path, data=json.dumps(body).encode(), headers=headers)
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def test_process_restart_and_replace_restore(self):
        self.start("original.sqlite3")
        initial = self.get("/api/state")
        self.assertEqual(initial["summary"]["forecastFreePieces"], 495)
        self.assertIsNone(initial["summary"]["actualOnHandPieces"])
        material = initial["data"]["materials"][-1]["id"]
        request = {"action": "stock.record", "requestId": "http-receipt-zero", "payload": {"materialId": material, "type": "receipt", "quantity": 0, "reason": "测试：确认实收为零", "locationCode": "测试盒 A-03"}}
        response = self.post("/api/action", request)
        self.post("/api/action", request)
        saved = self.get("/api/state")
        self.assertEqual(saved, response["snapshot"])
        red = next(s for s in saved["stocks"] if s["materialId"] == material)
        self.assertEqual(red["actualOnHand"], 0)
        self.assertEqual(red["locationCode"], "测试盒 A-03")
        backup = self.get("/api/backup")
        preview = self.post("/api/backup/preview", backup)
        self.assertIn("preview", preview)
        with self.assertRaises(HTTPError) as blocked:
            self.post("/api/action", request, "https://example.invalid")
        self.assertEqual(blocked.exception.code, 403)
        broken = dict(backup, version=999)
        with self.assertRaises(HTTPError):
            self.post("/api/backup/preview", broken)
        self.assertEqual(self.get("/api/state"), saved)
        self.stop()
        self.start("original.sqlite3")
        self.assertEqual(self.get("/api/state"), saved)
        self.stop()
        self.start("restored.sqlite3", empty=True)
        self.assertEqual(self.get("/api/state")["summary"]["materialKinds"], 0)
        self.post("/api/backup/preview", backup)
        self.post("/api/action", {"action": "backup.restore", "payload": {"backup": backup}, "requestId": "http-restore"})
        restored = self.get("/api/state")
        self.assertEqual(restored["stocks"], saved["stocks"])
        self.assertEqual(restored["summary"], saved["summary"])
        self.assertEqual(restored["data"]["stockMovements"], saved["data"]["stockMovements"])
        self.post("/api/action", request)  # 恢复也带回防重复请求记录。
        self.assertEqual(self.get("/api/state")["data"]["stockMovements"], saved["data"]["stockMovements"])
        self.stop()
        self.start("restored.sqlite3", empty=True)
        self.assertEqual(self.get("/api/state")["stocks"], saved["stocks"])


if __name__ == "__main__":
    unittest.main()
