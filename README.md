# Task Manager Pro

Aplicação de gerenciamento de tarefas com autenticação JWT, categorias, notificações e colaboração entre usuários.

**Stack:** Python · Flask · PostgreSQL · Supabase · Vanilla JS

---

## Tecnologias

- **Backend:** Flask, SQLAlchemy, Flask-Migrate, Flask-Limiter
- **Banco de dados:** PostgreSQL via Supabase
- **Auth:** JWT com access + refresh tokens
- **Frontend:** HTML, CSS, JavaScript (sem framework)
- **Deploy:** Vercel

## Funcionalidades

- Autenticação com JWT e refresh token automático
- CRUD de tarefas com filtros, busca, paginação e ordenação
- Categorias personalizadas com cores
- Sistema de notificações
- Colaboração entre usuários em tarefas
- Dashboard com estatísticas
- Rate limiting e validação de senha
- Interface responsiva

## Configuração local

```bash
git clone https://github.com/SEU_USUARIO/task-manager-pro.git
cd task-manager-pro
python3 -m venv venv
source venv/bin/activate
pip install -r backend/requirements.txt
cp backend/.env.example backend/.env
cd backend && flask db upgrade
python3 run.py
```

O frontend pode ser aberto via `frontend/index.html` ou com Live Server no VS Code.

## Variáveis de ambiente

Veja o template em `backend/.env.example`.

## Deploy

Projeto configurado para deploy automático no Vercel via `vercel.json`. Configure as variáveis de ambiente no painel do Vercel antes do primeiro deploy.