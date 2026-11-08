"""
Remoção de dados em massa (limpeza individual, exclusão de utilizador e limpeza global).

Regras:
  * Tudo corre na transação do chamador: este módulo NUNCA faz commit.
  * Antes de apagar, as contagens são recalculadas DENTRO da transação e comparadas com as que o administrador
    viu na pré-visualização; qualquer divergência levanta `PurgeMismatch` (o chamador faz rollback).
  * Cada DELETE/UPDATE confere o número de linhas afetadas com o esperado; se divergir, aborta.
  * Ordem segura para as FKs reais (todas NO ACTION, exceto user_avatars):
    notificações -> colaborações -> tarefas -> categorias -> tokens -> avatares -> utilizador.
  * Nunca usa CASCADE nem TRUNCATE e não altera o esquema.
"""
from sqlalchemy import delete, func, or_, select, update

from backend.app.models import (db, User, Task, Category, Notification, TaskCollaborator,
                                RefreshToken, UserAvatar)


class PurgeMismatch(Exception):
    """As contagens (ou as linhas afetadas) divergem do esperado: a operação deve ser revertida."""

    def __init__(self, what, expected=None, found=None):
        super().__init__(what)
        self.what, self.expected, self.found = what, expected, found


class HasTasks(Exception):
    def __init__(self, counts):
        super().__init__('has_tasks')
        self.counts = counts


def _count(stmt):
    return db.session.execute(stmt).scalar_one()


def _exec(stmt, expected, what):
    found = db.session.execute(stmt).rowcount
    if found != expected:
        raise PurgeMismatch(what, expected, found)
    return found


def _owned_task_ids(user_id):
    return select(Task.id).where(Task.user_id == user_id)


# ──────────────────────────────────────────────
# Por utilizador
# ──────────────────────────────────────────────
USER_COUNT_KEYS = ('tasks', 'categories', 'notifications', 'collaborations', 'refresh_tokens', 'avatars',
                   'third_party_notifications')


def user_counts(user_id):
    """Contagens do que pertence ao utilizador (e do que nele se apoia)."""
    owned = _owned_task_ids(user_id)
    return {
        'tasks':          _count(select(func.count()).select_from(Task).where(Task.user_id == user_id)),
        'categories':     _count(select(func.count()).select_from(Category).where(Category.user_id == user_id)),
        'notifications':  _count(select(func.count()).select_from(Notification).where(Notification.user_id == user_id)),
        'collaborations': _count(select(func.count()).select_from(TaskCollaborator).where(
            or_(TaskCollaborator.user_id == user_id, TaskCollaborator.task_id.in_(owned)))),
        'refresh_tokens': _count(select(func.count()).select_from(RefreshToken).where(RefreshToken.user_id == user_id)),
        'avatars':        _count(select(func.count()).select_from(UserAvatar).where(UserAvatar.user_id == user_id)),
        # notificações de OUTROS utilizadores que apontam para tarefas deste: ficam, com task_id = NULL
        'third_party_notifications': _count(select(func.count()).select_from(Notification).where(
            Notification.task_id.in_(owned), Notification.user_id != user_id)),
    }


def _check_expected(user_id, expected):
    current = user_counts(user_id)
    if not isinstance(expected, dict) or {k: expected.get(k) for k in USER_COUNT_KEYS} != current:
        raise PurgeMismatch('counts', expected, current)
    return current


def _clear_owned_data(user_id, counts):
    """Passos comuns à limpeza e à exclusão (tudo o que depende do utilizador, menos tokens/avatar/conta)."""
    owned = _owned_task_ids(user_id)
    # 1. notificações de terceiros sobre tarefas deste utilizador: ficam, sem tarefa (como no ORM ao apagar uma tarefa)
    _exec(update(Notification).where(Notification.task_id.in_(owned), Notification.user_id != user_id)
          .values(task_id=None), counts['third_party_notifications'], 'notifications.third_party')
    # 2. notificações do próprio utilizador
    _exec(delete(Notification).where(Notification.user_id == user_id), counts['notifications'], 'notifications')
    # 3. colaborações: as do utilizador e as das suas tarefas
    _exec(delete(TaskCollaborator).where(or_(TaskCollaborator.user_id == user_id, TaskCollaborator.task_id.in_(owned))),
          counts['collaborations'], 'task_collaborators')
    # 4. por regra, tarefas de outros não usam categorias alheias; se existirem, desligam-se em vez de falhar a FK
    own_categories = select(Category.id).where(Category.user_id == user_id)
    db.session.execute(update(Task).where(Task.category_id.in_(own_categories), Task.user_id != user_id)
                       .values(category_id=None))
    # 5. tarefas e 6. categorias
    _exec(delete(Task).where(Task.user_id == user_id), counts['tasks'], 'tasks')
    _exec(delete(Category).where(Category.user_id == user_id), counts['categories'], 'categories')


def clear_user_data(user_id, expected):
    """Limpa os dados do utilizador mantendo a conta, o papel, o estado, o avatar e os refresh tokens."""
    counts = _check_expected(user_id, expected)
    _clear_owned_data(user_id, counts)
    return counts


def delete_user(user_id, expected, delete_tasks=False):
    """Exclui o utilizador. Recusa se tiver tarefas e `delete_tasks` não for verdadeiro."""
    counts = _check_expected(user_id, expected)
    if counts['tasks'] > 0 and not delete_tasks:
        raise HasTasks(counts)
    _clear_owned_data(user_id, counts)
    _exec(delete(RefreshToken).where(RefreshToken.user_id == user_id), counts['refresh_tokens'], 'refresh_tokens')
    _exec(delete(UserAvatar).where(UserAvatar.user_id == user_id), counts['avatars'], 'user_avatars')
    _exec(delete(User).where(User.id == user_id), 1, 'users')
    return counts


# ──────────────────────────────────────────────
# Global
# ──────────────────────────────────────────────
GLOBAL_COUNT_KEYS = ('tasks', 'categories', 'notifications', 'collaborations', 'refresh_tokens', 'avatars')


def global_counts():
    """Contagens do que a limpeza global apagaria. As contas, o esquema e a auditoria não entram."""
    n = lambda model: _count(select(func.count()).select_from(model))
    return {
        'tasks': n(Task), 'categories': n(Category), 'notifications': n(Notification),
        'collaborations': n(TaskCollaborator), 'refresh_tokens': n(RefreshToken), 'avatars': n(UserAvatar),
    }


def wipe_all(expected):
    """Apaga todos os dados operacionais. Preserva contas, esquema, alembic_version e auditoria."""
    current = global_counts()
    if not isinstance(expected, dict) or {k: expected.get(k) for k in GLOBAL_COUNT_KEYS} != current:
        raise PurgeMismatch('counts', expected, current)
    active_superadmins = _count(select(func.count()).select_from(User).where(User.role == 'superadmin',
                                                                             User.is_active.is_(True)))
    if active_superadmins < 1:
        raise PurgeMismatch('no_active_superadmin', 1, active_superadmins)
    _exec(delete(Notification), current['notifications'], 'notifications')
    _exec(delete(TaskCollaborator), current['collaborations'], 'task_collaborators')
    _exec(delete(Task), current['tasks'], 'tasks')
    _exec(delete(Category), current['categories'], 'categories')
    _exec(delete(RefreshToken), current['refresh_tokens'], 'refresh_tokens')
    _exec(delete(UserAvatar), current['avatars'], 'user_avatars')
    return current
