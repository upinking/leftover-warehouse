#!/usr/bin/env python3
"""Mac 应用内部的本地服务入口。

界面由原生窗口显示，账本放在应用数据目录。第一次打开时可承接旧版账本；
迁移只读旧数据库，用 SQLite 自己的备份机制取得一致快照，不碰采购和库存口径。
"""
import argparse
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import threading
import uuid

import app
from warehouse import DomainError, Warehouse, calculate, now, validate_state


class StartupError(Exception):
    pass


def _load_json(value):
    return json.loads(value, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("非法数字")))


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            os.chmod(str(temporary), 0o600)
            json.dump(value, handle, ensure_ascii=False, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_only_connection(path):
    # mode=ro prevents updates to the original SQLite database. Do not use immutable=1:
    # that would ignore committed changes still present in an active WAL file.
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=15)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _validate_connection(connection):
    integrity = connection.execute("PRAGMA quick_check").fetchall()
    if integrity != [("ok",)]:
        raise StartupError("数据库完整性检查未通过，请保留原文件并从已知备份恢复。")
    rows = connection.execute("SELECT id, data FROM ledger").fetchall()
    if len(rows) != 1 or rows[0][0] != 1:
        raise StartupError("数据库没有唯一有效的仓库账本；不会覆盖或重新初始化。")
    data = _load_json(rows[0][1])
    validate_state(data)
    # Requests are part of the ledger: losing them could turn a retry into another issue.
    for request_id, digest, result in connection.execute("SELECT id, hash, result FROM requests"):
        if not isinstance(request_id, str) or not request_id or not isinstance(digest, str):
            raise StartupError("数据库中的防重复请求记录无效。")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise StartupError("数据库中的请求校验记录无效。")
        if not isinstance(_load_json(result), dict):
            raise StartupError("数据库中的请求回执无效。")
    return calculate(data)["summary"]


def validate_database(path):
    """在创建 Warehouse 之前只读检查，坏库绝不能被当成新库播种。"""
    connection = _read_only_connection(path)
    try:
        return _validate_connection(connection)
    finally:
        connection.close()


def _snapshot_database(source_path, destination_path):
    source = _read_only_connection(source_path)
    destination = None
    try:
        # An explicit read transaction holds one consistent source snapshot throughout
        # validation and backup, including committed pages in the old server's WAL.
        source.execute("BEGIN")
        summary = _validate_connection(source)
        destination = sqlite3.connect(str(destination_path), timeout=15)
        source.backup(destination)
        destination.execute("PRAGMA journal_mode=DELETE")
        destination.commit()
        _validate_connection(destination)
        destination.close()
        destination = None
        with Path(destination_path).open("rb") as handle:
            os.fsync(handle.fileno())
        return summary
    finally:
        if destination is not None:
            destination.close()
        source.close()


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def migrate_legacy(source_path, database_path, data_dir):
    """Only run for a missing target, preserving an independently readable safety copy."""
    source_path = Path(source_path).expanduser().resolve()
    database_path = Path(database_path)
    if database_path.exists():
        return None
    if not source_path.exists():
        return None
    if not source_path.is_file():
        raise StartupError("旧版数据库路径不是文件，未执行迁移。")
    backups = Path(data_dir) / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    migration_id = uuid.uuid4().hex
    stamp = now().replace(":", "").replace("+", "-")
    safety_path = backups / ("before-desktop-migration-" + stamp + "-" + migration_id[:8] + ".sqlite3")
    staged_path = database_path.with_name(".migration-" + migration_id + ".sqlite3")
    receipt_path = Path(data_dir) / "desktop-migration.json"
    if receipt_path.exists():
        # A missing target alongside a completed receipt is unexpected. Never silently
        # replace a user's missing/newer ledger by reimporting an older source again.
        receipt = _load_json(receipt_path.read_text(encoding="utf-8"))
        if isinstance(receipt, dict) and receipt.get("status") == "complete":
            raise StartupError("检测到已完成的迁移记录，但桌面账本缺失。请从备份恢复，不能重复迁移旧账。")
    try:
        summary = _snapshot_database(source_path, safety_path)
        os.chmod(str(safety_path), 0o600)
        # Copy through SQLite again so both the safety file and new target are standalone
        # consistent databases; neither relies on a WAL file from the source directory.
        _snapshot_database(safety_path, staged_path)
        os.chmod(str(staged_path), 0o600)
        receipt = {"version": 1, "migrationId": migration_id, "status": "prepared", "at": now(),
                   "source": str(source_path), "database": str(database_path.resolve()),
                   "safetyBackup": str(safety_path.resolve()), "sha256": _sha256(safety_path), "summary": summary}
        _atomic_json(receipt_path, receipt)
        # Publishing occurs only after the safety copy and receipt have reached disk.
        # Hard-link creation fails atomically if a target appeared while we prepared.
        os.link(str(staged_path), str(database_path))
        staged_path.unlink()
        receipt["status"] = "complete"
        _atomic_json(receipt_path, receipt)
        return str(source_path)
    finally:
        if staged_path.exists():
            staged_path.unlink()


def _parent_exists(pid):
    if pid is None:
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError as error:
        return error.errno == errno.EPERM


def _acquire_lock(data_dir):
    path = Path(data_dir) / "desktop.lock"
    descriptor = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.ftruncate(descriptor, 0)
        os.write(descriptor, (str(os.getpid()) + "\n").encode("ascii"))
        return descriptor
    except BlockingIOError:
        os.close(descriptor)
        raise StartupError("余料仓库的桌面服务已在运行，请切换到现有窗口。")
    except BaseException:
        os.close(descriptor)
        raise


class DesktopServer(app.LocalServer):
    def get_request(self):
        client, address = super().get_request()
        client.settimeout(2)
        return client, address


def main(argv=None):
    parser = argparse.ArgumentParser(description="余料仓库 Mac 内部服务")
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--legacy-db", type=Path)
    parser.add_argument("--ready-file", required=True, type=Path)
    parser.add_argument("--parent-pid", type=int)
    args = parser.parse_args(argv)
    data_dir = args.data_dir.expanduser().resolve()
    ready_file = args.ready_file.expanduser().resolve()
    database = data_dir / "warehouse.sqlite3"
    stop = threading.Event()
    store, server, descriptor, became_ready = None, None, None, False

    def request_stop(signum=None, frame=None):
        stop.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, request_stop)
    try:
        if ready_file == database or (args.legacy_db and ready_file == args.legacy_db.expanduser().resolve()):
            raise StartupError("服务状态文件不能覆盖仓库数据库。")
        if args.parent_pid is not None and args.parent_pid <= 1:
            raise StartupError("原生应用进程标识无效。")
        if not _parent_exists(args.parent_pid):
            raise StartupError("原生应用已经退出，内部服务不再启动。")
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = _acquire_lock(data_dir)
        migrated_from = None
        if database.exists():
            validate_database(database)
        elif args.legacy_db:
            migrated_from = migrate_legacy(args.legacy_db, database, data_dir)
        if stop.is_set():
            return 0
        if not database.exists() and (data_dir / "desktop-migration.json").exists():
            raise StartupError("存在旧版迁移记录但桌面账本缺失，请使用迁移安全备份恢复。")
        store = Warehouse(database, app.ROOT / "data" / "seed.json")
        validate_database(database)
        server = DesktopServer(("127.0.0.1", 0), app.make_handler(store))
        server.timeout = 0.25
        if args.parent_pid is not None:
            def watch_parent():
                while not stop.wait(0.5):
                    if not _parent_exists(args.parent_pid):
                        stop.set()
                        break
            threading.Thread(target=watch_parent, name="native-parent-watch", daemon=True).start()
        if stop.is_set() or not _parent_exists(args.parent_pid):
            return 0
        ready = {"status": "ready", "port": server.server_port, "database": str(database),
                 "dataDir": str(data_dir), "pid": os.getpid()}
        if migrated_from:
            ready["migratedFrom"] = migrated_from
        _atomic_json(ready_file, ready)
        became_ready = True
        print("余料仓库桌面服务已启动，本地端口 %s" % server.server_port, flush=True)
        # handle_request + short timeout keeps signals on the main thread responsive.
        # Calling HTTPServer.shutdown() from the serving thread would deadlock.
        while not stop.is_set():
            server.handle_request()
        return 0
    except Exception as error:
        if isinstance(error, (StartupError, DomainError)):
            message = str(error)
        elif isinstance(error, (sqlite3.Error, ValueError, KeyError, TypeError)):
            message = "账本读取或校验失败；原文件已保留，不会重新初始化。详情：" + str(error)
        else:
            message = "桌面服务启动失败：" + str(error)
        print(message, file=sys.stderr, flush=True)
        # Leave startup errors for the native launcher to read after process termination.
        # Successful ready files are removed on every normal or signalled shutdown.
        if ready_file != database and not (args.legacy_db and ready_file == args.legacy_db.expanduser().resolve()):
            try:
                _atomic_json(ready_file, {"status": "error", "message": message, "pid": os.getpid()})
            except OSError:
                pass
        return 1
    finally:
        stop.set()
        if server is not None:
            server.server_close()
        if store is not None:
            store.close()
        if became_ready:
            try:
                ready_file.unlink()
            except FileNotFoundError:
                pass
        if descriptor is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


if __name__ == "__main__":
    sys.exit(main())
