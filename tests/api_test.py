"""Поведенческие проверки HTTP API смены источника.

Запуск (приложение должно быть запущено, БД будет сброшена через database/reset.sql):

    python3 tests/api_test.py http://localhost:5173

Сброс БД выполняется командой из RESET_DB_CMD (SQL передаётся через stdin).
По умолчанию: локальный psql с переменными PG* из окружения.
Для Docker:
    RESET_DB_CMD="docker compose exec -T db psql -U assessment -d airnet_assessment" \
        python3 tests/api_test.py http://localhost:5173
"""
import concurrent.futures
import http.cookiejar
import json
import os
import shlex
import subprocess
import sys
import unittest
import urllib.error
import urllib.request

BASE = 'http://localhost:5173'
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESET_CMD = os.environ.get('RESET_DB_CMD', 'psql')


def run_sql(sql):
    subprocess.run(shlex.split(RESET_CMD) + ['-q', '-v', 'ON_ERROR_STOP=1'], input=sql, check=True,
                   stdout=subprocess.DEVNULL, cwd=ROOT)


def reset_db():
    with open(os.path.join(ROOT, 'database', 'reset.sql'), 'rb') as sql:
        run_sql(sql.read())


BREAK_HISTORY = b"""
CREATE OR REPLACE FUNCTION test_fail_history() RETURNS trigger LANGUAGE plpgsql AS
$$ BEGIN RAISE EXCEPTION 'secret internal detail'; END $$;
CREATE TRIGGER test_fail_history BEFORE INSERT ON ticket_history FOR EACH ROW EXECUTE FUNCTION test_fail_history();
"""
FIX_HISTORY = b"""
DROP TRIGGER IF EXISTS test_fail_history ON ticket_history;
DROP FUNCTION IF EXISTS test_fail_history();
"""


class Client:
    def __init__(self, role=None):
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.csrf = ''
        if role:
            status, data = self.call('POST', '/demo/login', {'role': role})
            assert status == 200, data
            self.csrf = data['csrf']
            self.employee = data['employee']

    def call(self, method, path, body=None, raw=None):
        payload = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        request = urllib.request.Request(BASE + '/api' + path, data=payload, method=method, headers={
            'Content-Type': 'application/json', 'X-CSRF-Token': self.csrf})
        try:
            response = self.opener.open(request, timeout=20)
            status, text = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, text = error.code, error.read()
        try:
            return status, json.loads(text)
        except ValueError:
            raise AssertionError('Ответ не JSON (%s): %r' % (status, text[:200]))

    def change(self, ticket_id, body=None, raw=None):
        return self.call('POST', '/tickets/%s/source' % ticket_id, body, raw)

    def card(self, ticket_id):
        status, data = self.call('GET', '/tickets/%s' % ticket_id)
        assert status == 200, (status, data)
        return data


class ApiTest(unittest.TestCase):
    def setUp(self):
        reset_db()
        self.editor = Client('editor')
        self.viewer = Client('viewer')

    def assertError(self, response, status):
        code, data = response
        self.assertEqual(code, status, data)
        self.assertIsInstance(data.get('error'), str, data)
        self.assertIsInstance(data.get('message'), str, data)

    def assertUnchanged(self, ticket_id, source_id):
        data = self.editor.card(ticket_id)
        self.assertEqual((data['ticket']['source'] or {}).get('id'), source_id)
        self.assertEqual(data['history'], [])

    # --- Исходная ошибка 3: просмотрщик смог изменить источник ---
    def test_viewer_cannot_change_source(self):
        self.assertError(self.viewer.change(102, {'source_id': 3}), 403)
        self.assertUnchanged(102, 2)

    def test_viewer_forged_client_fields_give_no_rights(self):
        body = {'source_id': 3, 'employee_id': 2, 'employee_name': 'Илья Учебный',
                'role': 'editor', 'permissions': ['FUNC_TICKETS_VIEW', 'FUNC_TICKETS_SOURCE_EDIT']}
        self.assertError(self.viewer.change(102, body), 403)
        self.assertUnchanged(102, 2)

    def test_viewer_rejected_before_ticket_and_input_checks(self):
        self.assertError(self.viewer.change(999, {'source_id': 'x'}), 403)
        self.assertError(self.viewer.change(104, {'source_id': 1}), 403)
        self.assertUnchanged(104, 3)

    def test_viewer_can_read_list_card_history(self):
        status, data = self.viewer.call('GET', '/tickets')
        self.assertEqual(status, 200)
        self.assertEqual([t['id'] for t in data['tickets']], [101, 102, 103, 104, 105])
        self.editor.change(101, {'source_id': 2})
        card = self.viewer.card(101)
        self.assertEqual(card['ticket']['source'], {'id': 2, 'name': 'Личный кабинет'})
        self.assertEqual(len(card['history']), 1)

    # --- Исходная ошибка 2: карточка без источника ---
    def test_ticket_without_source(self):
        card = self.viewer.card(103)
        self.assertEqual(card['ticket']['id'], 103)
        self.assertIsNone(card['ticket']['source'])
        self.assertEqual(card['history'], [])
        _, data = self.viewer.call('GET', '/tickets')
        self.assertIsNone(next(t for t in data['tickets'] if t['id'] == 103)['source_name'])

    def test_set_source_on_ticket_without_source(self):
        status, data = self.editor.change(103, {'source_id': 3})
        self.assertEqual(status, 200, data)
        self.assertEqual(data['ticket']['source']['id'], 3)
        self.assertIsNone(data['history'][0]['old_source_id'])
        self.assertEqual(data['history'][0]['new_source_id'], 3)

    # --- Успешное изменение ---
    def test_editor_changes_source_with_history_from_session(self):
        body = {'source_id': 2, 'employee_id': 1, 'employee_name': 'Анна Примерова'}
        status, data = self.editor.change(101, body)
        self.assertEqual(status, 200, data)
        self.assertEqual(data['ticket']['source'], {'id': 2, 'name': 'Личный кабинет'})
        self.assertEqual(len(data['history']), 1)
        entry = data['history'][0]
        self.assertEqual(entry['employee_id'], 2)
        self.assertEqual(entry['employee_name'], 'Илья Учебный')
        self.assertEqual((entry['old_source_id'], entry['new_source_id']), (1, 2))
        self.assertRegex(entry['created_at'], r'[+-]\d\d(:?\d\d)?$')
        # Карточка, список и история при повторной загрузке отражают изменение.
        self.assertEqual(self.viewer.card(101)['history'], data['history'])
        _, listing = self.viewer.call('GET', '/tickets')
        self.assertEqual(next(t for t in listing['tickets'] if t['id'] == 101)['source_name'], 'Личный кабинет')

    def test_same_source_is_success_without_history(self):
        status, data = self.editor.change(102, {'source_id': 2})
        self.assertEqual(status, 200, data)
        self.assertEqual(data['ticket']['source']['id'], 2)
        self.assertEqual(data['history'], [])
        self.editor.change(102, {'source_id': 3})
        status, data = self.editor.change(102, {'source_id': 3})
        self.assertEqual(status, 200)
        self.assertEqual(len(data['history']), 1)

    # --- Некорректный ввод ---
    def test_invalid_source_values(self):
        cases = [{'source_id': '2'}, {'source_id': 2.5}, {'source_id': 2.0}, {'source_id': None}, {},
                 {'source_id': 0}, {'source_id': -1}, {'source_id': True}, {'source_id': [3]},
                 {'source_id': {'id': 3}}, {'source_id': 999}, {'source_id': 2147483648},
                 {'source_id': 10 ** 30}, {'source_id': 3.0e0}]
        for body in cases:
            with self.subTest(body=body):
                self.assertError(self.editor.change(102, body), 400)
        for raw in [b'not json', b'', b'[3]', b'3', b'{"source_id": 3e0}']:
            with self.subTest(raw=raw):
                self.assertError(self.editor.change(102, raw=raw), 400)
        self.assertUnchanged(102, 2)

    def test_editor_check_order_ticket_then_archive_then_source(self):
        self.assertError(self.editor.change(999, {'source_id': 'bad'}), 404)
        self.assertError(self.editor.change(99999999999, {'source_id': 1}), 404)
        self.assertError(self.editor.change(104, {'source_id': 'bad'}), 403)
        self.assertError(self.editor.change(104, {'source_id': 1}), 403)
        self.assertUnchanged(104, 3)

    def test_archived_readable(self):
        card = self.viewer.card(104)
        self.assertEqual(card['ticket']['status'], 'archived')

    # --- Сессия, CSRF, 404 ---
    def test_no_session(self):
        anon = Client()
        self.assertError(anon.call('GET', '/tickets'), 401)
        self.assertError(anon.call('GET', '/tickets/101'), 401)
        self.assertError(anon.change(101, {'source_id': 2}), 401)
        self.assertUnchanged(101, 1)

    def test_csrf_required(self):
        self.editor.csrf = 'wrong'
        self.assertError(self.editor.change(101, {'source_id': 2}), 403)
        self.assertUnchanged(101, 1)

    def test_missing_ticket(self):
        self.assertError(self.viewer.call('GET', '/tickets/999'), 404)
        self.assertError(self.viewer.call('GET', '/tickets/99999999999'), 404)

    # --- Атомарность: сбой записи истории откатывает изменение источника ---
    def test_history_failure_rolls_back_change(self):
        run_sql(BREAK_HISTORY)
        try:
            code, data = self.editor.change(101, {'source_id': 2})
        finally:
            run_sql(FIX_HISTORY)
        self.assertEqual(code, 500, data)
        self.assertEqual(set(data), {'error', 'message'})
        self.assertNotIn('secret', json.dumps(data, ensure_ascii=False))
        self.assertNotIn('ticket_history', json.dumps(data, ensure_ascii=False))
        self.assertUnchanged(101, 1)
        # После сбоя соединение/транзакция не «залипли»: следующая операция успешна.
        self.assertEqual(self.editor.change(101, {'source_id': 2})[0], 200)

    # --- Параллельные изменения: история остаётся согласованной ---
    def test_concurrent_changes_keep_consistent_history(self):
        editors = [Client('editor') for _ in range(4)]
        targets = [2, 3, 1, 2, 3, 1, 2, 3]

        def run(i):
            return editors[i % len(editors)].change(105, {'source_id': targets[i]})[0]

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            statuses = list(pool.map(run, range(len(targets))))
        self.assertTrue(all(s == 200 for s in statuses), statuses)
        data = self.editor.card(105)
        chain = list(reversed(data['history']))
        previous = 1
        for entry in chain:
            self.assertEqual(entry['old_source_id'], previous, chain)
            self.assertNotEqual(entry['old_source_id'], entry['new_source_id'])
            previous = entry['new_source_id']
        self.assertEqual(data['ticket']['source']['id'], previous)


if __name__ == '__main__':
    if len(sys.argv) > 1 and not sys.argv[1].startswith('-'):
        BASE = sys.argv.pop(1).rstrip('/')
    unittest.main(verbosity=2)
