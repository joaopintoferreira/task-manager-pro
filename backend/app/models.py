from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timezone
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


def iso_utc(dt):
    """Serializa um datetime como ISO 8601 em UTC com sufixo 'Z'.

    As colunas são TIMESTAMP sem fuso e guardam sempre UTC (datetime.utcnow).
    O sufixo 'Z' deixa explícito para o cliente que é UTC; sem ele o JavaScript
    interpretaria o valor como horário local.
    """
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat() + 'Z'

class User(db.Model):
    __tablename__ = 'users'

    id           = db.Column(db.Integer, primary_key=True)
    username     = db.Column(db.String(80),  unique=True, nullable=False)
    email        = db.Column(db.String(120), unique=True, nullable=False)
    password_hash= db.Column(db.String(255), nullable=False)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    last_login   = db.Column(db.DateTime)
    is_active    = db.Column(db.Boolean, default=True)

    tasks          = db.relationship('Task',             backref='owner',   lazy='dynamic', foreign_keys='Task.user_id')
    categories     = db.relationship('Category',         backref='user',    lazy='dynamic')
    refresh_tokens = db.relationship('RefreshToken',     backref='user',    lazy='dynamic')
    collaborations = db.relationship('TaskCollaborator', backref='user',    lazy='dynamic')
    notifications  = db.relationship('Notification',     backref='user',    lazy='dynamic')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def to_dict(self):
        # Consulta só a data: o conteúdo binário da foto não é carregado aqui
        avatar_updated_at = db.session.query(UserAvatar.updated_at).filter_by(user_id=self.id).scalar()
        return {
            'id':         self.id,
            'username':   self.username,
            'email':      self.email,
            'created_at': iso_utc(self.created_at),
            'is_active':  self.is_active,
            'has_avatar': avatar_updated_at is not None,
            'avatar_updated_at': iso_utc(avatar_updated_at),
        }


class UserAvatar(db.Model):
    """Foto de perfil (uma por usuário), armazenada no PostgreSQL.

    Fica em tabela própria para que a imagem não seja carregada junto com o
    usuário a cada requisição autenticada. Armazenar no banco evita depender do
    disco local, que é efêmero na Vercel.
    """
    __tablename__ = 'user_avatars'

    user_id      = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    content_type = db.Column(db.String(50), nullable=False)
    data         = db.Column(db.LargeBinary, nullable=False)
    updated_at   = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class RefreshToken(db.Model):
    __tablename__ = 'refresh_tokens'

    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    token      = db.Column(db.String(500), nullable=False, unique=True)
    expires_at = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Category(db.Model):
    __tablename__ = 'categories'

    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(80), nullable=False)
    color      = db.Column(db.String(20), default='#6c757d')
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    tasks = db.relationship('Task', backref='category', lazy='dynamic')

    def to_dict(self):
        return {
            'id':         self.id,
            'name':       self.name,
            'color':      self.color,
            'user_id':    self.user_id,
            'task_count': self.tasks.count(),
        }


class Task(db.Model):
    __tablename__ = 'tasks'

    id          = db.Column(db.Integer, primary_key=True)
    title       = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    due_date    = db.Column(db.DateTime)
    priority    = db.Column(db.String(20), default='medium')   # high / medium / low
    status      = db.Column(db.String(20), default='pending')  # pending / in_progress / completed
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at  = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user_id     = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('categories.id'))

    collaborators = db.relationship('TaskCollaborator', backref='task', lazy='dynamic', cascade='all, delete-orphan')
    notifications = db.relationship('Notification',     backref='task', lazy='dynamic')

    def to_dict(self):
        return {
            'id':           self.id,
            'title':        self.title,
            'description':  self.description,
            'due_date':     iso_utc(self.due_date),
            'priority':     self.priority,
            'status':       self.status,
            'created_at':   iso_utc(self.created_at),
            'updated_at':   iso_utc(self.updated_at),
            'user_id':      self.user_id,
            # Nome do dono para quem vê a tarefa partilhada (sem email); None se o dono não existir
            'owner':        {'id': self.owner.id, 'username': self.owner.username} if self.owner else None,
            'category_id':  self.category_id,
            'category':     self.category.to_dict() if self.category else None,
            'collaborators': [c.to_dict() for c in self.collaborators],
            'is_overdue':   (
                self.due_date is not None
                and self.due_date < datetime.utcnow()
                and self.status != 'completed'
            ),
        }

    def can_access(self, user_id):
        if self.user_id == user_id:
            return True
        return self.collaborators.filter_by(user_id=user_id).first() is not None

    def can_edit(self, user_id):
        if self.user_id == user_id:
            return True
        return self.collaborators.filter_by(user_id=user_id, role='editor').first() is not None


class TaskCollaborator(db.Model):
    __tablename__ = 'task_collaborators'

    id         = db.Column(db.Integer, primary_key=True)
    task_id    = db.Column(db.Integer, db.ForeignKey('tasks.id'), nullable=False)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    role       = db.Column(db.String(20), default='viewer')   # viewer / editor
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('task_id', 'user_id', name='unique_collaborator'),
    )

    def to_dict(self):
        return {
            'id':       self.id,
            'task_id':  self.task_id,
            'user_id':  self.user_id,
            'username': self.user.username,
            'email':    self.user.email,
            'role':     self.role,
        }


class Notification(db.Model):
    __tablename__ = 'notifications'

    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    task_id    = db.Column(db.Integer, db.ForeignKey('tasks.id'))
    title      = db.Column(db.String(200), nullable=False)
    message    = db.Column(db.Text, nullable=False)
    type       = db.Column(db.String(50), nullable=False)
    is_read    = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id':         self.id,
            'title':      self.title,
            'message':    self.message,
            'type':       self.type,
            'is_read':    self.is_read,
            'created_at': iso_utc(self.created_at),
            'task_id':    self.task_id,
        }
