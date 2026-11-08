import logging
from flask import Blueprint, request, jsonify, current_app, Response
from functools import wraps
import jwt
import uuid
from datetime import datetime, timedelta
from sqlalchemy.exc import IntegrityError
#from app.models import db, User, RefreshToken
from backend.app.models import db, User, RefreshToken, UserAvatar
#from app.utils import validate_password, validate_email
from backend.app.utils import validate_password, validate_email
#from app import limiter
from backend.app import limiter

logger = logging.getLogger(__name__)
auth_bp = Blueprint('auth', __name__)

# ──────────────────────────────────────────────
# Decorator de autenticação
# ──────────────────────────────────────────────
def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            token = auth_header.split(' ')[1]

        if not token:
            return jsonify({'message': 'Token ausente!'}), 401

        try:
            data = jwt.decode(token, current_app.config['SECRET_KEY'], algorithms=['HS256'])
            # Refresh tokens não podem ser usados para acessar a API
            if data.get('type') != 'access':
                return jsonify({'message': 'Token inválido!'}), 401
            current_user = db.session.get(User, data['user_id'])
            if not current_user or not current_user.is_active:
                return jsonify({'message': 'Usuário não encontrado ou inativo!'}), 401
        except jwt.ExpiredSignatureError:
            return jsonify({'message': 'Token expirado!'}), 401
        except jwt.InvalidTokenError:
            return jsonify({'message': 'Token inválido!'}), 401
        except Exception as e:
            logger.error(f"Erro de autenticação: {e}")
            return jsonify({'message': 'Erro de autenticação!'}), 401

        return f(current_user, *args, **kwargs)
    return decorated


# ──────────────────────────────────────────────
# Geração de tokens
# ──────────────────────────────────────────────
def generate_tokens(user_id):
    access_token = jwt.encode(
        {
            'user_id': user_id,
            'type': 'access',
            'exp': datetime.utcnow() + current_app.config['JWT_ACCESS_TOKEN_EXPIRES'],
        },
        current_app.config['SECRET_KEY'],
        algorithm='HS256',
    )

    refresh_token = jwt.encode(
        {
            'user_id': user_id,
            'type': 'refresh',
            'jti': uuid.uuid4().hex,   # garante que cada refresh token seja único
            'exp': datetime.utcnow() + current_app.config['JWT_REFRESH_TOKEN_EXPIRES'],
        },
        current_app.config['SECRET_KEY'],
        algorithm='HS256',
    )

    # Limpar tokens antigos do usuário (evitar acúmulo)
    RefreshToken.query.filter_by(user_id=user_id).filter(
        RefreshToken.expires_at < datetime.utcnow()
    ).delete()

    db.session.add(RefreshToken(
        user_id=user_id,
        token=refresh_token,
        expires_at=datetime.utcnow() + current_app.config['JWT_REFRESH_TOKEN_EXPIRES'],
    ))
    db.session.commit()

    return {
        'access_token':  access_token,
        'refresh_token': refresh_token,
        'expires_in':    int(current_app.config['JWT_ACCESS_TOKEN_EXPIRES'].total_seconds()),
    }


# ──────────────────────────────────────────────
# Rotas
# ──────────────────────────────────────────────
@auth_bp.route('/register', methods=['POST'])
@limiter.limit("10 per hour")
def register():
    data = request.get_json() or {}

    email    = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''
    username = (data.get('username') or '').strip()

    # Validações
    if not email or not password:
        return jsonify({'message': 'Email e senha são obrigatórios!'}), 400

    if not validate_email(email) or len(email) > 120:
        return jsonify({'message': 'Formato de email inválido!'}), 400

    if len(username) > 80:
        return jsonify({'message': 'Nome de usuário deve ter no máximo 80 caracteres!'}), 400

    is_valid, msg = validate_password(password)
    if not is_valid:
        return jsonify({'message': msg}), 400

    if User.query.filter_by(email=email).first():
        return jsonify({'message': 'Email já cadastrado!'}), 400

    if username and User.query.filter_by(username=username).first():
        return jsonify({'message': 'Nome de usuário já existe!'}), 400

    if not username:
        # Nome derivado do email; evita colisão com a restrição de unicidade
        base = email.split('@')[0][:70]
        username, n = base, 1
        while User.query.filter_by(username=username).first():
            n += 1
            username = f'{base}{n}'

    user = User(username=username, email=email)
    user.set_password(password)

    db.session.add(user)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({'message': 'Email ou nome de usuário já cadastrado!'}), 400

    logger.info(f"Novo usuário registrado: {email}")
    tokens = generate_tokens(user.id)

    return jsonify({
        'message': 'Usuário registrado com sucesso!',
        'user':    user.to_dict(),
        'tokens':  tokens,
    }), 201


@auth_bp.route('/login', methods=['POST'])
@limiter.limit("5 per minute")
def login():
    data = request.get_json() or {}

    email    = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''

    if not email or not password:
        return jsonify({'message': 'Email e senha são obrigatórios!'}), 400

    user = User.query.filter_by(email=email).first()

    # Mesmo delay para email inexistente (evitar user enumeration)
    if not user or not user.check_password(password):
        logger.warning(f"Tentativa de login falhou para: {email}")
        return jsonify({'message': 'Credenciais inválidas!'}), 401

    if not user.is_active:
        return jsonify({'message': 'Conta desativada. Entre em contato com o suporte.'}), 403

    user.last_login = datetime.utcnow()
    db.session.commit()

    logger.info(f"Login bem-sucedido: {email}")
    tokens = generate_tokens(user.id)

    return jsonify({
        'message': 'Login realizado com sucesso!',
        'user':    user.to_dict(),
        'tokens':  tokens,
    })


@auth_bp.route('/refresh', methods=['POST'])
def refresh():
    data          = request.get_json() or {}
    refresh_token = data.get('refresh_token')

    if not refresh_token:
        return jsonify({'message': 'Refresh token é obrigatório!'}), 400

    try:
        payload = jwt.decode(
            refresh_token,
            current_app.config['SECRET_KEY'],
            algorithms=['HS256'],
        )

        if payload.get('type') != 'refresh' or 'user_id' not in payload:
            return jsonify({'message': 'Refresh token inválido!'}), 401

        stored = RefreshToken.query.filter_by(
            token=refresh_token,
            user_id=payload['user_id'],
        ).first()

        if not stored or stored.expires_at < datetime.utcnow():
            return jsonify({'message': 'Refresh token inválido ou expirado!'}), 401

        user = db.session.get(User, payload['user_id'])
        if not user or not user.is_active:
            return jsonify({'message': 'Usuário não encontrado ou inativo!'}), 401

        # Uso único: a remoção atômica impede que o mesmo token seja usado duas vezes
        if RefreshToken.query.filter_by(id=stored.id).delete() == 0:
            db.session.rollback()
            return jsonify({'message': 'Refresh token inválido ou expirado!'}), 401
        tokens = generate_tokens(payload['user_id'])

        return jsonify({
            'message': 'Tokens atualizados com sucesso!',
            'tokens':  tokens,
        })

    except jwt.ExpiredSignatureError:
        return jsonify({'message': 'Refresh token inválido ou expirado!'}), 401
    except jwt.InvalidTokenError:
        return jsonify({'message': 'Refresh token inválido!'}), 401


@auth_bp.route('/logout', methods=['POST'])
@token_required
def logout(current_user):
    data          = request.get_json() or {}
    refresh_token = data.get('refresh_token')

    if refresh_token:
        RefreshToken.query.filter_by(
            token=refresh_token,
            user_id=current_user.id,
        ).delete()
        db.session.commit()

    logger.info(f"Logout: {current_user.email}")
    return jsonify({'message': 'Logout realizado com sucesso!'})


@auth_bp.route('/me', methods=['GET'])
@token_required
def me(current_user):
    """Retorna os dados do usuário autenticado."""
    return jsonify({'user': current_user.to_dict()})


# ──────────────────────────────────────────────
# Perfil (qualquer utilizador): nome, email e senha
# Só aceita campos de uma lista fixa: papel, estado e outros campos administrativos NUNCA são alteráveis aqui.
# ──────────────────────────────────────────────
PROFILE_FIELDS = {'username', 'email', 'current_password'}
PASSWORD_FIELDS = {'current_password', 'new_password'}


def _confirm_own_password(user, password):
    """Valida a senha atual (com limite de tentativas por utilizador). Devolve uma resposta de erro ou None."""
    from backend.app.services.permissions import AdminError, verify_actor_password   # import tardio: evita ciclo
    try:
        verify_actor_password(user, password)
    except AdminError as exc:
        return exc.response()
    return None


@auth_bp.route('/me', methods=['PATCH'])
@limiter.limit("30 per hour")
@token_required
def update_me(current_user):
    """Altera nome de exibição e/ou email. Mudar o email exige a senha atual (é o identificador de login)."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not data:
        return jsonify({'message': 'Corpo da requisição inválido.', 'code': 'invalid_request'}), 400
    extra = sorted(set(data) - PROFILE_FIELDS)
    if extra:
        return jsonify({'message': 'Campos não permitidos neste pedido.', 'code': 'invalid_fields'}), 400

    username = current_user.username
    if 'username' in data:
        value = data['username']
        if not isinstance(value, str) or not value.strip():
            return jsonify({'message': 'O nome de usuário não pode ficar vazio.', 'code': 'invalid_username'}), 400
        username = value.strip()
        if len(username) > 80:
            return jsonify({'message': 'Nome de usuário deve ter no máximo 80 caracteres!', 'code': 'invalid_username'}), 400

    email = current_user.email
    if 'email' in data:
        value = data['email']
        if not isinstance(value, str):
            return jsonify({'message': 'Formato de email inválido!', 'code': 'invalid_email'}), 400
        email = value.strip().lower()
        if not validate_email(email) or len(email) > 120:
            return jsonify({'message': 'Formato de email inválido!', 'code': 'invalid_email'}), 400

    if username != current_user.username:
        if User.query.filter(User.username == username, User.id != current_user.id).first():
            return jsonify({'message': 'Nome de usuário já existe!', 'code': 'username_taken'}), 409
    if email != current_user.email:
        if User.query.filter(User.email == email, User.id != current_user.id).first():
            return jsonify({'message': 'Email já cadastrado!', 'code': 'email_taken'}), 409
        error = _confirm_own_password(current_user, data.get('current_password'))
        if error:
            return error

    if username == current_user.username and email == current_user.email:
        return jsonify({'message': 'Nada para alterar.', 'user': current_user.to_dict()})

    current_user.username, current_user.email = username, email
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({'message': 'Email ou nome de usuário já cadastrado!', 'code': 'conflict'}), 409

    logger.info(f"Perfil atualizado: user {current_user.id}")
    return jsonify({'message': 'Perfil atualizado!', 'user': current_user.to_dict()})


@auth_bp.route('/password', methods=['PUT'])
@limiter.limit("10 per hour")
@token_required
def change_password(current_user):
    """Altera a senha: exige a senha atual, aplica a política e revoga TODOS os refresh tokens do utilizador.

    Devolve tokens novos para a sessão atual não terminar. Limitação conhecida: os JWT de ACESSO já emitidos
    (outros dispositivos) continuam válidos até expirarem (15 min por omissão), porque o token não leva versão.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not data or set(data) - PASSWORD_FIELDS:
        return jsonify({'message': 'Corpo da requisição inválido.', 'code': 'invalid_request'}), 400
    new_password = data.get('new_password')
    if not isinstance(new_password, str) or not new_password:
        return jsonify({'message': 'Indique a nova senha.', 'code': 'invalid_password'}), 400

    error = _confirm_own_password(current_user, data.get('current_password'))
    if error:
        return error
    is_valid, msg = validate_password(new_password)
    if not is_valid:
        return jsonify({'message': msg, 'code': 'weak_password'}), 400
    if current_user.check_password(new_password):
        return jsonify({'message': 'A nova senha deve ser diferente da atual.', 'code': 'same_password'}), 400

    current_user.set_password(new_password)
    RefreshToken.query.filter_by(user_id=current_user.id).delete()      # revoga todas as sessões
    tokens = generate_tokens(current_user.id)                            # mesmo commit: senha + revogação + token novo

    logger.info(f"Senha alterada: user {current_user.id}")
    return jsonify({'message': 'Senha alterada! As outras sessões foram encerradas.', 'tokens': tokens})


# ──────────────────────────────────────────────
# Foto de perfil
# O usuário é sempre o do token: não há parâmetro de id, então ninguém
# consegue ler ou alterar a foto de outra pessoa.
# ──────────────────────────────────────────────
AVATAR_MAX_BYTES = 2 * 1024 * 1024


def detect_image_type(data: bytes):
    """Identifica o formato pelos bytes iniciais (não confia no Content-Type nem na extensão)."""
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    return None


@auth_bp.route('/avatar', methods=['GET'])
@token_required
def get_avatar(current_user):
    avatar = db.session.get(UserAvatar, current_user.id)
    if not avatar:
        return jsonify({'message': 'Sem foto de perfil.'}), 404
    return Response(
        bytes(avatar.data),
        mimetype=avatar.content_type,
        headers={'Cache-Control': 'private, no-cache', 'X-Content-Type-Options': 'nosniff'},
    )


@auth_bp.route('/avatar', methods=['PUT'])
@token_required
def set_avatar(current_user):
    """Corpo da requisição = bytes da imagem (PNG, JPEG, GIF ou WebP, até 2 MB)."""
    data = request.get_data()  # acima de MAX_CONTENT_LENGTH resulta em 413
    if not data:
        return jsonify({'message': 'Nenhuma imagem enviada!'}), 400
    if len(data) > AVATAR_MAX_BYTES:
        return jsonify({'message': 'Imagem muito grande (máximo 2 MB).'}), 413

    content_type = detect_image_type(data)
    if not content_type:
        return jsonify({'message': 'Formato inválido. Use PNG, JPEG, GIF ou WebP.'}), 400

    avatar = db.session.get(UserAvatar, current_user.id)
    if avatar is None:
        avatar = UserAvatar(user_id=current_user.id)
        db.session.add(avatar)
    avatar.content_type = content_type
    avatar.data = data
    avatar.updated_at = datetime.utcnow()
    db.session.commit()

    logger.info(f"Foto de perfil atualizada: user {current_user.id}")
    return jsonify({'message': 'Foto atualizada!', 'user': current_user.to_dict()})


@auth_bp.route('/avatar', methods=['DELETE'])
@token_required
def delete_avatar(current_user):
    UserAvatar.query.filter_by(user_id=current_user.id).delete()
    db.session.commit()
    return jsonify({'message': 'Foto removida!', 'user': current_user.to_dict()})
