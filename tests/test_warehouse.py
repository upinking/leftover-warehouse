"""业务验收：所有测试都使用临时数据库，不改用户账本。"""
import copy
import json
import sys
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from warehouse import DomainError, Warehouse

SEED_PATH = ROOT / 'data' / 'seed.json'
SEED = json.loads(SEED_PATH.read_text(encoding='utf-8'))
EXPECTED = json.loads((ROOT / 'data' / 'expected-results.json').read_text(encoding='utf-8'))
RED = 'material-C84270'
HEADER = 'material-C124375'
LAB = 'project-stm32-led-lab'
RUNNER = 'project-runner'


class WarehouseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'warehouse.sqlite3'
        self.store = Warehouse(self.db, SEED_PATH)

    def tearDown(self):
        close = getattr(self.store, 'close', None)
        if close:
            close()
        self.temp.cleanup()

    def act(self, action, payload, request_id=None):
        return self.store.execute(action, payload, request_id or str(uuid.uuid4()))

    def stock(self, material_id, snapshot=None):
        return next(s for s in (snapshot or self.store.snapshot())['stocks'] if s['materialId'] == material_id)

    def capacity(self, project_id=LAB, snapshot=None):
        return next(c for c in (snapshot or self.store.snapshot())['capacities'] if c['projectId'] == project_id)

    def project(self, project_id=LAB):
        return copy.deepcopy(next(p for p in self.store.snapshot()['data']['projects'] if p['id'] == project_id))

    def receive_all(self):
        for row in self.store.snapshot()['stocks']:
            self.act('stock.record', {'materialId': row['materialId'], 'type': 'receipt',
                                     'quantity': row['ordered'], 'reason': '仅测试：按采购数量点数'})

    def assert_unchanged_after_error(self, action, payload):
        before = self.store.snapshot()
        with self.assertRaises(DomainError):
            self.act(action, payload)
        self.assertEqual(self.store.snapshot(), before)

    def test_initial_summary_matches_independent_expected_results(self):
        snap = self.store.snapshot()
        expected = EXPECTED['baseline']
        for key in ['materialKinds', 'orderedPieces', 'forecastFreePieces',
                    'actualOnHandPieces', 'notCountedKinds']:
            self.assertEqual(snap['summary'][key], expected[key], key)
        self.assertEqual(snap['summary']['reservedPieces'], expected['totalReservedPieces'])
        self.assertEqual(len(snap['data']['projects']), 2)
        self.assertEqual(snap['data']['stockMovements'], [])
        self.assertEqual(snap['data']['importId'], SEED['importId'])
        self.assertTrue(all(o['receivedCumulative'] is None for o in snap['data']['openingStock']))
        self.assertIsNone(snap['data']['purchaseBatches'][0]['orderedAt'])
        self.assertIsNone(snap['data']['purchaseBatches'][0]['totalPaid'])

    def test_every_initial_stock_matches_expected_results(self):
        for expected in EXPECTED['stocks']:
            with self.subTest(material=expected['materialId']):
                actual = self.stock(expected['materialId'])
                for key in ['ordered', 'forecastFree', 'actualOnHand']:
                    self.assertEqual(actual[key], expected[key], key)
                self.assertEqual(actual['reserved'], expected['runnerReserved'] + expected['practiceReserved'])

    def test_initial_capacity_uses_led_not_headers_as_bottleneck(self):
        capacity = self.capacity()
        expected = EXPECTED['practiceCapacity']
        self.assertEqual(capacity['forecastWholeUnits'], 1)
        self.assertIsNone(capacity['actualWholeUnits'])
        self.assertIn(RED, capacity['bottleneckMaterialIds'])
        limits = {r['materialId']: r for r in capacity['limits']}
        for row in expected['limits']:
            self.assertEqual(limits[row['materialId']]['availableForecast'], row['afterRunnerReserved'])
            self.assertEqual(limits[row['materialId']]['quantityPerUnit'], row['practicePerUnit'])
            self.assertEqual(limits[row['materialId']]['wholeUnits'], row['wholeUnits'])

    def test_unknown_stock_blocks_issue_and_preserves_all_state(self):
        self.assert_unchanged_after_error('project.issue', {'projectId': LAB, 'units': 1})

    def test_confirmed_zero_is_distinct_from_unknown(self):
        self.act('stock.record', {'materialId': RED, 'type': 'receipt', 'quantity': 0, 'reason': '点数结果确认为零'})
        row = self.stock(RED)
        self.assertEqual(row['actualOnHand'], 0)
        self.assertEqual(row['received'], 0)
        self.assertEqual(self.store.snapshot()['summary']['notCountedKinds'], 15)
        self.assertIsNone(self.stock(HEADER)['actualOnHand'])
        self.assert_unchanged_after_error('project.issue', {'projectId': LAB, 'units': 1})

    def test_two_lab_plans_keep_negative_shortage_visible(self):
        project = self.project()
        project['plannedUnits'] = 2
        self.act('project.save', project)
        self.assertEqual(self.stock(RED)['forecastFree'], -1)
        self.assertEqual(self.stock(RED)['shortage'], 1)
        self.assertEqual(self.store.snapshot()['summary']['forecastFreePieces'], 470)
        self.assertEqual(self.capacity()['forecastWholeUnits'], 1)
        self.assertEqual(self.store.snapshot()['data']['stockMovements'], [])

    def test_whole_board_issue_consumes_once_and_releases_reservation(self):
        self.receive_all()
        self.act('project.issue', {'projectId': LAB, 'units': 1})
        snap = self.store.snapshot()
        self.assertEqual(snap['summary']['issuedPieces'], 25)
        self.assertEqual(snap['summary']['actualOnHandPieces'], 650)
        self.assertEqual(snap['summary']['reservedPieces'], 155)
        self.assertEqual(snap['summary']['forecastFreePieces'], 495)
        self.assertEqual(self.stock(RED)['actualOnHand'], 12)
        self.assertEqual(self.stock(RED)['forecastFree'], 7)
        self.assertEqual(self.project()['issuedUnits'], 1)

    def test_retry_same_request_is_idempotent_and_mismatched_payload_rejected(self):
        self.receive_all()
        request_id = 'board-double-click'
        payload = {'projectId': LAB, 'units': 1}
        first = self.act('project.issue', payload, request_id)
        before = self.store.snapshot()
        second = self.act('project.issue', payload, request_id)
        self.assertEqual(first, second)
        self.assertEqual(self.store.snapshot(), before)
        with self.assertRaises(DomainError):
            self.act('project.issue', {'projectId': LAB, 'units': 2}, request_id)
        self.assertEqual(self.store.snapshot(), before)

    def test_partial_receipt_reports_all_missing_lines_with_no_partial_issue(self):
        self.act('stock.record', {'materialId': RED, 'type': 'receipt', 'quantity': 20, 'reason': '只收到红灯'})
        before = self.store.snapshot()
        with self.assertRaises(DomainError) as raised:
            self.act('project.issue', {'projectId': LAB, 'units': 1})
        detail = json.dumps(getattr(raised.exception, 'details', None), ensure_ascii=False)
        for line in self.project()['bom']:
            if line['materialId'] != RED:
                self.assertIn(line['materialId'], detail)
        self.assertEqual(self.store.snapshot(), before)

    def test_purchase_changes_forecast_only(self):
        self.act('purchase.add', {'materialId': RED, 'quantity': 10, 'reason': '追加采购记录'})
        self.assertEqual(self.stock(RED)['ordered'], 30)
        self.assertIsNone(self.stock(RED)['actualOnHand'])
        self.assertEqual(self.capacity()['forecastWholeUnits'], 2)
        self.assertIn(HEADER, self.capacity()['bottleneckMaterialIds'])

    def test_damage_exposes_forecast_shortage_and_zero_capacity(self):
        self.receive_all()
        self.act('stock.record', {'materialId': RED, 'type': 'damage', 'quantity': 8, 'reason': '练焊损坏'})
        self.assertEqual(self.stock(RED)['actualOnHand'], 12)
        self.assertEqual(self.stock(RED)['forecastFree'], -1)
        self.assertEqual(self.capacity()['forecastWholeUnits'], 0)

    def test_issue_return_loss_and_count_leave_traceable_history(self):
        self.receive_all()
        for kind, quantity, reason in [('issue', 2, '临时实验领用'), ('return', 1, '实验余料归还'),
                                       ('loss', 1, '掉落遗失'), ('count', 17, '复点纠正余额')]:
            self.act('stock.record', {'materialId': RED, 'type': kind, 'quantity': quantity, 'reason': reason})
        snap = self.store.snapshot()
        self.assertEqual(self.stock(RED)['actualOnHand'], 17)
        movements = snap['data']['stockMovements'][-4:]
        self.assertEqual([m['type'] for m in movements], ['issue', 'return', 'loss', 'count'])
        for movement in movements:
            self.assertTrue(movement.get('reason'))
            self.assertTrue(movement.get('actor'))
            self.assertTrue(movement.get('occurredAt') or movement.get('at'))
        self.assertEqual(self.project()['issuedUnits'], 0)

    def test_bom_edit_does_not_rewrite_historic_issue(self):
        self.receive_all()
        self.act('project.issue', {'projectId': LAB, 'units': 1})
        before = self.store.snapshot()
        previous_movements = copy.deepcopy(before['data']['stockMovements'])
        project = self.project()
        project['plannedUnits'] = 2
        for line in project['bom']:
            if line['materialId'] == RED:
                line['quantityPerUnit'] = 1
        self.act('project.save', project)
        after = self.store.snapshot()
        self.assertEqual(after['data']['stockMovements'], previous_movements)
        self.assertEqual(self.stock(RED)['actualOnHand'], 12)
        self.assertEqual(after['summary']['issuedPieces'], 25)
        self.assertEqual(self.project()['bomVersion'], 2)
        self.assertEqual(self.stock(RED)['reserved'], 6)  # 跑马灯 5 + 新版灯板待领 1

    def test_plan_cannot_drop_below_already_issued_units(self):
        self.receive_all()
        self.act('project.issue', {'projectId': LAB, 'units': 1})
        project = self.project()
        project['plannedUnits'] = 0
        self.assert_unchanged_after_error('project.save', project)

    def test_completed_plan_cannot_be_issued_again_under_new_request(self):
        self.receive_all()
        self.act('project.issue', {'projectId': LAB, 'units': 1})
        self.assert_unchanged_after_error('project.issue', {'projectId': LAB, 'units': 1})

    def test_cancel_pending_plan_releases_reservations(self):
        project = self.project()
        project['plannedUnits'] = 0
        self.act('project.save', project)
        self.assertEqual(self.store.snapshot()['summary']['reservedPieces'], 155)
        self.assertEqual(self.store.snapshot()['summary']['forecastFreePieces'], 520)
        self.assertIsNone(self.stock(RED)['actualOnHand'])

    def test_repeat_seed_import_never_duplicates_data(self):
        before = self.store.snapshot()
        try:
            self.act('seed.import', {'seed': copy.deepcopy(SEED)})
        except DomainError:
            pass
        after = self.store.snapshot()
        self.assertEqual(after['summary'], before['summary'])
        self.assertEqual(after['data']['materials'], before['data']['materials'])
        self.assertEqual(after['data']['purchaseBatches'], before['data']['purchaseBatches'])

    def test_new_own_material_without_supplier_number_and_string_package(self):
        self.act('material.save', {'name': '自有 1uF 电容', 'category': '电容',
                                  'manufacturerPartNumber': '', 'supplier': '', 'supplierPartNumber': '',
                                  'specification': '1uF', 'package': '0603', 'locationCode': 'A盒/03格', 'notes': '旧料'})
        new = next(m for m in self.store.snapshot()['data']['materials'] if m['name'] == '自有 1uF 电容')
        self.assertEqual(new['package'], '0603')
        self.assertEqual(self.stock(new['id'])['locationCode'], 'A盒/03格')
        self.assertIsNone(self.stock(new['id'])['actualOnHand'])
        self.act('project.save', {'name': '小实验', 'plannedUnits': 2,
                                  'bom': [{'materialId': new['id'], 'quantityPerUnit': 1, 'designatorsText': 'C1'}]})
        new_project = next(p for p in self.store.snapshot()['data']['projects'] if p['name'] == '小实验')
        self.assertEqual(self.stock(new['id'])['forecastFree'], -2)
        self.assertIsNone(self.capacity(new_project['id'])['actualWholeUnits'])

    def test_invalid_quantities_are_rejected_without_writes(self):
        for value in [-1, True, 1.5, '1', None]:
            with self.subTest(value=value):
                self.assert_unchanged_after_error('stock.record', {'materialId': RED, 'type': 'receipt',
                                                                   'quantity': value, 'reason': '错误输入'})
                self.assert_unchanged_after_error('purchase.add', {'materialId': RED, 'quantity': value, 'reason': '错误输入'})

    def test_unknown_material_and_invalid_bom_reference_are_rejected(self):
        self.assert_unchanged_after_error('stock.record', {'materialId': 'missing', 'type': 'receipt', 'quantity': 1, 'reason': '错误'})
        self.assert_unchanged_after_error('project.save', {'name': '坏BOM', 'plannedUnits': 1,
                                                          'bom': [{'materialId': 'missing', 'quantityPerUnit': 1, 'designatorsText': ''}]})

    def test_no_negative_actual_stock_and_unknown_stock_cannot_be_consumed(self):
        for kind in ['issue', 'loss', 'damage']:
            with self.subTest(type=kind):
                self.assert_unchanged_after_error('stock.record', {'materialId': RED, 'type': kind, 'quantity': 1, 'reason': '错误'})
        self.act('stock.record', {'materialId': RED, 'type': 'receipt', 'quantity': 1, 'reason': '已点数'})
        self.assert_unchanged_after_error('stock.record', {'materialId': RED, 'type': 'loss', 'quantity': 2, 'reason': '不足'})

    def test_whole_board_issue_protects_other_projects_reservations(self):
        self.receive_all()
        # 红灯有 12 颗，灯板要 8，跑马灯还预留 5，不能挪用。
        self.act('stock.record', {'materialId': RED, 'type': 'damage', 'quantity': 8, 'reason': '焊坏'})
        self.assert_unchanged_after_error('project.issue', {'projectId': LAB, 'units': 1})

    def test_reopen_database_restores_saved_data(self):
        self.act('stock.record', {'materialId': RED, 'type': 'receipt', 'quantity': 0, 'reason': '确认未收到'})
        before = self.store.snapshot()
        close = getattr(self.store, 'close', None)
        if close:
            close()
        self.store = Warehouse(self.db, SEED_PATH)
        self.assertEqual(self.store.snapshot(), before)

    def test_backup_restore_into_empty_database_preserves_state_and_idempotency(self):
        self.receive_all()
        payload = {'projectId': LAB, 'units': 1}
        result = self.act('project.issue', payload, 'restored-issue-request')
        backup = self.store.backup()
        preview = self.store.preview_backup(backup)
        self.assertTrue(preview)
        target = Warehouse(Path(self.temp.name) / 'restored.sqlite3', None)
        try:
            target.execute('backup.restore', {'backup': backup}, 'restore-test')
            source_snap = self.store.snapshot()
            target_snap = target.snapshot()
            self.assertEqual(target_snap['summary'], source_snap['summary'])
            self.assertEqual(target_snap['stocks'], source_snap['stocks'])
            self.assertEqual(target_snap['capacities'], source_snap['capacities'])
            for key in ['materials', 'purchaseBatches', 'projects', 'openingStock', 'stockMovements']:
                self.assertEqual(target_snap['data'][key], source_snap['data'][key])
            before = target.snapshot()
            self.assertEqual(target.execute('project.issue', payload, 'restored-issue-request'), result)
            self.assertEqual(target.snapshot(), before)
        finally:
            if getattr(target, 'close', None):
                target.close()

    def test_bad_backup_preview_and_restore_do_not_change_live_data(self):
        for mutation in ['schema', 'reference', 'negative', 'boolean', 'package', 'missing', 'wrongtype']:
            with self.subTest(mutation=mutation):
                backup = copy.deepcopy(self.store.backup())
                if mutation == 'schema':
                    backup['version'] = 99
                elif mutation == 'reference':
                    backup['data']['projects'][0]['bom'][0]['materialId'] = 'missing'
                elif mutation == 'negative':
                    backup['data']['purchaseBatches'][0]['lines'][0]['orderedQuantity'] = -1
                elif mutation == 'boolean':
                    backup['data']['projects'][0]['plannedUnits'] = True
                elif mutation == 'package':
                    backup['data']['materials'][0]['package'] = 603
                elif mutation == 'missing':
                    del backup['data']['openingStock']
                else:
                    backup['data']['stockMovements'] = '坏数据'
                before = self.store.snapshot()
                with self.assertRaises(DomainError):
                    self.store.preview_backup(backup)
                self.assertEqual(self.store.snapshot(), before)
                self.assert_unchanged_after_error('backup.restore', {'backup': backup})

    def test_non_string_backup_references_raise_readable_domain_errors(self):
        # [] / {} 是合法 JSON，但不能作为物料 ID。校验应给出业务错误，而非 TypeError。
        for collection in ['bom', 'purchase', 'opening', 'movement']:
            for invalid_id in [[], {}]:
                with self.subTest(collection=collection, invalid_id=invalid_id):
                    backup = copy.deepcopy(self.store.backup())
                    if collection == 'bom':
                        backup['data']['projects'][0]['bom'][0]['materialId'] = invalid_id
                    elif collection == 'purchase':
                        backup['data']['purchaseBatches'][0]['lines'][0]['materialId'] = invalid_id
                    elif collection == 'opening':
                        backup['data']['openingStock'][0]['materialId'] = invalid_id
                    else:
                        self.act('stock.record', {'materialId': RED, 'type': 'receipt', 'quantity': 1, 'reason': '测试坏备份'})
                        backup = copy.deepcopy(self.store.backup())
                        backup['data']['stockMovements'][0]['materialId'] = invalid_id
                    with self.assertRaises(DomainError):
                        self.store.preview_backup(backup)

    def test_backup_rejects_empty_bom_for_active_plan(self):
        backup = copy.deepcopy(self.store.backup())
        backup['data']['projects'][0]['bom'] = []
        with self.assertRaises(DomainError):
            self.store.preview_backup(backup)

    def test_backup_replay_rejects_forged_delta_and_historic_balances(self):
        self.receive_all()
        self.act('project.issue', {'projectId': LAB, 'units': 1})
        for mutation in ['delta', 'before', 'after', 'project_lines', 'issued_units']:
            with self.subTest(mutation=mutation):
                backup = copy.deepcopy(self.store.backup())
                if mutation == 'delta':
                    backup['data']['stockMovements'][0]['delta'] += 1
                elif mutation == 'before':
                    backup['data']['stockMovements'][0]['balanceBefore'] = 10
                elif mutation == 'after':
                    backup['data']['stockMovements'][0]['balanceAfter'] += 1
                elif mutation == 'issued_units':
                    backup['data']['projects'][1]['issuedUnits'] = 0
                else:
                    backup['data']['stockMovements'][-1]['lines'][0]['quantity'] += 1
                with self.assertRaises(DomainError):
                    self.store.preview_backup(backup)

    def test_initial_count_confirms_physical_balance_without_inventing_receipt(self):
        self.act('stock.record', {'materialId': RED, 'type': 'count', 'quantity': 0, 'reason': '确认盒内为空'})
        row = self.stock(RED)
        self.assertEqual(row['actualOnHand'], 0)
        self.assertIsNone(row['received'])  # 点数不虚构采购到货数。
        self.assertEqual(row['ordered'], 20)
        self.assertEqual(row['forecastFree'], 7)
        self.assertEqual(self.store.snapshot()['summary']['notCountedKinds'], 15)
        # 后续按差额纠正已有实物，会同步影响预计可用量，但采购仍原样保留。
        self.act('stock.record', {'materialId': RED, 'type': 'receipt', 'quantity': 20, 'reason': '收到20颗'})
        self.act('stock.record', {'materialId': RED, 'type': 'count', 'quantity': 18, 'reason': '重盘发现少2颗'})
        self.assertEqual(self.stock(RED)['forecastFree'], 5)
        self.assertEqual(self.stock(RED)['actualOnHand'], 18)
        self.assertEqual(self.stock(RED)['ordered'], 20)

    def test_concurrent_connections_cannot_commit_same_request_twice(self):
        payload = {'materialId': RED, 'type': 'receipt', 'quantity': 20, 'reason': '双窗口相同请求'}
        peers = [Warehouse(self.db, SEED_PATH) for _ in range(4)]
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(peer.execute, 'stock.record', payload, 'same-concurrent-request') for peer in peers]
                results = [future.result(timeout=10) for future in futures]
            self.assertTrue(all(result == results[0] for result in results))
            self.assertEqual(self.stock(RED)['actualOnHand'], 20)
            self.assertEqual(len(self.store.snapshot()['data']['stockMovements']), 1)
            self.assertEqual(self.store.snapshot()['data']['revision'], 1)
        finally:
            for peer in peers:
                peer.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
