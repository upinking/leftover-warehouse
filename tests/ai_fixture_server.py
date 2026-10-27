"""手动 UI 验收用的本机协议夹具。它不是 AI，不会随 .app 打包。"""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if not body.get('tools'):
            message = {'content': '仅协议测试：连接正常。'}
        else:
            users = [m['content'] for m in body['messages'] if m['role'] == 'user']
            if '收到' in users[-1]:
                message = {'content': None, 'tool_calls': [{'id': 'fixture-call', 'type': 'function', 'function': {
                    'name': 'propose_actions', 'arguments': json.dumps({'summary': '仅UI验收：红灯收货20颗，放QA-A01', 'actions': [
                        {'action': 'stock.record', 'payload': {'materialId': 'material-C84270', 'type': 'receipt',
                         'quantity': 20, 'reason': '仅AI协议UI测试，非真实收货', 'locationCode': 'QA-A01'}}]}, ensure_ascii=False)}}]}
            else:
                message = {'content': '仅协议测试返回：C84270采购20个，预留13个，预计自由7个，实物待点数。'}
        data = json.dumps({'choices': [{'message': message}]}, ensure_ascii=False).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)


if __name__ == '__main__':
    server = HTTPServer(('127.0.0.1', 0), Provider)
    print('http://127.0.0.1:%s/v1' % server.server_port, flush=True)
    server.serve_forever()
