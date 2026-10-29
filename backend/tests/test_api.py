"""
Testes da API — rodam em SQLite temporário, nunca contra um banco real.

Executar a partir da raiz do projeto:
    python -m unittest backend.tests.test_api -v
"""
import json
import os
import re
import tempfile
import time
import unittest
from pathlib import Path

# Isola o ambiente ANTES de importar a aplicação (o .env real não deve ser usado)
os.environ['DATABASE_URL'] = 'sqlite://'
os.environ['SECRET_KEY'] = 'chave-de-teste'
os.environ.pop('VERCEL', None)

from sqlalchemy import event
from sqlalchemy.engine import Engine

from backend.app import create_app, limiter
from backend.app.models import db, RefreshToken, Task, Notification, TaskCollaborator, User
from backend.config import DevelopmentConfig

ROOT = Path(__file__).resolve().parents[2]
PASSWORD = 'Senha123Forte'


@event.listens_for(Engine, 'connect')
def _enforce_sqlite_fks(dbapi_connection, _):
    # PostgreSQL sempre valida chaves estrangeiras; o SQLite só com este PRAGMA
    if dbapi_connection.__class__.__module__.startswith('sqlite3'):
        dbapi_connection.execute('PRAGMA foreign_keys=ON')


def make_config(db_path, ratelimit=False):
    class TestConfig(DevelopmentConfig):
        TESTING = True
        SQLALCHEMY_DATABASE_URI = f'sqlite:///{db_path}'
        RATELIMIT_ENABLED = ratelimit
    return TestConfig


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.app = create_app(make_config(os.path.join(self._tmp.name, 'test.db')))
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        db.engine.dispose()
        self.ctx.pop()
        self._tmp.cleanup()

    # ── helpers ──
    def register(self, email, username=None):
        body = {'email': email, 'password': PASSWORD}
        if username:
            body['username'] = username
        r = self.client.post('/auth/register', json=body)
        self.assertEqual(r.status_code, 201, r.get_json())
        return r.get_json()

    def login(self, email):
        r = self.client.post('/auth/login', json={'email': email, 'password': PASSWORD})
        self.assertEqual(r.status_code, 200, r.get_json())
        return r.get_json()['tokens']

    @staticmethod
    def hdr(tokens):
        return {'Authorization': f"Bearer {tokens['access_token']}"}

    def user(self, email):
        """Registra e devolve os headers de um usuário."""
        return self.hdr(self.register(email)['tokens'])


class TestAuth(ApiTestCase):
    def test_register_and_login(self):
        reg = self.register('a@example.com', 'alice')
        self.assertEqual(reg['user']['username'], 'alice')
        tokens = self.login('a@example.com')
        me = self.client.get('/auth/me', headers=self.hdr(tokens))
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.get_json()['user']['email'], 'a@example.com')

    def test_register_validations(self):
        r = self.client.post('/auth/register', json={'email': 'x', 'password': PASSWORD})
        self.assertEqual(r.status_code, 400)
        r = self.client.post('/auth/register', json={'email': 'b@example.com', 'password': 'curta1'})
        self.assertEqual(r.status_code, 400)
        self.register('c@example.com')
        r = self.client.post('/auth/register', json={'email': 'c@example.com', 'password': PASSWORD})
        self.assertEqual(r.status_code, 400)

    def test_default_usernames_do_not_collide(self):
        # mesmo prefixo de email -> antes causava IntegrityError (500) por username único
        a = self.register('joao@a.com')['user']['username']
        b = self.register('joao@b.com')['user']['username']
        self.assertNotEqual(a, b)

    def test_wrong_password(self):
        self.register('a@example.com')
        r = self.client.post('/auth/login', json={'email': 'a@example.com', 'password': 'Errada123'})
        self.assertEqual(r.status_code, 401)

    def test_consecutive_logins_have_distinct_refresh_tokens(self):
        # sem 'jti', logins no mesmo segundo geravam tokens idênticos (UNIQUE violation)
        self.register('a@example.com')
        t1 = self.login('a@example.com')
        t2 = self.login('a@example.com')
        t3 = self.login('a@example.com')
        self.assertEqual(len({t1['refresh_token'], t2['refresh_token'], t3['refresh_token']}), 3)
        self.assertEqual(RefreshToken.query.count(), 4)  # registro + 3 logins

    def test_refresh_rotates_and_revokes_old_token(self):
        self.register('a@example.com')
        t = self.login('a@example.com')
        r = self.client.post('/auth/refresh', json={'refresh_token': t['refresh_token']})
        self.assertEqual(r.status_code, 200)
        new = r.get_json()['tokens']
        self.assertNotEqual(new['refresh_token'], t['refresh_token'])
        # novo access token funciona
        self.assertEqual(self.client.get('/auth/me', headers=self.hdr(new)).status_code, 200)
        # o refresh antigo foi consumido e não pode ser reutilizado
        r = self.client.post('/auth/refresh', json={'refresh_token': t['refresh_token']})
        self.assertEqual(r.status_code, 401)
        # o novo continua válido
        r = self.client.post('/auth/refresh', json={'refresh_token': new['refresh_token']})
        self.assertEqual(r.status_code, 200)

    def test_each_token_can_be_refreshed(self):
        self.register('a@example.com')
        t1, t2 = self.login('a@example.com'), self.login('a@example.com')
        for t in (t1, t2):
            r = self.client.post('/auth/refresh', json={'refresh_token': t['refresh_token']})
            self.assertEqual(r.status_code, 200)

    def test_logout_revokes_refresh_token(self):
        self.register('a@example.com')
        t = self.login('a@example.com')
        r = self.client.post('/auth/logout', headers=self.hdr(t), json={'refresh_token': t['refresh_token']})
        self.assertEqual(r.status_code, 200)
        r = self.client.post('/auth/refresh', json={'refresh_token': t['refresh_token']})
        self.assertEqual(r.status_code, 401)

    def test_logout_cannot_revoke_other_users_token(self):
        self.register('a@example.com')
        self.register('b@example.com')
        ta, tb = self.login('a@example.com'), self.login('b@example.com')
        self.client.post('/auth/logout', headers=self.hdr(tb), json={'refresh_token': ta['refresh_token']})
        r = self.client.post('/auth/refresh', json={'refresh_token': ta['refresh_token']})
        self.assertEqual(r.status_code, 200)

    def test_token_types_are_not_interchangeable(self):
        self.register('a@example.com')
        t = self.login('a@example.com')
        # refresh token não acessa a API
        r = self.client.get('/auth/me', headers={'Authorization': f"Bearer {t['refresh_token']}"})
        self.assertEqual(r.status_code, 401)
        # access token não renova sessão
        r = self.client.post('/auth/refresh', json={'refresh_token': t['access_token']})
        self.assertEqual(r.status_code, 401)

    def test_missing_and_invalid_tokens(self):
        self.assertEqual(self.client.get('/tasks').status_code, 401)
        self.assertEqual(self.client.get('/tasks', headers={'Authorization': 'Bearer lixo'}).status_code, 401)
        r = self.client.post('/auth/refresh', json={})
        self.assertEqual(r.status_code, 400)
        r = self.client.post('/auth/refresh', json={'refresh_token': 'lixo'})
        self.assertEqual(r.status_code, 401)

    def test_expired_tokens(self):
        self.register('a@example.com')
        self.app.config['JWT_ACCESS_TOKEN_EXPIRES'] = __import__('datetime').timedelta(seconds=-1)
        self.app.config['JWT_REFRESH_TOKEN_EXPIRES'] = __import__('datetime').timedelta(seconds=-1)
        t = self.login('a@example.com')
        self.assertEqual(self.client.get('/auth/me', headers=self.hdr(t)).status_code, 401)
        r = self.client.post('/auth/refresh', json={'refresh_token': t['refresh_token']})
        self.assertEqual(r.status_code, 401)

    def test_inactive_user_cannot_refresh_or_access(self):
        from backend.app.models import User
        self.register('a@example.com')
        t = self.login('a@example.com')
        User.query.filter_by(email='a@example.com').update({'is_active': False})
        db.session.commit()
        self.assertEqual(self.client.get('/auth/me', headers=self.hdr(t)).status_code, 401)
        r = self.client.post('/auth/refresh', json={'refresh_token': t['refresh_token']})
        self.assertEqual(r.status_code, 401)


class TestTasks(ApiTestCase):
    def test_crud(self):
        h = self.user('a@example.com')
        r = self.client.post('/tasks', headers=h, json={'title': 'Primeira', 'priority': 'high'})
        self.assertEqual(r.status_code, 201)
        tid = r.get_json()['task']['id']
        self.assertEqual(self.client.get(f'/tasks/{tid}', headers=h).status_code, 200)
        r = self.client.put(f'/tasks/{tid}', headers=h, json={'status': 'completed', 'title': 'Nova'})
        self.assertEqual(r.get_json()['task']['status'], 'completed')
        # exclusão de tarefa que possui notificações (FK notifications.task_id)
        self.assertEqual(self.client.delete(f'/tasks/{tid}', headers=h).status_code, 200)
        self.assertEqual(self.client.get(f'/tasks/{tid}', headers=h).status_code, 404)

    def test_validation(self):
        h = self.user('a@example.com')
        for body in ({}, {'title': 'x' * 201}, {'title': 'ok', 'priority': 'x'},
                     {'title': 'ok', 'status': 'x'}, {'title': 'ok', 'due_date': 'ontem'},
                     {'title': 'ok', 'category_id': 9999}, {'title': 'ok', 'category_id': 'abc'}):
            self.assertEqual(self.client.post('/tasks', headers=h, json=body).status_code, 400, body)

    def test_due_date_with_timezone_is_accepted(self):
        h = self.user('a@example.com')
        r = self.client.post('/tasks', headers=h, json={'title': 't', 'due_date': '2030-01-01T10:00:00Z'})
        self.assertEqual(r.status_code, 201, r.get_json())
        r = self.client.post('/tasks', headers=h, json={'title': 't', 'due_date': '2030-01-01T10:00'})
        self.assertEqual(r.status_code, 201)

    def test_filters_search_sort_pagination(self):
        h = self.user('a@example.com')
        for i, (p, s) in enumerate([('high', 'pending'), ('low', 'completed'), ('medium', 'pending')]):
            self.client.post('/tasks', headers=h, json={'title': f'Tarefa {i}', 'priority': p, 'status': s,
                                                         'description': 'compras' if i == 2 else ''})
        get = lambda q: self.client.get('/tasks?' + q, headers=h).get_json()
        self.assertEqual(get('status=pending')['total'], 2)
        self.assertEqual(get('priority=low')['total'], 1)
        self.assertEqual(get('search=compras')['total'], 1)
        self.assertEqual(get('search=TAREFA 1')['total'], 1)
        self.assertEqual(get('sort_by=title&order=asc')['tasks'][0]['title'], 'Tarefa 0')
        page = get('per_page=2&page=2')
        self.assertEqual((len(page['tasks']), page['total_pages'], page['has_prev']), (1, 2, True))
        # parâmetros inválidos não derrubam a API (antes: ValueError -> 500)
        r = self.client.get('/tasks?page=abc&per_page=xx&category_id=zz', headers=h)
        self.assertEqual(r.status_code, 200)

    def test_sort_by_priority_uses_business_order(self):
        h = self.user('a@example.com')
        for p in ['low', 'high', 'medium', 'low', 'high']:
            self.client.post('/tasks', headers=h, json={'title': f't-{p}', 'priority': p})
        get = lambda o: [t['priority'] for t in
                         self.client.get(f'/tasks?sort_by=priority&order={o}', headers=h).get_json()['tasks']]
        self.assertEqual(get('asc'),  ['high', 'high', 'medium', 'low', 'low'])    # Alta → Média → Baixa
        self.assertEqual(get('desc'), ['low', 'low', 'medium', 'high', 'high'])    # Baixa → Média → Alta

    def test_isolation_between_users(self):
        ha, hb = self.user('a@example.com'), self.user('b@example.com')
        tid = self.client.post('/tasks', headers=ha, json={'title': 'Privada'}).get_json()['task']['id']
        self.assertEqual(self.client.get(f'/tasks/{tid}', headers=hb).status_code, 403)
        self.assertEqual(self.client.put(f'/tasks/{tid}', headers=hb, json={'title': 'x'}).status_code, 403)
        self.assertEqual(self.client.delete(f'/tasks/{tid}', headers=hb).status_code, 403)
        self.assertEqual(self.client.get('/tasks', headers=hb).get_json()['total'], 0)
        self.assertEqual(self.client.post(f'/tasks/{tid}/collaborators', headers=hb,
                                          json={'email': 'b@example.com'}).status_code, 403)
        # categoria de outro usuário não pode ser vinculada à tarefa
        cid = self.client.post('/categories', headers=ha, json={'name': 'Minha'}).get_json()['category']['id']
        r = self.client.post('/tasks', headers=hb, json={'title': 'x', 'category_id': cid})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.get('/tasks/stats', headers=hb).get_json()['total'], 0)

    def test_collaboration_roles(self):
        ha, hb, hc = self.user('a@example.com'), self.user('b@example.com'), self.user('c@example.com')
        tid = self.client.post('/tasks', headers=ha, json={'title': 'Compartilhada'}).get_json()['task']['id']
        add = lambda email, role: self.client.post(f'/tasks/{tid}/collaborators', headers=ha,
                                                   json={'email': email, 'role': role})
        self.assertEqual(add('b@example.com', 'viewer').status_code, 201)
        self.assertEqual(add('c@example.com', 'editor').status_code, 201)
        self.assertEqual(add('b@example.com', 'viewer').status_code, 400)  # duplicado
        self.assertEqual(add('nao@existe.com', 'viewer').status_code, 404)
        self.assertEqual(add('a@example.com', 'viewer').status_code, 400)  # dono
        self.assertEqual(add('c@example.com', 'admin').status_code, 400)
        # colaboradores enxergam a tarefa
        self.assertEqual(self.client.get('/tasks', headers=hb).get_json()['total'], 1)
        # viewer não edita; editor edita; nenhum dos dois exclui
        self.assertEqual(self.client.put(f'/tasks/{tid}', headers=hb, json={'title': 'v'}).status_code, 403)
        self.assertEqual(self.client.put(f'/tasks/{tid}', headers=hc, json={'title': 'e'}).status_code, 200)
        self.assertEqual(self.client.delete(f'/tasks/{tid}', headers=hc).status_code, 403)
        # colaborador recebeu notificação
        n = self.client.get('/notifications', headers=hb).get_json()
        self.assertTrue(any(x['type'] == 'task_assignment' for x in n['notifications']))
        # remoção
        uid_b = next(c['user_id'] for c in self.client.get(f'/tasks/{tid}', headers=ha).get_json()['collaborators']
                     if c['email'] == 'b@example.com')
        self.assertEqual(self.client.delete(f'/tasks/{tid}/collaborators/{uid_b}', headers=ha).status_code, 200)
        self.assertEqual(self.client.get(f'/tasks/{tid}', headers=hb).status_code, 403)
        # dono exclui com colaboradores
        self.assertEqual(self.client.delete(f'/tasks/{tid}', headers=ha).status_code, 200)

    def test_shared_task_permissions_used_by_the_form(self):
        """Contrato em que a interface se apoia: estado, categoria do dono e título."""
        ha, hb, hc = self.user('a@example.com'), self.user('b@example.com'), self.user('c@example.com')
        cat = self.client.post('/categories', headers=ha, json={'name': 'Do dono'}).get_json()['category']['id']
        mine = self.client.post('/categories', headers=hc, json={'name': 'Do editor'}).get_json()['category']['id']
        tid = self.client.post('/tasks', headers=ha, json={'title': 't', 'category_id': cat}).get_json()['task']['id']
        for email, role in (('b@example.com', 'viewer'), ('c@example.com', 'editor')):
            self.client.post(f'/tasks/{tid}/collaborators', headers=ha, json={'email': email, 'role': role})
        put = lambda h, **body: self.client.put(f'/tasks/{tid}', headers=h, json=body)
        # leitor: nem o estado (checkbox) nem o resto; a mensagem vem do servidor
        r = put(hb, status='completed')
        self.assertEqual(r.status_code, 403)
        self.assertTrue(r.get_json()['message'])
        # editor altera estado e campos, mas sem enviar category_id a categoria do dono é preservada
        self.assertEqual(put(hc, status='completed', title='novo').status_code, 200)
        self.assertEqual(self.client.get(f'/tasks/{tid}', headers=ha).get_json()['category_id'], cat)
        # categoria do editor não é válida para a tarefa de outro dono
        self.assertEqual(put(hc, category_id=mine).status_code, 400)
        # editor não exclui
        self.assertEqual(self.client.delete(f'/tasks/{tid}', headers=hc).status_code, 403)

    def test_title_length_limits(self):
        h = self.user('a@example.com')
        post = lambda title: self.client.post('/tasks', headers=h, json={'title': title}).status_code
        self.assertEqual(post('x' * 200), 201)
        self.assertEqual(post('x' * 201), 400)
        self.assertEqual(post('   '), 400)
        tid = self.client.post('/tasks', headers=h, json={'title': 't'}).get_json()['task']['id']
        self.assertEqual(self.client.put(f'/tasks/{tid}', headers=h, json={'title': 'x' * 201}).status_code, 400)
        self.assertEqual(self.client.put(f'/tasks/{tid}', headers=h, json={'title': 'x' * 200}).status_code, 200)

    def test_stats(self):
        h = self.user('a@example.com')
        mk = lambda **kw: self.client.post('/tasks', headers=h, json={'title': 't', **kw})
        mk(status='completed'); mk(status='in_progress', priority='high')
        mk(status='pending', due_date='2000-01-01T00:00')
        s = self.client.get('/tasks/stats', headers=h).get_json()
        self.assertEqual((s['total'], s['completed'], s['in_progress'], s['pending'], s['overdue']), (3, 1, 1, 1, 1))
        self.assertEqual(s['completion_rate'], 33.3)
        self.assertEqual(s['priority_breakdown']['high'], 1)


class TestCollaboratorManagement(ApiTestCase):
    """Gestão de partilhas: perfis, remoção, contas inativas e consistência com notificações."""

    def setUp(self):
        super().setUp()
        self.ha = self.user('dono@example.com')
        self.hb = self.user('editor@example.com')
        self.hc = self.user('leitor@example.com')
        self.hx = self.user('terceiro@example.com')
        self.tid = self.client.post('/tasks', headers=self.ha, json={'title': 'Partilhada'}).get_json()['task']['id']
        self.add('editor@example.com', 'editor')
        self.add('leitor@example.com', 'viewer')
        self.uid = {c['email']: c['user_id'] for c in
                    self.client.get(f'/tasks/{self.tid}', headers=self.ha).get_json()['collaborators']}

    def add(self, email, role, headers=None):
        return self.client.post(f'/tasks/{self.tid}/collaborators', headers=headers or self.ha,
                                json={'email': email, 'role': role})

    def put_role(self, email, role, headers=None, task_id=None, user_id=None):
        return self.client.put(f'/tasks/{task_id or self.tid}/collaborators/{user_id or self.uid[email]}',
                               headers=headers or self.ha, json={'role': role})

    def notif_types(self, headers):
        return [n['type'] for n in self.client.get('/notifications', headers=headers).get_json()['notifications']]

    def count_collabs(self):
        return TaskCollaborator.query.filter_by(task_id=self.tid).count()

    # ── alteração de perfil ──
    def test_owner_changes_role_and_effective_permissions_follow(self):
        edit = lambda h: self.client.put(f'/tasks/{self.tid}', headers=h, json={'status': 'in_progress'}).status_code
        self.assertEqual(edit(self.hc), 403)                       # leitor
        r = self.put_role('leitor@example.com', 'editor')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()['collaborator']                        # mesmo formato do POST
        self.assertEqual(set(body), {'id', 'task_id', 'user_id', 'username', 'email', 'role'})
        self.assertEqual(body['role'], 'editor')
        self.assertEqual(edit(self.hc), 200)                       # agora edita, de imediato
        self.assertEqual(self.put_role('leitor@example.com', 'viewer').status_code, 200)
        self.assertEqual(edit(self.hc), 403)                       # voltou a só leitura
        # o perfil novo está nos dados da tarefa
        roles = {c['email']: c['role'] for c in
                 self.client.get(f'/tasks/{self.tid}', headers=self.ha).get_json()['collaborators']}
        self.assertEqual(roles['leitor@example.com'], 'viewer')

    def test_only_owner_can_manage_shares(self):
        for h in (self.hb, self.hc, self.hx):                      # editor, leitor, terceiro
            self.assertEqual(self.put_role('leitor@example.com', 'editor', headers=h).status_code, 403)
            self.assertEqual(self.add('terceiro@example.com', 'viewer', headers=h).status_code, 403)
            self.assertEqual(self.client.delete(f'/tasks/{self.tid}/collaborators/{self.uid["leitor@example.com"]}',
                                                headers=h).status_code, 403)
        self.assertEqual(self.count_collabs(), 2)                  # nada mudou
        self.assertEqual(self.client.get(f'/tasks/{self.tid}', headers=self.ha).get_json()['collaborators'][1]['role'],
                         'viewer')

    def test_invalid_roles_and_missing_resources(self):
        for bad in ('owner', 'admin', '', None, 'EDITOR', 1):
            self.assertEqual(self.put_role('leitor@example.com', bad).status_code, 400, bad)
        self.assertEqual(self.client.put(f'/tasks/{self.tid}/collaborators/{self.uid["leitor@example.com"]}',
                                         headers=self.ha).status_code, 400)       # sem corpo
        # nenhum desses pedidos alterou o perfil
        self.assertTrue(all(c['role'] in ('viewer', 'editor') for c in
                            self.client.get(f'/tasks/{self.tid}', headers=self.ha).get_json()['collaborators']))
        # partilha inexistente (utilizador sem partilha) e tarefa inexistente
        other = User.query.filter_by(email='terceiro@example.com').first().id
        self.assertEqual(self.put_role(None, 'editor', user_id=other).status_code, 404)
        self.assertEqual(self.put_role('leitor@example.com', 'editor', task_id=99999).status_code, 404)
        # o dono não é colaborador: não pode ser "promovido" nem "despromovido" por esta via
        owner_id = User.query.filter_by(email='dono@example.com').first().id
        self.assertEqual(self.put_role(None, 'editor', user_id=owner_id).status_code, 404)

    def test_cannot_manage_shares_of_someone_elses_task(self):
        other = self.client.post('/tasks', headers=self.hx, json={'title': 'Alheia'}).get_json()['task']['id']
        r = self.client.post(f'/tasks/{other}/collaborators', headers=self.ha, json={'email': 'leitor@example.com'})
        self.assertEqual(r.status_code, 403)
        r = self.client.put(f'/tasks/{other}/collaborators/{self.uid["leitor@example.com"]}',
                            headers=self.ha, json={'role': 'editor'})
        self.assertIn(r.status_code, (403, 404))

    # ── nome do dono ──
    def test_owner_name_visible_to_owner_and_collaborators_without_email(self):
        owner_id = User.query.filter_by(email='dono@example.com').first().id
        owner_name = User.query.filter_by(email='dono@example.com').first().username
        for h in (self.ha, self.hb, self.hc):                      # dono, editor, leitor
            detail = self.client.get(f'/tasks/{self.tid}', headers=h).get_json()
            self.assertEqual(detail['owner'], {'id': owner_id, 'username': owner_name})
            listed = self.client.get('/tasks', headers=h).get_json()['tasks'][0]
            self.assertEqual(listed['owner'], {'id': owner_id, 'username': owner_name})
            self.assertEqual(listed['user_id'], owner_id)          # campos existentes mantêm-se
            self.assertNotIn('dono@example.com', json.dumps(detail['owner']))
        # nenhum dado de email do dono em lado nenhum do payload da lista do colaborador
        self.assertNotIn('dono@example.com', json.dumps(self.client.get('/tasks', headers=self.hb).get_json()))
        # quem não tem acesso continua sem ver nada
        self.assertEqual(self.client.get(f'/tasks/{self.tid}', headers=self.hx).status_code, 403)
        self.assertEqual(self.client.get('/tasks', headers=self.hx).get_json()['total'], 0)

    def test_owner_field_tolerates_missing_owner(self):
        from unittest import mock
        with mock.patch.object(Task, 'owner', None):               # dados incompletos: dono não carregável
            r = self.client.get(f'/tasks/{self.tid}', headers=self.ha)
            self.assertEqual(r.status_code, 200)
            self.assertIsNone(r.get_json()['owner'])
            self.assertEqual(self.client.get('/tasks', headers=self.hb).status_code, 200)

    # ── remoção ──
    def test_removed_user_loses_access(self):
        uid = self.uid['editor@example.com']
        self.assertEqual(self.client.put(f'/tasks/{self.tid}', headers=self.hb, json={'title': 'a'}).status_code, 200)
        self.assertEqual(self.client.delete(f'/tasks/{self.tid}/collaborators/{uid}', headers=self.ha).status_code, 200)
        self.assertEqual(self.client.get(f'/tasks/{self.tid}', headers=self.hb).status_code, 403)
        self.assertEqual(self.client.put(f'/tasks/{self.tid}', headers=self.hb, json={'title': 'b'}).status_code, 403)
        self.assertEqual(self.client.get('/tasks', headers=self.hb).get_json()['total'], 0)
        self.assertEqual(self.client.delete(f'/tasks/{self.tid}/collaborators/{uid}', headers=self.ha).status_code, 404)

    # ── contas inativas, duplicados, auto-partilha ──
    def test_inactive_account_cannot_be_added(self):
        self.user('inativo@example.com')
        User.query.filter_by(email='inativo@example.com').update({'is_active': False})
        db.session.commit()
        r = self.add('inativo@example.com', 'viewer')
        ghost = self.add('nao-existe@example.com', 'viewer')
        self.assertEqual(r.status_code, 404)
        # mesma resposta que um email inexistente: não revela qual dos casos é
        self.assertEqual((r.status_code, r.get_json()), (ghost.status_code, ghost.get_json()))
        self.assertEqual(self.count_collabs(), 2)

    def test_duplicates_self_share_and_invalid_input(self):
        self.assertEqual(self.add('editor@example.com', 'viewer').status_code, 400)   # duplicado
        self.assertEqual(self.add('DONO@example.com', 'viewer').status_code, 400)     # o próprio (case-insensitive)
        self.assertEqual(self.add('', 'viewer').status_code, 400)
        self.assertEqual(self.add('terceiro@example.com', 'owner').status_code, 400)
        self.assertEqual(self.count_collabs(), 2)

    # ── notificações ──
    def test_notifications_for_add_role_change_and_removal(self):
        self.assertIn('task_assignment', self.notif_types(self.hc))
        self.assertNotIn('task_permission_changed', self.notif_types(self.hc))
        self.put_role('leitor@example.com', 'editor')
        self.assertIn('task_permission_changed', self.notif_types(self.hc))
        n = [x for x in self.client.get('/notifications', headers=self.hc).get_json()['notifications']
             if x['type'] == 'task_permission_changed'][0]
        self.assertIn('editor', n['message'])
        self.assertEqual(n['task_id'], self.tid)
        # repetir o mesmo perfil não gera outra notificação
        before = len(self.notif_types(self.hc))
        self.assertEqual(self.put_role('leitor@example.com', 'editor').status_code, 200)
        self.assertEqual(len(self.notif_types(self.hc)), before)
        self.client.delete(f'/tasks/{self.tid}/collaborators/{self.uid["leitor@example.com"]}', headers=self.ha)
        self.assertIn('task_access_removed', self.notif_types(self.hc))
        # quem gere não recebe as notificações dos outros
        self.assertNotIn('task_permission_changed', self.notif_types(self.ha))

    def test_notification_failure_leaves_no_partial_state(self):
        from unittest import mock
        boom = mock.Mock(side_effect=RuntimeError('falha ao notificar'))
        with mock.patch('backend.app.routes.tasks.create_notification', boom):
            r = self.add('terceiro@example.com', 'viewer')
            self.assertEqual(r.status_code, 500)
            self.assertTrue(r.get_json()['message'])
            self.assertEqual(self.put_role('leitor@example.com', 'editor').status_code, 500)
            self.assertEqual(self.client.delete(f'/tasks/{self.tid}/collaborators/{self.uid["leitor@example.com"]}',
                                                headers=self.ha).status_code, 500)
        # nada ficou gravado: continua tudo como antes
        roles = {c['email']: c['role'] for c in
                 self.client.get(f'/tasks/{self.tid}', headers=self.ha).get_json()['collaborators']}
        self.assertEqual(roles, {'editor@example.com': 'editor', 'leitor@example.com': 'viewer'})
        self.assertEqual(self.client.get('/tasks', headers=self.hx).get_json()['total'], 0)
        # e a API continua utilizável (sessão sem transação presa)
        self.assertEqual(self.add('terceiro@example.com', 'viewer').status_code, 201)
        self.assertIn('task_assignment', self.notif_types(self.hx))

    def test_commit_failure_rolls_back_share_and_notification(self):
        from unittest import mock
        real_commit = db.session.commit
        calls = {'n': 0}

        def flaky():
            calls['n'] += 1
            raise RuntimeError('commit falhou')
        with mock.patch.object(db.session, 'commit', side_effect=flaky):
            r = self.add('terceiro@example.com', 'viewer')
        self.assertEqual(r.status_code, 500)
        self.assertEqual(calls['n'], 1)                              # um único commit: sem commits parciais
        self.assertEqual(self.count_collabs(), 2)
        self.assertEqual(self.notif_types(self.hx), [])              # nem a notificação ficou gravada


class TestNotificationDeletion(ApiTestCase):
    """Remoção individual de notificações (a interface usa DELETE /notifications/<id>)."""

    def setUp(self):
        super().setUp()
        self.ha, self.hb = self.user('a@example.com'), self.user('b@example.com')
        for i in range(3):                               # 3 notificações de A ("Tarefa criada")
            self.client.post('/tasks', headers=self.ha, json={'title': f't{i}'})
        self.client.post('/tasks', headers=self.hb, json={'title': 'de B'})

    def listing(self, h):
        return self.client.get('/notifications', headers=h).get_json()

    def test_delete_only_that_notification_and_unread_count_follows(self):
        data = self.listing(self.ha)
        ids = [n['id'] for n in data['notifications']]
        self.assertEqual((len(ids), data['unread_count']), (3, 3))
        self.assertEqual(self.client.delete(f'/notifications/{ids[0]}', headers=self.ha).status_code, 200)
        after = self.listing(self.ha)
        self.assertEqual(sorted(n['id'] for n in after['notifications']), sorted(ids[1:]))
        self.assertEqual(after['unread_count'], 2)
        # repetir devolve 404 (já removida), sem afetar as restantes
        self.assertEqual(self.client.delete(f'/notifications/{ids[0]}', headers=self.ha).status_code, 404)
        self.assertEqual(len(self.listing(self.ha)['notifications']), 2)

    def test_cannot_delete_someone_elses_notification(self):
        mine = self.listing(self.ha)['notifications'][0]['id']
        theirs = self.listing(self.hb)['notifications'][0]['id']
        self.assertEqual(self.client.delete(f'/notifications/{mine}', headers=self.hb).status_code, 404)
        self.assertEqual(self.client.delete(f'/notifications/{theirs}', headers=self.ha).status_code, 404)
        self.assertEqual(self.client.delete(f'/notifications/{mine}').status_code, 401)   # sem token
        self.assertEqual(len(self.listing(self.ha)['notifications']), 3)
        self.assertEqual(len(self.listing(self.hb)['notifications']), 1)

    def test_mark_read_and_delete_are_independent(self):
        ids = [n['id'] for n in self.listing(self.ha)['notifications']]
        self.assertEqual(self.client.post(f'/notifications/{ids[0]}/read', headers=self.ha).status_code, 200)
        data = self.listing(self.ha)
        self.assertEqual(data['unread_count'], 2)
        self.assertEqual(len(data['notifications']), 3)             # marcar como lida não remove
        self.client.delete(f'/notifications/{ids[1]}', headers=self.ha)  # remover uma não lida
        data = self.listing(self.ha)
        self.assertEqual((len(data['notifications']), data['unread_count']), (2, 1))
        self.assertEqual(self.client.post(f'/notifications/{ids[2]}/read', headers=self.ha).status_code, 200)
        self.assertEqual(self.client.post(f'/notifications/{ids[1]}/read', headers=self.ha).status_code, 404)
        self.assertEqual(self.listing(self.ha)['unread_count'], 0)


class TestCategoriesAndNotifications(ApiTestCase):
    def test_categories(self):
        ha, hb = self.user('a@example.com'), self.user('b@example.com')
        r = self.client.post('/categories', headers=ha, json={'name': 'Trabalho', 'color': '#ff0000'})
        self.assertEqual(r.status_code, 201)
        cid = r.get_json()['category']['id']
        self.assertEqual(self.client.post('/categories', headers=ha, json={'name': 'Trabalho'}).status_code, 400)
        self.assertEqual(self.client.post('/categories', headers=ha, json={'name': ''}).status_code, 400)
        self.assertEqual(self.client.post('/categories', headers=ha, json={'name': 'x' * 81}).status_code, 400)
        self.assertEqual(self.client.post('/categories', headers=ha,
                                          json={'name': 'c', 'color': '#' + '0' * 30}).status_code, 400)
        self.assertEqual(self.client.put(f'/categories/{cid}', headers=hb, json={'name': 'x'}).status_code, 403)
        self.assertEqual(self.client.delete(f'/categories/{cid}', headers=hb).status_code, 403)
        self.assertEqual(self.client.get('/categories', headers=hb).get_json(), [])
        # vincular tarefa; excluir categoria desvincula a tarefa
        tid = self.client.post('/tasks', headers=ha, json={'title': 't', 'category_id': cid}).get_json()['task']['id']
        self.assertEqual(self.client.get('/categories', headers=ha).get_json()[0]['task_count'], 1)
        self.assertEqual(self.client.put(f'/categories/{cid}', headers=ha, json={'name': 'Novo'}).status_code, 200)
        self.assertEqual(self.client.delete(f'/categories/{cid}', headers=ha).status_code, 200)
        self.assertIsNone(self.client.get(f'/tasks/{tid}', headers=ha).get_json()['category_id'])

    def test_notifications(self):
        ha, hb = self.user('a@example.com'), self.user('b@example.com')
        self.client.post('/tasks', headers=ha, json={'title': 't'})
        n = self.client.get('/notifications', headers=ha).get_json()
        self.assertEqual(n['unread_count'], 1)
        nid = n['notifications'][0]['id']
        self.assertEqual(self.client.get('/notifications?limit=abc', headers=ha).status_code, 200)
        # outro usuário não acessa
        self.assertEqual(self.client.post(f'/notifications/{nid}/read', headers=hb).status_code, 404)
        self.assertEqual(self.client.delete(f'/notifications/{nid}', headers=hb).status_code, 404)
        self.assertEqual(self.client.post(f'/notifications/{nid}/read', headers=ha).status_code, 200)
        self.assertEqual(self.client.get('/notifications?unread_only=true', headers=ha).get_json()['unread_count'], 0)
        self.client.post('/tasks', headers=ha, json={'title': 't2'})
        self.assertEqual(self.client.post('/notifications/read-all', headers=ha).status_code, 200)
        self.assertEqual(self.client.delete(f'/notifications/{nid}', headers=ha).status_code, 200)


# PNG 1x1 válido
PNG = bytes.fromhex('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489'
                    '0000000d49444154789c6360f8cfc0f01f0005000201a5f645400000000049454e44ae426082')
JPEG = b'\xff\xd8\xff\xe0' + b'0' * 50
WEBP = b'RIFF\x10\x00\x00\x00WEBPVP8 ' + b'0' * 10
GIF = b'GIF89a' + b'0' * 20


class TestTimezone(ApiTestCase):
    """Instantes são guardados em UTC (naive) e serializados com 'Z'."""

    def test_timestamps_are_serialized_as_utc_with_z(self):
        from datetime import datetime, timezone
        h = self.user('a@example.com')
        before = datetime.now(timezone.utc).replace(microsecond=0)
        t = self.client.post('/tasks', headers=h, json={'title': 't'}).get_json()['task']
        after = datetime.now(timezone.utc)
        for field in ('created_at', 'updated_at'):
            self.assertTrue(t[field].endswith('Z'), t[field])
            parsed = datetime.fromisoformat(t[field])      # aware (UTC)
            self.assertTrue(before <= parsed <= after, (field, t[field]))
        n = self.client.get('/notifications', headers=h).get_json()['notifications'][0]
        self.assertTrue(n['created_at'].endswith('Z'))

    def test_due_date_is_an_utc_instant(self):
        h = self.user('a@example.com')
        mk = lambda due: self.client.post('/tasks', headers=h, json={'title': 't', 'due_date': due}).get_json()['task']['due_date']
        self.assertEqual(mk('2030-01-01T13:00:00Z'), '2030-01-01T13:00:00Z')
        self.assertEqual(mk('2030-01-01T10:00:00-03:00'), '2030-01-01T13:00:00Z')   # Brasília -> UTC
        self.assertEqual(mk('2030-01-01T13:00'), '2030-01-01T13:00:00Z')            # sem fuso = UTC

    def test_create_read_update(self):
        h = self.user('a@example.com')
        t = self.client.post('/tasks', headers=h, json={'title': 't', 'due_date': '2030-06-01T15:00:00Z'}).get_json()['task']
        got = self.client.get(f"/tasks/{t['id']}", headers=h).get_json()
        self.assertEqual((got['created_at'], got['due_date']), (t['created_at'], '2030-06-01T15:00:00Z'))
        time.sleep(1.1)
        up = self.client.put(f"/tasks/{t['id']}", headers=h,
                             json={'due_date': '2030-06-02T09:30:00-03:00'}).get_json()['task']
        self.assertEqual(up['due_date'], '2030-06-02T12:30:00Z')
        self.assertEqual(up['created_at'], t['created_at'])
        self.assertGreater(up['updated_at'], t['updated_at'])

    def test_overdue_uses_utc(self):
        from datetime import datetime, timedelta, timezone
        h = self.user('a@example.com')
        fmt = lambda d: d.strftime('%Y-%m-%dT%H:%M:%SZ')
        now = datetime.now(timezone.utc)
        past = self.client.post('/tasks', headers=h, json={'title': 'p', 'due_date': fmt(now - timedelta(minutes=5))})
        future = self.client.post('/tasks', headers=h, json={'title': 'f', 'due_date': fmt(now + timedelta(hours=2))})
        self.assertTrue(past.get_json()['task']['is_overdue'])
        self.assertFalse(future.get_json()['task']['is_overdue'])


class TestAvatar(ApiTestCase):
    def put(self, h, data, ctype='image/png'):
        return self.client.put('/auth/avatar', headers={**h, 'Content-Type': ctype}, data=data)

    def test_upload_download_replace_delete(self):
        h = self.user('a@example.com')
        self.assertEqual(self.client.get('/auth/avatar', headers=h).status_code, 404)
        self.assertFalse(self.client.get('/auth/me', headers=h).get_json()['user']['has_avatar'])

        r = self.put(h, PNG)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertTrue(r.get_json()['user']['has_avatar'])
        got = self.client.get('/auth/avatar', headers=h)
        self.assertEqual((got.status_code, got.mimetype, got.data), (200, 'image/png', PNG))
        self.assertEqual(got.headers['X-Content-Type-Options'], 'nosniff')

        # troca (o tipo vem dos bytes, não do cabeçalho)
        self.assertEqual(self.put(h, JPEG, 'application/octet-stream').status_code, 200)
        got = self.client.get('/auth/avatar', headers=h)
        self.assertEqual((got.mimetype, got.data), ('image/jpeg', JPEG))
        for img, mime in ((WEBP, 'image/webp'), (GIF, 'image/gif')):
            self.assertEqual(self.put(h, img).status_code, 200)
            self.assertEqual(self.client.get('/auth/avatar', headers=h).mimetype, mime)
        from backend.app.models import UserAvatar
        self.assertEqual(UserAvatar.query.count(), 1)   # uma linha por usuário

        r = self.client.delete('/auth/avatar', headers=h)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()['user']['has_avatar'])
        self.assertEqual(self.client.get('/auth/avatar', headers=h).status_code, 404)

    def test_persists_after_logout_login_and_backend_restart(self):
        h = self.user('a@example.com')
        self.put(h, PNG)
        t = self.login('a@example.com')
        self.client.post('/auth/logout', headers=self.hdr(t), json={'refresh_token': t['refresh_token']})
        # "reinicia" o backend: nova aplicação sobre o mesmo banco
        db.session.remove(); db.engine.dispose()
        app2 = create_app(make_config(os.path.join(self._tmp.name, 'test.db')))
        with app2.app_context():
            c2 = app2.test_client()
            time.sleep(1.1)
            t2 = c2.post('/auth/login', json={'email': 'a@example.com', 'password': PASSWORD}).get_json()['tokens']
            self.assertTrue(c2.get('/auth/me', headers=self.hdr(t2)).get_json()['user']['has_avatar'])
            self.assertEqual(c2.get('/auth/avatar', headers=self.hdr(t2)).data, PNG)
            db.session.remove(); db.engine.dispose()

    def test_validation_errors(self):
        h = self.user('a@example.com')
        self.assertEqual(self.put(h, b'').status_code, 400)
        self.assertEqual(self.put(h, b'texto qualquer').status_code, 400)
        self.assertEqual(self.put(h, b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
                                  'image/svg+xml').status_code, 400)
        self.assertEqual(self.put(h, b'%PDF-1.4 ...', 'image/png').status_code, 400)  # cabeçalho mente
        self.assertEqual(self.put(h, PNG + b'0' * (2 * 1024 * 1024)).status_code, 413)
        self.assertEqual(self.client.get('/auth/avatar', headers=h).status_code, 404)  # nada foi gravado

    def test_authentication_and_isolation(self):
        self.assertEqual(self.client.get('/auth/avatar').status_code, 401)
        self.assertEqual(self.client.put('/auth/avatar', data=PNG).status_code, 401)
        self.assertEqual(self.client.delete('/auth/avatar').status_code, 401)
        ha, hb = self.user('a@example.com'), self.user('b@example.com')
        self.put(ha, PNG)
        # B não vê a foto de A, e alterar/remover só afeta o próprio usuário
        self.assertEqual(self.client.get('/auth/avatar', headers=hb).status_code, 404)
        self.assertEqual(self.client.get('/auth/avatar?user_id=1', headers=hb).status_code, 404)
        self.put(hb, JPEG)
        self.client.delete('/auth/avatar', headers=hb)
        got = self.client.get('/auth/avatar', headers=ha)
        self.assertEqual((got.status_code, got.data), (200, PNG))
        # refresh token não serve para acessar a foto
        t = self.login('a@example.com')
        r = self.client.get('/auth/avatar', headers={'Authorization': f"Bearer {t['refresh_token']}"})
        self.assertEqual(r.status_code, 401)

    def test_avatar_does_not_leak_in_task_or_collaborator_payloads(self):
        ha, hb = self.user('a@example.com'), self.user('b@example.com')
        self.put(ha, PNG)
        tid = self.client.post('/tasks', headers=hb, json={'title': 't'}).get_json()['task']['id']
        r = self.client.post(f'/tasks/{tid}/collaborators', headers=hb, json={'email': 'a@example.com'})
        self.assertEqual(set(r.get_json()['collaborator']),
                         {'id', 'task_id', 'user_id', 'username', 'email', 'role'})


class TestRateLimit(unittest.TestCase):
    def test_login_rate_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(make_config(os.path.join(tmp, 'rl.db'), ratelimit=True))
            limiter.reset()
            with app.app_context():
                db.create_all()
                client = app.test_client()
                codes = [client.post('/auth/login', json={'email': 'x@example.com', 'password': 'Senha123'}).status_code
                         for _ in range(6)]
                db.session.remove(); db.engine.dispose()
            limiter.reset()
        self.assertEqual(codes[:5], [401] * 5)
        self.assertEqual(codes[5], 429)


class TestRateLimitPreflight(unittest.TestCase):
    PREFLIGHT = {'Origin': 'http://localhost:5500',
                 'Access-Control-Request-Method': 'GET',
                 'Access-Control-Request-Headers': 'authorization'}

    def test_options_exempt_but_real_requests_limited(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(make_config(os.path.join(tmp, 'pf.db'), ratelimit=True))
            limiter.reset()
            with app.app_context():
                db.create_all()
                client = app.test_client()
                options = [client.options('/tasks', headers=self.PREFLIGHT).status_code for _ in range(60)]
                # Os 60 preflights não gastaram o limite de 50/hora: os GET reais ainda têm as 50 vagas
                gets = [client.get('/tasks').status_code for _ in range(51)]
                db.session.remove(); db.engine.dispose()
            limiter.reset()
        self.assertEqual(set(options), {200})
        self.assertEqual(gets[:50], [401] * 50)   # sem token: 401, mas dentro do limite
        self.assertEqual(gets[50], 429)           # o 51º GET real é bloqueado


class TestConfigAndDeploy(unittest.TestCase):
    def _run(self, code, **env):
        import subprocess, sys
        e = {k: v for k, v in os.environ.items() if k not in ('SECRET_KEY', 'VERCEL', 'DATABASE_URL')}
        e.update(env)
        return subprocess.run([sys.executable, '-c', code], cwd=ROOT, env=e, capture_output=True, text=True)

    def test_dev_app_starts_and_root_responds(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._run("from backend.app import create_app; a=create_app(); "
                          "print(a.test_client().get('/').status_code, a.config['DEBUG'])",
                          DATABASE_URL='sqlite://', DEV_DATABASE_URL=f'sqlite:///{tmp}/d.db')
        self.assertEqual(r.stdout.split(), ['200', 'True'], r.stderr[-300:])

    def test_production_without_secret_key_fails(self):
        r = self._run("from backend.app import create_app; create_app()",
                      DATABASE_URL='postgresql://u:p@ep-x-pooler.sa-east-1.aws.neon.tech/db')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('SECRET_KEY', r.stderr)

    def test_production_config_for_neon(self):
        r = self._run("from backend.app import create_app; a=create_app(); import json; "
                      "print(json.dumps([a.config['SQLALCHEMY_DATABASE_URI'], a.config['SQLALCHEMY_ENGINE_OPTIONS']]))",
                      DATABASE_URL='postgres://u:p@ep-x-pooler.sa-east-1.aws.neon.tech/db?channel_binding=require',
                      SECRET_KEY='x', DB_POOL_SIZE='2', DB_MAX_OVERFLOW='0')  # isola do backend/.env local
        uri, opts = json.loads(r.stdout)
        self.assertTrue(uri.startswith('postgresql://'))
        self.assertIn('sslmode=require', uri)
        self.assertEqual(opts['connect_args']['sslmode'], 'require')
        self.assertLessEqual(opts['pool_size'], 5)
        self.assertTrue(opts['pool_pre_ping'])

    def test_vercel_without_postgres_url_fails_clearly(self):
        r = self._run("from backend.app import create_app; create_app()", VERCEL='1', SECRET_KEY='x')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('DATABASE_URL', r.stderr)

    def test_vercel_json_and_entrypoint(self):
        cfg = json.loads((ROOT / 'vercel.json').read_text())
        srcs = [b['src'] for b in cfg['builds']]
        self.assertIn('backend/wsgi.py', srcs)
        self.assertTrue((ROOT / 'backend/wsgi.py').exists())
        self.assertIn('app = create_app()', (ROOT / 'backend/wsgi.py').read_text())
        api_route = next(r for r in cfg['routes'] if r['dest'] == 'backend/wsgi.py')
        for path in ('/auth/login', '/tasks', '/tasks/1', '/categories', '/notifications/read-all'):
            self.assertTrue(re.match(api_route['src'] + '$', path), path)
        for page in ('frontend/auth.html', 'frontend/index.html', 'frontend/script.js', 'frontend/style.css'):
            self.assertTrue((ROOT / page).exists(), page)


class TestFrontendCompatibility(ApiTestCase):
    def test_frontend_endpoints_exist_in_backend(self):
        js = (ROOT / 'frontend/script.js').read_text() + (ROOT / 'frontend/auth.html').read_text()
        found = set(re.findall(r"""(?:apiRequest\(|\$\{API\})[`'"]?(/[a-z/_\-]+)""", js))
        self.assertTrue({'/tasks', '/tasks/stats', '/categories', '/notifications', '/auth/login',
                         '/auth/register', '/auth/refresh', '/auth/logout'} <= found, found)
        rules = [r.rule for r in self.app.url_map.iter_rules()]
        for ep in found:
            base = ep.rstrip('/')
            self.assertTrue(any(r == base or r.startswith(base + '/') for r in rules), ep)

    def test_response_shapes_used_by_frontend(self):
        reg = self.register('a@example.com')
        self.assertEqual({'access_token', 'refresh_token', 'expires_in'}, set(reg['tokens']))
        self.assertIn('username', reg['user'])
        h = self.hdr(reg['tokens'])
        self.client.post('/tasks', headers=h, json={'title': 't'})
        t = self.client.get('/tasks', headers=h).get_json()
        for k in ('tasks', 'total', 'page', 'total_pages', 'has_next', 'has_prev'):
            self.assertIn(k, t)
        s = self.client.get('/tasks/stats', headers=h).get_json()
        for k in ('total', 'completed', 'in_progress', 'pending', 'overdue', 'completion_rate'):
            self.assertIn(k, s)
        self.assertIn('notifications', self.client.get('/notifications', headers=h).get_json())
        self.assertIsInstance(self.client.get('/categories', headers=h).get_json(), list)
        r = self.client.post('/tasks', headers=h, json={})
        self.assertIn('message', r.get_json())  # frontend lê data.message


class TestMigrations(unittest.TestCase):
    def test_migration_creates_schema_matching_models(self):
        from alembic.autogenerate import compare_metadata
        from alembic.migration import MigrationContext
        from flask_migrate import upgrade, downgrade

        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(make_config(os.path.join(tmp, 'mig.db')))
            with app.app_context():
                upgrade(directory=str(ROOT / 'backend/migrations'))
                tables = set(db.inspect(db.engine).get_table_names())
                self.assertEqual(tables, {'users', 'categories', 'refresh_tokens', 'tasks',
                                          'notifications', 'task_collaborators', 'user_avatars',
                                          'alembic_version'})
                with db.engine.connect() as conn:
                    diff = compare_metadata(MigrationContext.configure(conn), db.metadata)
                self.assertEqual(diff, [], f'Modelos e migrações divergem: {diff}')
                downgrade(directory=str(ROOT / 'backend/migrations'), revision='base')
                db.session.remove(); db.engine.dispose()

    def test_migrations_are_versionable(self):
        import subprocess
        files = list((ROOT / 'backend/migrations/versions').glob('*.py'))
        self.assertTrue(files)
        r = subprocess.run(['git', 'check-ignore', str(files[0])], cwd=ROOT, capture_output=True)
        self.assertEqual(r.returncode, 1, 'migração está sendo ignorada pelo git')


if __name__ == '__main__':
    unittest.main()
