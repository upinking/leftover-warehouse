#!/usr/bin/env python3
"""余料仓库的本地入口。只监听本机；业务计算都在 warehouse.py。"""
import argparse
import json
import mimetypes
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

from warehouse import DomainError, Warehouse

ROOT = Path(__file__).resolve().parent
APP_ID = "leftover-warehouse-local-v1"
MAX_BODY = 64 * 1024 * 1024


class LocalServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def make_handler(warehouse, ai_service=None):
    # 按需启动 AI。普通库存功能不依赖模型、密钥或网络。
    assistant_box = [ai_service]
    assistant_lock = threading.Lock()

    def assistant():
        with assistant_lock:
            if assistant_box[0] is None:
                from ai_assistant import AIService
                assistant_box[0] = AIService(warehouse, Path(warehouse.db_path).resolve().parent)
            return assistant_box[0]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # 不在日志输出库存正文、备份内容。
            sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

        def send_bytes(self, status, body, mime, headers=None):
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, status, data, headers=None):
            self.send_bytes(status, json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8"), "application/json; charset=utf-8", headers)

        def valid_host(self):
            port = self.server.server_port
            return self.headers.get("Host") in {"127.0.0.1:%s" % port, "localhost:%s" % port}

        def do_GET(self):
            if not self.valid_host():
                return self.send_json(403, {"error": "请使用本机地址访问余料仓库"})
            path = urlparse(self.path).path
            try:
                if path == "/api/health":
                    return self.send_json(200, {"app": APP_ID, "database": str(warehouse.db_path)})
                if path == "/api/state":
                    return self.send_json(200, warehouse.snapshot())
                if path == "/api/ai/settings":
                    return self.send_json(200, assistant().settings())
                if path == "/api/ai/history":
                    return self.send_json(200, assistant().history())
                if path == "/api/backup":
                    return self.send_json(200, warehouse.backup(), {"Content-Disposition": 'attachment; filename="warehouse-backup.json"'})
                files = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/ai.js": "ai.js", "/bom.js": "bom.js", "/styles.css": "styles.css"}
                if path == "/favicon.ico":
                    return self.send_bytes(204, b"", "image/x-icon")
                if path not in files:
                    return self.send_json(404, {"error": "找不到这个页面"})
                file = ROOT / "web" / files[path]
                mime = mimetypes.guess_type(str(file))[0] or "application/octet-stream"
                return self.send_bytes(200, file.read_bytes(), mime + "; charset=utf-8")
            except DomainError as exc:
                return self.send_json(exc.status, {"error": str(exc), "details": exc.details})
            except Exception:
                import traceback
                traceback.print_exc()
                return self.send_json(500, {"error": "读取失败，请查看启动窗口中的错误信息"})

        def do_POST(self):
            if not self.valid_host():
                return self.send_json(403, {"error": "请求来源不是本机仓库"})
            allowed_origins = {"http://127.0.0.1:%s" % self.server.server_port, "http://localhost:%s" % self.server.server_port}
            if self.headers.get("Origin") and self.headers.get("Origin") not in allowed_origins:
                return self.send_json(403, {"error": "不接受其他网页发起的修改"})
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                return self.send_json(415, {"error": "请提交 JSON 数据"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BODY:
                    return self.send_json(413, {"error": "文件为空或超过 64 MB"})
                body = json.loads(self.rfile.read(length).decode("utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError("非法数字")))
                if not isinstance(body, dict):
                    return self.send_json(400, {"error": "请求必须是 JSON 对象"})
                path = urlparse(self.path).path
                if path == "/api/ai/settings":
                    return self.send_json(200, assistant().save_settings(body))
                if path == "/api/ai/test":
                    return self.send_json(200, assistant().test_connection())
                if path == "/api/ai/chat":
                    return self.send_json(200, assistant().chat(body.get("message"), body.get("requestId")))
                if path == "/api/ai/confirm":
                    return self.send_json(200, assistant().confirm(body.get("planId"), body.get("requestId")))
                if path == "/api/ai/cancel":
                    return self.send_json(200, assistant().cancel(body.get("planId")))
                if path == "/api/ai/clear-key":
                    return self.send_json(200, assistant().clear_key())
                if path == "/api/bom/parse":
                    from bom_import import parse_bom
                    return self.send_json(200, parse_bom(body))
                if path == "/api/bom/preview":
                    return self.send_json(200, warehouse.preview_bom(body))
                if path == "/api/backup/preview":
                    return self.send_json(200, {"preview": warehouse.preview_backup(body)})
                if path == "/api/action":
                    if not isinstance(body.get("action"), str) or not isinstance(body.get("payload"), dict) or not isinstance(body.get("requestId"), str):
                        return self.send_json(400, {"error": "缺少操作名、操作数据或请求标识"})
                    result = warehouse.execute(body["action"], body["payload"], body["requestId"])
                    return self.send_json(200, {"result": result, "snapshot": warehouse.snapshot()})
                return self.send_json(404, {"error": "找不到这个接口"})
            except DomainError as exc:
                return self.send_json(getattr(exc, "status", 400), {"error": str(exc), "details": getattr(exc, "details", None)})
            except (ValueError, UnicodeError):
                return self.send_json(400, {"error": "JSON 格式错误，请检查文件"})
            except Exception:
                import traceback
                traceback.print_exc()
                return self.send_json(500, {"error": "操作失败，未确认保存；请查看启动窗口中的错误信息后重试"})
    return Handler


def main():
    parser = argparse.ArgumentParser(description="本地单用户余料仓库")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", type=Path, default=ROOT / "app_data" / "warehouse.sqlite3")
    parser.add_argument("--no-seed", action="store_true", help="空数据库启动，不导入初始化文件")
    parser.add_argument("--open", action="store_true", help="自动打开默认浏览器")
    args = parser.parse_args()
    address = "http://127.0.0.1:%s" % args.port
    # 重复双击打开已有实例，不创建第二个仓库进程。
    try:
        with urlopen(address + "/api/health", timeout=1) as response:
            health = json.load(response)
        if health.get("app") == APP_ID and Path(health.get("database", "")).resolve() == args.db.resolve():
            print("余料仓库已在运行：" + address)
            if args.open:
                webbrowser.open(address)
            return
    except Exception:
        pass
    store = Warehouse(args.db, None if args.no_seed else ROOT / "data" / "seed.json")
    try:
        server = LocalServer(("127.0.0.1", args.port), make_handler(store))
    except OSError as exc:
        print("端口被占用：%s。可使用 python3 app.py --port 8766 --open" % exc, file=sys.stderr)
        return 1
    print("余料仓库：" + address, flush=True)
    print("本地数据：" + str(args.db.resolve()), flush=True)
    print("停止服务：在此窗口按 Control+C。关闭网页不会丢失已保存数据。", flush=True)
    if args.open:
        webbrowser.open(address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。已提交的数据保留在本地数据库。")
    finally:
        server.server_close()
        if hasattr(store, "close"):
            store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
