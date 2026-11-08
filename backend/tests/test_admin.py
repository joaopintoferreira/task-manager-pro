"""
Testes do painel administrativo (papéis, autorização, auditoria, perfil, limpeza, exclusão e limpeza global).

Rodam em SQLite temporário, nunca contra um banco real. LIMITAÇÃO: o SQLite ignora `SELECT ... FOR UPDATE`;
estes testes validam a LÓGICA (incluindo a releitura do ator e a proteção do último SUPERADMIN), mas a
concorrência real entre dois administradores só se prova num PostgreSQL.

Executar a partir da raiz do projeto:
    python -m unittest backend.tests.test_admin -v
"""
import json
import os
import tempfile
import types
from datetime import datetime, timedelta
from unittest import mock

from sqlalchemy import text, select, func

from backend.app import create_app
from backend.app.models import (db, User, Task, Category, Notification, TaskCollaborator, RefreshToken,
                                UserAvatar, AdminAuditLog)
from backend.app.services import audit, maintenance, permissions, purge, user_admin
from backend.app.cli import promote_superadmin
from backend.tests.test_api import ApiTestCase, make_config, ROOT, PASSWORD

PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 40


class AdminTestCase(ApiTestCase):
    def setUp(self):
        super().setUp()
        permissions.reset_reauth_throttle()

    def make(self, email, role='user', username=None, active=True):
        data = self.register(email, username or email.split('@')[0])
        user = db.session.get(User, data['user']['id'])
        user.role, user.is_active = role, active
        db.session.commit()
        return types.SimpleNamespace(id=user.id, email=email, username=user.username, tokens=data['tokens'],
                                     h=self.hdr(data['tokens']))

    def call(self, who, method, path, **kw):
        return getattr(self.client, method)(path, headers=who.h if who else {}, **kw)

    def audit_rows(self, **filters):
        db.session.expire_all()
        return AdminAuditLog.query.filter_by(**filters).order_by(AdminAuditLog.id).all()

    def counts(self, user_id):
        db.session.expire_all()
        return purge.user_counts(user_id)

    def fk_check(self):
        return db.session.execute(text('PRAGMA foreign_key_check')).fetchall()


# ══════════════════════════════════════════════
# Fase 2 — modelo e migration
# ══════════════════════════════════════════════
class TestAdminMigration(AdminTestCase):
    def test_new_users_default_to_role_user_and_role_is_exposed(self):
        who = self.make('u@example.com')
        me = self.call(who, 'get', '/auth/me').get_json()['user']
        self.assertEqual(me['role'], 'user')
        self.assertNotIn('password_hash', json.dumps(me))

    def test_check_constraint_rejects_invalid_roles(self):
        from sqlalchemy.exc import IntegrityError
        self.make('u@example.com')
        with self.assertRaises(IntegrityError):
            db.session.execute(text("UPDATE users SET role='root'"))
        db.session.rollback()

    def test_upgrade_preserves_existing_data_and_downgrade_works(self):
        from flask_migrate import upgrade, downgrade
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(make_config(os.path.join(tmp, 'mig.db')))
            with app.app_context():
                d = str(ROOT / 'backend/migrations')
                upgrade(directory=d, revision='9ee9fe5f43cf')
                db.session.execute(text("INSERT INTO users (id, username, email, password_hash, is_active) "
                                        "VALUES (1, 'ana', 'ana@example.com', 'h', 1), (2, 'bia', 'bia@example.com', 'h', 0)"))
                db.session.execute(text("INSERT INTO tasks (id, title, user_id) VALUES (1, 'tarefa', 1)"))
                db.session.commit()
                upgrade(directory=d)                                    # com filhos e FKs ativas
                rows = db.session.execute(text('SELECT id, username, is_active, role FROM users ORDER BY id')).fetchall()
                self.assertEqual([tuple(r) for r in rows], [(1, 'ana', 1, 'user'), (2, 'bia', 0, 'user')])
                self.assertEqual(db.session.execute(text('SELECT count(*) FROM tasks')).scalar(), 1)
                self.assertIn('admin_audit_log', db.inspect(db.engine).get_table_names())
                self.assertEqual(db.session.execute(text('SELECT count(*) FROM admin_audit_log')).scalar(), 0)
                downgrade(directory=d, revision='9ee9fe5f43cf')
                self.assertNotIn('admin_audit_log', db.inspect(db.engine).get_table_names())
                self.assertNotIn('role', [c['name'] for c in db.inspect(db.engine).get_columns('users')])
                self.assertEqual(db.session.execute(text('SELECT count(*) FROM users')).scalar(), 2)
                self.assertEqual(db.session.execute(text('SELECT count(*) FROM tasks')).scalar(), 1)
                downgrade(directory=d, revision='base')
                db.session.remove(); db.engine.dispose()

    def test_migration_does_not_promote_anyone(self):
        src = (ROOT / 'backend/migrations/versions/c4d7a1e08b52_add_user_role_and_admin_audit_log.py').read_text()
        body = src.split('def upgrade')[1].split('def downgrade')[0].upper()
        for statement in ('UPDATE', 'INSERT', 'DELETE'):
            self.assertNotIn(statement, body)

    def test_audit_table_has_no_foreign_keys(self):
        self.assertEqual(db.inspect(db.engine).get_foreign_keys('admin_audit_log'), [])


# ══════════════════════════════════════════════
# Fase 3 — papéis, autorização e auditoria
# ══════════════════════════════════════════════
# (método, rota de exemplo, papel mínimo)
ROUTES = [
    ('GET',    '/admin/overview',                   'admin'),
    ('GET',    '/admin/users',                      'admin'),
    ('GET',    '/admin/users/{t}',                  'admin'),
    ('POST',   '/admin/users/{t}/activate',         'admin'),
    ('POST',   '/admin/users/{t}/deactivate',       'admin'),
    ('DELETE', '/admin/users/{t}/avatar',           'admin'),
    ('GET',    '/admin/audit',                      'admin'),
    ('GET',    '/admin/users/{t}/data-summary',     'superadmin'),
    ('POST',   '/admin/users/{t}/clear-data',       'superadmin'),
    ('DELETE', '/admin/users/{t}',                  'superadmin'),
    ('GET',    '/admin/admins',                     'superadmin'),
    ('GET',    '/admin/users/lookup',               'superadmin'),
    ('POST',   '/admin/admins',                     'superadmin'),
    ('PUT',    '/admin/users/{t}/role',             'superadmin'),
    ('GET',    '/admin/maintenance/summary',        'superadmin'),
    ('POST',   '/admin/maintenance/wipe/preview',   'superadmin'),
    ('POST',   '/admin/maintenance/wipe',           'superadmin'),
]


class TestPermissions(AdminTestCase):
    def test_permission_map(self):
        self.assertEqual(permissions.permissions_for('user'), frozenset())
        self.assertEqual(permissions.permissions_for('root'), frozenset())          # papel desconhecido: falha fechada
        self.assertEqual(permissions.permissions_for(None), frozenset())
        self.assertTrue(permissions.ROLE_PERMISSIONS['admin'] < permissions.ROLE_PERMISSIONS['superadmin'])
        for perm in ('users.delete', 'users.clear_data', 'roles.manage', 'maintenance.wipe', 'audit.view_all'):
            self.assertNotIn(perm, permissions.ROLE_PERMISSIONS['admin'])
            self.assertIn(perm, permissions.ROLE_PERMISSIONS['superadmin'])

    def test_every_admin_route_is_in_the_matrix_and_guarded(self):
        rules = {(m, r.rule) for r in self.app.url_map.iter_rules() if r.rule.startswith('/admin')
                 for m in r.methods - {'HEAD', 'OPTIONS'}}
        declared = {(m, p.replace('{t}', '<int:user_id>')) for m, p, _ in ROUTES}
        self.assertEqual(rules, declared, 'rota /admin nova sem teste de matriz (ou removida)')
        for rule in self.app.url_map.iter_rules():
            if rule.rule.startswith('/admin'):
                view = self.app.view_functions[rule.endpoint]
                self.assertTrue(getattr(view, 'admin_permission', None), f'{rule.rule} sem require_permission')

    def test_matrix_anonymous_user_admin_superadmin(self):
        user = self.make('u@example.com')
        admin = self.make('adm@example.com', 'admin')
        sup = self.make('sup@example.com', 'superadmin')
        target = self.make('alvo@example.com')
        order = {'user': 0, 'admin': 1, 'superadmin': 2}
        for method, path, minimum in ROUTES:
            url = path.replace('{t}', str(target.id))
            self.assertEqual(self.call(None, method.lower(), url).status_code, 401, f'anónimo {method} {url}')
            r = self.call(user, method.lower(), url, json={})
            self.assertEqual((r.status_code, r.get_json()['code']), (403, 'forbidden'), f'user {method} {url}')
            r = self.call(admin, method.lower(), url, json={})
            if order[minimum] > order['admin']:
                self.assertEqual((r.status_code, r.get_json()['code']), (403, 'forbidden'), f'admin {method} {url}')
            else:
                self.assertNotEqual(r.status_code, 401, f'admin {method} {url}')
                self.assertNotEqual(r.get_json().get('code'), 'forbidden', f'admin {method} {url}')
        # o ADMIN não alterou nada nas rotas que lhe estavam vetadas
        db.session.expire_all()
        self.assertEqual(db.session.get(User, target.id).role, 'user')
        self.assertEqual(User.query.count(), 4)

    def test_role_is_read_from_the_database_on_every_request(self):
        who = self.make('u@example.com')
        self.assertEqual(self.call(who, 'get', '/admin/users').status_code, 403)
        user = db.session.get(User, who.id); user.role = 'admin'; db.session.commit()
        self.assertEqual(self.call(who, 'get', '/admin/users').status_code, 200)        # mesmo JWT, efeito imediato
        user.role = 'user'; db.session.commit()
        self.assertEqual(self.call(who, 'get', '/admin/users').status_code, 403)

    def test_inactive_admin_cannot_use_the_panel(self):
        who = self.make('adm@example.com', 'admin')
        db.session.get(User, who.id).is_active = False; db.session.commit()
        self.assertEqual(self.call(who, 'get', '/admin/users').status_code, 401)

    def test_role_cannot_be_chosen_at_registration_or_through_the_profile(self):
        r = self.client.post('/auth/register', json={'email': 'x@example.com', 'password': PASSWORD, 'role': 'superadmin'})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()['user']['role'], 'user')
        who = self.hdr(r.get_json()['tokens'])
        for body in ({'role': 'superadmin'}, {'username': 'x', 'role': 'admin'}, {'is_active': True}, {'id': 1},
                     {'password_hash': 'x'}):
            resp = self.client.patch('/auth/me', headers=who, json=body)
            self.assertEqual((resp.status_code, resp.get_json()['code']), (400, 'invalid_fields'), body)
        db.session.expire_all()
        self.assertEqual(User.query.filter_by(email='x@example.com').one().role, 'user')

    def test_denied_attempts_are_audited(self):
        who = self.make('u@example.com')
        self.call(who, 'get', '/admin/users')
        rows = self.audit_rows(result='denied')
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].actor_id, rows[0].action), (who.id, 'denied:users.list'))

    def test_responses_are_not_cacheable_and_carry_security_headers(self):
        sup = self.make('sup@example.com', 'superadmin')
        r = self.call(sup, 'get', '/admin/users')
        self.assertEqual(r.headers['Cache-Control'], 'no-store')
        self.assertEqual(r.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(r.headers['Referrer-Policy'], 'no-referrer')

    def test_global_wipe_flag_defaults_to_disabled(self):
        from backend.config import Config
        self.assertFalse(os.environ.get('ADMIN_ALLOW_GLOBAL_WIPE'))
        self.assertIs(Config.ADMIN_ALLOW_GLOBAL_WIPE, False)
        self.assertFalse(self.app.config['ADMIN_ALLOW_GLOBAL_WIPE'])


class TestAudit(AdminTestCase):
    def test_sanitize_drops_sensitive_keys_and_limits_values(self):
        clean = audit.sanitize_details({'password': 'x', 'new_password': 'x', 'refresh_token': 't', 'password_hash': 'h',
                                        'secret_key': 'k', 'Authorization': 'Bearer x', 'api_key': 'k', 'password': 5,
                                        'tasks': 3, 'refresh_tokens': 4, 'note': 'a' * 999,
                                        'nested': {'token': 'x', 'ok': 1, 'refresh_tokens': 2}})
        self.assertEqual(set(clean), {'tasks', 'refresh_tokens', 'note', 'nested'})   # contagens ficam; segredos saem
        self.assertEqual(len(clean['note']), 200)
        self.assertEqual(clean['nested'], {'ok': 1, 'refresh_tokens': 2})

    def test_admin_sees_only_own_actions_and_superadmin_sees_all(self):
        a1 = self.make('a1@example.com', 'admin'); a2 = self.make('a2@example.com', 'admin')
        sup = self.make('sup@example.com', 'superadmin')
        t1, t2 = self.make('t1@example.com'), self.make('t2@example.com')
        self.assertEqual(self.call(a1, 'post', f'/admin/users/{t1.id}/deactivate').status_code, 200)
        self.assertEqual(self.call(a2, 'post', f'/admin/users/{t2.id}/deactivate').status_code, 200)
        mine = self.call(a1, 'get', '/admin/audit').get_json()
        self.assertEqual((mine['scope'], mine['total']), ('own', 1))
        self.assertEqual({e['actor_id'] for e in mine['entries']}, {a1.id})
        # um ADMIN não consegue ver as ações de outro, nem com o filtro actor_id
        other = self.call(a1, 'get', f'/admin/audit?actor_id={a2.id}').get_json()
        self.assertEqual({e['actor_id'] for e in other['entries']}, {a1.id})
        everything = self.call(sup, 'get', '/admin/audit?action=user.deactivate').get_json()
        self.assertEqual((everything['scope'], everything['total']), ('all', 2))
        only_a2 = self.call(sup, 'get', f'/admin/audit?actor_id={a2.id}').get_json()
        self.assertEqual({e['actor_id'] for e in only_a2['entries']}, {a2.id})

    def test_filters_pagination_and_period(self):
        sup = self.make('sup@example.com', 'superadmin')
        for i in range(5):
            t = self.make(f't{i}@example.com')
            self.call(sup, 'post', f'/admin/users/{t.id}/deactivate')
        self.call(sup, 'post', f'/admin/users/{sup.id}/deactivate')                  # negado (self_action)
        page = self.call(sup, 'get', '/admin/audit?per_page=2&page=2').get_json()
        self.assertEqual((len(page['entries']), page['per_page'], page['page']), (2, 2, 2))
        self.assertTrue(page['total_pages'] >= 3)
        denied = self.call(sup, 'get', '/admin/audit?result=denied').get_json()
        self.assertEqual({e['result'] for e in denied['entries']}, {'denied'})
        today = datetime.utcnow().strftime('%Y-%m-%d')
        self.assertGreaterEqual(self.call(sup, 'get', f'/admin/audit?from={today}&to={today}').get_json()['total'], 6)
        future = (datetime.utcnow() + timedelta(days=2)).strftime('%Y-%m-%d')
        self.assertEqual(self.call(sup, 'get', f'/admin/audit?from={future}').get_json()['total'], 0)
        self.assertEqual(self.call(sup, 'get', '/admin/audit?from=lixo&actor_id=abc&result=x').status_code, 200)

    def test_audit_is_read_only_through_the_api(self):
        sup = self.make('sup@example.com', 'superadmin')
        for method in ('post', 'put', 'patch', 'delete'):
            self.assertEqual(self.call(sup, method, '/admin/audit', json={}).status_code, 405, method)
            self.assertEqual(self.call(sup, method, '/admin/audit/1', json={}).status_code, 404, method)

    def test_no_secrets_reach_the_audit_log(self):
        sup = self.make('sup@example.com', 'superadmin')
        victim = self.make('v@example.com')
        summary = self.call(sup, 'get', f'/admin/users/{victim.id}/data-summary').get_json()
        r = self.call(sup, 'post', f'/admin/users/{victim.id}/clear-data',
                      json={'password': PASSWORD, 'confirm_username': victim.username, 'expected_counts': summary['counts']})
        self.assertEqual(r.status_code, 200, r.get_json())
        dump = json.dumps([e.to_dict() for e in self.audit_rows()])
        for secret in (PASSWORD, 'password_hash', victim.tokens['access_token'], victim.tokens['refresh_token']):
            self.assertNotIn(secret, dump)

    def test_audit_survives_user_deletion(self):
        sup = self.make('sup@example.com', 'superadmin')
        victim = self.make('v@example.com', username='vitima')
        counts = self.call(sup, 'get', f'/admin/users/{victim.id}/data-summary').get_json()['counts']
        r = self.call(sup, 'delete', f'/admin/users/{victim.id}',
                      json={'password': PASSWORD, 'confirm_username': 'vitima', 'expected_counts': counts})
        self.assertEqual(r.status_code, 200, r.get_json())
        row = self.audit_rows(action='user.deleted')[0]
        self.assertEqual((row.target_id, row.target_label, row.actor_id, row.result), (victim.id, 'vitima', sup.id, 'success'))
        self.assertIsNone(db.session.get(User, victim.id))
        # o próprio administrador também pode ser excluído por outro SUPERADMIN sem perder o histórico
        sup2 = self.make('sup2@example.com', 'superadmin', username='sup2')
        counts = self.call(sup2, 'get', f'/admin/users/{sup.id}/data-summary').get_json()['counts']
        self.assertEqual(self.call(sup2, 'delete', f'/admin/users/{sup.id}', json={
            'password': PASSWORD, 'confirm_username': 'sup', 'expected_counts': counts}).status_code, 200)
        self.assertEqual(self.audit_rows(action='user.deleted')[0].actor_label, 'sup')


# ══════════════════════════════════════════════
# Fase 4 — perfil de qualquer utilizador
# ══════════════════════════════════════════════
class TestProfile(AdminTestCase):
    def test_change_username(self):
        who = self.make('u@example.com', username='antigo')
        self.make('o@example.com', username='ocupado')
        r = self.client.patch('/auth/me', headers=who.h, json={'username': '  novo nome  '})
        self.assertEqual((r.status_code, r.get_json()['user']['username']), (200, 'novo nome'))
        for body, status, code in (({'username': ''}, 400, 'invalid_username'), ({'username': '   '}, 400, 'invalid_username'),
                                   ({'username': 'x' * 81}, 400, 'invalid_username'), ({'username': 5}, 400, 'invalid_username'),
                                   ({'username': 'ocupado'}, 409, 'username_taken')):
            r = self.client.patch('/auth/me', headers=who.h, json=body)
            self.assertEqual((r.status_code, r.get_json()['code']), (status, code), body)

    def test_change_email_requires_the_current_password(self):
        who = self.make('u@example.com')
        self.make('o@example.com')
        url = '/auth/me'
        r = self.client.patch(url, headers=who.h, json={'email': 'novo@example.com'})
        self.assertEqual((r.status_code, r.get_json()['code']), (403, 'invalid_password'))
        r = self.client.patch(url, headers=who.h, json={'email': 'novo@example.com', 'current_password': 'errada123'})
        self.assertEqual(r.status_code, 403)
        for body, status, code in (({'email': 'sem-arroba', 'current_password': PASSWORD}, 400, 'invalid_email'),
                                   ({'email': 7, 'current_password': PASSWORD}, 400, 'invalid_email'),
                                   ({'email': 'o@example.com', 'current_password': PASSWORD}, 409, 'email_taken'),
                                   ({'email': ('a' * 120) + '@x.com', 'current_password': PASSWORD}, 400, 'invalid_email')):
            r = self.client.patch(url, headers=who.h, json=body)
            self.assertEqual((r.status_code, r.get_json()['code']), (status, code), body)
        r = self.client.patch(url, headers=who.h, json={'email': '  Novo@Example.COM ', 'current_password': PASSWORD})
        self.assertEqual((r.status_code, r.get_json()['user']['email']), (200, 'novo@example.com'))
        self.assertEqual(self.client.post('/auth/login', json={'email': 'novo@example.com', 'password': PASSWORD}).status_code, 200)
        self.assertEqual(self.client.post('/auth/login', json={'email': 'u@example.com', 'password': PASSWORD}).status_code, 401)

    def test_unchanged_values_and_bad_bodies(self):
        who = self.make('u@example.com', username='igual')
        r = self.client.patch('/auth/me', headers=who.h, json={'username': 'igual'})
        self.assertEqual((r.status_code, r.get_json()['message']), (200, 'Nada para alterar.'))
        for body in ({}, [], 'x', None):
            self.assertEqual(self.client.patch('/auth/me', headers=who.h, json=body).status_code, 400, body)
        self.assertEqual(self.client.patch('/auth/me', json={'username': 'x'}).status_code, 401)

    def test_change_password(self):
        who = self.make('u@example.com')
        old_refresh = who.tokens['refresh_token']
        url = '/auth/password'
        for body, status, code in (({}, 400, 'invalid_request'), ({'new_password': 'Nova12345'}, 403, 'invalid_password'),
                                   ({'current_password': PASSWORD}, 400, 'invalid_password'),
                                   ({'current_password': PASSWORD, 'new_password': 'curta1'}, 400, 'weak_password'),
                                   ({'current_password': PASSWORD, 'new_password': 'semnumeros'}, 400, 'weak_password'),
                                   ({'current_password': PASSWORD, 'new_password': PASSWORD}, 400, 'same_password'),
                                   ({'current_password': PASSWORD, 'new_password': 'Nova12345', 'role': 'admin'}, 400, 'invalid_request')):
            r = self.client.put(url, headers=who.h, json=body)
            self.assertEqual((r.status_code, r.get_json()['code']), (status, code), body)
        r = self.client.put(url, headers=who.h, json={'current_password': PASSWORD, 'new_password': 'Nova12345'})
        self.assertEqual(r.status_code, 200, r.get_json())
        data = r.get_json()
        self.assertNotIn('password', json.dumps(data).replace('new_password', ''))
        self.assertNotIn('hash', json.dumps(data).lower())
        self.assertEqual(self.client.post('/auth/login', json={'email': 'u@example.com', 'password': PASSWORD}).status_code, 401)
        self.assertEqual(self.client.post('/auth/login', json={'email': 'u@example.com', 'password': 'Nova12345'}).status_code, 200)
        # refresh tokens antigos revogados; os novos funcionam
        self.assertEqual(self.client.post('/auth/refresh', json={'refresh_token': old_refresh}).status_code, 401)
        new_refresh = data['tokens']['refresh_token']
        self.assertEqual(self.client.post('/auth/refresh', json={'refresh_token': new_refresh}).status_code, 200)
        self.assertEqual(self.client.get('/auth/me', headers={'Authorization': f"Bearer {data['tokens']['access_token']}"}).status_code, 200)

    def test_password_change_revokes_every_refresh_token(self):
        who = self.make('u@example.com')
        extra = [self.login('u@example.com') for _ in range(3)]
        self.assertGreaterEqual(RefreshToken.query.filter_by(user_id=who.id).count(), 4)
        r = self.client.put('/auth/password', headers=who.h, json={'current_password': PASSWORD, 'new_password': 'Nova12345'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(RefreshToken.query.filter_by(user_id=who.id).count(), 1)          # só o da sessão atual
        for tokens in extra:
            self.assertEqual(self.client.post('/auth/refresh', json={'refresh_token': tokens['refresh_token']}).status_code, 401)

    def test_wrong_current_password_is_throttled_per_user(self):
        who = self.make('u@example.com')
        for _ in range(permissions.REAUTH_MAX_FAILURES):
            self.assertEqual(self.client.put('/auth/password', headers=who.h,
                                             json={'current_password': 'errada123', 'new_password': 'Nova12345'}).status_code, 403)
        r = self.client.put('/auth/password', headers=who.h, json={'current_password': PASSWORD, 'new_password': 'Nova12345'})
        self.assertEqual((r.status_code, r.get_json()['code']), (429, 'reauth_throttled'))

    def test_other_users_data_untouched_and_avatar_still_works(self):
        a, b = self.make('a@example.com', username='aaa'), self.make('b@example.com', username='bbb')
        self.client.patch('/auth/me', headers=a.h, json={'username': 'novo'})
        db.session.expire_all()
        self.assertEqual(db.session.get(User, b.id).username, 'bbb')
        self.assertEqual(self.client.put('/auth/avatar', headers=a.h, data=PNG).status_code, 200)
        self.assertEqual(self.client.delete('/auth/avatar', headers=a.h).status_code, 200)


# ══════════════════════════════════════════════
# Fase 5 — leitura e operações simples
# ══════════════════════════════════════════════
class TestUserListing(AdminTestCase):
    def test_list_search_filters_and_pagination(self):
        sup = self.make('sup@example.com', 'superadmin', username='chefe')
        admin = self.make('adm@example.com', 'admin', username='gestor')
        for i in range(25):
            self.make(f'user{i:02d}@example.com', username=f'user{i:02d}', active=(i % 5 != 0))
        r = self.call(admin, 'get', '/admin/users').get_json()
        self.assertEqual((r['total'], r['per_page'], r['total_pages'], len(r['users'])), (27, 20, 2, 20))
        self.assertEqual(len(self.call(admin, 'get', '/admin/users?page=2').get_json()['users']), 7)
        self.assertEqual(self.call(admin, 'get', '/admin/users?per_page=1000').get_json()['per_page'], 100)
        self.assertEqual(self.call(admin, 'get', '/admin/users?page=abc&per_page=xx').status_code, 200)
        self.assertEqual(self.call(admin, 'get', '/admin/users?search=USER07').get_json()['total'], 1)
        self.assertEqual(self.call(admin, 'get', '/admin/users?role=admin').get_json()['total'], 1)
        self.assertEqual(self.call(admin, 'get', '/admin/users?status=inactive').get_json()['total'], 5)
        self.assertEqual(self.call(admin, 'get', '/admin/users?status=active').get_json()['total'], 22)
        self.assertEqual(self.call(admin, 'get', '/admin/users?role=root').get_json()['total'], 27)       # filtro inválido ignorado

    def test_search_escapes_like_wildcards(self):
        admin = self.make('adm@example.com', 'admin')
        self.make('a1@example.com', username='a_b'); self.make('a2@example.com', username='axb')
        self.make('a3@example.com', username='100%'); self.make('a4@example.com', username='1000')
        self.assertEqual({u['username'] for u in self.call(admin, 'get', '/admin/users?search=a_b').get_json()['users']}, {'a_b'})
        self.assertEqual({u['username'] for u in self.call(admin, 'get', '/admin/users?search=100%25').get_json()['users']}, {'100%'})

    def test_payload_has_only_minimal_safe_fields(self):
        admin = self.make('adm@example.com', 'admin')
        user = self.make('u@example.com')
        listing = self.call(admin, 'get', '/admin/users').get_json()
        row = next(u for u in listing['users'] if u['id'] == user.id)
        self.assertEqual(set(row), {'id', 'username', 'email', 'role', 'is_active', 'has_avatar', 'created_at', 'last_login', 'actions'})
        detail = self.call(admin, 'get', f'/admin/users/{user.id}').get_json()['user']
        self.assertEqual(set(detail), set(row))
        dump = json.dumps(listing) + json.dumps(detail)
        for forbidden in ('password', 'hash', 'refresh', 'access_token', user.tokens['refresh_token']):
            self.assertNotIn(forbidden, dump)
        self.assertEqual(self.call(admin, 'get', '/admin/users/9999').status_code, 404)

    def test_overview_counts(self):
        admin = self.make('adm@example.com', 'admin'); self.make('s@example.com', 'superadmin')
        self.make('i@example.com', active=False); self.make('u@example.com')
        r = self.call(admin, 'get', '/admin/overview').get_json()
        self.assertEqual((r['users'], r['active'], r['inactive'], r['admins'], r['superadmins']), (4, 3, 1, 1, 1))

    def test_allowed_actions_reflect_the_role_matrix(self):
        sup = self.make('sup@example.com', 'superadmin'); admin = self.make('adm@example.com', 'admin')
        user = self.make('u@example.com')
        acts = lambda who, t: self.call(who, 'get', f'/admin/users/{t.id}').get_json()['user']['actions']
        self.assertEqual(acts(admin, user), {'toggle_active': True, 'remove_avatar': False, 'clear_data': False,
                                             'delete': False, 'change_role': False})
        self.assertFalse(acts(admin, sup)['toggle_active'])
        self.assertFalse(acts(admin, admin)['toggle_active'])                        # nunca sobre si próprio
        self.assertEqual(acts(sup, user), {'toggle_active': True, 'remove_avatar': False, 'clear_data': True,
                                           'delete': True, 'change_role': True})
        self.assertEqual(acts(sup, sup), {'toggle_active': False, 'remove_avatar': False, 'clear_data': True,
                                          'delete': False, 'change_role': False})


class TestSimpleOperations(AdminTestCase):
    def test_admin_deactivates_and_reactivates_a_regular_user(self):
        admin = self.make('adm@example.com', 'admin'); user = self.make('u@example.com')
        r = self.call(admin, 'post', f'/admin/users/{user.id}/deactivate')
        self.assertEqual((r.status_code, r.get_json()['user']['is_active']), (200, False))
        self.assertEqual(self.call(user, 'get', '/auth/me').status_code, 401)             # token já não serve
        self.assertEqual(self.client.post('/auth/login', json={'email': user.email, 'password': PASSWORD}).status_code, 403)
        self.assertEqual(self.call(admin, 'post', f'/admin/users/{user.id}/deactivate').status_code, 409)
        self.assertEqual(self.call(admin, 'post', f'/admin/users/{user.id}/activate').status_code, 200)
        self.assertEqual(self.call(user, 'get', '/auth/me').status_code, 200)
        self.assertEqual([e.action for e in self.audit_rows(result='success')], ['user.deactivate', 'user.activate'])

    def test_admin_cannot_touch_admins_or_superadmins_but_superadmin_can_manage_admins(self):
        sup = self.make('sup@example.com', 'superadmin'); a1 = self.make('a1@example.com', 'admin')
        a2 = self.make('a2@example.com', 'admin'); sup2 = self.make('sup2@example.com', 'superadmin')
        for target in (a2, sup2, sup):
            r = self.call(a1, 'post', f'/admin/users/{target.id}/deactivate')
            self.assertEqual((r.status_code, r.get_json()['code']), (403, 'forbidden'), target.email)
        db.session.expire_all()
        self.assertTrue(all(db.session.get(User, u.id).is_active for u in (a2, sup2, sup)))
        self.assertEqual(self.call(sup, 'post', f'/admin/users/{a2.id}/deactivate').status_code, 200)
        self.assertEqual(self.call(sup, 'post', f'/admin/users/{sup2.id}/deactivate').status_code, 200)    # há outro SUPERADMIN ativo

    def test_nobody_acts_on_their_own_account(self):
        sup = self.make('sup@example.com', 'superadmin'); admin = self.make('adm@example.com', 'admin')
        for who in (sup, admin):
            r = self.call(who, 'post', f'/admin/users/{who.id}/deactivate')
            self.assertEqual((r.status_code, r.get_json()['code']), (403, 'self_action'))
        r = self.call(sup, 'delete', f'/admin/users/{sup.id}', json={})
        self.assertEqual((r.status_code, r.get_json()['code']), (403, 'self_action'))
        r = self.call(sup, 'put', f'/admin/users/{sup.id}/role', json={'role': 'admin', 'password': PASSWORD})
        self.assertEqual((r.status_code, r.get_json()['code']), (403, 'self_action'))
        db.session.expire_all()
        self.assertEqual((db.session.get(User, sup.id).role, db.session.get(User, sup.id).is_active), ('superadmin', True))

    def test_remove_avatar(self):
        sup = self.make('sup@example.com', 'superadmin'); admin = self.make('adm@example.com', 'admin')
        user = self.make('u@example.com'); other = self.make('o@example.com', 'admin')
        self.client.put('/auth/avatar', headers=user.h, data=PNG); self.client.put('/auth/avatar', headers=other.h, data=PNG)
        self.assertEqual(self.call(admin, 'delete', f'/admin/users/{other.id}/avatar').status_code, 403)    # ADMIN não toca em ADMIN
        self.assertEqual(self.call(admin, 'delete', f'/admin/users/{admin.id}/avatar').status_code, 403)
        r = self.call(admin, 'delete', f'/admin/users/{user.id}/avatar')
        self.assertEqual((r.status_code, r.get_json()['user']['has_avatar']), (200, False))
        self.assertEqual(self.call(admin, 'delete', f'/admin/users/{user.id}/avatar').status_code, 404)
        self.assertEqual(self.call(sup, 'delete', f'/admin/users/{other.id}/avatar').status_code, 200)
        self.assertEqual([e.action for e in self.audit_rows(result='success')], ['user.avatar_removed'] * 2)

    def test_failed_and_denied_operations_are_audited(self):
        admin = self.make('adm@example.com', 'admin'); sup = self.make('sup@example.com', 'superadmin')
        self.call(admin, 'post', f'/admin/users/{sup.id}/deactivate')                       # negado
        self.call(admin, 'post', f'/admin/users/{admin.id}/deactivate')                     # negado (self)
        u = self.make('u@example.com'); self.call(admin, 'post', f'/admin/users/{u.id}/activate')   # falhado (já ativo)
        rows = {(r.result, r.details.get('code')) for r in self.audit_rows() if r.actor_id == admin.id}
        self.assertEqual(rows, {('denied', 'forbidden'), ('denied', 'self_action'), ('failed', 'no_change')})


class TestLastSuperadminAndStaleActor(AdminTestCase):
    def test_pure_rule(self):
        mk = lambda role, active: types.SimpleNamespace(role=role, is_active=active)
        a, b, c = mk('superadmin', True), mk('superadmin', True), mk('superadmin', False)
        self.assertFalse(user_admin._is_last_active_superadmin(a, [a, b, c]))
        self.assertTrue(user_admin._is_last_active_superadmin(a, [a, c]))
        self.assertFalse(user_admin._is_last_active_superadmin(c, [a, c]))                  # inativo: já não conta
        self.assertFalse(user_admin._is_last_active_superadmin(mk('admin', True), [a]))

    def test_stale_actor_deactivated_meanwhile_cannot_remove_the_last_superadmin(self):
        """Simula a corrida: B foi desativado por A depois de B autenticar; B tenta desativar A."""
        a = self.make('a@example.com', 'superadmin', username='aa'); b = self.make('b@example.com', 'superadmin', username='bb')
        stale_b = types.SimpleNamespace(id=b.id, role='superadmin', username='bb', is_active=True)      # estado antigo de B
        self.assertEqual(self.call(a, 'post', f'/admin/users/{b.id}/deactivate').status_code, 200)       # A desativa B
        with self.assertRaises(permissions.AdminError) as ctx:
            user_admin.set_active(stale_b, a.id, False)
        self.assertEqual((ctx.exception.code, ctx.exception.status), ('inactive_actor', 401))
        db.session.rollback(); db.session.expire_all()
        self.assertEqual(User.query.filter_by(role='superadmin', is_active=True).count(), 1)             # A continua ativo

    def test_stale_actor_demoted_meanwhile_loses_the_permission(self):
        a = self.make('a@example.com', 'superadmin', username='aa'); b = self.make('b@example.com', 'superadmin', username='bb')
        stale_b = types.SimpleNamespace(id=b.id, role='superadmin', username='bb', is_active=True)
        db.session.get(User, b.id).role = 'admin'; db.session.commit()
        victim = self.make('v@example.com')
        for call in (lambda: user_admin.set_role(stale_b, victim.id, 'admin', {'password': PASSWORD}),
                     lambda: user_admin.delete_user(stale_b, victim.id, {})):
            with self.assertRaises(permissions.AdminError) as ctx:
                call()
            self.assertEqual((ctx.exception.code, ctx.exception.status), ('forbidden', 403))
            db.session.rollback()

    def test_inactive_superadmin_does_not_count_as_a_spare(self):
        a = self.make('a@example.com', 'superadmin', username='aa')
        self.make('b@example.com', 'superadmin', username='bb', active=False)
        rows = db.session.execute(select(User).where(User.role == 'superadmin')).scalars().all()
        self.assertTrue(user_admin._is_last_active_superadmin(db.session.get(User, a.id), rows))


# ══════════════════════════════════════════════
# Fase 7 — gestão de administradores
# ══════════════════════════════════════════════
class TestRoleManagement(AdminTestCase):
    def role(self, who, user_id, role, password=PASSWORD):
        return self.call(who, 'put', f'/admin/users/{user_id}/role', json={'role': role, 'password': password})

    def test_lookup_by_email_and_promote_to_admin(self):
        sup = self.make('sup@example.com', 'superadmin'); user = self.make('alvo@example.com')
        r = self.call(sup, 'get', '/admin/users/lookup?email=%20ALVO@example.com')
        self.assertEqual((r.status_code, r.get_json()['user']['id']), (200, user.id))
        self.assertEqual(self.call(sup, 'get', '/admin/users/lookup?email=nao@existe.com').status_code, 404)
        self.assertEqual(self.call(sup, 'get', '/admin/users/lookup').status_code, 400)
        r = self.call(sup, 'post', '/admin/admins', json={'user_id': user.id, 'password': PASSWORD})
        self.assertEqual((r.status_code, r.get_json()['user']['role']), (200, 'admin'))
        listing = self.call(sup, 'get', '/admin/admins').get_json()['admins']
        self.assertEqual({u['id'] for u in listing}, {sup.id, user.id})
        row = self.audit_rows(action='role.changed')[0]
        self.assertEqual((row.details, row.target_id, row.actor_id), ({'from': 'user', 'to': 'admin'}, user.id, sup.id))

    def test_password_is_required_and_throttled(self):
        sup = self.make('sup@example.com', 'superadmin'); user = self.make('u@example.com')
        body = lambda pw: {'user_id': user.id, 'password': pw}
        for pw in (None, '', 'errada123'):
            r = self.call(sup, 'post', '/admin/admins', json=body(pw))
            self.assertEqual((r.status_code, r.get_json()['code']), (403, 'invalid_password'), pw)
        db.session.expire_all()
        self.assertEqual(db.session.get(User, user.id).role, 'user')
        for _ in range(permissions.REAUTH_MAX_FAILURES):
            self.call(sup, 'post', '/admin/admins', json=body('errada123'))
        r = self.call(sup, 'post', '/admin/admins', json=body(PASSWORD))                    # certa, mas bloqueada
        self.assertEqual((r.status_code, r.get_json()['code']), (429, 'reauth_throttled'))

    def test_allowed_and_forbidden_transitions(self):
        sup = self.make('sup@example.com', 'superadmin'); user = self.make('u@example.com')
        steps = [('admin', 200), ('superadmin', 200), ('admin', 200), ('user', 200)]
        for role, status in steps:
            r = self.role(sup, user.id, role)
            self.assertEqual((r.status_code, r.get_json().get('user', {}).get('role')), (status, role), role)
        self.assertEqual(self.role(sup, user.id, 'user').get_json()['code'], 'no_change')
        r = self.role(sup, user.id, 'superadmin')                                            # user -> superadmin: passa por admin
        self.assertEqual((r.status_code, r.get_json()['code']), (409, 'invalid_transition'))
        self.assertEqual(self.role(sup, user.id, 'root').get_json()['code'], 'invalid_role')
        self.assertEqual(self.role(sup, user.id, None).get_json()['code'], 'invalid_role')
        sa = self.make('sa@example.com', 'superadmin')
        self.assertEqual(self.role(sup, sa.id, 'user').get_json()['code'], 'invalid_transition')   # superadmin -> user directo
        self.assertEqual(self.role(sup, 9999, 'admin').status_code, 404)

    def test_inactive_accounts_cannot_be_promoted(self):
        sup = self.make('sup@example.com', 'superadmin'); user = self.make('u@example.com', active=False)
        r = self.role(sup, user.id, 'admin')
        self.assertEqual((r.status_code, r.get_json()['code']), (409, 'inactive_target'))

    def test_admin_and_user_cannot_change_roles_or_escalate(self):
        sup = self.make('sup@example.com', 'superadmin'); admin = self.make('adm@example.com', 'admin')
        user = self.make('u@example.com')
        for who in (admin, user):
            for call in (lambda: self.role(who, user.id, 'admin'), lambda: self.role(who, who.id, 'superadmin'),
                         lambda: self.call(who, 'post', '/admin/admins', json={'user_id': user.id, 'password': PASSWORD}),
                         lambda: self.call(who, 'get', '/admin/admins')):
                self.assertEqual(call().status_code, 403)
        db.session.expire_all()
        self.assertEqual([db.session.get(User, u.id).role for u in (sup, admin, user)], ['superadmin', 'admin', 'user'])

    def test_superadmin_demotes_another_superadmin_through_admin(self):
        a = self.make('a@example.com', 'superadmin'); b = self.make('b@example.com', 'superadmin')
        self.assertEqual(self.role(a, b.id, 'admin').status_code, 200)
        self.assertEqual(self.call(b, 'get', '/admin/admins').status_code, 403)               # perdeu os privilégios já
        self.assertEqual(self.call(b, 'get', '/admin/users').status_code, 200)

    def test_stale_superadmin_cannot_demote_the_last_one(self):
        a = self.make('a@example.com', 'superadmin', username='aa'); b = self.make('b@example.com', 'superadmin', username='bb')
        stale_b = types.SimpleNamespace(id=b.id, role='superadmin', username='bb', is_active=True)
        self.assertEqual(self.role(a, b.id, 'admin').status_code, 200)                         # A despromove B
        with self.assertRaises(permissions.AdminError) as ctx:
            user_admin.set_role(stale_b, a.id, 'admin', {'password': PASSWORD})                 # B (desatualizado) tenta despromover A
        self.assertEqual(ctx.exception.code, 'forbidden')
        db.session.rollback(); db.session.expire_all()
        self.assertEqual(db.session.get(User, a.id).role, 'superadmin')


# ══════════════════════════════════════════════
# Fase 6 — limpeza individual e exclusão
# ══════════════════════════════════════════════
class DestructiveBase(AdminTestCase):
    def setUp(self):
        super().setUp()
        self.sup = self.make('sup@example.com', 'superadmin', username='chefe')
        self.admin = self.make('adm@example.com', 'admin', username='gestor')
        self.victim = self.make('v@example.com', username='vitima')
        self.bystander = self.make('b@example.com', username='terceiro')
        self.build_data()

    def build_data(self):
        v, b = self.victim, self.bystander
        cat = self.client.post('/categories', headers=v.h, json={'name': 'Trabalho'}).get_json()
        cat = cat.get('category', cat)
        self.vt1 = self.client.post('/tasks', headers=v.h, json={'title': 'T1', 'category_id': cat['id']}).get_json()
        self.vt1 = self.vt1.get('task', self.vt1)
        self.vt2 = self.client.post('/tasks', headers=v.h, json={'title': 'T2'}).get_json()
        self.vt2 = self.vt2.get('task', self.vt2)
        # a vítima partilha T1 com o terceiro; o terceiro partilha uma tarefa sua com a vítima
        r = self.client.post(f"/tasks/{self.vt1['id']}/collaborators", headers=v.h, json={'email': b.email, 'role': 'editor'})
        self.assertEqual(r.status_code, 201, r.get_json())
        self.bt = self.client.post('/tasks', headers=b.h, json={'title': 'Do terceiro'}).get_json()
        self.bt = self.bt.get('task', self.bt)
        r = self.client.post(f"/tasks/{self.bt['id']}/collaborators", headers=b.h, json={'email': v.email, 'role': 'viewer'})
        self.assertEqual(r.status_code, 201, r.get_json())
        self.client.put('/auth/avatar', headers=v.h, data=PNG)
        self.client.put('/auth/avatar', headers=b.h, data=PNG)

    def snapshot_others(self):
        """Tudo o que NÃO pertence à vítima, para provar que nada de terceiros muda."""
        db.session.expire_all()
        keep = lambda m, *f: sorted(tuple(getattr(r, c) for c in f) for r in m.query.all())
        b = self.bystander.id
        return {
            'users': sorted((u.id, u.username, u.email, u.role, u.is_active, u.password_hash) for u in User.query.all()
                            if u.id != self.victim.id),
            'tasks': [(t.id, t.title, t.user_id, t.category_id) for t in Task.query.filter(Task.user_id != self.victim.id)],
            'categories': [(c.id, c.name) for c in Category.query.filter(Category.user_id != self.victim.id)],
            'bystander_notifications': sorted((n.id, n.title, n.task_id) for n in Notification.query.filter_by(user_id=b)
                                              if n.task_id not in (self.vt1['id'], self.vt2['id'])),
            'avatars': [a.user_id for a in UserAvatar.query.filter(UserAvatar.user_id != self.victim.id)],
            'tokens': RefreshToken.query.filter(RefreshToken.user_id != self.victim.id).count(),
        }

    def summary(self, who=None):
        return self.call(who or self.sup, 'get', f'/admin/users/{self.victim.id}/data-summary').get_json()['counts']

    def body(self, **over):
        data = {'password': PASSWORD, 'confirm_username': 'vitima', 'expected_counts': self.summary()}
        data.update(over)
        return data

    def clear(self, **over):
        return self.call(self.sup, 'post', f'/admin/users/{self.victim.id}/clear-data', json=self.body(**over))

    def delete(self, who=None, **over):
        return self.call(who or self.sup, 'delete', f'/admin/users/{self.victim.id}', json=self.body(**over))


class TestClearUserData(DestructiveBase):
    def test_summary(self):
        counts = self.summary()
        self.assertEqual((counts['tasks'], counts['categories'], counts['collaborations'], counts['avatars']), (2, 1, 2, 1))
        self.assertGreaterEqual(counts['refresh_tokens'], 1)
        self.assertGreaterEqual(counts['third_party_notifications'], 1)       # o terceiro foi notificado da partilha de T1
        self.assertEqual(self.call(self.admin, 'get', f'/admin/users/{self.victim.id}/data-summary').status_code, 403)

    def test_clear_data_keeps_the_account_and_everything_of_others(self):
        before = self.snapshot_others()
        tokens_before = RefreshToken.query.filter_by(user_id=self.victim.id).count()
        third_party = Notification.query.filter(Notification.user_id == self.bystander.id,
                                                Notification.task_id == self.vt1['id']).count()
        self.assertGreaterEqual(third_party, 1)
        r = self.clear()
        self.assertEqual(r.status_code, 200, r.get_json())
        db.session.expire_all()
        c = self.counts(self.victim.id)
        self.assertEqual((c['tasks'], c['categories'], c['collaborations'], c['notifications']), (0, 0, 0, 0))
        victim = db.session.get(User, self.victim.id)
        self.assertEqual((victim.role, victim.is_active, victim.username), ('user', True, 'vitima'))     # conta intacta
        self.assertEqual(c['avatars'], 1)                                                                # avatar mantido
        self.assertEqual(RefreshToken.query.filter_by(user_id=self.victim.id).count(), tokens_before)     # tokens mantidos
        # notificações de terceiros ficam, sem tarefa
        orphaned = Notification.query.filter(Notification.user_id == self.bystander.id, Notification.task_id.is_(None)).count()
        self.assertGreaterEqual(orphaned, third_party)
        self.assertEqual(self.snapshot_others(), before | {'bystander_notifications': self.snapshot_others()['bystander_notifications']})
        self.assertEqual(self.snapshot_others()['tasks'], before['tasks'])
        self.assertEqual(Task.query.filter_by(id=self.bt['id']).count(), 1)                              # tarefa do terceiro intacta
        self.assertEqual(self.fk_check(), [])
        row = self.audit_rows(action='user.data_cleared')[0]
        self.assertEqual((row.result, row.target_id, row.target_label, row.details['tasks']), ('success', self.victim.id, 'vitima', 2))

    def test_confirmations_are_enforced_and_nothing_is_deleted_without_them(self):
        before = self.counts(self.victim.id)
        for over, status, code in (({'password': 'errada123'}, 403, 'invalid_password'), ({'password': None}, 403, 'invalid_password'),
                                   ({'confirm_username': 'outro'}, 400, 'confirmation_mismatch'),
                                   ({'confirm_username': None}, 400, 'confirmation_mismatch'),
                                   ({'expected_counts': None}, 409, 'counts_changed'),
                                   ({'expected_counts': {'tasks': 99}}, 409, 'counts_changed')):
            r = self.clear(**over)
            self.assertEqual((r.status_code, r.get_json()['code']), (status, code), over)
        self.assertEqual(self.counts(self.victim.id), before)

    def test_counts_changed_since_preview_aborts_everything(self):
        stale = self.summary()
        self.client.post('/tasks', headers=self.victim.h, json={'title': 'Nova depois da pré-visualização'})
        before = self.counts(self.victim.id)
        r = self.clear(expected_counts=stale)
        self.assertEqual((r.status_code, r.get_json()['code']), (409, 'counts_changed'))
        self.assertEqual(r.get_json()['current_counts']['tasks'], stale['tasks'] + 1)
        self.assertEqual(self.counts(self.victim.id), before)

    def test_only_superadmin_can_clear(self):
        before = self.counts(self.victim.id)
        for who in (self.admin, self.bystander):
            r = self.call(who, 'post', f'/admin/users/{self.victim.id}/clear-data', json=self.body())
            self.assertEqual((r.status_code, r.get_json()['code']), (403, 'forbidden'))
        self.assertEqual(self.counts(self.victim.id), before)

    def test_rollback_is_total_when_a_step_fails(self):
        before, others = self.counts(self.victim.id), self.snapshot_others()
        real = purge._exec

        def failing(stmt, expected, what):
            if what == 'categories':                       # último passo: tudo o que veio antes tem de ser desfeito
                raise RuntimeError('falha simulada no último passo')
            return real(stmt, expected, what)
        with mock.patch.object(purge, '_exec', side_effect=failing):
            r = self.clear()
        self.assertEqual((r.status_code, r.get_json()['code']), (500, 'internal_error'))
        self.assertEqual(self.counts(self.victim.id), before)
        self.assertEqual(self.snapshot_others(), others)
        self.assertEqual(self.fk_check(), [])
        self.assertEqual([e.result for e in self.audit_rows(action='user.data_cleared')], ['failed'])   # sem "success" órfão

    def test_row_count_mismatch_in_a_step_aborts_everything(self):
        before = self.counts(self.victim.id)
        real = purge._exec

        def off_by_one(stmt, expected, what):
            return real(stmt, expected + 1 if what == 'tasks' else expected, what)
        with mock.patch.object(purge, '_exec', side_effect=off_by_one):
            r = self.clear()
        self.assertEqual((r.status_code, r.get_json()['code']), (409, 'counts_changed'))
        self.assertEqual(self.counts(self.victim.id), before)


class TestDeleteUser(DestructiveBase):
    def test_refused_by_default_when_the_user_has_tasks(self):
        before, others = self.counts(self.victim.id), self.snapshot_others()
        r = self.delete()
        self.assertEqual((r.status_code, r.get_json()['code']), (409, 'has_tasks'))
        self.assertEqual(r.get_json()['current_counts']['tasks'], 2)
        r = self.delete(delete_tasks='sim')                                      # só o booleano true conta
        self.assertEqual(r.get_json()['code'], 'has_tasks')
        self.assertEqual((self.counts(self.victim.id), self.snapshot_others()), (before, others))
        self.assertIsNotNone(db.session.get(User, self.victim.id))

    def test_delete_with_tasks_when_explicitly_chosen(self):
        others = self.snapshot_others()
        r = self.delete(delete_tasks=True)
        self.assertEqual(r.status_code, 200, r.get_json())
        db.session.expire_all()
        self.assertIsNone(db.session.get(User, self.victim.id))
        for model in (Task, Category, Notification, TaskCollaborator, RefreshToken, UserAvatar):
            col = model.user_id if hasattr(model, 'user_id') else None
            if col is not None:
                self.assertEqual(model.query.filter(col == self.victim.id).count(), 0, model.__name__)
        self.assertEqual(TaskCollaborator.query.filter(TaskCollaborator.task_id.in_([self.vt1['id'], self.vt2['id']])).count(), 0)
        self.assertEqual(Task.query.filter(Task.id.in_([self.vt1['id'], self.vt2['id']])).count(), 0)
        after = self.snapshot_others()
        self.assertEqual({k: after[k] for k in after if k != 'bystander_notifications'},
                         {k: others[k] for k in others if k != 'bystander_notifications'})
        self.assertEqual(Task.query.filter_by(id=self.bt['id']).count(), 1)                 # tarefa do terceiro preservada
        self.assertEqual(TaskCollaborator.query.filter_by(task_id=self.bt['id']).count(), 0)  # só saiu a colaboração da vítima
        self.assertEqual(Notification.query.filter(Notification.task_id.in_([self.vt1['id'], self.vt2['id']])).count(), 0)
        self.assertEqual(self.fk_check(), [])
        row = self.audit_rows(action='user.deleted')[0]
        self.assertEqual((row.result, row.details['delete_tasks'], row.details['tasks']), ('success', True, 2))

    def test_delete_user_without_tasks_does_not_need_the_option(self):
        db.session.expire_all()
        self.assertEqual(self.clear().status_code, 200)                                      # esvazia primeiro
        r = self.delete()
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertIsNone(db.session.get(User, self.victim.id))
        self.assertEqual(self.fk_check(), [])

    def test_confirmations_are_enforced(self):
        for over, status, code in (({'password': 'errada123'}, 403, 'invalid_password'),
                                   ({'confirm_username': 'Vitima'}, 400, 'confirmation_mismatch'),
                                   ({'confirm_username': ''}, 400, 'confirmation_mismatch')):
            r = self.delete(delete_tasks=True, **over)
            self.assertEqual((r.status_code, r.get_json()['code']), (status, code), over)
        self.assertIsNotNone(db.session.get(User, self.victim.id))
        self.assertEqual(self.counts(self.victim.id)['tasks'], 2)

    def test_user_and_admin_cannot_delete(self):
        for who in (self.admin, self.bystander, self.victim):
            r = self.delete(who=who, delete_tasks=True)
            self.assertEqual((r.status_code, r.get_json()['code']), (403, 'forbidden'))
        self.assertIsNotNone(db.session.get(User, self.victim.id))

    def test_superadmin_cannot_delete_self_and_deletes_another_superadmin_only_if_one_remains(self):
        r = self.call(self.sup, 'delete', f'/admin/users/{self.sup.id}', json={
            'password': PASSWORD, 'confirm_username': 'chefe', 'expected_counts': {}})
        self.assertEqual((r.status_code, r.get_json()['code']), (403, 'self_action'))
        other = self.make('s2@example.com', 'superadmin', username='segundo')
        counts = self.call(self.sup, 'get', f'/admin/users/{other.id}/data-summary').get_json()['counts']
        r = self.call(self.sup, 'delete', f'/admin/users/{other.id}', json={
            'password': PASSWORD, 'confirm_username': 'segundo', 'expected_counts': counts})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(User.query.filter_by(role='superadmin').count(), 1)

    def test_stale_superadmin_cannot_delete_the_last_one(self):
        other = self.make('s2@example.com', 'superadmin', username='segundo')
        stale = types.SimpleNamespace(id=other.id, role='superadmin', username='segundo', is_active=True)
        db.session.get(User, other.id).is_active = False; db.session.commit()                # desativado entretanto
        counts = purge.user_counts(self.sup.id)
        with self.assertRaises(permissions.AdminError) as ctx:
            user_admin.delete_user(stale, self.sup.id, {'password': PASSWORD, 'confirm_username': 'chefe', 'expected_counts': counts})
        self.assertEqual(ctx.exception.code, 'inactive_actor')
        db.session.rollback()
        self.assertIsNotNone(db.session.get(User, self.sup.id))

    def test_rollback_is_total_when_the_last_step_fails(self):
        before, others = self.counts(self.victim.id), self.snapshot_others()
        real = purge._exec

        def failing(stmt, expected, what):
            if what == 'users':                            # depois de apagar quase tudo
                raise RuntimeError('falha simulada ao apagar o utilizador')
            return real(stmt, expected, what)
        with mock.patch.object(purge, '_exec', side_effect=failing):
            r = self.delete(delete_tasks=True)
        self.assertEqual((r.status_code, r.get_json()['code']), (500, 'internal_error'))
        self.assertEqual(self.counts(self.victim.id), before)
        self.assertEqual(self.snapshot_others(), others)
        self.assertIsNotNone(db.session.get(User, self.victim.id))
        self.assertEqual(self.fk_check(), [])
        self.assertEqual([e.result for e in self.audit_rows(action='user.deleted')], ['failed'])

    def test_counts_changed_aborts_the_deletion(self):
        stale = self.summary()
        self.client.post('/tasks', headers=self.victim.h, json={'title': 'surpresa'})
        r = self.delete(delete_tasks=True, expected_counts=stale)
        self.assertEqual((r.status_code, r.get_json()['code']), (409, 'counts_changed'))
        self.assertEqual(self.counts(self.victim.id)['tasks'], 3)

    def test_unknown_user(self):
        r = self.call(self.sup, 'delete', '/admin/users/99999', json={'password': PASSWORD})
        self.assertEqual(r.status_code, 404)

    def test_fk_rules_are_untouched(self):
        fks = db.session.execute(text("PRAGMA foreign_key_list('tasks')")).fetchall()
        self.assertTrue(all(row[6] in ('NO ACTION', None) for row in fks))                   # sem ON DELETE CASCADE


# ══════════════════════════════════════════════
# Fase 8 — limpeza global
# ══════════════════════════════════════════════
class TestGlobalWipe(DestructiveBase):
    PHRASE = maintenance.WIPE_PHRASE

    def preview(self, who=None):
        return self.call(who or self.sup, 'post', '/admin/maintenance/wipe/preview')

    def wipe(self, preview, who=None, **over):
        data = {'token': preview['token'], 'password': PASSWORD, 'confirmation': self.PHRASE}
        data.update(over)
        return self.call(who or self.sup, 'post', '/admin/maintenance/wipe', json=data)

    def enable(self):
        self.app.config['ADMIN_ALLOW_GLOBAL_WIPE'] = True

    def everything(self):
        db.session.expire_all()
        return purge.global_counts()

    def test_summary_and_preview_do_not_change_anything(self):
        before = self.everything()
        summary = self.call(self.sup, 'get', '/admin/maintenance/summary').get_json()
        self.assertEqual(summary['tables']['users'], 4)
        self.assertEqual(summary['users_by_role'], {'user': 2, 'admin': 1, 'superadmin': 1})
        self.assertFalse(summary['wipe_enabled'])
        p = self.preview().get_json()
        self.assertEqual((p['counts'], p['wipe_enabled'], p['phrase']), (before, False, self.PHRASE))
        self.assertTrue(p['token'])
        self.assertEqual(self.everything(), before)

    def test_disabled_by_default_even_with_a_valid_token_password_and_phrase(self):
        p = self.preview().get_json()
        before = self.everything()
        r = self.wipe(p)
        self.assertEqual((r.status_code, r.get_json()['code']), (403, 'wipe_disabled'))
        self.assertEqual(self.everything(), before)
        self.assertEqual([(e.result, e.details['code']) for e in self.audit_rows(action='maintenance.wipe')], [('denied', 'wipe_disabled')])

    def test_only_superadmin(self):
        self.enable()
        p = self.preview().get_json()
        for who in (self.admin, self.bystander):
            self.assertEqual(self.preview(who).status_code, 403)
            r = self.wipe(p, who=who)
            self.assertEqual((r.status_code, r.get_json()['code']), (403, 'forbidden'))
        self.assertGreater(self.everything()['tasks'], 0)

    def test_every_confirmation_is_required(self):
        self.enable()
        p = self.preview().get_json()
        before = self.everything()
        cases = [({'token': None}, 400, 'token_invalid'), ({'token': 'lixo'}, 400, 'token_invalid'),
                 ({'confirmation': 'limpar'}, 400, 'confirmation_mismatch'), ({'confirmation': None}, 400, 'confirmation_mismatch'),
                 ({'password': 'errada123'}, 403, 'invalid_password'), ({'password': None}, 403, 'invalid_password')]
        for over, status, code in cases:
            r = self.wipe(p, **over)
            self.assertEqual((r.status_code, r.get_json()['code']), (status, code), over)
        self.assertEqual(self.everything(), before)

    def test_token_is_bound_to_the_administrator_and_expires(self):
        self.enable()
        sup2 = self.make('sup2@example.com', 'superadmin', username='segundo')
        p = self.preview().get_json()
        r = self.wipe(p, who=sup2)
        self.assertEqual((r.status_code, r.get_json()['code']), (400, 'token_invalid'))      # token de outro administrador
        with mock.patch.object(maintenance, 'WIPE_TOKEN_MAX_AGE', -1):
            r = self.wipe(p)
        self.assertEqual((r.status_code, r.get_json()['code']), (409, 'token_expired'))
        forged = dict(p, token=p['token'][:-3] + ('AAA' if not p['token'].endswith('AAA') else 'BBB'))
        self.assertEqual(self.wipe(forged).get_json()['code'], 'token_invalid')
        self.assertGreater(self.everything()['tasks'], 0)

    def test_counts_changed_between_preview_and_execution_abort(self):
        self.enable()
        p = self.preview().get_json()
        self.client.post('/tasks', headers=self.bystander.h, json={'title': 'surgiu depois'})
        before = self.everything()
        r = self.wipe(p)
        self.assertEqual((r.status_code, r.get_json()['code']), (409, 'counts_changed'))
        self.assertEqual(self.everything(), before)

    def test_wipe_removes_operational_data_and_preserves_accounts_schema_and_audit(self):
        self.enable()
        users_before = sorted((u.id, u.username, u.email, u.role, u.is_active, u.password_hash) for u in User.query.all())
        audit_before = AdminAuditLog.query.count()
        p = self.preview().get_json()
        r = self.wipe(p)
        data = r.get_json()
        self.assertEqual(r.status_code, 200, data)
        self.assertEqual(data['deleted'], p['counts'])
        # tudo a zero, exceto o refresh token NOVO do próprio administrador (a limpeza revoga todos e emite um novo)
        self.assertEqual(self.everything(), dict({k: 0 for k in p['counts']}, refresh_tokens=1))
        self.assertEqual([t.user_id for t in RefreshToken.query.all()], [self.sup.id])
        db.session.expire_all()
        self.assertEqual(sorted((u.id, u.username, u.email, u.role, u.is_active, u.password_hash) for u in User.query.all()), users_before)
        self.assertEqual(User.query.filter_by(role='superadmin', is_active=True).count(), 1)
        self.assertGreaterEqual(AdminAuditLog.query.count(), audit_before + 1)                # só cresceu
        row = self.audit_rows(action='maintenance.wipe', result='success')[0]
        self.assertEqual({k: row.details[k] for k in p['counts']}, p['counts'])
        self.assertEqual(set(db.inspect(db.engine).get_table_names()) >= {'users', 'tasks', 'admin_audit_log'}, True)
        self.assertEqual(self.fk_check(), [])
        # o administrador recebe tokens novos e mantém a sessão; as outras sessões foram revogadas
        self.assertEqual(self.client.get('/auth/me', headers={'Authorization': f"Bearer {data['tokens']['access_token']}"}).status_code, 200)
        self.assertEqual(self.client.post('/auth/refresh', json={'refresh_token': data['tokens']['refresh_token']}).status_code, 200)
        self.assertEqual(self.client.post('/auth/refresh', json={'refresh_token': self.victim.tokens['refresh_token']}).status_code, 401)
        # contas continuam a poder entrar
        self.assertEqual(self.client.post('/auth/login', json={'email': 'v@example.com', 'password': PASSWORD}).status_code, 200)

    def test_token_is_single_use(self):
        self.enable()
        p = self.preview().get_json()
        self.assertEqual(self.wipe(p).status_code, 200)
        self.client.post('/tasks', headers=self.bystander.h, json={'title': 'nova'})
        r = self.wipe(p)
        self.assertEqual((r.status_code, r.get_json()['code']), (409, 'token_used'))
        self.assertEqual(self.everything()['tasks'], 1)

    def test_nothing_is_deleted_without_an_active_superadmin(self):
        db.session.get(User, self.sup.id).is_active = False; db.session.commit()
        before = self.everything()
        with self.assertRaises(purge.PurgeMismatch) as ctx:
            purge.wipe_all(before)
        self.assertEqual(ctx.exception.what, 'no_active_superadmin')
        db.session.rollback()
        self.assertEqual(self.everything(), before)

    def test_rollback_is_total_when_a_step_fails(self):
        self.enable()
        p = self.preview().get_json()
        before = self.everything()
        real = purge._exec

        def failing(stmt, expected, what):
            if what == 'user_avatars':                      # último passo
                raise RuntimeError('falha simulada')
            return real(stmt, expected, what)
        with mock.patch.object(purge, '_exec', side_effect=failing):
            r = self.wipe(p)
        self.assertEqual((r.status_code, r.get_json()['code']), (500, 'internal_error'))
        self.assertEqual(self.everything(), before)
        self.assertEqual([e.result for e in self.audit_rows(action='maintenance.wipe')], ['failed'])
        self.assertEqual(self.audit_rows(action='maintenance.wipe', result='success'), [])

    def test_wipe_never_uses_truncate_cascade_or_schema_changes(self):
        """Analisa o código (AST): nenhuma instrução SQL textual perigosa e nenhuma API de esquema."""
        import ast
        import inspect
        for module in (purge, maintenance):
            tree = ast.parse(inspect.getsource(module))
            sql = [n.args[0].value for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, 'id', '') == 'text'
                   and n.args and isinstance(n.args[0], ast.Constant)]
            for statement in sql:
                for forbidden in ('TRUNCATE', 'CASCADE', 'DROP', 'ALTER', 'CREATE'):
                    self.assertNotIn(forbidden, statement.upper(), statement)
            names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
            self.assertTrue({'drop_all', 'create_all', 'drop_table', 'create_table', 'truncate'}.isdisjoint(names), module.__name__)
        self.assertNotIn('text', {n.id for n in ast.walk(ast.parse(inspect.getsource(purge))) if isinstance(n, ast.Name)})


# ══════════════════════════════════════════════
# Promoção inicial do primeiro SUPERADMIN (comando explícito)
# ══════════════════════════════════════════════
class TestPromoteSuperadminCommand(AdminTestCase):
    def invoke(self, *args):
        return self.app.test_cli_runner().invoke(args=['promote-superadmin', *args])

    def test_dry_run_is_the_default_and_changes_nothing(self):
        user = self.make('u@example.com')
        res = self.invoke('--user-id', str(user.id))
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertIn('Simulação', res.output)
        db.session.expire_all()
        self.assertEqual(db.session.get(User, user.id).role, 'user')
        self.assertEqual(AdminAuditLog.query.count(), 0)

    def test_execute_promotes_once_and_audits(self):
        user = self.make('u@example.com'); other = self.make('o@example.com')
        res = self.invoke('--user-id', str(user.id), '--execute')
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertIn('promovido', res.output)
        db.session.expire_all()
        self.assertEqual((db.session.get(User, user.id).role, db.session.get(User, other.id).role), ('superadmin', 'user'))
        row = self.audit_rows()[0]
        self.assertEqual((row.action, row.result, row.target_id, row.actor_id, row.details['via']),
                         ('role.bootstrap_superadmin', 'success', user.id, None, 'cli'))
        # idempotente: não altera nem regista de novo
        res = self.invoke('--user-id', str(user.id), '--execute')
        self.assertEqual(res.exit_code, 0)
        self.assertIn('já é SUPERADMIN', res.output)
        self.assertEqual(AdminAuditLog.query.count(), 1)

    def test_missing_and_inactive_users_are_refused(self):
        inactive = self.make('i@example.com', active=False)
        self.assertEqual(self.invoke('--user-id', '999', '--execute').exit_code, 1)
        res = self.invoke('--user-id', str(inactive.id), '--execute')
        self.assertEqual(res.exit_code, 1)
        db.session.expire_all()
        self.assertEqual(db.session.get(User, inactive.id).role, 'user')
        self.assertEqual(self.invoke().exit_code, 2)                                          # --user-id obrigatório

    def test_function_is_not_called_by_the_app_or_the_migration(self):
        for path in ('backend/app/__init__.py', 'backend/wsgi.py', 'backend/run.py',
                     'backend/migrations/versions/c4d7a1e08b52_add_user_role_and_admin_audit_log.py'):
            self.assertNotIn('promote_superadmin(', (ROOT / path).read_text(), path)

    def test_nobody_is_superadmin_after_migration_and_registration(self):
        self.register('a@example.com'); self.register('b@example.com')
        self.assertEqual(User.query.filter(User.role != 'user').count(), 0)


if __name__ == '__main__':
    import unittest
    unittest.main()
