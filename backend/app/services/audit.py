"""
Auditoria administrativa.

  * `record` acrescenta o registo à sessão atual (sem commit): entra na MESMA transação da operação, por isso
    uma operação revertida não deixa um registo "success" e uma operação confirmada nunca fica sem registo.
  * `record_isolated` grava num commit próprio: serve para tentativas negadas e falhadas (depois de um rollback).
  * `details` aceita só metadados simples e contagens; chaves com nomes sensíveis são descartadas.
"""
import logging
import re

from backend.app.models import db, AdminAuditLog

logger = logging.getLogger(__name__)

# Chaves de segredos óbvios: descartadas sempre, seja qual for o valor.
_ALWAYS_SENSITIVE = re.compile(r'pass|secret|hash|authorization|cookie|credential', re.I)
# "token"/"key" aparecem em contagens legítimas (ex.: refresh_tokens = 3): só se descartam quando o valor
# NÃO é um número (um token ou chave real é texto).
_SENSITIVE_UNLESS_NUMBER = re.compile(r'token|key', re.I)
_MAX_STR = 200


def _clean_value(value, depth=0):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:_MAX_STR]
    if isinstance(value, dict) and depth < 2:
        return sanitize_details(value, depth + 1)
    if isinstance(value, (list, tuple)) and depth < 2:
        return [_clean_value(v, depth + 1) for v in list(value)[:50]]
    return str(value)[:_MAX_STR]


def sanitize_details(details, depth=0):
    """Remove chaves sensíveis e limita tipos e tamanhos."""
    if not details:
        return {}
    clean = {}
    for key, value in details.items():
        name = str(key)
        if _ALWAYS_SENSITIVE.search(name):
            continue
        if _SENSITIVE_UNLESS_NUMBER.search(name) and not (isinstance(value, int) and not isinstance(value, bool)):
            continue
        clean[name[:60]] = _clean_value(value, depth)
    return clean


def _label(user):
    return (getattr(user, 'username', None) or '')[:120] or None


def _build(actor, action, result, target_type, target_id, target_label, details):
    return AdminAuditLog(
        actor_id=getattr(actor, 'id', None),
        actor_label=_label(actor) if actor is not None else None,
        action=action[:60],
        result=result,
        target_type=target_type,
        target_id=target_id,
        target_label=(target_label or None) and str(target_label)[:120],
        details=sanitize_details(details),
    )


def record(actor, action, result='success', target_type=None, target_id=None, target_label=None, details=None):
    """Acrescenta o registo à transação atual (o chamador faz o commit)."""
    entry = _build(actor, action, result, target_type, target_id, target_label, details)
    db.session.add(entry)
    return entry


def record_isolated(actor, action, result, target_type=None, target_id=None, target_label=None, details=None):
    """Grava num commit próprio (negações e falhas). Nunca levanta: a auditoria não pode derrubar a resposta."""
    try:
        entry = _build(actor, action, result, target_type, target_id, target_label, details)   # antes do rollback
        db.session.rollback()
        db.session.add(entry)
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception('Falha ao gravar o registo de auditoria (%s)', action)
