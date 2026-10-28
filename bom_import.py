"""离线 BOM 文件解析与匹配。这里只读文件内容、试算，不写入账本。

CSV 和 XLSX 都被转成带原行号的文本单元格。匹配只依赖明确的立创料号；
没有可靠身份时交给用户选择，数量和位号矛盾也不会替用户猜答案。
"""
import base64
import binascii
import copy
import csv
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import posixpath
import re
import zipfile
import zlib
from xml.etree import ElementTree as ET

from warehouse import DomainError, MAX_QUANTITY, encoded, integer, require, string

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_EXPANDED_BYTES = 32 * 1024 * 1024
MAX_ROWS = 10000
MAX_COLUMNS = 256
MAX_CELLS = 150000
FIELDS = ("supplierPartNumber", "manufacturerPartNumber", "package", "quantityPerUnit", "designatorsText", "name", "specification")
ALIASES = {
    "supplierPartNumber": ("供应商编号", "供应商料号", "立创编号", "立创料号", "嘉立创编号", "嘉立创料号", "lcscpartnumber", "lcscpart", "lcsc", "supplierpartnumber", "supplierpart", "suppliernumber", "c编号", "商城编号"),
    "manufacturerPartNumber": ("制造商编号", "制造商料号", "制造商型号", "厂家型号", "器件型号", "型号", "manufacturerpartnumber", "manufacturerpart", "mpn", "partnumber"),
    "package": ("供应商封装", "制造商封装", "器件封装", "封装", "package", "footprint", "pcbfootprint"),
    "quantityPerUnit": ("每板用量", "每板数量", "数量", "用量", "quantity", "qty", "count"),
    "designatorsText": ("位号", "元件位号", "参考位号", "designator", "designators", "reference", "references", "refdes"),
    "name": ("名称", "元件名称", "器件名称", "name", "comment", "value"),
    "specification": ("规格", "参数", "描述", "specification", "description"),
}
LCSC_SUPPLIERS = {"lcsc", "立创", "嘉立创", "立创商城", "嘉立创商城", "lcsc.com", "lcsc立创商城"}


def _clean(value):
    return str(value).strip() if value is not None else ""


def _normal(value):
    return re.sub(r"[\s_\-:：#（）()]+", "", _clean(value)).casefold()


def _supplier_is_lcsc(value):
    return re.sub(r"\s+", "", _clean(value)).casefold() in LCSC_SUPPLIERS


def _column_suggestions(cells):
    result = {key: None for key in FIELDS}
    claimed = set()
    for key in FIELDS:
        # Alias order is a priority list; supplier package takes precedence over
        # an EDA footprint column when the file contains both.
        for alias in ALIASES[key]:
            match = next((i for i, label in enumerate(cells) if i not in claimed and _normal(label) == _normal(alias)), None)
            if match is not None:
                result[key] = match
                claimed.add(match)
                break
    return result


def _xml(archive, path):
    try:
        raw = archive.read(path)
        require(len(raw) <= MAX_EXPANDED_BYTES, "XLSX XML 内容过大")
        # Null stripping also catches declarations in UTF-16 / UTF-32 XML.
        declarations = raw.upper().replace(b"\x00", b"")
        require(b"<!DOCTYPE" not in declarations and b"<!ENTITY" not in declarations, "XLSX 含不支持的 XML 实体声明")
        return ET.fromstring(raw)
    except (KeyError, ET.ParseError) as error:
        raise DomainError("XLSX 结构不完整或 XML 格式有误") from error


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _descendants(element, name):
    return [node for node in element.iter() if _local(node.tag) == name]


def _sheet_target(target):
    target = target.replace("\\", "/")
    path = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join("xl", target))
    require(path.startswith("xl/") and not path.startswith("xl/../") and ":" not in path, "XLSX 工作表关系路径无效")
    return path


def _numeric_cell(value, number_format):
    try:
        require(len(value) <= 200, "XLSX 数字文本过长")
        number = Decimal(value)
        require(number.is_finite(), "XLSX 含非有限数字")
        # Decimal("1E999999999") itself is cheap; converting it to int or formatting
        # it would allocate enormous memory. Bound magnitude before either step.
        require(number.is_zero() or -100 <= number.adjusted() <= 100, "XLSX 数字指数过大或过小，请转成普通文本/整数")
        if number == number.to_integral_value():
            text = str(int(number))
            positive_format = number_format.split(";", 1)[0]
            if re.fullmatch(r"0{2,20}", positive_format) and number >= 0:
                text = text.zfill(len(positive_format))
            return text
        return format(number, "f")
    except (InvalidOperation, ValueError, OverflowError) as error:
        raise DomainError("XLSX 数字单元格格式无效") from error


def _read_xlsx(raw):
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except (zipfile.BadZipFile, OSError) as error:
        raise DomainError("这不是有效的 XLSX 文件，请从表格软件重新另存为 .xlsx") from error
    with archive:
        infos = archive.infolist()
        require(len(infos) <= 2500, "XLSX 内部文件过多")
        require(sum(i.file_size for i in infos) <= MAX_EXPANDED_BYTES, "XLSX 解压内容超过 32 MB")
        require(all(not (i.flag_bits & 1) for i in infos), "不支持加密的 XLSX")
        names = [i.filename for i in infos]
        require(len(names) == len(set(names)), "XLSX 含重复的内部文件名")
        workbook = _xml(archive, "xl/workbook.xml")
        relationships = _xml(archive, "xl/_rels/workbook.xml.rels")
        rels = {}
        for relation in relationships:
            if _local(relation.tag) != "Relationship":
                continue
            # Never fetch external relationships. A workbook sheet itself may not
            # point outside this archive; unrelated hyperlinks are simply ignored.
            rels[relation.get("Id")] = relation
        sheets = []
        for sheet in _descendants(workbook, "sheet"):
            relation_id = next((v for k, v in sheet.attrib.items() if _local(k) == "id"), None)
            relation = rels.get(relation_id)
            require(relation is not None and relation.get("TargetMode", "").casefold() != "external", "XLSX 工作表引用无效或指向外部文件")
            target = _sheet_target(relation.get("Target", ""))
            require(target in names, "XLSX 缺少工作表内容")
            sheets.append({"id": str(sheet.get("sheetId", len(sheets) + 1)), "name": sheet.get("name", "工作表"), "path": target})
        require(bool(sheets) and len(sheets) <= 100, "XLSX 工作表为空或超过 100 张")
        shared = []
        if "xl/sharedStrings.xml" in names:
            shared_xml = _xml(archive, "xl/sharedStrings.xml")
            for item in shared_xml:
                if _local(item.tag) == "si":
                    shared.append("".join(node.text or "" for node in item.iter() if _local(node.tag) == "t"))
            require(len(shared) <= MAX_CELLS, "XLSX 共享文本数量过多")
        formats, xfs = {}, []
        if "xl/styles.xml" in names:
            styles = _xml(archive, "xl/styles.xml")
            formats = {node.get("numFmtId"): node.get("formatCode", "") for node in _descendants(styles, "numFmt")}
            cell_xfs = next((node for node in styles if _local(node.tag) == "cellXfs"), None)
            if cell_xfs is not None:
                xfs = [node.get("numFmtId", "0") for node in cell_xfs if _local(node.tag) == "xf"]
        result = []
        cell_count = 0
        for sheet in sheets:
            xml = _xml(archive, sheet["path"])
            rows, seen_rows = [], set()
            for row in _descendants(xml, "row"):
                try:
                    row_number = int(row.get("r", len(rows) + 1))
                except ValueError as error:
                    raise DomainError("XLSX 行号无效") from error
                require(1 <= row_number <= MAX_ROWS and row_number not in seen_rows, "XLSX 行号重复或超过 10000 行")
                seen_rows.add(row_number)
                values, formulas = {}, set()
                for cell in row:
                    if _local(cell.tag) != "c":
                        continue
                    address = cell.get("r", "")
                    if address:
                        match = re.fullmatch(r"([A-Za-z]+)([1-9][0-9]*)", address)
                        require(match is not None and int(match.group(2)) == row_number, "XLSX 单元格地址无效")
                        index = 0
                        for letter in match.group(1).upper():
                            index = index * 26 + ord(letter) - ord("A") + 1
                        index -= 1
                    else:
                        index = len(values)
                    require(0 <= index < MAX_COLUMNS and index not in values, "XLSX 列数超过 256 或单元格地址重复")
                    cell_count += 1
                    require(cell_count <= MAX_CELLS, "XLSX 单元格数量过多")
                    children = {_local(node.tag): node for node in cell}
                    if "f" in children:
                        formulas.add(index)
                    value = children.get("v")
                    text = value.text or "" if value is not None else ""
                    kind = cell.get("t", "n")
                    if kind == "s":
                        require(text.isdigit() and int(text) < len(shared), "XLSX 共享文本引用无效")
                        text = shared[int(text)]
                    elif kind == "inlineStr":
                        text = "".join(node.text or "" for node in cell.iter() if _local(node.tag) == "t")
                    elif kind == "n" and text:
                        style = cell.get("s", "0")
                        require(style.isdigit(), "XLSX 单元格样式引用无效")
                        fmt_id = xfs[int(style)] if int(style) < len(xfs) else "0"
                        text = _numeric_cell(text, formats.get(fmt_id, ""))
                    elif kind == "e":
                        formulas.add(index)
                    require(len(text) <= 10000, "单元格文本超过 10000 字")
                    values[index] = text
                width = max(values, default=-1) + 1
                cells = [values.get(i, "") for i in range(width)]
                if any(_clean(v) for v in cells) or formulas:
                    rows.append({"rowNumber": row_number, "cells": cells, "formulas": formulas})
            result.append({"id": sheet["id"], "name": sheet["name"], "rows": rows})
        return result


def _read_csv(raw, filename):
    try:
        text = raw.decode("utf-8-sig")
        encoding = "UTF-8"
    except UnicodeDecodeError:
        try:
            text = raw.decode("gb18030")
            encoding = "GB18030"
        except UnicodeDecodeError as error:
            raise DomainError("CSV 编码无法识别，请另存为 UTF-8 或 GB18030") from error
    require("\x00" not in text, "CSV 含无效空字符；请另存为 UTF-8 CSV")
    try:
        dialect = csv.Sniffer().sniff(text[:65536], delimiters=",\t;")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = max((",", "\t", ";"), key=lambda d: sum(line.count(d) for line in text.splitlines()[:30]))
    if text.splitlines() and text.splitlines()[0].strip().lower().startswith("sep="):
        candidate = text.splitlines()[0].strip()[4:]
        if candidate in (",", "\t", ";"):
            delimiter = candidate
    rows, total_cells = [], 0
    previous_line = 0
    try:
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
        for cells in reader:
            row_number = previous_line + 1
            previous_line = reader.line_num
            require(previous_line <= MAX_ROWS and len(cells) <= MAX_COLUMNS, "CSV 超过 10000 行或 256 列")
            total_cells += len(cells)
            require(total_cells <= MAX_CELLS and all(len(v) <= 10000 for v in cells), "CSV 单元格过多或文本过长")
            if any(_clean(v) for v in cells):
                rows.append({"rowNumber": row_number, "cells": cells, "formulas": {i for i, v in enumerate(cells) if v.lstrip().startswith("=")}})
    except csv.Error as error:
        raise DomainError("CSV 引号或分隔符格式有误，请重新导出") from error
    return [{"id": "csv", "name": filename, "rows": rows}], encoding, delimiter


def _read_file(payload):
    require(isinstance(payload, dict), "BOM 请求必须是对象")
    filename = string(payload.get("filename"), "文件名", True).replace("\\", "/").rsplit("/", 1)[-1]
    require(0 < len(filename) <= 250, "文件名过长")
    extension = filename.rsplit(".", 1)[-1].lower()
    require(extension != "xls", "不支持旧版 .xls，请另存为 .xlsx 或 CSV 后导入")
    require(extension in ("csv", "tsv", "xlsx"), "只支持 CSV、TSV 或 XLSX 文件")
    content = payload.get("contentBase64")
    require(isinstance(content, str) and 0 < len(content) <= ((MAX_FILE_BYTES + 2) // 3) * 4, "BOM 文件为空或超过 8 MB")
    try:
        raw = base64.b64decode(content, validate=True)
    except (ValueError, binascii.Error) as error:
        raise DomainError("文件内容不是有效的 Base64 编码") from error
    require(0 < len(raw) <= MAX_FILE_BYTES, "BOM 文件为空或超过 8 MB")
    if extension == "xlsx":
        sheets = _read_xlsx(raw)
        encoding, delimiter = None, None
    else:
        sheets, encoding, delimiter = _read_csv(raw, filename)
    chosen = payload.get("sheet")
    require(chosen is None or isinstance(chosen, str), "工作表名称必须是文本")
    sheet = next((s for s in sheets if chosen in (s["id"], s["name"])), None) if chosen else next((s for s in sheets if s["rows"]), sheets[0])
    require(sheet is not None, "所选工作表不存在")
    require(bool(sheet["rows"]), "所选工作表没有可读取的内容")
    header_number = payload.get("headerRow")
    if header_number is None:
        def score(row):
            mapped = _column_suggestions(row["cells"])
            count = sum(value is not None for value in mapped.values())
            return count + (3 if mapped["quantityPerUnit"] is not None else 0) + (2 if mapped["supplierPartNumber"] is not None else 0)
        header_number = max(sheet["rows"][:30], key=score)["rowNumber"]
    integer(header_number, "表头行号", 1, MAX_ROWS)
    header = next((r for r in sheet["rows"] if r["rowNumber"] == header_number), None)
    require(header is not None, "表头行号不存在或为空")
    width = max(len(r["cells"]) for r in sheet["rows"])
    columns = [{"index": i, "label": _clean(header["cells"][i]) if i < len(header["cells"]) and _clean(header["cells"][i]) else "第 %s 列" % (i + 1)} for i in range(width)]
    data_rows = [row for row in sheet["rows"] if row["rowNumber"] > header_number]
    public = {"filename": filename, "fileHash": hashlib.sha256(raw).hexdigest(), "sheets": [{"id": s["id"], "name": s["name"]} for s in sheets],
              "sheet": sheet["id"], "sheetName": sheet["name"], "headerRow": header_number, "columns": columns,
              "sampleRows": [{"rowNumber": r["rowNumber"], "cells": r["cells"]} for r in data_rows[:12]],
              "suggestedMapping": _column_suggestions(header["cells"]), "rowCount": len(data_rows),
              "encoding": encoding, "delimiter": delimiter}
    return public, data_rows


def parse_bom(payload):
    try:
        return _read_file(payload)[0]
    except DomainError:
        raise
    except (zipfile.BadZipFile, zlib.error, RuntimeError, OSError, ValueError, OverflowError, KeyError, TypeError) as error:
        raise DomainError("BOM 文件损坏或格式不支持，请重新导出 CSV / XLSX") from error


def _part_number(value):
    value = _clean(value)
    return value.upper() if re.fullmatch(r"[Cc][0-9]+", value) else value


def _quantity(value):
    value = _clean(value)
    require(bool(re.fullmatch(r"[0-9]+(?:\.0+)?", value)), "每板数量必须是正整数，不能是空值、小数或公式")
    number = Decimal(value)
    require(number <= MAX_QUANTITY, "每板数量过大")
    return integer(int(number), "每板数量", 1)


def _designators(value):
    value = _clean(value)
    if not value or value in ("-", "—", "/"):
        return [], False
    value = re.sub(r"(?<=[0-9])\s*([-–—~～])\s*(?=[A-Za-z0-9])", r"\1", value)
    tokens = re.split(r"[,，、;；\s]+", value)
    refs = []
    for token in tokens:
        if not token:
            continue
        match = re.fullmatch(r"([A-Za-z]+)([0-9]+)\s*[-–—~～]\s*([A-Za-z]*)([0-9]+)", token)
        if match:
            prefix, start, end_prefix, end = match.groups()
            require(not end_prefix or prefix.casefold() == end_prefix.casefold(), "位号范围的前缀不一致")
            start, end = int(start), int(end)
            require(0 <= end - start < MAX_ROWS, "位号范围顺序错误或过大")
            refs.extend(prefix.upper() + str(number) for number in range(start, end + 1))
        elif re.fullmatch(r"[A-Za-z]+[0-9]+", token):
            refs.append(token.upper())
        else:
            return [], False
    require(len(refs) <= MAX_ROWS, "位号数量过多")
    require(len(set(refs)) == len(refs), "同一行含重复位号，请核对")
    return refs, bool(refs)


def _issue(message, row_number=None, field=None):
    result = {"message": message}
    if row_number is not None:
        result["rowNumber"] = row_number
    if field:
        result["field"] = field
    return result


def _same(value, other):
    return re.sub(r"\s+", "", _clean(value)).casefold() == re.sub(r"\s+", "", _clean(other)).casefold()


def preview_bom(payload, state, include_internal=False):
    """Return a reviewable plan tied to the exact ledger fingerprint."""
    try:
        return _preview_bom(payload, state, include_internal)
    except DomainError:
        raise
    except (zipfile.BadZipFile, zlib.error, RuntimeError, OSError, ValueError, OverflowError, KeyError, TypeError) as error:
        raise DomainError("BOM 文件损坏或格式不支持，请重新导出 CSV / XLSX") from error


def _preview_bom(payload, state, include_internal=False):
    public, source_rows = _read_file(payload)
    mapping = payload.get("mapping", public["suggestedMapping"])
    require(isinstance(mapping, dict) and all(k in FIELDS for k in mapping), "列映射字段无效")
    mapping = {key: mapping.get(key) for key in FIELDS}
    for key, index in mapping.items():
        require(index is None or (type(index) is int and 0 <= index < len(public["columns"])), "列映射索引无效：" + key)
    require(mapping["quantityPerUnit"] is not None, "请明确选择每板数量列；不会从位号自动猜数量")
    indices = [value for value in mapping.values() if value is not None]
    require(len(indices) == len(set(indices)), "同一列不能同时映射成多个字段")
    resolutions = payload.get("resolutions", {})
    require(isinstance(resolutions, dict), "逐行处理选择必须是对象")
    valid_row_keys = {str(row["rowNumber"]) for row in source_rows}
    require(all(isinstance(key, str) and key in valid_row_keys for key in resolutions), "逐行处理选择含不存在的行号")
    project_id = payload.get("projectId")
    require(project_id is None or isinstance(project_id, str), "项目 ID 无效")
    project_id = project_id or None
    existing_project = next((p for p in state["projects"] if p["id"] == project_id), None)
    require(project_id is None or existing_project is not None, "找不到要替换 BOM 的项目")
    name = string(payload.get("name", existing_project["name"] if existing_project else None), "项目名称", True).strip()
    planned = integer(payload.get("plannedUnits", existing_project["plannedUnits"] if existing_project else 0), "计划套数")
    require(existing_project is None or planned >= existing_project["issuedUnits"], "计划套数不能低于已领套数")
    fingerprint = hashlib.sha256(encoded(state).encode("utf-8")).hexdigest()
    materials = {m["id"]: m for m in state["materials"]}
    rows, errors, warnings, new_materials, new_identities, merged, ref_owners = [], [], [], {}, {}, {}, {}
    for source in source_rows:
        number = source["rowNumber"]
        cells = source["cells"]
        values = {key: _clean(cells[index]) if index is not None and index < len(cells) else "" for key, index in mapping.items()}
        values["supplierPartNumber"] = _part_number(values["supplierPartNumber"])
        output = {"rowNumber": number, **values, "quantityPerUnit": None, "status": "unresolved", "message": ""}
        rows.append(output)
        resolution = resolutions.get(str(number), {})
        require(isinstance(resolution, dict) and set(resolution).issubset({"mode", "materialId"}), "行 %s 的处理选择无效" % number)
        mode = resolution.get("mode")
        require(mode in (None, "match", "new", "skip"), "行 %s 的处理方式无效" % number)
        if mode == "skip":
            output.update(status="skipped", message="已按你的选择跳过，不计入 BOM")
            continue
        try:
            require(not any(index in source["formulas"] for index in indices), "关键列含公式或错误单元格，请转为已核实的文本/数字后导入，或明确跳过此行")
            require(all(len(value) <= 5000 for value in values.values()), "单元格文本过长")
            quantity = _quantity(values["quantityPerUnit"])
            output["quantityPerUnit"] = quantity
            refs, recognized = _designators(values["designatorsText"])
            if recognized:
                require(len(refs) == quantity, "每板数量 %s 与位号数 %s 不一致；请核对数量，或取消位号列映射后仅导入数量" % (quantity, len(refs)))
            elif values["designatorsText"]:
                warnings.append(_issue("位号格式未识别，保留原文字；每板用量使用数量列，不从位号推断", number, "designatorsText"))
            code = values["supplierPartNumber"]
            if code:
                require(bool(re.fullmatch(r"C[0-9]+", code)), "立创编号应是 C 加数字；其他供应商编号请取消该列映射后手选元件")
            candidates = [m for m in materials.values() if code and _supplier_is_lcsc(m.get("supplier")) and _part_number(m.get("supplierPartNumber")) == code]
            output["candidates"] = [{"id": m["id"], "name": m["name"], "supplierPartNumber": m.get("supplierPartNumber", ""), "manufacturerPartNumber": m.get("manufacturerPartNumber", ""), "package": m.get("package", "")} for m in candidates]
            material = None
            if mode == "match":
                require(isinstance(resolution.get("materialId"), str) and resolution["materialId"] in materials, "请明确选择账本中已有的元件")
                material = materials[resolution["materialId"]]
                discrepancies = [key for key in ("supplierPartNumber", "manufacturerPartNumber", "package") if values[key] and not _same(values[key], material.get(key))]
                if discrepancies or (code and not _supplier_is_lcsc(material.get("supplier"))):
                    warnings.append(_issue("已按你的明确选择使用已有元件，保留账本原型号、封装和料号；文件不同字段不会覆盖资料", number))
                output.update(status="matched", message="已手选已有元件；保留原资料")
            elif mode == "new":
                require(not candidates, "该立创料号已存在，请选已有元件；不能为相同商城物料再建一份")
                require(bool(values["name"] or values["manufacturerPartNumber"] or code), "新元件至少需要名称、型号或 C 编号之一")
                identity = encoded([code, values["manufacturerPartNumber"], values["package"], values["name"]] if not code else [code])
                if identity in new_identities:
                    material = new_materials[new_identities[identity]]
                    for key in ("manufacturerPartNumber", "package"):
                        require(not values[key] or not material.get(key) or _same(values[key], material[key]), "同一新料号在文件中出现互相冲突的型号或封装")
                else:
                    mid = "material-bom-" + hashlib.sha256((fingerprint + public["fileHash"] + identity).encode()).hexdigest()[:24]
                    material = {"id": mid, "name": values["name"] or values["manufacturerPartNumber"] or code, "category": "未分类",
                                "manufacturerPartNumber": values["manufacturerPartNumber"], "supplier": "LCSC" if code else "",
                                "supplierPartNumber": code, "specification": values["specification"], "package": values["package"], "unit": "piece", "notes": "由 BOM 文件明确新建；实物待点数"}
                    new_materials[mid], new_identities[identity] = material, mid
                output.update(status="new", message="将新建元件；采购 0、实物待点数，不生成收货")
            elif len(candidates) == 1:
                candidate = candidates[0]
                conflicts = [key for key in ("manufacturerPartNumber", "package") if values[key] and candidate.get(key) and not _same(values[key], candidate[key])]
                if conflicts:
                    output.update(status="conflict", message="C 编号相同，但%s不同；请明确选择已有元件以保留原资料，或跳过" % "、".join("型号" if k == "manufacturerPartNumber" else "封装" for k in conflicts))
                else:
                    material = candidate
                    output.update(status="matched", message="立创 C 编号精确匹配，型号/封装无冲突")
            elif len(candidates) > 1:
                output.update(status="conflict", message="多个立创供应商别名记录含相同 C 编号，请明确选择一个已有元件")
            else:
                output.update(status="unresolved", message="未找到可靠的 C 编号匹配，请手选已有元件、明确新建或跳过")
            if material is None:
                errors.append(_issue(output["message"], number))
                continue
            mid = material["id"]
            output["materialId"] = mid
            output["materialName"] = material["name"]
            if mid not in merged:
                merged[mid] = {"materialId": mid, "quantityPerUnit": 0, "designatorsText": "", "_refs": [], "_texts": [], "_rows": []}
            aggregate = merged[mid]
            aggregate["quantityPerUnit"] += quantity
            require(aggregate["quantityPerUnit"] <= MAX_QUANTITY, "合并后的每板用量过大")
            aggregate["_rows"].append(number)
            if recognized:
                for ref in refs:
                    require(ref not in ref_owners, "位号 %s 在多行重复，不能重复计数" % ref)
                    ref_owners[ref] = number
                aggregate["_refs"].extend(refs)
                aggregate["_texts"].append(values["designatorsText"])
            elif values["designatorsText"]:
                aggregate["_texts"].append(values["designatorsText"])
        except DomainError as error:
            output.update(status="invalid", message=str(error))
            errors.append(_issue(str(error), number))
    bom = []
    for mid, aggregate in merged.items():
        texts = aggregate["_texts"]
        bom.append({"materialId": mid, "quantityPerUnit": aggregate["quantityPerUnit"], "designatorsText": "、".join(dict.fromkeys(texts))})
    counts = {key: sum(row["status"] == key for row in rows) for key in ("matched", "new", "unresolved", "conflict", "skipped", "invalid")}
    counts.update(total=len(rows), materialKinds=len(bom), unitsPerBoard=sum(line["quantityPerUnit"] for line in bom), newMaterialKinds=len(new_materials))
    if not bom:
        errors.append(_issue("没有可导入的 BOM 行；请至少保留一项正整数用料"))
    for aggregate in merged.values():
        if len(aggregate["_rows"]) > 1:
            warnings.append(_issue("同一元件的第 %s 行已合并，每板数量相加、位号合并" % "、".join(map(str, aggregate["_rows"]))))
    result = {**public, "mapping": mapping, "rows": rows, "errors": errors, "warnings": warnings,
              "canImport": bool(bom) and not errors, "counts": counts, "baseRevision": state.get("revision", 0), "baseFingerprint": fingerprint,
              "projectId": project_id, "name": name, "plannedUnits": planned, "bom": bom,
              "newMaterials": list(new_materials.values()), "replacesExistingBom": existing_project is not None}
    if include_internal:
        result["_newMaterials"] = list(new_materials.values())
        result["_bom"] = bom
    return result
