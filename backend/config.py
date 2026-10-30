import os
from dotenv import load_dotenv
from datetime import timedelta

load_dotenv()

class Config:
    # Sem valor padrão: a ausência é tratada em create_app (falha fora do desenvolvimento).
    SECRET_KEY = os.getenv('SECRET_KEY')

    SQLALCHEMY_DATABASE_URI = os.getenv('DATABASE_URL', 'sqlite:///task_manager.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    MAX_CONTENT_LENGTH = 2 * 1024 * 1024   # limite do corpo (foto de perfil: 2 MB)
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_recycle': 3600,
        'pool_pre_ping': True,
    }

    JWT_ACCESS_TOKEN_EXPIRES  = timedelta(minutes=int(os.getenv('JWT_ACCESS_EXPIRY') or 15))
    JWT_REFRESH_TOKEN_EXPIRES = timedelta(days=int(os.getenv('JWT_REFRESH_EXPIRY') or 7))

    CORS_ORIGINS = os.getenv('CORS_ORIGINS', 'http://localhost:3000,http://127.0.0.1:5500').split(',')

    # ── Assistente de IA (opcional; sem AI_PROVIDER/AI_API_KEY/AI_MODEL o recurso fica desativado) ──
    # Sem modelo padrão de propósito: o modelo disponível/gratuito muda e deve ser escolhido pelo operador.
    AI_PROVIDER = (os.getenv('AI_PROVIDER') or '').strip().lower()      # groq | openrouter
    AI_API_KEY  = (os.getenv('AI_API_KEY') or '').strip()
    AI_MODEL    = (os.getenv('AI_MODEL') or '').strip()
    AI_BASE_URL = (os.getenv('AI_BASE_URL') or '').strip()              # opcional: endpoint oficial se vazio
    AI_TIMEOUT_SECONDS = os.getenv('AI_TIMEOUT_SECONDS') or '20'
    AI_MAX_TOKENS      = os.getenv('AI_MAX_TOKENS') or '600'

    DEBUG = False
    TESTING = False

class DevelopmentConfig(Config):
    DEBUG = True
    # Chave de conveniência apenas para desenvolvimento local
    SECRET_KEY = os.getenv('SECRET_KEY') or 'dev-secret-key'
    SQLALCHEMY_DATABASE_URI = os.getenv('DEV_DATABASE_URL', 'sqlite:///dev_task_manager.db')
    SQLALCHEMY_ENGINE_OPTIONS = {}          # sem pool para SQLite

class ProductionConfig(Config):
    DEBUG = False

    # NeonDB (PostgreSQL). Use a connection string do endpoint "pooled" (host com "-pooler").
    # Normaliza o prefixo antigo "postgres://" aceito por alguns provedores
    _db_url = os.getenv('DATABASE_URL', '')
    if _db_url.startswith('postgres://'):
        _db_url = _db_url.replace('postgres://', 'postgresql://', 1)

    # Neon exige TLS: adiciona sslmode se a URL não trouxer
    if _db_url and 'sslmode' not in _db_url:
        _db_url += ('&' if '?' in _db_url else '?') + 'sslmode=require'

    SQLALCHEMY_DATABASE_URI = _db_url

    # Ambiente serverless (Vercel): cada instância mantém poucas conexões,
    # pois o pooling real é feito pelo PgBouncer do Neon.
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_size':     int(os.getenv('DB_POOL_SIZE', 2)),
        'max_overflow':  int(os.getenv('DB_MAX_OVERFLOW', 0)),
        'pool_recycle':  300,
        'pool_pre_ping': True,   # reconecta após o Neon suspender o compute por inatividade
        'connect_args': {
            'sslmode': 'require',
            'connect_timeout': 10,
        },
    }
