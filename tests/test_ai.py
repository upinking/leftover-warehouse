"""AI 协议/确认流程验证；模型响应使用明确的测试服务，不冒充真实模型联通。"""
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ai_assistant import AIService, endpoint_for, http_transport
from ai_credentials import CredentialStore
from warehouse import DomainError, Warehouse

RED = "material-C84270"
LAB = "project-stm32-led-lab"


class FakeCredentials:
    def __init__(self): self.key = None; self.remember = False
    def get(self): return self.key
    def set(self, value, remember=False): self.key = value; self.remember = remember
    def clear(self): self.key = None
    def status(self): return {"hasKey": bool(self.key), "storage": "session" if self.key else "none"}


def answer(text): return {"choices": [{"message": {"content": text}}]}
def calls(name, arguments):
    return {"choices": [{"message": {"content": None, "tool_calls": [{"id": "call-test", "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]}}]}


class AITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.w = Warehouse(self.path / "warehouse.sqlite3", ROOT / "data/seed.json")
        self.key = FakeCredentials()
        self.responses = []
        self.sent = []
        def transport(endpoint, payload, key, timeout):
            self.sent.append(copy.deepcopy({"endpoint": endpoint, "payload": payload, "key": key}))
            return self.responses.pop(0)
        self.transport = transport
        self.ai = AIService(self.w, self.path, self.key, transport)
        self.ai.save_settings({"baseUrl": "https://model.example/v1", "model": "chosen-model", "apiKey": "test-secret-value"})

    def tearDown(self):
        self.ai.close(); self.w.close(); self.temp.cleanup()

    def receipt(self, quantity=20):
        return {"action": "stock.record", "payload": {"materialId": RED, "type": "receipt", "quantity": quantity,
                                                     "reason": "仅测试收货", "locationCode": "QA-A01"}}

    def propose(self, actions=None, request="chat-1"):
        self.responses.append(calls("propose_actions", {"summary": "登记红灯收货", "actions": actions or [self.receipt()]}))
        return self.ai.chat("收到20颗C84270，放QA-A01", request)

    def test_proposal_does_not_write_and_confirmation_is_idempotent(self):
        before = self.w.snapshot()
        result = self.propose()
        self.assertEqual(self.w.snapshot(), before)
        p = result["pendingPlan"]
        self.assertIsNone(p["preview"]["changes"][0]["before"]["actualOnHand"])
        self.assertEqual(p["preview"]["changes"][0]["after"]["actualOnHand"], 20)
        result = self.ai.confirm(p["id"], "confirm-1")
        self.ai.confirm(p["id"], "confirm-new-id")
        snap = self.w.snapshot()
        self.assertEqual(len(snap["data"]["stockMovements"]), 1)
        self.assertEqual(snap["data"]["stockMovements"][0]["actor"], "AI助手（用户确认）")
        self.assertEqual(result["snapshot"]["summary"]["notCountedKinds"], 15)

    def test_natural_confirmation_and_chat_retry_do_not_call_model_twice(self):
        self.propose()
        self.ai.chat("收到20颗C84270，放QA-A01", "chat-1")
        self.assertEqual(len(self.sent), 1)
        self.ai.chat("确认执行", "confirm-text")
        self.ai.chat("确认执行", "confirm-text")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.w.snapshot()["data"]["stockMovements"]), 1)

    def test_read_query_receives_real_data_without_secrets_or_audit_paths(self):
        self.responses += [calls("search_inventory", {"query": "C84270"}), answer("该红灯预计自由7个，实物待点数。")]
        result = self.ai.chat("C84270还有多少", "read")
        self.assertIn("待点数", result["reply"])
        self.assertEqual(self.w.snapshot()["data"]["stockMovements"], [])
        prompt = json.dumps(self.sent[0]["payload"], ensure_ascii=False)
        self.assertIn('"orderedPieces":675', prompt.replace(" ", "").replace('\\"', '"'))
        self.assertNotIn("test-secret-value", prompt)
        self.assertNotIn(str(self.path), prompt)
        self.assertNotIn("auditLog", prompt)
        self.assertEqual(self.sent[0]["endpoint"], "https://model.example/v1/chat/completions")
        self.assertEqual(self.sent[0]["payload"]["model"], "chosen-model")

    def test_stale_preview_cannot_be_confirmed(self):
        p = self.propose()["pendingPlan"]
        self.w.execute("stock.record", {"materialId": RED, "type": "receipt", "quantity": 0, "reason": "手动点数"}, "manual")
        before = self.w.snapshot()
        with self.assertRaisesRegex(DomainError, "账本已经变化"):
            self.ai.confirm(p["id"])
        self.assertEqual(self.w.snapshot(), before)

    def test_confirm_recovers_commit_before_ai_receipt_was_saved(self):
        p = self.propose()["pendingPlan"]
        self.w.execute_batch(p["actions"], "AI-plan:" + p["id"],
            expected_revision=p["preview"]["baseRevision"],
            expected_fingerprint=p["preview"]["baseFingerprint"])
        self.ai.confirm(p["id"])
        self.assertEqual(len(self.w.snapshot()["data"]["stockMovements"]), 1)
        self.assertIsNone(self.ai.history()["pendingPlan"])

    def test_same_revision_different_ledger_is_also_rejected(self):
        p = self.propose()["pendingPlan"]
        state = self.w.snapshot()["data"]
        state["openingStock"][0]["locationCode"] = "恢复的另一份账本"
        self.w.db.execute("UPDATE ledger SET data=? WHERE id=1", (json.dumps(state),))
        with self.assertRaisesRegex(DomainError, "账本已经变化"):
            self.ai.confirm(p["id"])
        self.assertEqual(self.w.snapshot()["data"]["stockMovements"], [])

    def test_cancelled_and_replaced_plans_cannot_execute(self):
        old = self.propose()["pendingPlan"]
        new = self.propose([self.receipt(30)], "chat-2")["pendingPlan"]
        with self.assertRaises(DomainError): self.ai.confirm(old["id"])
        self.ai.cancel(new["id"])
        with self.assertRaises(DomainError): self.ai.confirm(new["id"])
        self.assertEqual(self.w.snapshot()["data"]["stockMovements"], [])

    def test_pending_and_history_survive_restart(self):
        p = self.propose()["pendingPlan"]
        self.ai.close()
        self.ai = AIService(self.w, self.path, self.key, self.transport)
        self.assertEqual(self.ai.history()["pendingPlan"]["id"], p["id"])
        self.assertEqual(len(self.ai.history()["messages"]), 2)
        self.ai.confirm(p["id"])
        self.assertEqual(len(self.w.snapshot()["data"]["stockMovements"]), 1)

    def test_forbidden_tools_actions_and_bad_parameters_do_not_write(self):
        before = self.w.snapshot()
        for action in ["backup.restore", "seed.import", "run_shell"]:
            self.responses.append(calls("propose_actions", {"summary": "不应执行", "actions": [{"action": action, "payload": {}}]}))
            with self.assertRaises(DomainError): self.ai.chat("不合法测试", action)
        self.responses.append(calls("execute_sql", {"sql": "DROP TABLE ledger"}))
        with self.assertRaises(DomainError): self.ai.chat("坏工具", "bad-tool")
        self.responses.append({"choices": []})
        with self.assertRaises(DomainError): self.ai.chat("坏响应", "bad-response")
        self.assertEqual(self.w.snapshot(), before)

    def test_unknown_stock_blocks_whole_board_proposal(self):
        self.responses.append(calls("propose_actions", {"summary": "领板", "actions": [
            {"action": "project.issue", "payload": {"projectId": LAB, "units": 1}}]}))
        before = self.w.snapshot()
        with self.assertRaises(DomainError) as raised: self.ai.chat("领一块灯板", "unknown")
        self.assertEqual(len(raised.exception.details["missing"]), 6)
        self.assertEqual(self.w.snapshot(), before)

    def test_batch_all_or_nothing_and_whole_board_only_consumes_25(self):
        bad = [self.receipt(), {"action": "stock.record", "payload": {"materialId": "missing", "type": "loss", "quantity": 1, "reason": "错误"}}]
        with self.assertRaises(DomainError): self.w.execute_batch(bad, "bad-batch")
        self.assertEqual(self.w.snapshot()["data"]["stockMovements"], [])
        for row in self.w.snapshot()["stocks"]:
            self.w.execute("stock.record", {"materialId": row["materialId"], "type": "receipt", "quantity": row["ordered"], "reason": "测试收齐"}, row["materialId"])
        action = [{"action": "project.issue", "payload": {"projectId": LAB, "units": 1}}]
        p = self.propose(action)["pendingPlan"]
        self.assertEqual(p["preview"]["summaryAfter"]["actualOnHandPieces"], 650)
        self.ai.confirm(p["id"])
        summary = self.w.snapshot()["summary"]
        self.assertEqual((summary["actualOnHandPieces"], summary["reservedPieces"], summary["forecastFreePieces"]), (650, 155, 495))

    def test_configuration_and_backups_do_not_contain_key(self):
        self.assertNotIn("test-secret-value", self.ai.config_path.read_text())
        self.assertNotIn("test-secret-value", json.dumps(self.ai.settings()))
        self.assertNotIn("test-secret-value", json.dumps(self.w.backup()))
        self.ai.save_settings({"baseUrl": "https://different.example/v1", "model": "m"})
        self.assertIsNone(self.key.get())
        with self.assertRaises(DomainError): self.ai.chat("查库存", "no-key")
        store = CredentialStore(self.path / "session-only")
        store.set("session-secret")
        self.assertNotIn("session-secret", store.marker.read_text())
        self.assertIsNone(CredentialStore(self.path / "session-only").get())

    def test_connection_test_does_not_send_inventory(self):
        self.responses.append(answer("连接正常"))
        self.ai.test_connection()
        body = json.dumps(self.sent[-1]["payload"], ensure_ascii=False)
        self.assertNotIn(RED, body)
        self.assertNotIn("675", body)
        self.assertNotIn("tools", self.sent[-1]["payload"])

    def test_endpoint_validation(self):
        for value in ["http://remote.example/v1", "https://user:secret@example.com/v1", "https://example.com/v1?api_key=x", "file:///tmp/key"]:
            with self.assertRaises(DomainError): endpoint_for(value)
        self.assertEqual(endpoint_for("http://127.0.0.1:11434/v1"), "http://127.0.0.1:11434/v1/chat/completions")

    def test_real_local_http_protocol_and_401_redacts_key(self):
        received = []
        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                received.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"]))), self.headers.get("Authorization")))
                self.send_response(401); self.end_headers(); self.wfile.write(b'test-secret-value')
        server = HTTPServer(("127.0.0.1", 0), Provider)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with self.assertRaises(DomainError) as raised:
                http_transport("http://127.0.0.1:%s/v1/chat/completions" % server.server_port,
                               {"model": "m", "messages": [{"role": "user", "content": "仅连接测试"}]}, "test-secret-value", 2)
            self.assertNotIn("test-secret-value", str(raised.exception))
            self.assertIn("密钥", str(raised.exception))
            self.assertEqual(received[0][0], "/v1/chat/completions")
            self.assertEqual(received[0][2], "Bearer test-secret-value")
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == "__main__": unittest.main()
