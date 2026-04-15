"""
Utilitários — Task Manager
"""
import re
import logging
from datetime import datetime, timedelta
from flask import jsonify
#from app.models import db, Notification, Task, RefreshToken
from backend.app.models import db, Notification, Task, RefreshToken

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Notificações
# ──────────────────────────────────────────────
def create_notification(user_id, title, message, notification_type, task_id=None):
    try:
        notification = Notification(
            user_id=user_id,
            title=title,
            message=message,
            type=notification_type,
            task_id=task_id,
            is_read=False,
            created_at=datetime.utcnow(),
        )
        db.session.add(notification)
        db.session.commit()
        logger.debug(f"Notificação criada para user {user_id}: {title}")
        return notification
    except Exception as e:
        db.session.rollback()
        logger.error(f"Erro ao criar notificação: {e}")
        return None


def check_due_date_notifications():
    """Verifica tarefas próximas do prazo e cria notificações. Executar via cron."""
    try:
        upcoming = Task.query.filter(
            Task.due_date.isnot(None),
            Task.due_date.between(datetime.utcnow(), datetime.utcnow() + timedelta(hours=24)),
            Task.status != 'completed',
        ).all()

        count = 0
        for task in upcoming:
            hours_until = int((task.due_date - datetime.utcnow()).total_seconds() / 3600)

            create_notification(
                user_id=task.user_id,
                title=f"Prazo se aproximando: {task.title}",
                message=f"A tarefa '{task.title}' vence em {hours_until}h.",
                notification_type='due_date_reminder',
                task_id=task.id,
            )
            count += 1

            for collab in task.collaborators:
                create_notification(
                    user_id=collab.user_id,
                    title=f"Prazo se aproximando: {task.title}",
                    message=f"A tarefa compartilhada '{task.title}' vence em {hours_until}h.",
                    notification_type='due_date_reminder',
                    task_id=task.id,
                )
                count += 1

        logger.info(f"{count} notificações de prazo criadas")
        return count
    except Exception as e:
        logger.error(f"Erro ao verificar prazos: {e}")
        return 0


# ──────────────────────────────────────────────
# Validações
# ──────────────────────────────────────────────
def validate_email(email: str) -> bool:
    pattern = r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$'
    return bool(re.match(pattern, email))


def validate_password(password: str) -> tuple[bool, str]:
    if len(password) < 8:
        return False, "A senha deve ter pelo menos 8 caracteres."
    if len(password) > 100:
        return False, "A senha deve ter no máximo 100 caracteres."
    if not re.search(r'\d', password):
        return False, "A senha deve conter pelo menos um número."
    if not re.search(r'[A-Za-z]', password):
        return False, "A senha deve conter pelo menos uma letra."
    return True, "Senha válida."


# ──────────────────────────────────────────────
# Limpeza periódica
# ──────────────────────────────────────────────
def cleanup_old_data():
    """Remove tokens e notificações antigas. Executar periodicamente."""
    try:
        thirty_days_ago  = datetime.utcnow() - timedelta(days=30)
        ninety_days_ago  = datetime.utcnow() - timedelta(days=90)

        tokens_deleted = RefreshToken.query.filter(
            RefreshToken.expires_at < datetime.utcnow()
        ).delete()

        notifs_deleted = Notification.query.filter(
            Notification.created_at < ninety_days_ago,
            Notification.is_read == True,
        ).delete()

        db.session.commit()
        logger.info(f"Limpeza: {tokens_deleted} tokens e {notifs_deleted} notificações removidas")
        return tokens_deleted + notifs_deleted
    except Exception as e:
        db.session.rollback()
        logger.error(f"Erro na limpeza: {e}")
        return 0


# ──────────────────────────────────────────────
# Respostas padrão
# ──────────────────────────────────────────────
def success_response(data=None, message="Sucesso", status_code=200):
    response = {'success': True, 'message': message}
    if data is not None:
        response['data'] = data
    return jsonify(response), status_code


def error_response(message="Erro", error_code=None, status_code=400):
    response = {'success': False, 'message': message}
    if error_code:
        response['error_code'] = error_code
    return jsonify(response), status_code
