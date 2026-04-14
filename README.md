# Task Manager Pro

Sistema de gerenciamento de tarefas com Flask + Supabase, pronto para deploy no Vercel.

---

## Estrutura

```
task-manager-pro/
├── backend/
│   ├── app/
│   │   ├── __init__.py       ← App factory + rate limiter
│   │   ├── auth.py           ← JWT auth + token refresh
│   │   ├── models.py         ← SQLAlchemy models
│   │   ├── utils.py          ← Helpers, validações, notificações
│   │   └── routes/
│   │       ├── tasks.py      ← CRUD + paginação + stats + colaboradores
│   │       ├── categories.py ← CRUD categorias
│   │       └── notifications.py
│   ├── .env.example          ← Template de variáveis de ambiente
│   ├── config.py
│   ├── requirements.txt
│   ├── run.py                ← Desenvolvimento local
│   └── wsgi.py               ← Entry point para Vercel
├── frontend/
│   ├── auth.html
│   ├── index.html
│   ├── style.css
│   └── script.js
├── requirements.txt          ← Para o Vercel (raiz)
├── vercel.json
├── .gitignore
└── README.md
```

---

## 1. Configuração local

### Pré-requisitos
- Python 3.10+
- pip

### Passos

```bash
# 1. Clone / extraia o projeto
cd task-manager-pro

# 2. Crie e ative o virtualenv
python3 -m venv venv
source venv/bin/activate        # Linux/Mac
venv\Scripts\activate           # Windows

# 3. Instale as dependências
pip install -r backend/requirements.txt

# 4. Crie o .env a partir do template
cp backend/.env.example backend/.env
# Edite backend/.env com suas credenciais

# 5. Rode as migrations (cria as tabelas no banco)
cd backend
flask db init       # só na primeira vez (cria pasta migrations/)
flask db migrate -m "initial"
flask db upgrade

# 6. Inicie o servidor
python3 run.py
# API disponível em http://localhost:5000
```

### Abrir o frontend
Abra `frontend/index.html` direto no navegador, ou use uma extensão Live Server no VS Code.

---

## 2. Configurar o Supabase

### Passo 1 — Criar projeto
1. Acesse [supabase.com](https://supabase.com) → **New project**
2. Escolha um nome, região **South America (São Paulo)** e uma senha forte

### Passo 2 — Pegar a connection string
1. No painel do projeto: **Settings → Database → Connection string → URI**
2. Copie a URL (formato abaixo) e cole no `.env`:

```
DATABASE_URL=postgresql://postgres.XXXX:SUA_SENHA@aws-0-sa-east-1.pooler.supabase.com:6543/postgres
```

### Passo 3 — Rodar migrations no Supabase
```bash
# Com DATABASE_URL apontando para o Supabase:
cd backend
flask db upgrade
```

Verifique as tabelas criadas em **Table Editor** no painel do Supabase.

---

## 3. Deploy no Vercel

### Passo 1 — Subir para o GitHub
```bash
git init
git add .
git commit -m "feat: task manager pro"
git remote add origin https://github.com/SEU_USUARIO/task-manager-pro.git
git push -u origin main
```

### Passo 2 — Importar no Vercel
1. Acesse [vercel.com](https://vercel.com) → **Add New Project**
2. Importe o repositório do GitHub
3. Vercel detecta o `vercel.json` automaticamente

### Passo 3 — Variáveis de ambiente no Vercel
Em **Settings → Environment Variables**, adicione:

| Variável | Valor |
|---|---|
| `SECRET_KEY` | Gere com: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `DATABASE_URL` | URL de conexão do Supabase |
| `FLASK_ENV` | `production` |
| `CORS_ORIGINS` | `https://seu-projeto.vercel.app` |
| `JWT_ACCESS_EXPIRY` | `15` |
| `JWT_REFRESH_EXPIRY` | `7` |

### Passo 4 — Deploy
Clique em **Deploy**. A cada `git push`, o Vercel faz redeploy automático.

---

## 4. Endpoints da API

### Auth
| Método | Rota | Descrição |
|---|---|---|
| POST | `/auth/register` | Criar conta |
| POST | `/auth/login` | Login (rate limit: 5/min) |
| POST | `/auth/refresh` | Renovar token |
| POST | `/auth/logout` | Logout |
| GET  | `/auth/me` | Dados do usuário atual |

### Tarefas
| Método | Rota | Descrição |
|---|---|---|
| GET  | `/tasks` | Listar (filtros, busca, paginação, ordenação) |
| POST | `/tasks` | Criar tarefa |
| GET  | `/tasks/<id>` | Detalhe |
| PUT  | `/tasks/<id>` | Atualizar |
| DELETE | `/tasks/<id>` | Excluir |
| GET  | `/tasks/stats` | Estatísticas do usuário |
| POST | `/tasks/<id>/collaborators` | Adicionar colaborador |
| DELETE | `/tasks/<id>/collaborators/<user_id>` | Remover colaborador |

### Categorias
| Método | Rota | Descrição |
|---|---|---|
| GET  | `/categories` | Listar |
| POST | `/categories` | Criar |
| PUT  | `/categories/<id>` | Atualizar |
| DELETE | `/categories/<id>` | Excluir |

### Notificações
| Método | Rota | Descrição |
|---|---|---|
| GET  | `/notifications` | Listar (com unread_count) |
| POST | `/notifications/<id>/read` | Marcar como lida |
| POST | `/notifications/read-all` | Marcar todas como lidas |
| DELETE | `/notifications/<id>` | Excluir |

---

## 5. Funcionalidades

- ✅ Autenticação JWT com access + refresh tokens
- ✅ Rate limiting: 5 tentativas de login por minuto por IP
- ✅ Validação de força de senha (8+ chars, número obrigatório)
- ✅ CRUD completo de tarefas com paginação
- ✅ Filtros por status, prioridade, categoria e busca por texto
- ✅ Ordenação por data de criação, prazo, prioridade ou título
- ✅ Sistema de categorias com cores customizáveis
- ✅ Notificações em tempo real (criação, conclusão, prazo)
- ✅ Colaboração em tarefas (adicionar/remover colaboradores)
- ✅ Dashboard com estatísticas (total, concluídas, atrasadas, taxa de conclusão)
- ✅ Interface responsiva (mobile + desktop)
- ✅ Modo sidebar retrátil no mobile
- ✅ Toast notifications no frontend
- ✅ Indicador de força de senha no cadastro
- ✅ Logging estruturado (substitui print() por logging module)
- ✅ .gitignore completo (protege .env e dados sensíveis)
- ✅ Compatível com Vercel + Supabase
