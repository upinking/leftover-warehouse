"""CSV/XLSX 与真实种子账本的导入验收；不读写正式账本。"""
import base64
import copy
import csv
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock
import uuid
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from xml.sax.saxutils import escape
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import LocalServer, make_handler
from bom_import import parse_bom
from warehouse import DomainError, Warehouse

SEED = ROOT / 'data' / 'seed.json'
RED = 'material-C84270'
LAB = 'project-stm32-led-lab'
HEADERS = ['名称', '制造商编号', '供应商编号', '封装', '数量', '位号', '规格']
MAPPING = {'name': 0, 'manufacturerPartNumber': 1, 'supplierPartNumber': 2,
           'package': 3, 'quantityPerUnit': 4, 'designatorsText': 5, 'specification': 6}


def upload(filename, content):
    return {'filename': filename, 'contentBase64': base64.b64encode(content).decode('ascii')}


def csv_file(rows, encoding='utf-8-sig', delimiter=',', preamble=None):
    text = io.StringIO(newline='')
    writer = csv.writer(text, delimiter=delimiter)
    if preamble is not None:
        writer.writerow(preamble)
    writer.writerow(HEADERS)
    writer.writerows(rows)
    return upload('嘉立创BOM.csv', text.getvalue().encode(encoding))


def red_row(quantity='8', name='红色 LED（贴片）', model='NCD1206R1', package='1206', designators='LED1–LED8'):
    return [name, model, 'C84270', package, quantity, designators, '红色']


def column_letter(index):
    result = ''
    while index >= 0:
        result = chr(ord('A') + index % 26) + result
        index = index // 26 - 1
    return result


def xlsx_file(sheets):
    """用 OOXML 标准部件造小夹具；同时覆盖 shared/inline/numeric/formula 单元格。"""
    buffer = io.BytesIO()
    shared_strings = []
    worksheets = []
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    for _, rows in sheets:
        xml_rows = []
        for row_number, cells in enumerate(rows, 1):
            xml_cells = []
            for index, source in enumerate(cells):
                cell = source if isinstance(source, dict) else {'value': source, 'kind': 'inline'}
                value, kind = cell.get('value', ''), cell.get('kind', 'inline')
                ref = column_letter(index) + str(row_number)
                style = ' s="1"' if cell.get('style') == '0000' else ''
                if kind == 'shared':
                    shared_strings.append(str(value))
                    xml_cells.append('<c r="%s" t="s"><v>%s</v></c>' % (ref, len(shared_strings) - 1))
                elif kind == 'number':
                    xml_cells.append('<c r="%s"%s><v>%s</v></c>' % (ref, style, escape(str(value))))
                elif kind == 'formula':
                    xml_cells.append('<c r="%s"><f>%s</f><v>%s</v></c>' % (ref, escape(cell.get('formula', '1+1')), escape(str(value))))
                else:
                    xml_cells.append('<c r="%s" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>' % (ref, escape(str(value))))
            xml_rows.append('<row r="%d">%s</row>' % (row_number, ''.join(xml_cells)))
        worksheets.append('<worksheet xmlns="%s"><sheetData>%s</sheetData></worksheet>' % (ns, ''.join(xml_rows)))
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
            + ''.join('<Override PartName="/xl/worksheets/sheet%d.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' % i for i in range(1, len(sheets) + 1))
            + '</Types>')
        archive.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr('xl/workbook.xml', '<workbook xmlns="%s" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>%s</sheets></workbook>' % (
            ns, ''.join('<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (escape(name), i, i) for i, (name, _) in enumerate(sheets, 1))))
        archive.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + ''.join('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet%d.xml"/>' % (i, i) for i in range(1, len(sheets) + 1))
            + '<Relationship Id="rStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            + '<Relationship Id="rStrings" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
            + '</Relationships>')
        archive.writestr('xl/sharedStrings.xml', '<sst xmlns="%s" count="%d" uniqueCount="%d">%s</sst>' % (
            ns, len(shared_strings), len(shared_strings), ''.join('<si><t>%s</t></si>' % escape(value) for value in shared_strings)))
        archive.writestr('xl/styles.xml', '<styleSheet xmlns="%s"><numFmts count="1"><numFmt numFmtId="164" formatCode="0000"/></numFmts><cellStyleXfs count="1"><xf numFmtId="0"/></cellStyleXfs><cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="164" applyNumberFormat="1"/></cellXfs></styleSheet>' % ns)
        for i, xml in enumerate(worksheets, 1):
            archive.writestr('xl/worksheets/sheet%d.xml' % i, xml)
    return upload('嘉立创BOM.xlsx', buffer.getvalue())


def rewrite_xlsx(file, path, transform):
    original = zipfile.ZipFile(io.BytesIO(base64.b64decode(file['contentBase64'])))
    buffer = io.BytesIO()
    with original, zipfile.ZipFile(buffer, 'w', zipfile.ZIP_STORED) as modified:
        for info in original.infolist():
            content = original.read(info.filename)
            modified.writestr(info.filename, transform(content) if info.filename == path else content)
    return upload(file['filename'], buffer.getvalue())


class BOMImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / 'warehouse.sqlite3'
        self.w = Warehouse(self.db_path, SEED)

    def tearDown(self):
        self.w.close()
        self.temp.cleanup()

    def payload(self, file=None, name='导入测试电路', planned=1, **extra):
        return {**(file or csv_file([red_row()])), 'headerRow': 1, 'mapping': copy.deepcopy(MAPPING),
                'resolutions': {}, 'projectId': None, 'name': name, 'plannedUnits': planned, **extra}

    def preview(self, payload):
        return self.w.preview_bom(payload)

    def confirmed_payload(self, payload):
        preview = self.preview(payload)
        self.assertTrue(preview['canImport'], preview)
        return {**copy.deepcopy(payload), 'baseRevision': preview['baseRevision'],
                'baseFingerprint': preview['baseFingerprint']}

    def confirm(self, payload, request_id=None):
        return self.w.execute('bom.import', self.confirmed_payload(payload), request_id or str(uuid.uuid4()))

    def project(self, project_id):
        return next(project for project in self.w.snapshot()['data']['projects'] if project['id'] == project_id)

    def stock(self, material_id):
        return next(stock for stock in self.w.snapshot()['stocks'] if stock['materialId'] == material_id)

    def test_csv_gb18030_quoted_commas_and_tabs_keep_text_identity(self):
        for encoding, delimiter in [('gb18030', ','), ('utf-8-sig', '\t')]:
            with self.subTest(encoding=encoding, delimiter=repr(delimiter)):
                file = csv_file([red_row(name='红色 LED, 贴片 "练焊"')], encoding, delimiter)
                parsed = parse_bom(file)
                self.assertEqual(parsed['rowCount'], 1)
                cells = parsed['sampleRows'][0]['cells']
                self.assertEqual(cells[0], '红色 LED, 贴片 "练焊"')
                self.assertEqual(cells[2], 'C84270')
                self.assertEqual(cells[3], '1206')
                self.assertEqual(parsed['sampleRows'][0]['rowNumber'], 2)
                self.assertEqual(parsed['suggestedMapping']['supplierPartNumber'], 2)
                self.assertEqual(parsed['suggestedMapping']['quantityPerUnit'], 4)
                self.assertTrue(self.preview(self.payload(file))['canImport'])

    def test_header_row_selection_uses_physical_file_row_numbers(self):
        file = csv_file([red_row()], preamble=['由嘉立创EDA导出，仅作BOM'])
        parsed = parse_bom({**file, 'headerRow': 2})
        self.assertEqual(parsed['headerRow'], 2)
        self.assertEqual(parsed['sampleRows'][0]['rowNumber'], 3)
        self.assertEqual(parsed['columns'][2]['label'], '供应商编号')
        result = self.preview(self.payload(file, headerRow=2))
        self.assertTrue(result['canImport'], result)

    def test_csv_header_is_detected_after_a_report_title(self):
        file = csv_file([red_row()], preamble=['嘉立创EDA工程BOM，只导入用料'])
        parsed = parse_bom(file)
        self.assertEqual(parsed['headerRow'], 2)
        self.assertEqual(parsed['rowCount'], 1)
        self.assertEqual(parsed['suggestedMapping']['supplierPartNumber'], 2)
        self.assertEqual(parsed['sampleRows'][0]['rowNumber'], 3)

    def test_supplier_package_mapping_takes_precedence_over_eda_footprint(self):
        text = io.StringIO(newline='')
        writer = csv.writer(text)
        writer.writerow(['名称', '制造商编号', '供应商编号', 'Footprint', '数量', '位号', '规格', '供应商封装'])
        writer.writerow(['红色 LED（贴片）', 'NCD1206R1', 'C84270', 'LED-SMD_L3.2-W1.6', 8, 'LED1–LED8', '红色', '1206'])
        file = upload('有两种封装列.csv', text.getvalue().encode('utf-8-sig'))
        parsed = parse_bom(file)
        self.assertEqual(parsed['suggestedMapping']['package'], 7)
        preview = self.preview(self.payload(file, mapping=parsed['suggestedMapping']))
        self.assertTrue(preview['canImport'], preview)
        self.assertEqual(preview['rows'][0]['package'], '1206')

    def test_xlsx_shared_inline_multiple_sheets_and_0603_number_format(self):
        headers = [{'kind': 'shared', 'value': value} for value in HEADERS]
        row = ['自有测试电容', {'kind': 'shared', 'value': 'TEST-CAP-0603'}, '',
               {'kind': 'number', 'value': 603, 'style': '0000'},
               {'kind': 'number', 'value': 2}, 'C1、C2', '100nF']
        file = xlsx_file([('说明', [['请选BOM工作表']]), ('BOM', [headers, row])])
        first = parse_bom(file)
        self.assertEqual([sheet['name'] for sheet in first['sheets']], ['说明', 'BOM'])
        sheet = next(sheet['id'] for sheet in first['sheets'] if sheet['name'] == 'BOM')
        parsed = parse_bom({**file, 'sheet': sheet, 'headerRow': 1})
        self.assertEqual(parsed['sampleRows'][0]['cells'][1], 'TEST-CAP-0603')
        self.assertEqual(parsed['sampleRows'][0]['cells'][3], '0603')
        payload = self.payload(file, sheet=sheet, resolutions={'2': {'mode': 'new'}})
        result = self.confirm(payload)
        mid = self.project(result['projectId'])['bom'][0]['materialId']
        material = next(m for m in self.w.snapshot()['data']['materials'] if m['id'] == mid)
        self.assertEqual(material['package'], '0603')
        self.assertEqual(self.stock(mid)['ordered'], 0)
        self.assertIsNone(self.stock(mid)['actualOnHand'])

    def test_xlsx_formula_is_never_treated_as_confirmed_quantity(self):
        row = red_row()
        row[4] = {'kind': 'formula', 'formula': '4+4', 'value': 8}
        file = xlsx_file([('BOM', [HEADERS, row])])
        before = self.w.snapshot()
        try:
            result = self.preview(self.payload(file))
        except DomainError:
            pass
        else:
            self.assertFalse(result['canImport'], '有缓存结果的公式也不能静默按已确认数量导入')
            self.assertTrue(result.get('errors'))
        self.assertEqual(self.w.snapshot(), before)

    def test_formula_row_can_be_explicitly_skipped_and_unmapped_formulas_are_not_used(self):
        row = red_row('1', designators='LED9')
        row[4] = {'kind': 'formula', 'formula': '1+0', 'value': 1}
        file = xlsx_file([('BOM', [HEADERS, red_row(), row])])
        before = self.w.snapshot()
        self.assertFalse(self.preview(self.payload(file))['canImport'])
        self.assertEqual(self.w.snapshot(), before)
        result = self.confirm(self.payload(file, resolutions={'3': {'mode': 'skip'}}))
        bom = self.project(result['projectId'])['bom']
        self.assertEqual(len(bom), 1)
        self.assertEqual(bom[0]['quantityPerUnit'], 8)
        unmapped_file = xlsx_file([('BOM', [HEADERS + ['不导入的计算备注'],
            red_row() + [{'kind': 'formula', 'formula': '8+8', 'value': 16}]])])
        self.assertTrue(self.preview(self.payload(unmapped_file))['canImport'],
                        '未映射列不进入物料或数量；其中的公式不计算也不应阻止其它已核实字段')

    def test_corrupt_xlsx_crc_is_a_readable_error_in_both_parse_and_preview(self):
        file = xlsx_file([('BOM', [HEADERS, red_row()])])
        stored = rewrite_xlsx(file, 'xl/workbook.xml', lambda value: value)
        content = bytearray(base64.b64decode(stored['contentBase64']))
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            info = archive.getinfo('xl/workbook.xml')
            offset = info.header_offset
            name_size = int.from_bytes(content[offset + 26:offset + 28], 'little')
            extra_size = int.from_bytes(content[offset + 28:offset + 30], 'little')
            data_offset = offset + 30 + name_size + extra_size
            content[data_offset + info.file_size - 1] ^= 1
        damaged = upload('CRC损坏.xlsx', bytes(content))
        before = self.w.snapshot()
        with self.assertRaises(DomainError):
            parse_bom(damaged)
        with self.assertRaises(DomainError):
            self.preview(self.payload(damaged))
        self.assertEqual(self.w.snapshot(), before)

    def test_xlsx_external_sheet_or_xml_entity_cannot_be_imported(self):
        file = xlsx_file([('BOM', [HEADERS, red_row()])])
        external = rewrite_xlsx(file, 'xl/_rels/workbook.xml.rels', lambda raw:
            raw.replace(b'Target="worksheets/sheet1.xml"', b'Target="https://example.invalid/sheet.xml" TargetMode="External"'))
        entity = rewrite_xlsx(file, 'xl/workbook.xml', lambda raw:
            b'<!DOCTYPE workbook [<!ENTITY example "not allowed">]>' + raw)
        before = self.w.snapshot()
        for invalid in [external, entity]:
            with self.subTest(case=invalid['filename']):
                with self.assertRaises(DomainError):
                    parse_bom(invalid)
                with self.assertRaises(DomainError):
                    self.preview(self.payload(invalid))
                self.assertEqual(self.w.snapshot(), before)

    def test_xlsx_expansion_limit_is_checked_before_reading_archive_members(self):
        # 一个压缩后很小、展开却超过上限的文件；应在读任何成员前拒绝。
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('xl/workbook.xml', b'0' * (33 * 1024 * 1024))
        file = upload('展开过大.xlsx', buffer.getvalue())
        before = self.w.snapshot()
        with mock.patch.object(zipfile.ZipFile, 'read', side_effect=AssertionError('不应展开超限成员')):
            with self.assertRaises(DomainError):
                parse_bom(file)
            with self.assertRaises(DomainError):
                self.preview(self.payload(file))
        self.assertEqual(self.w.snapshot(), before)

    def test_bad_archives_base64_or_unsupported_files_fail_without_writes(self):
        cases = [upload('broken.xlsx', b'PK broken zip'),
                 {'filename': 'bad.csv', 'contentBase64': 'not-valid-base64!'},
                 upload('legacy.xls', b'not an xlsx workbook')]
        for file in cases:
            with self.subTest(filename=file['filename']):
                before = self.w.snapshot()
                with self.assertRaises(DomainError):
                    parse_bom(file)
                self.assertEqual(self.w.snapshot(), before)

    def test_preview_is_readonly_and_c_number_match_does_not_add_stock(self):
        payload = self.payload(planned=2)
        before = self.w.snapshot()
        preview = self.preview(payload)
        self.assertTrue(preview['canImport'], preview)
        self.assertEqual(preview['counts']['matched'], 1)
        self.assertEqual(preview['counts']['new'], 0)
        self.assertEqual(self.w.snapshot(), before, '关闭或取消预览必须完全不记账')
        result = self.confirm(payload)
        after = self.w.snapshot()
        self.assertEqual(len(after['data']['materials']), 16)
        self.assertEqual(after['summary']['orderedPieces'], 675)
        self.assertEqual(after['summary']['notCountedKinds'], 16)
        self.assertEqual(after['data']['stockMovements'], [])
        bom = self.project(result['projectId'])['bom']
        self.assertEqual(len(bom), 1)
        self.assertEqual(bom[0]['materialId'], RED)
        self.assertEqual(bom[0]['quantityPerUnit'], 8)
        self.assertIn('LED1', bom[0]['designatorsText'])
        self.assertIn('LED8', bom[0]['designatorsText'])
        self.assertEqual(self.stock(RED)['reserved'], 29)  # 既有13 + 新计划2×8。

    def test_matching_c_number_with_model_or_package_conflict_is_blocked(self):
        for row in [red_row(model='NOT-NCD1206R1'), red_row(package='0603')]:
            with self.subTest(row=row):
                before = self.w.snapshot()
                result = self.preview(self.payload(csv_file([row])))
                self.assertFalse(result['canImport'])
                self.assertTrue(result.get('errors') or result['counts'].get('conflict'))
                self.assertEqual(self.w.snapshot(), before)

    def test_explicit_new_material_is_unknown_and_has_no_purchase(self):
        row = ['测试自有电阻', 'TEST-R-0603', 'C999999000', '0603', '2', 'R1、R2', '47kΩ']
        payload = self.payload(csv_file([row]), resolutions={'2': {'mode': 'new'}})
        result = self.confirm(payload)
        project = self.project(result['projectId'])
        mid = project['bom'][0]['materialId']
        self.assertNotEqual(mid, RED)
        self.assertEqual(self.stock(mid)['ordered'], 0)
        self.assertIsNone(self.stock(mid)['actualOnHand'])
        self.assertEqual(self.stock(mid)['reserved'], 2)
        self.assertEqual(self.w.snapshot()['summary']['orderedPieces'], 675)
        self.assertEqual(self.w.snapshot()['summary']['notCountedKinds'], 17)
        self.assertEqual(self.w.snapshot()['data']['stockMovements'], [])
        self.assertEqual(len(self.w.snapshot()['data']['purchaseBatches']), 1)

    def test_name_alone_never_automatically_merges_materials_without_c_number(self):
        row = ['红色 LED（贴片）', '', '', '1206', '1', 'LED1', '红色']
        payload = self.payload(csv_file([row]))
        result = self.preview(payload)
        self.assertFalse(result['canImport'], '只有相同名称不能认定是同一物料')
        new_result = self.confirm({**payload, 'resolutions': {'2': {'mode': 'new'}}})
        new_mid = self.project(new_result['projectId'])['bom'][0]['materialId']
        self.assertNotEqual(new_mid, RED)
        self.assertEqual(len(self.w.snapshot()['data']['materials']), 17)
        self.assertIsNone(self.stock(new_mid)['actualOnHand'])

    def test_duplicate_c_number_lines_merge_quantities_and_keep_designators(self):
        rows = [red_row('3', designators='LED1、LED2、LED3'), red_row('5', designators='LED4–LED8')]
        result = self.confirm(self.payload(csv_file(rows)))
        bom = self.project(result['projectId'])['bom']
        self.assertEqual(len(bom), 1)
        self.assertEqual(bom[0]['materialId'], RED)
        self.assertEqual(bom[0]['quantityPerUnit'], 8)
        self.assertIn('LED1', bom[0]['designatorsText'])
        self.assertIn('LED4', bom[0]['designatorsText'])
        self.assertEqual(len(self.w.snapshot()['data']['materials']), 16)

    def test_ambiguous_supplier_alias_requires_an_explicit_existing_material_choice(self):
        self.w.execute('material.save', {'name': '同C编号的旧记录', 'supplier': '立创商城',
                       'supplierPartNumber': 'C84270', 'manufacturerPartNumber': 'NCD1206R1',
                       'package': '1206'}, 'supplier-alias-fixture')
        payload = self.payload()
        before = self.w.snapshot()
        preview = self.preview(payload)
        self.assertFalse(preview['canImport'])
        self.assertEqual(self.w.snapshot(), before)
        chosen = self.confirm({**payload, 'resolutions': {'2': {'mode': 'match', 'materialId': RED}}})
        self.assertEqual(self.project(chosen['projectId'])['bom'][0]['materialId'], RED)
        self.assertEqual(len(self.w.snapshot()['data']['materials']), 17)

    def test_quantity_designator_disagreement_or_repeated_designator_blocks_import(self):
        cases = [[red_row('7')],
                 [red_row('1', designators='LED1'), red_row('1', designators='LED1')]]
        for rows in cases:
            with self.subTest(rows=rows):
                before = self.w.snapshot()
                preview = self.preview(self.payload(csv_file(rows)))
                self.assertFalse(preview['canImport'])
                self.assertTrue(preview['errors'])
                self.assertEqual(self.w.snapshot(), before)

    def test_invalid_quantities_are_not_silently_coerced_to_valid_counts(self):
        for quantity in ['', '0', '-1', '1.5', '八', 'NaN', 'TRUE']:
            with self.subTest(quantity=quantity):
                before = self.w.snapshot()
                result = self.preview(self.payload(csv_file([red_row(quantity)])))
                self.assertFalse(result['canImport'])
                self.assertTrue(result.get('errors'))
                self.assertEqual(self.w.snapshot(), before)

    def test_invalid_row_can_only_be_omitted_by_explicit_skip(self):
        rows = [red_row(), ['不导入的注释行', '', '', '', '无数量', '', '']]
        payload = self.payload(csv_file(rows), resolutions={'3': {'mode': 'skip'}})
        result = self.confirm(payload)
        self.assertEqual(len(self.project(result['projectId'])['bom']), 1)
        self.assertEqual(len(self.w.snapshot()['data']['materials']), 16)

    def test_failed_import_cannot_partially_add_an_earlier_new_material(self):
        rows = [['测试新元件', 'NEW-TEST-PART', '', '0603', '2', 'C1、C2', ''], red_row('0')]
        payload = self.payload(csv_file(rows), resolutions={'2': {'mode': 'new'}})
        before = self.w.snapshot()
        preview = self.preview(payload)
        self.assertFalse(preview['canImport'])
        confirm = {**payload, 'baseRevision': preview['baseRevision'], 'baseFingerprint': preview['baseFingerprint']}
        with self.assertRaises(DomainError):
            self.w.execute('bom.import', confirm, 'must-not-partially-add')
        self.assertEqual(self.w.snapshot(), before)

    def test_failure_in_existing_project_plan_cannot_leave_a_new_material_behind(self):
        for stock in self.w.snapshot()['stocks']:
            self.w.execute('stock.record', {'materialId': stock['materialId'], 'type': 'receipt',
                           'quantity': stock['ordered'], 'reason': '仅测试收齐'}, str(uuid.uuid4()))
        self.w.execute('project.issue', {'projectId': LAB, 'units': 1}, 'issued-before-invalid-import')
        row = ['不应留下的新元件', 'NEW-INVALID-PLAN', '', '0603', '1', 'C1', '']
        payload = self.payload(csv_file([row]), projectId=LAB, planned=1, resolutions={'2': {'mode': 'new'}})
        confirm = self.confirmed_payload(payload)
        confirm['plannedUnits'] = 0  # 即使元件行有效，项目计划也不能低于已经领出的1套。
        before = self.w.snapshot()
        with self.assertRaises(DomainError):
            self.w.execute('bom.import', confirm, 'invalid-project-plan-with-new-material')
        self.assertEqual(self.w.snapshot(), before)

    def test_invalid_mapping_and_all_skipped_bom_cannot_create_a_project(self):
        wrong_mapping = {**MAPPING, 'quantityPerUnit': 999}
        cases = [self.payload(mapping=wrong_mapping), self.payload(resolutions={'2': {'mode': 'skip'}})]
        for payload in cases:
            with self.subTest(mapping=payload['mapping'], resolutions=payload['resolutions']):
                before = self.w.snapshot()
                try:
                    result = self.preview(payload)
                except DomainError:
                    pass
                else:
                    self.assertFalse(result['canImport'])
                    self.assertTrue(result.get('errors'))
                self.assertEqual(self.w.snapshot(), before)

    def test_confirm_retry_is_idempotent_after_original_revision_changes(self):
        payload = self.confirmed_payload(self.payload())
        first = self.w.execute('bom.import', payload, 'bom-double-click')
        before = self.w.snapshot()
        second = self.w.execute('bom.import', payload, 'bom-double-click')
        self.assertEqual(first, second)
        self.assertEqual(self.w.snapshot(), before)
        self.assertEqual(len(before['data']['projects']), 3)
        with self.assertRaises(DomainError):
            self.w.execute('bom.import', payload, 'different-request-with-old-preview')
        self.assertEqual(self.w.snapshot(), before)

    def test_existing_project_bom_is_replaced_without_rewriting_issued_history(self):
        for stock in self.w.snapshot()['stocks']:
            self.w.execute('stock.record', {'materialId': stock['materialId'], 'type': 'receipt',
                           'quantity': stock['ordered'], 'reason': '仅测试：按采购数量收齐'}, str(uuid.uuid4()))
        self.w.execute('project.issue', {'projectId': LAB, 'units': 1}, 'prior-lab-issue')
        before = self.w.snapshot()
        payload = self.payload(csv_file([red_row('1', designators='LED1')]), name='STM32 八路灯效练焊板',
                               projectId=LAB, planned=2)
        result = self.confirm(payload)
        self.assertEqual(result['projectId'], LAB)
        after = self.w.snapshot()
        self.assertEqual(len(after['data']['projects']), 2)
        self.assertEqual(after['data']['stockMovements'], before['data']['stockMovements'])
        self.assertEqual(self.project(LAB)['issuedUnits'], 1)
        self.assertEqual(self.project(LAB)['bomVersion'], 2)
        self.assertEqual(self.project(LAB)['bom'][0]['quantityPerUnit'], 1)
        self.assertEqual(len(self.project(LAB)['bom']), 1)
        self.assertEqual(after['summary']['issuedPieces'], 25)
        self.assertEqual(after['summary']['actualOnHandPieces'], 650)
        self.assertEqual(self.stock(RED)['actualOnHand'], 12)
        self.assertEqual(self.stock(RED)['reserved'], 6)

    def test_stale_preview_and_wrong_fingerprint_are_rejected(self):
        payload = self.confirmed_payload(self.payload())
        self.w.execute('stock.record', {'materialId': RED, 'type': 'receipt', 'quantity': 0, 'reason': '预览后手动点数'}, 'change-between-preview-and-confirm')
        before = self.w.snapshot()
        with self.assertRaises(DomainError):
            self.w.execute('bom.import', payload, 'stale-import')
        self.assertEqual(self.w.snapshot(), before)
        current_payload = self.confirmed_payload(self.payload())
        current_payload['baseFingerprint'] = '0' * 64
        with self.assertRaises(DomainError):
            self.w.execute('bom.import', current_payload, 'wrong-ledger-import')
        self.assertEqual(self.w.snapshot(), before)

    def test_import_persists_and_backup_restores_new_materials_and_project(self):
        row = ['新测试电容', 'TEST-CAP', '', '0603', '2', 'C1、C2', '10nF']
        self.confirm(self.payload(csv_file([row]), resolutions={'2': {'mode': 'new'}}))
        saved = self.w.snapshot()
        backup = self.w.backup()
        self.w.close()
        self.w = Warehouse(self.db_path, SEED)
        self.assertEqual(self.w.snapshot(), saved)
        empty = Warehouse(Path(self.temp.name) / 'restored.sqlite3')
        try:
            empty.execute('backup.restore', {'backup': backup}, 'restore-imported-bom')
            restored = empty.snapshot()
            self.assertEqual(restored['stocks'], saved['stocks'])
            self.assertEqual(restored['summary'], saved['summary'])
            for key in ['materials', 'projects', 'openingStock', 'purchaseBatches', 'stockMovements']:
                self.assertEqual(restored['data'][key], saved['data'][key])
        finally:
            empty.close()

    def test_http_parse_preview_and_confirm_use_same_real_ledger(self):
        server = LocalServer(('127.0.0.1', 0), make_handler(self.w))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:%d' % server.server_port
        def post(path, body):
            request = Request(base + path, data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
                              headers={'Content-Type': 'application/json'})
            with urlopen(request, timeout=5) as response:
                return json.load(response)
        try:
            file = csv_file([red_row()])
            parsed = post('/api/bom/parse', file)
            self.assertEqual(parsed['rowCount'], 1)
            payload = self.payload(file)
            preview = post('/api/bom/preview', payload)
            self.assertTrue(preview['canImport'], preview)
            self.assertEqual(len(self.w.snapshot()['data']['projects']), 2)
            request = {'action': 'bom.import', 'payload': {**payload, 'baseRevision': preview['baseRevision'],
                       'baseFingerprint': preview['baseFingerprint']}, 'requestId': 'http-bom-import'}
            response = post('/api/action', request)
            self.assertEqual(response['snapshot'], self.w.snapshot())
            self.assertEqual(response['snapshot']['summary']['orderedPieces'], 675)
            self.assertEqual(response['snapshot']['summary']['notCountedKinds'], 16)
            post('/api/action', request)
            self.assertEqual(len(self.w.snapshot()['data']['projects']), 3)
            with self.assertRaises(HTTPError) as failure:
                post('/api/bom/parse', {'filename': 'broken.xlsx', 'contentBase64': '!!!'})
            self.assertEqual(failure.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main(verbosity=2)
