"""
Papéis e permissões do painel administrativo.

  * O papel é lido da BASE DE DADOS a cada pedido (o `token_required` recarrega o utilizador); nunca vem do
    JWT, do corpo do pedido nem do localStorage.
  * Esconder botões no frontend não é segurança: todas as rotas /admin passam por `require_permission`.
  * Tentativas negadas ficam registadas na auditoria.
"""
import time
from collections import defaultdict, deque
from functools import wraps

from flask import jsonify

from backend.app.auth import token_required

ROLE_USER = 'user'
ROLE_ADMIN = 'admin'
ROLE_SUPERADMIN = 'superadmin'
ROLES = (ROLE_USER, ROLE_ADMIN, ROLE_SUPERADMIN)

_ADMIN_PERMISSIONS = frozenset({
    'admin.access',
    'users.list',
    'users.view',
    'users.toggle_active_user',
    'users.remove_avatar_user',
    'audit.view_own',
})

_SUPERADMIN_PERMISSIONS = _ADMIN_PERMISSIONS | frozenset({
    'users.toggle_active_admin',
    'users.toggle_active_superadmin',
    'users.remove_avatar_staff',
    'users.clear_data',
    'users.delete',
    'roles.manage',
    'audit.view_all',
    'maintenance.view',
    'maintenance.wipe',
})

ROLE_PERMISSIONS = {
    ROLE_USER: frozenset(),
    ROLE_ADMIN: _ADMIN_PERMISSIONS,
    ROLE_SUPERADMIN: _SUPERADMIN_PERMISSIONS,
}

# Permissão exigida para ativar/desativar, conforme o papel do ALVO
TOGGLE_ACTIVE_PERMISSION = {
    ROLE_USER: 'users.toggle_active_user',
    ROLE_ADMIN: 'users.toggle_active_admin',
    ROLE_SUPERADMIN: 'users.toggle_active_superadmin',
}
REMOVE_AVATAR_PERMISSION = {
    ROLE_USER: 'users.remove_avatar_user',
    ROLE_ADMIN: 'users.remove_avatar_staff',
    ROLE_SUPERADMIN: 'users.remove_avatar_staff',
}


def permissions_for(role):
    """Permissões de um papel; um valor desconhecido não tem nenhuma (falha fechada)."""
    return ROLE_PERMISSIONS.get(role, frozenset())


def has_permission(user, permission):
    return permission in permissions_for(getattr(user, 'role', None))


def is_staff(user):
    return has_permission(user, 'admin.access')


def deny(message='Acesso negado.', code='forbidden', status=403):
    return jsonify({'message': message, 'code': code}), status


def require_permission(permission):
    """Decorador de rota: autentica (401) e exige `permission` (403, com registo de auditoria).

    A função decorada recebe `current_user` como primeiro argumento, como em `token_required`.
    O atributo `admin_permission` permite a um teste confirmar que NENHUMA rota /admin ficou sem proteção.
    """
    def decorator(view):
        @wraps(view)
        @token_required
        def decorated(current_user, *args, **kwargs):
            if not has_permission(current_user, permission):
                from backend.app.services import audit      # import tardio: evita ciclo no arranque
                audit.record_isolated(current_user, f'denied:{permission}', 'denied',
                                      details={'required_permission': permission})
                return deny('Não tem permissão para esta operação.', 'forbidden')
            return view(current_user, *args, **kwargs)
        decorated.admin_permission = permission
        return decorated
    return decorator


# ──────────────────────────────────────────────
# Limite de tentativas de confirmação de senha (por utilizador, em memória por instância)
# ──────────────────────────────────────────────
REAUTH_MAX_FAILURES = 5
REAUTH_WINDOW_SECONDS = 600
_failures = defaultdict(deque)


def reset_reauth_throttle():
    _failures.clear()


class AdminError(Exception):
    """Falha de regra de negócio do painel: `code` é estável (o frontend decide a mensagem); `message` é segura."""

    def __init__(self, code, message, status=400, extra=None):
        super().__init__(code)
        self.code, self.message, self.status, self.extra = code, message, status, extra or {}

    def response(self):
        body = {'message': self.message, 'code': self.code}
        body.update(self.extra)
        return jsonify(body), self.status


def verify_actor_password(user, password):
    """Confirma a senha do próprio administrador antes de uma operação destrutiva (levanta AdminError).

    Falhas repetidas bloqueiam novas tentativas durante uma janela (429), por utilizador e não por IP.
    O limite é em memória por instância (em serverless não é global): é uma proteção adicional, não a única.
    """
    now = time.monotonic()
    window = _failures[user.id]
    while window and now - window[0] > REAUTH_WINDOW_SECONDS:
        window.popleft()
    if len(window) >= REAUTH_MAX_FAILURES:
        raise AdminError('reauth_throttled', 'Demasiadas tentativas de confirmação. Aguarde alguns minutos.', 429)
    if not isinstance(password, str) or not password or not user.check_password(password):
        window.append(now)
        raise AdminError('invalid_password', 'Senha incorreta.', 403)
    window.clear()
