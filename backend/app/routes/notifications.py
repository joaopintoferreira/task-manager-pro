from flask import Blueprint, request, jsonify
#from app.models import db, Notification
from backend.app.models import db, Notification
#from app.auth import token_required
from backend.app.auth import token_required

notifications_bp = Blueprint('notifications', __name__)


@notifications_bp.route('', methods=['GET'])
@token_required
def get_notifications(current_user):
    unread_only = request.args.get('unread_only', 'false').lower() == 'true'
    limit       = min(100, max(1, int(request.args.get('limit', 20))))

    query = Notification.query.filter_by(user_id=current_user.id)
    if unread_only:
        query = query.filter_by(is_read=False)

    notifications = query.order_by(Notification.created_at.desc()).limit(limit).all()
    unread_count  = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()

    return jsonify({
        'notifications': [n.to_dict() for n in notifications],
        'unread_count':  unread_count,
    })


@notifications_bp.route('/<int:notification_id>/read', methods=['POST'])
@token_required
def mark_read(current_user, notification_id):
    notification = Notification.query.filter_by(
        id=notification_id,
        user_id=current_user.id,
    ).first_or_404()

    notification.is_read = True
    db.session.commit()

    return jsonify({'message': 'Notificação marcada como lida!'})


@notifications_bp.route('/read-all', methods=['POST'])
@token_required
def mark_all_read(current_user):
    count = Notification.query.filter_by(
        user_id=current_user.id,
        is_read=False,
    ).update({'is_read': True})
    db.session.commit()

    return jsonify({'message': f'{count} notificações marcadas como lidas!'})


@notifications_bp.route('/<int:notification_id>', methods=['DELETE'])
@token_required
def delete_notification(current_user, notification_id):
    notification = Notification.query.filter_by(
        id=notification_id,
        user_id=current_user.id,
    ).first_or_404()

    db.session.delete(notification)
    db.session.commit()

    return jsonify({'message': 'Notificação removida!'})
