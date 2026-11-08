# Painel administrativo

Área de administração do Task Manager Pro: gestão de utilizadores, administradores, auditoria e manutenção.
Acessível só a utilizadores com papel `admin` ou `superadmin`. **A segurança está toda no backend**: esconder botões
na interface não protege nada.

## Papéis e permissões

Papel em `users.role` (`user` por omissão, com `CHECK` na base). **Lido da base de dados em cada pedido**, nunca do JWT,
do corpo do pedido nem do `localStorage`: despromover ou desativar alguém tem efeito imediato.

| Operação | USER | ADMIN | SUPERADMIN |
|---|:-:|:-:|:-:|
| Funcionalidades normais e perfil próprio | ✓ | ✓ | ✓ |
| Aceder ao painel, listar e consultar utilizadores | – | ✓ | ✓ |
| Ativar/desativar utilizador comum | – | ✓ | ✓ |
| Ativar/desativar ADMIN | – | – | ✓ |
| Ativar/desativar SUPERADMIN | – | – | ✓ (nunca o último ativo, nunca a si próprio) |
| Remover a foto de um utilizador comum | – | ✓ | ✓ |
| Remover a foto de ADMIN/SUPERADMIN | – | – | ✓ |
| Consultar auditoria | – | só as próprias ações | completa |
| Limpar dados de um utilizador | – | – | ✓ (senha + confirmação) |
| Excluir utilizador | – | – | ✓ (senha + confirmação) |
| Promover a ADMIN, alterar papéis, promover a SUPERADMIN | – | – | ✓ (senha) |
| Manutenção e limpeza global | – | – | ✓ (proteções extras) |

Regras que valem para todas as operações:

- ninguém altera o **próprio** papel, estado ou conta pelo painel;
- um ADMIN nunca age sobre outro ADMIN nem sobre um SUPERADMIN (a permissão depende do papel do **alvo**);
- o **último SUPERADMIN ativo** nunca é desativado, despromovido nem excluído;
- transições de papel permitidas: `user ⇄ admin` e `admin ⇄ superadmin` (não se salta por cima de `admin`);
- só se promove uma conta **ativa**, a partir do email de um utilizador existente (não há criação de contas com senha temporária).

O código está em `backend/app/services/` (`permissions.py`, `audit.py`, `purge.py`, `user_admin.py`, `maintenance.py`) e
`backend/app/routes/admin.py`. Um teste percorre **todas** as rotas `/admin` e falha se alguma ficar sem
`require_permission` ou fora da matriz acima.

## Endpoints

Todos exigem JWT de acesso; o papel é verificado na base. `403` = sem permissão, `401` = sem sessão/conta inativa.

| Rota | Papel mínimo |
|---|---|
| `GET /admin/overview` | ADMIN |
| `GET /admin/users?search=&role=&status=&page=&per_page=` · `GET /admin/users/<id>` | ADMIN |
| `POST /admin/users/<id>/activate` · `POST /admin/users/<id>/deactivate` | ADMIN (regras da matriz) |
| `DELETE /admin/users/<id>/avatar` | ADMIN (regras da matriz) |
| `GET /admin/audit?action=&result=&actor_id=&from=&to=&page=` | ADMIN (só as suas) · SUPERADMIN (tudo) |
| `GET /admin/users/<id>/data-summary` | SUPERADMIN |
| `POST /admin/users/<id>/clear-data` · `DELETE /admin/users/<id>` | SUPERADMIN |
| `GET /admin/admins` · `GET /admin/users/lookup?email=` · `POST /admin/admins` · `PUT /admin/users/<id>/role` | SUPERADMIN |
| `GET /admin/maintenance/summary` | SUPERADMIN |
| `POST /admin/maintenance/wipe/preview` · `POST /admin/maintenance/wipe` | SUPERADMIN |

A auditoria **não tem** rotas de alteração nem de exclusão (`PUT/PATCH/DELETE/POST` em `/admin/audit` dão 405).
As respostas de `/admin` e `/auth` levam `Cache-Control: no-store`.

Perfil de qualquer utilizador (fora do painel): `PATCH /auth/me` (nome e email; mudar o email exige a senha atual) e
`PUT /auth/password` (exige a senha atual, aplica a política de senhas, **revoga todos os refresh tokens** e devolve tokens
novos para a sessão atual). Só aceitam uma lista fixa de campos: papel, estado e afins nunca são alteráveis por aqui.
**Limitação:** os JWT de **acesso** já emitidos (por exemplo noutro dispositivo) continuam válidos até expirarem (15 min por
omissão), porque o token não leva versão. Fechar esta janela exigiria um `token_version`, que muda a autenticação e
ficou fora do âmbito.

## Auditoria

Tabela `admin_audit_log`: `actor_id` e `target_id` são inteiros **sem chave estrangeira**, para o histórico sobreviver à
exclusão do administrador ou do alvo. `details` guarda só metadados e contagens; chaves como `password`, `secret`,
`hash` ou `authorization` são sempre descartadas, e `token`/`key` só passam quando o valor é um número (contagens como
`refresh_tokens`). Cada operação grava o registo **na mesma transação**; negações e falhas são gravadas à parte.
O IP não é registado: a identificação do IP real atrás da Vercel continua por validar (ver **Pendências**).

## Limpeza individual e exclusão (SUPERADMIN)

1. `GET /admin/users/<id>/data-summary` devolve as contagens (tarefas, categorias, notificações, colaborações, sessões,
   fotos e notificações de terceiros sobre as tarefas do utilizador).
2. A operação exige: **senha** do SUPERADMIN, **confirmação escrita** do nome do utilizador e as **contagens vistas**
   (`expected_counts`). Dentro da transação as contagens são recalculadas; se divergirem, nada é apagado (`409 counts_changed`).
3. Cada `DELETE`/`UPDATE` confere o número de linhas afetadas; se divergir, aborta e reverte **tudo**.

**Limpar dados** apaga tarefas, categorias, notificações e colaborações do utilizador (incluindo as colaborações nas suas
tarefas). **Mantém** a conta, o papel, o estado, a foto e as sessões. As notificações de **outros** utilizadores sobre as
tarefas apagadas ficam, com `task_id = NULL` (como o ORM já faz ao apagar uma tarefa).

**Excluir** faz o mesmo e depois remove sessões, foto e a conta. **Recusa por omissão se o utilizador tiver tarefas**
(`409 has_tasks`): o SUPERADMIN tem de escolher explicitamente apagá-las (`delete_tasks: true`). Não há transferência de
tarefas. Não é possível excluir a própria conta nem o último SUPERADMIN ativo.

Ordem (respeita as FKs reais, que são `NO ACTION`, exceto `user_avatars`): notificações → colaborações → tarefas →
categorias → sessões → foto → utilizador. **Não** se usa `CASCADE`, `TRUNCATE` nem alterações de esquema.

## Limpeza global (SUPERADMIN)

Apaga **só dados operacionais**: tarefas, categorias, notificações, colaborações, sessões e fotos. **Preserva** todas as
contas, os SUPERADMIN, o esquema, a tabela `alembic_version` e a auditoria. Apagar contas **não** faz parte desta função.

Proteções, todas verificadas no backend:

1. **Interruptor** `ADMIN_ALLOW_GLOBAL_WIPE`: desligado por omissão (ausente = `false`). Sem `true` a rota recusa
   (`403 wipe_disabled`) e a tentativa fica auditada. Para ativar, defina `ADMIN_ALLOW_GLOBAL_WIPE=true` no ambiente
   (Vercel: *Settings → Environment Variables*) e **volte a desligar** depois. **Não** está no `.env.example`.
2. **Pré-visualização** (`POST /admin/maintenance/wipe/preview`): devolve as contagens e um token assinado (HMAC com a
   `SECRET_KEY`), válido 5 minutos, ligado ao administrador e às contagens apresentadas.
3. **Execução** (`POST /admin/maintenance/wipe`): exige o token, a **senha**, a frase exata `LIMPAR TODOS OS DADOS` e o
   interruptor ligado. As contagens são reverificadas na transação e deve existir pelo menos um SUPERADMIN ativo; senão
   nada é apagado.
4. O token é de **uso único** (o `nonce` fica na auditoria do apagamento bem-sucedido).
5. Tudo numa transação, sem `TRUNCATE CASCADE`. A limpeza revoga todos os refresh tokens; o administrador recebe tokens
   novos na resposta para não perder a sessão (as outras sessões terminam).

**Duração na Vercel.** A [documentação](https://vercel.com/docs/functions/configuring-functions/duration) indica 300 s por
omissão com Fluid Compute. Este projeto usa `builds` no `vercel.json`, por isso o limite efetivo **continua por confirmar**
no painel (*Settings → Functions*). A limpeza são seis `DELETE` em massa numa transação e, no PostgreSQL, corre com
`statement_timeout = 25s`: se não couber, **reverte** em vez de ficar a meio. Para volumes grandes seria preciso outra
estratégia (por exemplo, lotes), a decidir à parte.

## Promoção do primeiro SUPERADMIN (utilizador 1)

A migration **não promove ninguém**. A promoção é um comando explícito, transacional (linha bloqueada), auditado e idempotente:

```bash
export FLASK_APP=backend.wsgi
flask promote-superadmin --user-id 1             # simulação: não altera nada
flask promote-superadmin --user-id 1 --execute   # aplica
```

Recusa utilizadores inexistentes ou inativos; se já for SUPERADMIN não altera nem regista nada. O comando usa a
`DATABASE_URL` do ambiente: **em produção, execute-o só depois da migration**, e com a string que quiser (direta ou pooled).

## Novos administradores

Um SUPERADMIN abre **Administração → Administradores**, procura o utilizador pelo **email**, confere os dados e
confirma com a sua senha: o papel passa a `admin`. O utilizador deve ter conta ativa. Para retirar privilégios, ação
**Papel** na lista (ADMIN → Utilizador).

## Migration

Revisão `c4d7a1e08b52` (sobre `9ee9fe5f43cf`). **Aditiva e reversível.** `DEFAULT 'user'` mantém o código anterior a
funcionar e dá o papel `user` a todos os utilizadores existentes; nenhum dado é alterado.

SQL gerado para PostgreSQL (`flask db upgrade 9ee9fe5f43cf:c4d7a1e08b52 --sql`):

```sql
BEGIN;
-- Running upgrade 9ee9fe5f43cf -> c4d7a1e08b52
ALTER TABLE users ADD COLUMN role VARCHAR(20) DEFAULT 'user' NOT NULL;
ALTER TABLE users ADD CONSTRAINT ck_users_role CHECK (role IN ('user', 'admin', 'superadmin'));
CREATE TABLE admin_audit_log (
    id SERIAL NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    actor_id INTEGER,
    actor_label VARCHAR(120),
    action VARCHAR(60) NOT NULL,
    target_type VARCHAR(30),
    target_id INTEGER,
    target_label VARCHAR(120),
    result VARCHAR(10) NOT NULL,
    details JSON,
    PRIMARY KEY (id)
);
CREATE INDEX ix_admin_audit_log_action ON admin_audit_log (action);
CREATE INDEX ix_admin_audit_log_actor_id ON admin_audit_log (actor_id);
CREATE INDEX ix_admin_audit_log_created_at ON admin_audit_log (created_at);
UPDATE alembic_version SET version_num='c4d7a1e08b52' WHERE alembic_version.version_num = '9ee9fe5f43cf';
COMMIT;
```

Reversão (`flask db downgrade c4d7a1e08b52:9ee9fe5f43cf --sql`):

```sql
BEGIN;
-- Running downgrade c4d7a1e08b52 -> 9ee9fe5f43cf
DROP INDEX ix_admin_audit_log_created_at;
DROP INDEX ix_admin_audit_log_actor_id;
DROP INDEX ix_admin_audit_log_action;
DROP TABLE admin_audit_log;
ALTER TABLE users DROP CONSTRAINT ck_users_role;
ALTER TABLE users DROP COLUMN role;
UPDATE alembic_version SET version_num='9ee9fe5f43cf' WHERE alembic_version.version_num = 'c4d7a1e08b52';
COMMIT;
```

No SQLite (desenvolvimento) a migration só acrescenta a coluna (não existe `ALTER` de constraints e recriar `users`
falharia com chaves estrangeiras e dados); o `CHECK` existe no PostgreSQL e em bases criadas por `create_all`.

## Sequência segura de publicação

O código novo **lê `users.role` em todos os pedidos**: publicá-lo antes da migration faz o login falhar.

1. Rever a migration e o SQL acima.
2. Aplicar a migration no Neon com a string **direta** (sem `-pooler`):
   `flask db upgrade -d backend/migrations` e confirmar `flask db current -d backend/migrations` = `c4d7a1e08b52`.
3. Promover o utilizador 1 (`flask promote-superadmin`, secção anterior) e confirmar o papel.
4. Fazer o push. A Vercel publica; o código novo funciona com a coluna já presente.
5. Validação funcional controlada: login, painel como SUPERADMIN, utilizador comum sem acesso (403 em `/admin/*`).
6. Consola do navegador: ver se a política CSP em modo de relatório (abaixo) não regista violações.

O código **antigo** continua a funcionar com a migration aplicada (ignora a coluna nova), por isso a migration pode
ser aplicada com antecedência.

## Segurança do frontend e cabeçalhos

- O painel (`painel-adm.js`/`painel-adm.css`) só é descarregado por ADMIN/SUPERADMIN. Os nomes **não** podem começar por
  `admin`, `auth`, `tasks`, `categories`, `notifications` ou `ai`: a regex do `vercel.json` enviaria esses pedidos ao Flask.
  Um teste vigia isto.
- Todo o texto dinâmico entra com `textContent`; nunca se constrói HTML com dados da API. Senhas só existem em campos do
  modal, são limpas após cada tentativa e nunca vão para `localStorage` (só os tokens novos da limpeza global).
- **SRI** (`integrity` sha384 + `crossorigin="anonymous"`) em Bootstrap, Bootstrap Icons e o JS do Bootstrap, calculados a
  partir dos ficheiros reais do CDN nas versões fixadas. As fontes carregadas pelo CSS dos ícones não são cobertas por SRI.
- **Cabeçalhos** no `vercel.json` (rota `/(.*)` com `continue: true`): `X-Content-Type-Options`, `X-Frame-Options: DENY`,
  `Referrer-Policy`, `Permissions-Policy` e uma CSP **aplicada** só com `object-src 'none'`, `base-uri 'self'`,
  `frame-ancestors 'none'`, `form-action 'self'`.
- **CSP completa em modo de relatório** (`Content-Security-Policy-Report-Only`): não bloqueia nada. A aplicação ainda usa
  `onclick=` e scripts inline, por isso a política tem `'unsafe-inline'` em scripts; é uma proteção parcial (bloqueia
  origens externas, mas não XSS inline). **Não há recolha de relatórios**: as violações veem-se na consola do navegador.
  Passar a política a obrigatória só depois de a consola ficar limpa em todas as telas; endurecer de verdade exige tirar
  os handlers inline (alteração maior, à parte).
- **CSRF:** não se aplica ao desenho atual: a autenticação é por cabeçalho `Authorization` (sem cookies), logo um site
  terceiro não consegue fazer o navegador enviar credenciais. O risco real é **XSS**, porque os tokens estão no
  `localStorage`: daí o `textContent`, o SRI e a CSP.
- Limite de tentativas: a confirmação de senha das operações sensíveis e a mudança de senha bloqueiam após 5 falhas em
  10 minutos **por utilizador** (em memória, por instância). Além disso as rotas têm limites por IP.

## Testes

```bash
python -W ignore -m unittest backend.tests.test_admin -v   # painel, papéis, auditoria, perfil, limpeza, exclusão, limpeza global
node frontend/tests/admin.test.js                          # painel (frontend), SRI, cabeçalhos, roteamento do Vercel
```

**Limitação importante:** os testes correm em SQLite, que **ignora `SELECT ... FOR UPDATE`**. A lógica da proteção do
último SUPERADMIN (releitura do ator depois dos bloqueios, contagem dos ativos) está testada, mas a **concorrência real**
entre dois administradores só se prova num PostgreSQL. Isto **não** foi validado.

## Pendências e riscos conhecidos

- Concorrência em PostgreSQL por validar (acima).
- Identificação do IP real atrás da Vercel por validar (`docs/AI_ASSISTANT.md`): o limite por IP e o registo de IP na
  auditoria dependem disso. Nada confia em `X-Forwarded-For`.
- Limite de duração das funções na Vercel por confirmar no painel.
- Janela de até 15 min dos JWT de acesso após mudar a senha (sem `token_version`).
- CSP completa ainda em modo de relatório.
- O limitador de tentativas e de pedidos é em memória, por instância (serverless).
