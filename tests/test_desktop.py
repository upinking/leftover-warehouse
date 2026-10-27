"""桌面 helper 的真实子进程验收；只读写临时目录。"""
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from warehouse import Warehouse

SEED = ROOT / 'data' / 'seed.json'
HELPER = ROOT / 'desktop_server.py'
RED = 'material-C84270'


class DesktopHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.processes = []
        self.logs = []
        self.sequence = 0

    def tearDown(self):
        for process in reversed(self.processes):
            self.stop(process)
        for log in self.logs:
            log.close()
        self.temp.cleanup()

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def launch(self, data_dir, legacy=None, parent_pid=None):
        self.assertTrue(HELPER.is_file(), '桌面 helper 尚未实现')
        self.sequence += 1
        ready_file = self.root / ('ready-%d.json' % self.sequence)
        log_path = self.root / ('helper-%d.log' % self.sequence)
        args = [sys.executable, str(HELPER), '--data-dir', str(data_dir), '--ready-file', str(ready_file)]
        if legacy is not None:
            args += ['--legacy-db', str(legacy)]
        if parent_pid is not None:
            args += ['--parent-pid', str(parent_pid)]
        log = log_path.open('wb')
        self.logs.append(log)
        process = subprocess.Popen(args, cwd=ROOT, stdout=log, stderr=log)
        self.processes.append(process)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if ready_file.is_file():
                try:
                    ready = json.loads(ready_file.read_text(encoding='utf-8'))
                except json.JSONDecodeError as error:
                    self.fail('就绪文件不是完整 JSON，应该原子写入：%s' % error)
                self.assertIn(ready.get('status'), ['ready', 'error'])
                return process, ready_file, ready
            if process.poll() is not None:
                self.fail('helper 未写就绪/错误文件就退出：\n' + log_path.read_text(encoding='utf-8'))
            time.sleep(0.03)
        self.fail('helper 未在12秒内就绪：\n' + log_path.read_text(encoding='utf-8'))

    def running(self, data_dir, legacy=None, parent_pid=None):
        process, ready_file, ready = self.launch(data_dir, legacy, parent_pid)
        self.assertEqual(ready['status'], 'ready', ready)
        self.assertGreater(ready['port'], 0)
        self.assertLessEqual(ready['port'], 65535)
        self.assertEqual(Path(ready['dataDir']).resolve(), Path(data_dir).resolve())
        self.assertEqual(Path(ready['database']).resolve(), (Path(data_dir) / 'warehouse.sqlite3').resolve())
        self.assertIsNone(process.poll())
        health = self.get(ready, '/api/health')
        self.assertEqual(Path(health['database']).resolve(), Path(ready['database']).resolve())
        return process, ready_file, ready

    @staticmethod
    def get(ready, path):
        with urlopen('http://127.0.0.1:%s%s' % (ready['port'], path), timeout=3) as response:
            return json.load(response)

    @staticmethod
    def post(ready, action, payload, request_id):
        request = Request('http://127.0.0.1:%s/api/action' % ready['port'],
                          data=json.dumps({'action': action, 'payload': payload, 'requestId': request_id}).encode('utf-8'),
                          headers={'Content-Type': 'application/json'})
        with urlopen(request, timeout=3) as response:
            return json.load(response)

    @staticmethod
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def read_ledger_readonly(path):
        connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
        try:
            return json.loads(connection.execute('SELECT data FROM ledger WHERE id=1').fetchone()[0])
        finally:
            connection.close()

    def test_fresh_dynamic_port_http_save_backup_and_graceful_restart(self):
        data_dir = self.root / 'fresh'
        process, ready_file, ready = self.running(data_dir)
        initial = self.get(ready, '/api/state')
        self.assertEqual(initial['summary']['orderedPieces'], 675)
        self.assertEqual(initial['summary']['forecastFreePieces'], 495)
        self.assertEqual(initial['summary']['notCountedKinds'], 16)
        self.assertEqual(initial['data']['stockMovements'], [])
        payload = {'materialId': RED, 'type': 'receipt', 'quantity': 0,
                   'reason': '桌面集成测试：确认实收为零', 'locationCode': '测试盒/03'}
        response = self.post(ready, 'stock.record', payload, 'desktop-receipt-zero')
        saved = self.get(ready, '/api/state')
        self.assertEqual(saved, response['snapshot'])
        backup = self.get(ready, '/api/backup')
        self.assertEqual(backup['data'], saved['data'])
        self.assertEqual(backup['format'], 'leftover-warehouse-backup')
        self.stop(process)
        self.assertEqual(process.returncode, 0, 'SIGTERM 应先完成清理再正常退出')
        self.assertFalse(ready_file.exists(), '退出应清除陈旧就绪文件')
        with self.assertRaises((URLError, OSError)):
            self.get(ready, '/api/health')
        _, _, restarted = self.running(data_dir)
        self.assertEqual(self.get(restarted, '/api/state'), saved)
        self.post(restarted, 'stock.record', payload, 'desktop-receipt-zero')
        self.assertEqual(self.get(restarted, '/api/state'), saved, '重启后防重复请求也应保留')

    def test_migration_copies_committed_wal_preserves_source_and_migrates_only_once(self):
        legacy = self.root / 'legacy.sqlite3'
        source = Warehouse(legacy, SEED)
        zero_payload = {'materialId': RED, 'type': 'receipt', 'quantity': 0,
                        'reason': '迁移测试：明确零库存', 'locationCode': '旧盒/A-02'}
        source.execute('stock.record', zero_payload, 'legacy-zero')
        source.execute('purchase.add', {'materialId': RED, 'quantity': 10, 'reason': '迁移前新增采购'}, 'legacy-purchase')
        expected = source.snapshot()
        source_main_hash = self.digest(legacy)
        wal = Path(str(legacy) + '-wal')
        self.assertTrue(wal.exists(), '这个案例应实际覆盖尚在WAL中的已提交数据')
        source_wal_hash = self.digest(wal)
        data_dir = self.root / 'desktop'
        try:
            process, _, ready = self.running(data_dir, legacy)
            migrated = self.get(ready, '/api/state')
            self.assertEqual(migrated, expected)
            self.assertEqual(Path(ready['migratedFrom']).resolve(), legacy.resolve())
            self.assertEqual(self.digest(legacy), source_main_hash)
            self.assertEqual(self.digest(wal), source_wal_hash)
            self.assertEqual(source.snapshot(), expected)
            self.post(ready, 'stock.record', zero_payload, 'legacy-zero')
            self.assertEqual(self.get(ready, '/api/state'), expected, '迁移必须包含请求回执，防止历史请求重复记账')
            files = list((data_dir / 'backups').rglob('*'))
            sqlite_backups = [p for p in files if p.is_file() and p.read_bytes()[:16] == b'SQLite format 3\x00']
            self.assertTrue(sqlite_backups, '迁移前应保留可读取的SQLite安全快照')
            receipt = json.loads((data_dir / 'desktop-migration.json').read_text(encoding='utf-8'))
            self.assertEqual(receipt['status'], 'complete', '迁移应保存完成回执')
            self.assertEqual(Path(receipt['source']).resolve(), legacy.resolve())
            self.assertEqual(self.read_ledger_readonly(sqlite_backups[0]), expected['data'])
            self.post(ready, 'purchase.add', {'materialId': RED, 'quantity': 1, 'reason': '桌面迁移后新增'}, 'desktop-new-purchase')
            saved_desktop = self.get(ready, '/api/state')
            source.execute('purchase.add', {'materialId': RED, 'quantity': 100, 'reason': '旧库后来改变'}, 'legacy-changed-later')
            self.stop(process)
            _, _, restarted = self.running(data_dir, legacy)
            self.assertEqual(self.get(restarted, '/api/state'), saved_desktop, '目标存在时不能再次迁移覆盖')
        finally:
            source.close()

    def test_same_data_directory_rejects_second_helper_and_first_keeps_serving(self):
        data_dir = self.root / 'shared'
        first, _, ready = self.running(data_dir)
        second, _, error = self.launch(data_dir)
        self.assertEqual(error['status'], 'error')
        self.assertTrue(error.get('message'))
        second.wait(timeout=8)
        self.assertNotEqual(second.returncode, 0)
        self.assertIsNone(first.poll())
        self.post(ready, 'stock.record', {'materialId': RED, 'type': 'receipt', 'quantity': 0, 'reason': '原服务仍可写'}, 'still-running')
        self.assertEqual(self.get(ready, '/api/state')['summary']['notCountedKinds'], 15)

    def test_different_data_directories_are_isolated(self):
        _, _, left = self.running(self.root / 'left')
        _, _, right = self.running(self.root / 'right')
        self.assertNotEqual(left['port'], right['port'])
        self.post(left, 'stock.record', {'materialId': RED, 'type': 'receipt', 'quantity': 1, 'reason': '仅左边'}, 'same-id')
        self.assertEqual(self.get(left, '/api/state')['summary']['notCountedKinds'], 15)
        self.assertEqual(self.get(right, '/api/state')['summary']['notCountedKinds'], 16)
        self.assertEqual(self.get(right, '/api/state')['data']['stockMovements'], [])

    def test_corrupt_legacy_reports_error_without_creating_target_inventory(self):
        source = self.root / 'bad-legacy.sqlite3'
        source.write_bytes(b'not a SQLite database; preserve these bytes')
        before = source.read_bytes()
        data_dir = self.root / 'no-target'
        process, _, ready = self.launch(data_dir, source)
        self.assertEqual(ready['status'], 'error')
        self.assertTrue(ready.get('message'))
        process.wait(timeout=8)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(source.read_bytes(), before)
        self.assertFalse((data_dir / 'warehouse.sqlite3').exists(), '坏旧库不能悄悄变成seed新账本')

    def test_existing_corrupt_or_unrelated_target_is_never_reinitialized(self):
        for kind in ['garbage', 'unrelated-schema', 'empty-file']:
            with self.subTest(kind=kind):
                data_dir = self.root / kind
                data_dir.mkdir()
                target = data_dir / 'warehouse.sqlite3'
                if kind == 'unrelated-schema':
                    connection = sqlite3.connect(target)
                    connection.execute('CREATE TABLE user_data (value TEXT)')
                    connection.execute("INSERT INTO user_data VALUES ('必须保留')")
                    connection.commit()
                    connection.close()
                else:
                    target.write_bytes(b'bad existing database' if kind == 'garbage' else b'')
                before = target.read_bytes()
                process, _, ready = self.launch(data_dir)
                self.assertEqual(ready['status'], 'error')
                self.assertTrue(ready.get('message'))
                process.wait(timeout=8)
                self.assertNotEqual(process.returncode, 0)
                self.assertEqual(target.read_bytes(), before, '发现旧文件异常时应保留原字节，不能seed覆盖')

    def test_missing_target_with_completed_migration_receipt_does_not_seed_again(self):
        legacy = self.root / 'receipt-source.sqlite3'
        source = Warehouse(legacy, SEED)
        source.close()
        data_dir = self.root / 'migrated-then-missing'
        process, _, _ = self.running(data_dir, legacy)
        self.stop(process)
        target = data_dir / 'warehouse.sqlite3'
        target.rename(data_dir / 'warehouse-kept-aside.sqlite3')
        old_receipt = (data_dir / 'desktop-migration.json').read_bytes()
        failed, _, ready = self.launch(data_dir, legacy)
        self.assertEqual(ready['status'], 'error')
        failed.wait(timeout=8)
        self.assertNotEqual(failed.returncode, 0)
        self.assertFalse(target.exists(), '迁移完成后目标丢失，应提示恢复而不是重播旧账或seed')
        self.assertEqual((data_dir / 'desktop-migration.json').read_bytes(), old_receipt)
        self.assertTrue((data_dir / 'warehouse-kept-aside.sqlite3').exists())

    def test_parent_exit_stops_helper_cleans_ready_and_releases_lock(self):
        parent = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.processes.append(parent)
        data_dir = self.root / 'parent-watched'
        child, ready_file, ready = self.running(data_dir, parent_pid=parent.pid)
        self.stop(parent)
        child.wait(timeout=10)
        self.assertEqual(child.returncode, 0)
        self.assertFalse(ready_file.exists())
        with self.assertRaises((URLError, OSError)):
            self.get(ready, '/api/health')
        self.running(data_dir)  # 看护退出也必须释放独占锁。


if __name__ == '__main__':
    unittest.main(verbosity=2)
