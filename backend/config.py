import os
from dotenv import load_dotenv
from datetime import timedelta

load_dotenv()

class Config:
    SECRET_KEY = os.getenv('SECRET_KEY')
    if not SECRET_KEY:
        raise ValueError("SECRET_KEY não definida nas variáveis de ambiente!")

    SQLALCHEMY_DATABASE_URI = os.getenv('DATABASE_URL', 'sqlite:///task_manager.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_recycle': 3600,
        'pool_pre_ping': True,
    }

    JWT_ACCESS_TOKEN_EXPIRES  = timedelta(minutes=int(os.getenv('JWT_ACCESS_EXPIRY',  15)))
    JWT_REFRESH_TOKEN_EXPIRES = timedelta(days=int(os.getenv('JWT_REFRESH_EXPIRY', 7)))

    CORS_ORIGINS = os.getenv('CORS_ORIGINS', 'http://localhost:3000,http://127.0.0.1:5500').split(',')

    DEBUG = False
    TESTING = False

class DevelopmentConfig(Config):
    DEBUG = True
    SQLALCHEMY_DATABASE_URI = os.getenv('DEV_DATABASE_URL', 'sqlite:///dev_task_manager.db')
    SQLALCHEMY_ENGINE_OPTIONS = {}          # sem pool para SQLite

class ProductionConfig(Config):
    DEBUG = False
    
    # Corrige prefixo antigo do Heroku/Supabase
    _db_url = os.getenv('DATABASE_URL', '')
    if _db_url.startswith('postgres://'):
        _db_url = _db_url.replace('postgres://', 'postgresql://', 1)
    
    # Adiciona sslmode se não tiver
    if _db_url and 'sslmode' not in _db_url:
        _db_url += '?sslmode=require'
    
    SQLALCHEMY_DATABASE_URI = _db_url
    
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_size':    int(os.getenv('DB_POOL_SIZE', 10)),
        'max_overflow': int(os.getenv('DB_MAX_OVERFLOW', 20)),
        'pool_recycle': 3600,
        'pool_pre_ping': True,
        'connect_args': {'sslmode': 'require'},  # força SSL no driver também
    }
