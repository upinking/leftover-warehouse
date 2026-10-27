"""余料仓库的业务层。

采购是预计到货；只有点数、收货才确认实物。SQLite 事务把一次操作的
库存、项目状态和请求回执一起保存，因此重试不会再次扣料。
"""
import copy
import hashlib
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

MAX_QUANTITY = 1_000_000_000


def now():
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def uid(prefix):
    return prefix + "-" + uuid.uuid4().hex


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class DomainError(Exception):
    def __init__(self, message, details=None, status=400):
        super().__init__(message)
        self.message, self.details, self.status = message, details, status


def require(condition, message, details=None):
    if not condition:
        raise DomainError(message, details)


def integer(value, label, minimum=0, maximum=MAX_QUANTITY):
    require(type(value) is int and minimum <= value <= maximum,
            label + "必须是" + ("正" if minimum == 1 else "非负") + "整数")
    return value


def string(value, label, required=False, nullable=False):
    if value is None and nullable:
        return value
    require(isinstance(value, str) and len(value) <= 5000, label + "必须是文本")
    require(not required or bool(value.strip()), label + "不能为空")
    return value


def obj(value, label):
    require(isinstance(value, dict), label + "必须是对象")
    return value


def array(value, label):
    require(isinstance(value, list) and len(value) <= 100_000, label + "必须是列表且不能过大")
    return value


def unique_id(row, seen, label):
    obj(row, label)
    value = string(row.get("id"), label + " ID", True)
    require(value not in seen, label + " ID 重复：" + value)
    seen.add(value)
    return value


def empty_state():
    return {"schemaVersion": 1, "importId": None, "asOfDate": None,
            "timezone": "Asia/Shanghai", "materials": [], "purchaseBatches": [],
            "projects": [], "openingStock": [], "stockMovements": [],
            "auditLog": [], "revision": 0}


def prepare_seed(seed):
    obj(seed, "初始化数据")
    state = copy.deepcopy(seed)
    state.setdefault("auditLog", [])
    state.setdefault("revision", 0)
    validate_state(state)
    require(bool(state.get("importId")), "初始化数据缺少 importId")
    require(not state["stockMovements"], "初始化数据只接受无实物流水的起点；有历史请使用备份恢复")
    require(all(p["issuedUnits"] == 0 for p in state["projects"]), "初始化数据不能包含无流水的已领料套数")
    return state


def validate_bom(bom, material_ids):
    seen = set()
    for line in array(bom, "BOM"):
        obj(line, "BOM 行")
        mid = string(line.get("materialId"), "BOM 元件 ID", True)
        require(mid in material_ids, "BOM 引用了不存在的元件", {"materialId": mid})
        require(mid not in seen, "同一 BOM 不得重复列入同一个元件，请合并每板用量")
        seen.add(mid)
        integer(line.get("quantityPerUnit"), "每板用量", 1)
        string(line.get("designatorsText", ""), "位号")


def validate_state(data):
    """验证完整恢复数据；任何一处错误都不替换现有账本。"""
    try:
        return _validate_state(data)
    except DomainError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise DomainError("账本结构无效，请检查字段类型和必填记录。") from exc


def _validate_state(data):
    obj(data, "账本")
    require(type(data.get("schemaVersion")) is int and data["schemaVersion"] == 1, "不支持的账本版本")
    string(data.get("importId"), "导入标识", nullable=True)
    string(data.get("asOfDate"), "快照日期", nullable=True)
    string(data.get("timezone"), "时区", True)
    integer(data.get("revision", 0), "数据版本")
    material_ids, identity = set(), set()
    for m in array(data.get("materials"), "物料"):
        unique_id(m, material_ids, "物料")
        for key in ("name", "category", "manufacturerPartNumber", "supplier", "supplierPartNumber", "specification", "package", "unit"):
            string(m.get(key, ""), "物料 " + key, key == "name")
        for key in ("notes", "locationCode"):
            if key in m:
                string(m[key], key, nullable=True)
        if m.get("supplierPartNumber"):
            ident = (m.get("supplier", "").strip().casefold(), m["supplierPartNumber"].strip().casefold())
            require(ident not in identity, "供应商与料号组合重复")
            identity.add(ident)
    purchase_ids = set()
    for p in array(data.get("purchaseBatches"), "采购"):
        unique_id(p, purchase_ids, "采购")
        for key in ("externalOrderId", "orderedAt"):
            string(p.get(key), "采购 " + key, nullable=True)
        string(p.get("status"), "采购状态", True)
        string(p.get("currency", "CNY"), "币种")
        total_paid = p.get("totalPaid")
        require(total_paid is None or (type(total_paid) in (int, float) and 0 <= total_paid <= MAX_QUANTITY), "采购金额无效")
        for line in array(p.get("lines"), "采购明细"):
            obj(line, "采购明细")
            require(string(line.get("materialId"), "采购元件 ID", True) in material_ids, "采购明细引用了不存在的元件")
            integer(line.get("orderedQuantity"), "采购数量")
            string(line.get("quantityEvidence", ""), "数量依据")
            require(type(line.get("finalOrderVerified", False)) is bool, "订单核实状态必须是布尔值")
    project_ids, projects = set(), {}
    for p in array(data.get("projects"), "项目"):
        pid = unique_id(p, project_ids, "项目")
        projects[pid] = p
        string(p.get("name"), "项目名称", True)
        integer(p.get("plannedUnits"), "计划套数")
        integer(p.get("issuedUnits"), "已领套数")
        require(p["plannedUnits"] >= p["issuedUnits"], "计划套数不能低于已领套数")
        integer(p.get("bomVersion"), "BOM 版本", 1)
        validate_bom(p.get("bom"), material_ids)
        require(bool(p["bom"]) or p["plannedUnits"] == 0, "计划制作的项目必须有 BOM")
    opening_ids = set()
    for row in array(data.get("openingStock"), "期初库存"):
        obj(row, "期初库存")
        mid = string(row.get("materialId"), "期初库存元件 ID", True)
        require(mid in material_ids and mid not in opening_ids, "期初库存引用无效或重复")
        opening_ids.add(mid)
        r = row.get("receivedCumulative")
        if r is not None:
            integer(r, "期初实收")
        integer(row.get("otherIssuedCumulative"), "期初零散领用")
        integer(row.get("scrappedCumulative"), "期初损耗")
        string(row.get("locationCode"), "库位", nullable=True)
        string(row.get("countedAt"), "点数时间", nullable=True)
        if r is not None:
            require(r >= row["otherIssuedCumulative"] + row["scrappedCumulative"], "期初实物余额不能为负")
    require(opening_ids == material_ids, "每种物料必须且只能有一条期初库存记录")
    movement_ids, issued = set(), {pid: 0 for pid in project_ids}
    for mv in array(data.get("stockMovements"), "库存流水"):
        unique_id(mv, movement_ids, "流水")
        kind = mv.get("type")
        require(kind in ("receipt", "issue", "return", "damage", "loss", "count", "project_issue"), "未知流水类型")
        for key in ("occurredAt", "actor", "reason"):
            string(mv.get(key), "流水 " + key, True)
        if kind == "project_issue":
            pid = string(mv.get("projectId"), "流水项目 ID", True)
            require(pid in project_ids, "流水引用不存在的项目")
            units = integer(mv.get("units"), "领料套数", 1)
            integer(mv.get("bomVersion"), "历史 BOM 版本", 1)
            require(mv["bomVersion"] <= projects[pid]["bomVersion"], "历史 BOM 版本不能高于当前版本")
            validate_bom(mv.get("bomSnapshot"), material_ids)
            require(bool(mv["bomSnapshot"]), "历史领料 BOM 不能为空")
            expected = {line["materialId"]: line["quantityPerUnit"] * units for line in mv["bomSnapshot"]}
            lines = array(mv.get("lines"), "领料行")
            actual = {}
            for line in lines:
                obj(line, "领料行")
                mid = string(line.get("materialId"), "领料元件 ID", True)
                require(mid in expected and mid not in actual, "领料行引用不一致或重复")
                qty = integer(line.get("quantity"), "领料数量", 1)
                require(type(line.get("delta")) is int and line["delta"] == -qty, "领料变化量与数量不符")
                actual[mid] = qty
            require(actual == expected, "领料行必须与历史 BOM 快照完全一致")
            issued[pid] += units
        else:
            require(string(mv.get("materialId"), "流水元件 ID", True) in material_ids, "流水引用不存在的元件")
            quantity = integer(mv.get("quantity"), "流水数量", 0 if kind in ("receipt", "count") else 1)
            delta = mv.get("delta")
            require(type(delta) is int and abs(delta) <= MAX_QUANTITY, "流水变化量无效")
            if kind != "count":
                require(delta == (quantity if kind in ("receipt", "return") else -quantity), "流水变化量与数量不符")
            for key in ("balanceBefore", "balanceAfter"):
                if mv.get(key) is not None:
                    integer(mv[key], "流水余额")
            if kind == "count":
                require(mv.get("balanceAfter") == quantity, "盘点余额与数量不符")
                expected_delta = 0 if mv.get("balanceBefore") is None else quantity - mv["balanceBefore"]
                require(delta == expected_delta, "盘点调整量无效")
    for pid, p in projects.items():
        require(issued[pid] == p["issuedUnits"], "项目已领套数与历史流水不一致")
    audit_ids = set()
    for entry in array(data.get("auditLog", []), "操作历史"):
        unique_id(entry, audit_ids, "操作记录")
        for key in ("at", "actor", "action"):
            string(entry.get(key), "操作记录 " + key, True)
        obj(entry.get("detail"), "操作详情")
    # Replay also detects negative or unknown outgoing balances and incoherent snapshots.
    calculate(data, validate_replay=True)
    return data


def calculate(data, validate_replay=False):
    materials = {m["id"]: m for m in data["materials"]}
    stocks = {}
    for opening in data["openingStock"]:
        mid = opening["materialId"]
        consumed = opening["otherIssuedCumulative"] + opening["scrappedCumulative"]
        received = opening["receivedCumulative"]
        stocks[mid] = {"materialId": mid, "ordered": 0, "received": received,
                       "reserved": 0, "netConsumption": consumed,
                       "actualOnHand": None if received is None else received - consumed,
                       "locationCode": opening.get("locationCode"), "issuedPieces": 0,
                       "reservedByProject": {}, "affectedProjects": []}
    for batch in data["purchaseBatches"]:
        for line in batch["lines"]:
            stocks[line["materialId"]]["ordered"] += line["orderedQuantity"]
    for mv in data["stockMovements"]:
        if mv["type"] == "project_issue":
            for line in mv["lines"]:
                s = stocks[line["materialId"]]
                if validate_replay:
                    require(s["actualOnHand"] is not None and s["actualOnHand"] >= line["quantity"], "历史整板领料时余额未知或不足")
                if s["actualOnHand"] is not None:
                    s["actualOnHand"] += line["delta"]
                s["netConsumption"] -= line["delta"]
                s["issuedPieces"] += line["quantity"]
            continue
        s = stocks[mv["materialId"]]
        before = s["actualOnHand"]
        kind = mv["type"]
        if validate_replay:
            require(mv.get("balanceBefore") == before, "流水中的操作前余额与历史不一致")
        if kind == "receipt":
            s["received"] = (s["received"] or 0) + mv["quantity"]
            s["actualOnHand"] = (before or 0) + mv["quantity"]
        elif kind == "count":
            s["actualOnHand"] = mv["quantity"]
            s["netConsumption"] -= mv["delta"]
        else:
            if validate_replay:
                require(before is not None, "未点数时不能存在领用、归还或损耗流水")
            if before is not None:
                s["actualOnHand"] += mv["delta"]
            s["netConsumption"] -= mv["delta"]
        if validate_replay:
            require(s["actualOnHand"] is not None and s["actualOnHand"] >= 0, "历史流水产生了负库存")
            require(mv.get("balanceAfter") == s["actualOnHand"], "流水中的操作后余额与历史不一致")
    for p in data["projects"]:
        pending = p["plannedUnits"] - p["issuedUnits"]
        for line in p["bom"]:
            s = stocks[line["materialId"]]
            qty = pending * line["quantityPerUnit"]
            s["reserved"] += qty
            s["reservedByProject"][p["id"]] = qty
            if qty:
                s["affectedProjects"].append({"projectId": p["id"], "name": p["name"], "reserved": qty})
    for s in stocks.values():
        s["forecastBalance"] = s["ordered"] - s.pop("netConsumption")
        s["forecastFree"] = s["forecastBalance"] - s["reserved"]
        s["actualFree"] = None if s["actualOnHand"] is None else s["actualOnHand"] - s["reserved"]
        s["shortage"] = max(0, -s["forecastFree"])
        s["notCounted"] = s["actualOnHand"] is None
    capacities = []
    for p in data["projects"]:
        limits = []
        for line in p["bom"]:
            s = stocks[line["materialId"]]
            other = s["reserved"] - s["reservedByProject"].get(p["id"], 0)
            available = s["forecastBalance"] - other
            actual = None if s["actualOnHand"] is None else s["actualOnHand"] - other
            limits.append({"materialId": line["materialId"], "availableForecast": available,
                           "availableActual": actual, "quantityPerUnit": line["quantityPerUnit"],
                           "wholeUnits": max(0, available // line["quantityPerUnit"]),
                           "actualWholeUnits": None if actual is None else max(0, actual // line["quantityPerUnit"])})
        additional = min((line["wholeUnits"] for line in limits), default=0)
        actual_known = bool(limits) and all(line["availableActual"] is not None for line in limits)
        actual_additional = min((line["actualWholeUnits"] for line in limits), default=0) if actual_known else None
        capacities.append({"projectId": p["id"], "forecastWholeUnits": p["issuedUnits"] + additional,
                           "actualWholeUnits": None if actual_additional is None else p["issuedUnits"] + actual_additional,
                           "forecastAdditionalUnits": additional, "actualAdditionalUnits": actual_additional,
                           "limits": limits, "bottleneckMaterialIds": [line["materialId"] for line in limits if line["wholeUnits"] == additional],
                           "otherProjectNames": [other["name"] for other in data["projects"] if other["id"] != p["id"] and other["plannedUnits"] > other["issuedUnits"]]})
    values = list(stocks.values())
    unknown = sum(s["actualOnHand"] is None for s in values)
    summary = {"materialKinds": len(materials), "orderedPieces": sum(s["ordered"] for s in values),
               "reservedPieces": sum(s["reserved"] for s in values),
               "forecastFreePieces": sum(s["forecastFree"] for s in values),
               "actualOnHandPieces": None if unknown else sum(s["actualOnHand"] for s in values),
               "notCountedKinds": unknown, "issuedPieces": sum(s["issuedPieces"] for s in values),
               "projectKinds": len(data["projects"]), "shortageKinds": sum(s["shortage"] > 0 for s in values)}
    return {"data": copy.deepcopy(data), "summary": summary, "stocks": values, "capacities": capacities}


class Warehouse:
    def __init__(self, db_path, seed_path=None):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS ledger (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, hash TEXT NOT NULL, result TEXT NOT NULL)")
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                if not self.db.execute("SELECT 1 FROM ledger WHERE id=1").fetchone():
                    state = empty_state() if seed_path is None else prepare_seed(json.loads(Path(seed_path).read_text(encoding="utf-8")))
                    self.db.execute("INSERT INTO ledger VALUES (1, ?)", (encoded(state),))
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def close(self):
        with self._lock:
            self.db.close()

    def _read(self):
        return json.loads(self.db.execute("SELECT data FROM ledger WHERE id=1").fetchone()[0])

    def snapshot(self):
        with self._lock:
            return calculate(self._read())

    def backup(self):
        with self._lock:
            self.db.execute("BEGIN")
            try:
                result = {"format": "leftover-warehouse-backup", "version": 1, "exportedAt": now(), "data": self._read(),
                          "requests": [{"id": row[0], "hash": row[1], "result": json.loads(row[2])}
                                       for row in self.db.execute("SELECT id, hash, result FROM requests ORDER BY id")]}
                self.db.execute("COMMIT")
                return result
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def preview_backup(self, backup):
        obj(backup, "备份")
        require(backup.get("format") == "leftover-warehouse-backup" and type(backup.get("version")) is int and backup["version"] == 1, "备份格式或版本不支持")
        string(backup.get("exportedAt"), "备份时间", True)
        validate_state(backup.get("data"))
        seen = set()
        for request in array(backup.get("requests"), "请求回执"):
            unique_id(request, seen, "请求回执")
            digest = string(request.get("hash"), "请求摘要", True)
            require(len(digest) == 64 and all(c in "0123456789abcdef" for c in digest), "请求摘要无效")
            obj(request.get("result"), "请求结果")
        snapshot = calculate(backup["data"])
        return {"message": "校验通过。恢复将替换整个当前账本，不会合并。", "summary": snapshot["summary"],
                "materialKinds": len(backup["data"]["materials"]), "projectKinds": len(backup["data"]["projects"]),
                "movementCount": len(backup["data"]["stockMovements"]), "exportedAt": backup["exportedAt"],
                "importId": backup["data"].get("importId"), "mode": "replace"}

    def execute(self, action, payload, request_id):
        string(action, "操作", True)
        obj(payload, "操作参数")
        string(request_id, "请求标识", True)
        require(len(request_id) <= 200, "请求标识过长")
        digest = hashlib.sha256(encoded({"action": action, "payload": payload}).encode("utf-8")).hexdigest()
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                prior = self.db.execute("SELECT hash, result FROM requests WHERE id=?", (request_id,)).fetchone()
                if prior:
                    require(prior[0] == digest, "同一个请求标识对应了不同内容，请刷新后重新操作")
                    result = json.loads(prior[1])
                    self.db.execute("COMMIT")
                    return result
                state = self._read()
                if action == "backup.restore":
                    self.preview_backup(payload.get("backup"))
                    # Keep a complete safety copy before replacing, inside the same transaction.
                    self.db.execute("CREATE TABLE IF NOT EXISTS recovery_snapshots (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, data TEXT NOT NULL, requests TEXT NOT NULL)")
                    old_requests = list(self.db.execute("SELECT id, hash, result FROM requests"))
                    safety_backup = {"format": "leftover-warehouse-backup", "version": 1, "exportedAt": now(),
                                     "data": state, "requests": [{"id": row[0], "hash": row[1], "result": json.loads(row[2])} for row in old_requests]}
                    safety_path = None
                    if self.db_path != ":memory:":
                        backup_dir = Path(self.db_path).resolve().parent / "backups"
                        backup_dir.mkdir(parents=True, exist_ok=True)
                        safety_path = backup_dir / ("pre-restore-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8] + ".json")
                        # Exclusive create prevents accidentally overwriting an earlier safety copy.
                        with safety_path.open("x", encoding="utf-8") as handle:
                            handle.write(encoded(safety_backup))
                            handle.flush()
                            os.fsync(handle.fileno())
                    self.db.execute("INSERT INTO recovery_snapshots VALUES (?, ?, ?, ?)", (uid("recovery"), now(), encoded(state), encoded(old_requests)))
                    backup = payload["backup"]
                    state = copy.deepcopy(backup["data"])
                    self.db.execute("DELETE FROM requests")
                    for entry in backup["requests"]:
                        self.db.execute("INSERT INTO requests VALUES (?, ?, ?)", (entry["id"], entry["hash"], encoded(entry["result"])))
                    require(not self.db.execute("SELECT 1 FROM requests WHERE id=?", (request_id,)).fetchone(), "恢复请求标识与备份中的历史请求冲突，请重新操作")
                    result = {"message": "备份已恢复，当前账本已整体替换；恢复前账本已留本地安全快照。", "safetyBackupPath": str(safety_path) if safety_path else None}
                elif action == "seed.import":
                    seed = prepare_seed(payload.get("seed"))
                    if state.get("importId") == seed["importId"]:
                        result = {"message": "这批初始化数据已经导入，已安全跳过。", "skipped": True}
                    else:
                        require(not state["materials"] and not state["purchaseBatches"] and not state["stockMovements"], "已有账本不能叠加初始化数据，请使用新增元件/采购或备份恢复")
                        state = seed
                        result = {"message": "初始化数据已导入。采购记录未计入实物库存。"}
                else:
                    result = self._mutate(state, action, payload)
                if action not in ("seed.import", "backup.restore") or not result.get("skipped"):
                    state["revision"] = state.get("revision", 0) + 1
                    state.setdefault("auditLog", []).append({"id": uid("audit"), "at": now(), "actor": "本地用户", "action": action,
                                                             "detail": {"summary": result["message"], "payload": copy.deepcopy(payload) if action not in ("backup.restore", "seed.import") else {"importId": state.get("importId")}}})
                    validate_state(state)
                    self.db.execute("UPDATE ledger SET data=? WHERE id=1", (encoded(state),))
                self.db.execute("INSERT INTO requests VALUES (?, ?, ?)", (request_id, digest, encoded(result)))
                self.db.execute("COMMIT")
                return result
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def execute_batch(self, actions, request_id, expected_revision=None,
                      expected_fingerprint=None, actor="AI助手（用户确认）"):
        """确认一张 AI 操作单。全部成功才保存，重试仍只记一次。"""
        allowed = {"stock.record", "purchase.add", "material.save", "project.save", "project.issue"}
        require(isinstance(actions, list) and 0 < len(actions) <= 30, "一次操作需要 1 到 30 项明细")
        for item in actions:
            require(isinstance(item, dict) and isinstance(item.get("action"), str) and item["action"] in allowed
                    and isinstance(item.get("payload"), dict), "AI 提供了不支持的操作")
        string(request_id, "请求标识", True)
        require(len(request_id) <= 200, "请求标识过长")
        digest = hashlib.sha256(encoded({"actions": actions, "revision": expected_revision,
                                        "fingerprint": expected_fingerprint}).encode()).hexdigest()
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                prior = self.db.execute("SELECT hash,result FROM requests WHERE id=?", (request_id,)).fetchone()
                if prior:
                    require(prior[0] == digest, "请求标识对应了不同的操作明细")
                    self.db.execute("COMMIT")
                    return json.loads(prior[1])
                state = self._read()
                if expected_revision is not None:
                    require(type(expected_revision) is int and state["revision"] == expected_revision,
                            "账本已经变化，请让 AI 重新核对操作明细")
                if expected_fingerprint is not None:
                    require(hashlib.sha256(encoded(state).encode()).hexdigest() == expected_fingerprint,
                            "账本已经变化，请让 AI 重新核对操作明细")
                results = []
                for item in actions:
                    start = len(state["stockMovements"])
                    result = self._mutate(state, item["action"], copy.deepcopy(item["payload"]))
                    for movement in state["stockMovements"][start:]:
                        movement["actor"] = actor
                    state["revision"] += 1
                    state["auditLog"].append({"id": uid("audit"), "at": now(), "actor": actor,
                        "action": item["action"], "detail": {"summary": result["message"],
                        "payload": copy.deepcopy(item["payload"]), "batchRequestId": request_id}})
                    validate_state(state)
                    results.append(result)
                result = {"message": "已确认并记账：" + " ".join(r["message"] for r in results), "results": results}
                self.db.execute("UPDATE ledger SET data=? WHERE id=1", (encoded(state),))
                self.db.execute("INSERT INTO requests VALUES (?,?,?)", (request_id, digest, encoded(result)))
                self.db.execute("COMMIT")
                return result
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def preview_batch(self, actions):
        """在内存中的账本副本上试算，正式账本完全不写入。"""
        trial = Warehouse(":memory:")
        try:
            with self._lock:
                before = self.snapshot()
                self.db.backup(trial.db)
            trial.execute_batch(actions, uid("preview"))
            after = trial.snapshot()
            old = {s["materialId"]: s for s in before["stocks"]}
            fields = ("ordered", "reserved", "forecastFree", "actualOnHand", "locationCode")
            changes = []
            for m in after["data"]["materials"]:
                new = next(s for s in after["stocks"] if s["materialId"] == m["id"])
                a = {k: old.get(m["id"], {}).get(k) for k in fields}
                b = {k: new[k] for k in fields}
                if a != b or m not in before["data"]["materials"]:
                    changes.append({"materialId": m["id"], "name": m["name"],
                        "supplierPartNumber": m.get("supplierPartNumber", ""), "package": m.get("package", ""),
                        "before": a, "after": b})
            previous = {p["id"]: p for p in before["data"]["projects"]}
            project_changes = [{"name": p["name"], "before": previous.get(p["id"]), "after": p}
                               for p in after["data"]["projects"] if previous.get(p["id"]) != p]
            labels = {"stock.record": "库存登记", "purchase.add": "采购登记", "material.save": "元件资料",
                      "project.save": "项目计划与 BOM", "project.issue": "整板领料"}
            return {"baseRevision": before["data"]["revision"],
                    "baseFingerprint": hashlib.sha256(encoded(before["data"]).encode()).hexdigest(),
                    "summaryBefore": before["summary"], "summaryAfter": after["summary"],
                    "changes": changes, "projectChanges": project_changes,
                    "operations": [{"action": i["action"], "label": labels[i["action"]],
                                    "payload": i["payload"]} for i in actions]}
        finally:
            trial.close()

    def _mutate(self, state, action, payload):
        if action == "material.save":
            mid = payload.get("id") or uid("material")
            existing = next((m for m in state["materials"] if m["id"] == mid), None)
            require(existing is not None or not payload.get("id"), "找不到要修改的元件")
            material = copy.deepcopy(existing or {"id": mid, "unit": "piece"})
            for key in ("name", "category", "manufacturerPartNumber", "supplier", "supplierPartNumber", "specification", "package", "notes"):
                value = payload.get(key, material.get(key, ""))
                material[key] = string(value, key, key == "name").strip()
            if existing:
                existing.update(material)
            else:
                state["materials"].append(material)
                state["openingStock"].append({"materialId": mid, "receivedCumulative": None, "otherIssuedCumulative": 0,
                                               "scrappedCumulative": 0, "locationCode": None, "countedAt": None})
            if "locationCode" in payload:
                location = string(payload["locationCode"], "库位", nullable=True)
                next(row for row in state["openingStock"] if row["materialId"] == mid)["locationCode"] = location
            return {"message": "元件资料已保存。", "materialId": mid}
        if action == "project.save":
            pid = payload.get("id") or uid("project")
            existing = next((p for p in state["projects"] if p["id"] == pid), None)
            require(existing is not None or not payload.get("id"), "找不到要修改的项目")
            project = copy.deepcopy(existing or {"id": pid, "issuedUnits": 0, "bomVersion": 1})
            project["name"] = string(payload.get("name", project.get("name")), "项目名称", True).strip()
            project["plannedUnits"] = integer(payload.get("plannedUnits", project.get("plannedUnits", 0)), "计划套数")
            require(project["plannedUnits"] >= project["issuedUnits"], "计划套数不能低于已领套数；已领元件请登记归还")
            bom = copy.deepcopy(payload.get("bom", project.get("bom", [])))
            validate_bom(bom, {m["id"] for m in state["materials"]})
            require(bool(bom) or project["plannedUnits"] == 0, "计划制作的项目必须有 BOM")
            for line in bom:
                line.setdefault("designatorsText", "")
            if existing and existing["bom"] != bom:
                project["bomVersion"] += 1
            project["bom"] = bom
            project["status"] = "已领齐" if project["plannedUnits"] and project["issuedUnits"] == project["plannedUnits"] else ("已取消" if project["plannedUnits"] == 0 else "待领料")
            if existing:
                existing.update(project)
            else:
                state["projects"].append(project)
            return {"message": "项目计划与 BOM 已保存。预留已重算，历史领料保持原样。", "projectId": pid}
        if action == "purchase.add":
            mid = payload.get("materialId")
            self._material(state, mid)
            quantity = integer(payload.get("quantity"), "采购数量", 1)
            reason = string(payload.get("reason"), "采购说明", True)
            order_id = string(payload.get("externalOrderId"), "订单号", nullable=True)
            state["purchaseBatches"].append({"id": uid("purchase"), "externalOrderId": order_id, "status": "reported_ordered", "orderedAt": now(),
                                             "currency": "CNY", "totalPaid": None, "lines": [{"materialId": mid, "orderedQuantity": quantity,
                                             "quantityEvidence": reason, "finalOrderVerified": False}]})
            return {"message": "采购记录已保存；实物数量没有改变。"}
        if action == "stock.record":
            return self._record_stock(state, payload)
        if action == "project.issue":
            return self._issue_project(state, payload)
        raise DomainError("不支持的操作：" + action)

    @staticmethod
    def _material(state, mid):
        material = next((m for m in state["materials"] if m["id"] == mid), None)
        require(material is not None, "找不到元件")
        return material

    def _record_stock(self, state, payload):
        mid = payload.get("materialId")
        self._material(state, mid)
        kind = payload.get("type")
        require(kind in ("receipt", "issue", "return", "damage", "loss", "count"), "请选择有效的库存操作")
        quantity = integer(payload.get("quantity"), "数量", 0 if kind in ("receipt", "count") else 1)
        reason = string(payload.get("reason"), "操作原因", True).strip()
        s = next(s for s in calculate(state)["stocks"] if s["materialId"] == mid)
        before = s["actualOnHand"]
        if kind not in ("count", "receipt"):
            require(before is not None, "这项元件尚未点数，请先登记收货或盘点")
        delta = quantity if kind in ("receipt", "return") else -quantity
        if kind == "count":
            delta = 0 if before is None else quantity - before
            after = quantity
        else:
            after = (before or 0) + delta
        require(after >= 0, "实物数量不足，不能提交", {"materialId": mid, "available": before, "required": quantity, "shortage": max(0, -after)})
        # Casual issues protect project promises. Losses/count corrections must record reality,
        # so they are allowed but explicitly identify affected projects in the result.
        if kind == "issue":
            require(after >= s["reserved"], "这些元件已有项目预留，零散领用会影响计划；请先调整相关计划", {"projects": s["affectedProjects"], "available": s["actualFree"], "required": quantity})
        mv = {"id": uid("movement"), "type": kind, "materialId": mid, "quantity": quantity, "delta": delta,
              "balanceBefore": before, "balanceAfter": after, "occurredAt": now(), "actor": "本地用户", "reason": reason}
        state["stockMovements"].append(mv)
        opening = next(o for o in state["openingStock"] if o["materialId"] == mid)
        if "locationCode" in payload:
            opening["locationCode"] = string(payload["locationCode"], "库位", nullable=True)
        if kind in ("receipt", "count"):
            opening["countedAt"] = mv["occurredAt"]
        names = {"receipt": "收货", "issue": "领用", "return": "归还", "damage": "焊坏", "loss": "损耗", "count": "盘点"}
        message = names[kind] + "已记账。"
        affected = s["affectedProjects"] if after < s["reserved"] else []
        if affected:
            message += "实物不足以覆盖全部预留，受影响项目：" + "、".join(p["name"] for p in affected) + "。"
        return {"message": message, "movementId": mv["id"], "affectedProjects": affected, "actualOnHand": after}

    def _issue_project(self, state, payload):
        pid = payload.get("projectId")
        p = next((p for p in state["projects"] if p["id"] == pid), None)
        require(p is not None, "找不到项目")
        units = integer(payload.get("units"), "领料套数", 1)
        require(units <= p["plannedUnits"] - p["issuedUnits"], "领料套数超过待领计划；请先增加计划")
        require(bool(p["bom"]), "空 BOM 不能领料")
        stock = {s["materialId"]: s for s in calculate(state)["stocks"]}
        missing, lines = [], []
        for bom in p["bom"]:
            mid = bom["materialId"]
            s, required = stock[mid], bom["quantityPerUnit"] * units
            other = s["reserved"] - s["reservedByProject"].get(pid, 0)
            available = None if s["actualOnHand"] is None else s["actualOnHand"] - other
            if available is None or available < required:
                material = self._material(state, mid)
                missing.append({"materialId": mid, "name": material["name"], "supplierPartNumber": material.get("supplierPartNumber", ""),
                                "required": required, "available": available, "actualOnHand": s["actualOnHand"], "otherReserved": other,
                                "shortage": None if available is None else required - available,
                                "reason": "待点数" if available is None else ("保护其他项目预留后数量不足" if other else "实物不足")})
            lines.append({"materialId": mid, "quantity": required, "delta": -required})
        require(not missing, "整板领料未提交，请先处理全部缺项。", {"missing": missing})
        movement = {"id": uid("movement"), "type": "project_issue", "projectId": pid, "units": units,
                    "bomVersion": p["bomVersion"], "bomSnapshot": copy.deepcopy(p["bom"]), "lines": lines,
                    "occurredAt": now(), "actor": "本地用户", "reason": p["name"] + "整板领料"}
        state["stockMovements"].append(movement)
        p["issuedUnits"] += units
        p["status"] = "已领齐" if p["issuedUnits"] == p["plannedUnits"] else "部分领料"
        return {"message": "已完成 " + str(units) + " 套整板领料，扣除 " + str(sum(line["quantity"] for line in lines)) + " 个元件；对应预留已释放。",
                "movementId": movement["id"], "issuedPieces": sum(line["quantity"] for line in lines)}
