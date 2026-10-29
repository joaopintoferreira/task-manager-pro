# Task Manager Pro

Aplicação de gerenciamento de tarefas com autenticação JWT, categorias, notificações e colaboração entre usuários.

**Stack:** Python · Flask · PostgreSQL · NeonDB · Vanilla JS

---

## Tecnologias

- **Backend:** Flask, SQLAlchemy, Flask-Migrate, Flask-Limiter
- **Banco de dados:** PostgreSQL via NeonDB
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

## Pré-requisitos

- Python 3.10+ (testado com 3.12) e `git`
- Uma conta gratuita no [Neon](https://neon.tech) (para produção ou para testar com PostgreSQL)
- Opcional: conta na [Vercel](https://vercel.com) para o deploy

## Instalação

```bash
git clone https://github.com/SEU_USUARIO/task-manager-pro.git
cd task-manager-pro
python3 -m venv venv
source venv/bin/activate
pip install -r backend/requirements.txt
cp backend/.env.example backend/.env
```

Edite `backend/.env` (veja [Variáveis de ambiente](#variáveis-de-ambiente)). Todos os comandos abaixo partem da **raiz do projeto**.

## Banco de dados (NeonDB)

1. No Neon, crie um projeto e um banco. Em **Connect**, copie as duas strings de conexão:
   - **Pooled** (host contém `-pooler`, usa PgBouncer): use na aplicação, local e na Vercel. Em serverless cada execução abre
     conexões novas, e o pooler evita esgotar o limite do banco.
   - **Direct** (sem `-pooler`): prefira-a para executar as migrações (DDL).
2. O Neon exige TLS. A aplicação já força `sslmode=require` e reconecta sozinha quando o Neon suspende o banco por inatividade
   (`pool_pre_ping`); a primeira requisição após a suspensão pode demorar alguns segundos.
3. Coloque a string **pooled** em `DATABASE_URL` no `backend/.env` e defina `SECRET_KEY`
   (`python -c "import secrets; print(secrets.token_hex(32))"`).

### Criar as tabelas

```bash
export FLASK_APP=backend.wsgi
# string DIRETA só para este comando (não precisa alterar o .env):
DATABASE_URL='postgresql://USUARIO:SENHA@ep-xxxx.REGIAO.aws.neon.tech/NOME_DO_BANCO?sslmode=require' \
  flask db upgrade -d backend/migrations
flask db current -d backend/migrations      # deve mostrar 9ee9fe5f43cf (head)
```

Isso cria `users`, `categories`, `refresh_tokens`, `tasks`, `notifications`, `task_collaborators` e `user_avatars` (duas migrações: `5e59e9ffa61a` e `9ee9fe5f43cf`). O banco começa vazio:
cadastre o primeiro usuário pela tela de cadastro. As migrações ficam em `backend/migrations/versions/` e são versionadas no Git.
Nunca use `flask db stamp` para "pular" a criação das tabelas.

Sem uma `DATABASE_URL` PostgreSQL, a aplicação usa SQLite local (`DEV_DATABASE_URL`), útil para desenvolvimento rápido
(crie as tabelas da mesma forma com `flask db upgrade -d backend/migrations`).

## Executando localmente

```bash
python -m backend.run                          # API em http://localhost:5000
python -m http.server 5500 -d frontend         # frontend em http://localhost:5500/auth.html (outro terminal)
curl http://localhost:5000/                    # {"status":"running",...}
```

Em `localhost` o frontend usa `http://localhost:5000` automaticamente; em outros domínios usa o mesmo domínio da página.
Se servir o frontend em outra porta, inclua a origem em `CORS_ORIGINS`.

## Testes

```bash
python -m unittest backend.tests.test_api -v     # backend
node frontend/tests/time.test.js                 # helpers de data/hora do frontend
```

Os testes usam SQLite temporário e **não** acessam o NeonDB. Cobrem autenticação (jti, rotação e revogação de refresh tokens),
CRUD e isolamento de tarefas, categorias, notificações, colaboração, estatísticas, limite de requisições, configuração de
produção, migrações e compatibilidade das rotas usadas pelo frontend.

Teste manual rápido da API (com a API rodando):

```bash
curl -X POST localhost:5000/auth/register -H 'Content-Type: application/json' \
  -d '{"email":"voce@exemplo.com","password":"Senha123Forte"}'
curl -X POST localhost:5000/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"voce@exemplo.com","password":"Senha123Forte"}'
curl localhost:5000/tasks -H "Authorization: Bearer ACCESS_TOKEN"
```

## Datas, fuso horário e foto de perfil

- **Datas:** o banco guarda todos os instantes em **UTC** (colunas `TIMESTAMP` sem fuso). A API devolve ISO 8601 com `Z`
  (ex.: `2026-10-17T15:00:00Z`) e o frontend exibe em `America/Sao_Paulo`. Ao enviar `due_date`, use UTC (`...Z`) ou informe o
  deslocamento (`-03:00`); um valor sem fuso é interpretado como UTC. O formulário do frontend já converte do horário de Brasília.
  Por isso, o Neon (console/SQL) mostra horários 3 h à frente de Brasília: é o UTC, e o correto.
- **Datas limite antigas:** antes desta mudança, o frontend gravava `due_date` no horário local de Brasília como se fosse UTC.
  Nenhum dado é alterado automaticamente. Se já existirem tarefas com prazo criadas assim, ajuste **uma única vez** no Neon
  (SQL Editor): `UPDATE tasks SET due_date = due_date + interval '3 hours' WHERE due_date IS NOT NULL;`
  (`created_at`/`updated_at` já eram UTC e não precisam de ajuste).
- **Foto de perfil:** clique no avatar/nome na barra lateral. A imagem é reduzida no navegador (256×256) e guardada no
  PostgreSQL (tabela `user_avatars`, `BYTEA`), portanto persiste após logout e reinício e não depende de disco local.
  Endpoints (JWT obrigatório, sempre do usuário do token): `GET|PUT|DELETE /auth/avatar`. `PUT` recebe os bytes da imagem
  (PNG, JPEG, GIF ou WebP, até 2 MB; o formato é validado pelo conteúdo). Após atualizar o código, aplique a nova migração
  (`flask db upgrade`, veja acima).

## Variáveis de ambiente

Template completo em `backend/.env.example`.

| Variável | Obrigatória | Descrição |
|---|---|---|
| `DATABASE_URL` | produção | Connection string PostgreSQL do Neon (pooled), com `sslmode=require` |
| `SECRET_KEY` | produção | Assina os JWT. Sem ela a aplicação **não inicia** (só o modo SQLite local usa uma chave de desenvolvimento) |
| `DEV_DATABASE_URL` | não | SQLite local quando não há `DATABASE_URL` PostgreSQL |
| `JWT_ACCESS_EXPIRY` / `JWT_REFRESH_EXPIRY` | não | Minutos (padrão 15) / dias (padrão 7) |
| `CORS_ORIGINS` | não | Origens permitidas, separadas por vírgula (só se o frontend estiver em outro domínio) |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | não | Pool por instância (padrão 2 / 0) |

## Deploy na Vercel

O `vercel.json` publica `backend/wsgi.py` (Flask, via `@vercel/python`) e `frontend/**` (estático); as rotas
`/auth`, `/tasks`, `/categories` e `/notifications` vão para o Flask e `/` abre `auth.html`.

1. Importe o repositório na Vercel.
2. Em **Settings > Environment Variables** defina `DATABASE_URL` (string **pooled** do Neon) e `SECRET_KEY`
   (e `CORS_ORIGINS` apenas se necessário). Sem `DATABASE_URL` PostgreSQL a aplicação falha com uma mensagem clara.
3. Aplique as migrações no Neon **antes** do primeiro uso (comandos acima, a partir da sua máquina).
4. Faça o deploy e teste `/`, cadastro e login.

### Limitações conhecidas

- O rate limiting usa memória por instância: em serverless o contador não é global nem compartilhado.
- Os limites padrão (50 requisições/hora e 200/dia por IP em todas as rotas) são baixos para uso intenso do dashboard.
- Refresh tokens são de uso único; usar a mesma sessão em várias abas ao mesmo tempo pode forçar um novo login.
- `api/index.py` não é usado (o `vercel.json` define `builds`, o que desativa a detecção automática de `api/`).
- `psycopg2-binary==2.9.9` tem wheels até o Python 3.12; se a Vercel usar uma versão mais nova do Python, atualize para `2.9.10+`.
