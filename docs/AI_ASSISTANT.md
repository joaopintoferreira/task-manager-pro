# Assistente de IA

Tela **Assistente de IA** do Task Manager Pro: um chat de orientação sobre organização do trabalho, prioridades,
produtividade e uso do sistema, com a opção de contextualizar a conversa com **uma tarefa** que o usuário tenha
permissão de consultar.

> O assistente **só responde e orienta**. Ele não cria, edita, conclui, exclui nem compartilha tarefas, e não tem
> acesso a outras tarefas, contas, notificações ou credenciais.

## Arquitetura

```
Navegador (assistant.js)  ──JWT──▶  POST /ai/chat (routes/ai.py)  ──HTTPS──▶  Groq ou OpenRouter
   conversa só na memória            valida, autoriza, limita                 (ai_service.py, urllib)
```

| Arquivo | Papel |
|---|---|
| `backend/app/routes/ai.py` | Rota: autenticação (`token_required`), validação, rate limit, autorização da tarefa, resposta HTTP |
| `backend/app/ai_service.py` | Serviço: valida a configuração, monta o prompt, chama o provedor (`urllib`, sem dependências), converte falhas em erros seguros |
| `frontend/assistant.js` / `assistant.css` | Tela, conversa em memória, consentimento, tratamento de erros |
| `vercel.json` | A regex de rotas inclui `ai`, para `/ai/chat` chegar ao Flask |

Não há tabelas, migrações nem persistência: nada da conversa é gravado no banco, no `localStorage` ou no `sessionStorage`.

## Provedores suportados

Ambos usam o formato *Chat Completions* da OpenAI, então há uma única camada de serviço.

| Provedor | `AI_PROVIDER` | Endpoint oficial usado quando `AI_BASE_URL` está vazio |
|---|---|---|
| Groq | `groq` | `https://api.groq.com/openai/v1/chat/completions` |
| OpenRouter | `openrouter` | `https://openrouter.ai/api/v1/chat/completions` |

Para trocar de provedor basta mudar as variáveis abaixo e reiniciar o backend; o frontend não muda.

## Configuração

Defina as variáveis no seu **`backend/.env` local** (nunca no repositório). O modelo de referência está em `backend/.env.example`.

| Variável | Obrigatória | Descrição |
|---|---|---|
| `AI_PROVIDER` | sim | `groq` ou `openrouter` |
| `AI_API_KEY` | sim | Chave privada do provedor escolhido |
| `AI_MODEL` | sim | Identificador **exato** do modelo. **Não há modelo padrão** |
| `AI_BASE_URL` | não | Outro endpoint compatível. Aceita a URL completa (`…/chat/completions`) ou só a base (`…/v1`). Precisa ser `https://` (`http://` só para `localhost`) |
| `AI_TIMEOUT_SECONDS` | não | Padrão 20 (limitado a 1–60) |
| `AI_MAX_TOKENS` | não | Padrão 600 (limitado a 1–2000) |

Se provedor, chave ou modelo faltarem, a tela avisa que o assistente não está configurado e **nenhuma chamada é feita**.
Não existe fallback automático para outro modelo: se o modelo escolhido ficar indisponível ou sem quota, o erro é mostrado.

### Obter a chave

- **Groq:** entre em <https://console.groq.com/keys> e crie uma *API key*.
- **OpenRouter:** entre em <https://openrouter.ai/keys> e crie uma chave.

Cole a chave **somente** no seu `backend/.env`. Não a envie em chats, issues ou commits.

### Escolher o modelo

A disponibilidade, os preços e as quotas mudam com frequência e **dependem da sua conta**. Confirme sempre no painel do provedor:

- **Groq:** lista de modelos em <https://console.groq.com/docs/models>; seus limites reais em *Settings → Limits* no console.
  Alguns modelos aparecem como restritos a planos específicos.
- **OpenRouter:** modelos em <https://openrouter.ai/models>. Modelos gratuitos têm o sufixo `:free` (filtro `pricing=free`).
  Segundo a documentação oficial, os `:free` têm limite de 20 requisições por minuto e 50 por dia (1000 por dia com pelo menos
  10 créditos comprados). Um saldo negativo pode causar erro 402 até mesmo nos modelos gratuitos.

Exemplo (use um identificador que exista **hoje** no seu painel):

```env
AI_PROVIDER=openrouter
AI_API_KEY=<sua-chave>
AI_MODEL=<id-do-modelo-gratuito-escolhido>
```

## Rodar e testar localmente

```bash
# 1) edite backend/.env com AI_PROVIDER, AI_API_KEY e AI_MODEL
python -m backend.run                        # API em http://localhost:5000
python -m http.server 5500 -d frontend       # frontend em http://localhost:5500/auth.html
# 2) entre, abra "Assistente de IA" no menu e envie uma pergunta
```

Testes automatizados (todas as chamadas ao provedor são simuladas; **nenhuma quota é consumida**):

```bash
python -W ignore -m unittest backend.tests.test_ai -v
node frontend/tests/assistant.test.js
```

Uma chamada real ao provedor só acontece quando você configura a sua chave e usa a tela.

## Contexto de tarefa e privacidade

- O frontend envia apenas o **`task_id`**. O backend carrega a tarefa e confere o acesso (`can_access`) **a cada pedido**.
  Proprietário, editor e leitor podem usar o contexto (todos podem ler a tarefa). Quem não tem acesso recebe o mesmo
  **404** de uma tarefa inexistente.
- Ao provedor seguem **apenas**: título, descrição (até 1500 caracteres), estado, prioridade, prazo, indicador de atraso,
  nome da categoria e o seu papel na tarefa. **Nunca** emails, colaboradores, IDs de outros usuários, notificações, outras
  tarefas, tokens ou credenciais.
- Antes de ativar o contexto a tela exige consentimento: *"Ao aceitar, o conteúdo da tarefa selecionada (título, descrição,
  estado, prioridade, prazo, categoria, o seu papel na tarefa e o indicador de atraso, que é calculado a partir do prazo e
  do estado) será enviado ao provedor externo de IA para gerar uma resposta. Isto inclui tarefas de outro usuário que foram compartilhadas com você. Evite incluir informações pessoais ou
  confidenciais."* O aceite vale só para a sessão da tela (não é guardado) e é pedido de novo depois de **Limpar conversa**.
  O consentimento é uma proteção da interface; as permissões não mudaram: continua a valer só o acesso à tarefa verificado
  pelo backend.
- As mensagens que você digita **também** são enviadas ao provedor. O histórico enviado é limitado (10 mensagens,
  2000 caracteres cada, 8000 no total).
- Os textos da tarefa e das mensagens são tratados como **dados, não instruções**: ficam delimitados no prompt e o
  assistente não tem ferramentas para agir. Mesmo assim, nenhum prompt é infalível: uma resposta pode estar errada.
- Logs do backend: apenas metadados (usuário, provedor, estado, latência, categoria do erro). Nunca a chave, as mensagens,
  os prompts, as respostas nem o conteúdo das tarefas.
- As respostas são exibidas como texto (nunca HTML).

## Limites

| Limite | Valor |
|---|---|
| Histórico enviado | 10 mensagens, 2000 caracteres cada, 8000 no total |
| Resposta | `AI_MAX_TOKENS` (padrão 600) |
| Timeout do provedor | `AI_TIMEOUT_SECONDS` (padrão 20 s) |
| Rate limit do endpoint | 10 por minuto e 100 por hora (por IP), além do limite global do app |
| Quota do provedor | A da **sua** conta/modelo |

O rate limit usa armazenamento em memória por instância (`memory://`): em ambiente serverless cada instância conta
separado, então é uma proteção básica contra abuso, não um teto rígido.

## Erros comuns

| O que a tela mostra | Código (`code`) | O que verificar |
|---|---|---|
| "O assistente de IA ainda não está configurado…" | `ai_not_configured` | `AI_PROVIDER`, `AI_API_KEY` e `AI_MODEL` no `.env`; reinicie o backend |
| "O provedor de IA recusou as credenciais…" | `ai_auth` | Chave inválida, revogada ou de outro provedor |
| "O modelo de IA configurado não está disponível…" | `ai_model` | O identificador do modelo existe hoje no painel? |
| "O provedor de IA recusou o pedido…" | `ai_rejected` | Modelo incompatível com chat ou parâmetros recusados |
| "O limite de uso do provedor… (quota ou saldo)" | `ai_quota` | Quota/limites da conta (HTTP 429/402 do provedor). Aguarde ou escolha outro modelo — o sistema **não** troca sozinho |
| "O assistente demorou demasiado…" | `ai_timeout` | Aumente `AI_TIMEOUT_SECONDS` ou tente de novo |
| "O serviço de IA está indisponível…" | `ai_unavailable` | Falha de rede/DNS/5xx do provedor |
| "O modelo atingiu o limite de saída antes de concluir a resposta…" | `ai_truncated` | O modelo parou por `finish_reason: length` sem texto (veja [Modelos de raciocínio](#modelos-de-raciocínio-openaigpt-oss-20b)) |
| "Enviou mensagens demasiado depressa…" | HTTP 429 do app | Limite de 10 por minuto |

Os detalhes técnicos nunca vão para a interface. Para diagnosticar, veja os logs do backend (`ai_chat user=… provider=… status=… error=…`).

## Modelos de raciocínio (`openai/gpt-oss-20b`)

O que a documentação oficial da Groq ([Reasoning](https://console.groq.com/docs/reasoning), consultada em 2026-10-29) confirma
para os modelos `openai/gpt-oss-*`:

- aceitam `reasoning_effort` com os valores `low`, `medium` e `high` e `include_reasoning` (verdadeiro por omissão);
- `reasoning_format` **não** é suportado;
- o raciocínio é devolvido num campo separado, `message.reasoning`.

A página **não diz** se os tokens de raciocínio contam para `max_completion_tokens`. Por prudência, assuma que podem contar:
com `AI_MAX_TOKENS=600`, uma pergunta complexa pode esgotar o limite antes de produzir texto.

Como o sistema se comporta:

- Resposta com texto, mesmo cortada por `length`: o texto recebido é mostrado.
- `content` vazio com `finish_reason: length`: erro `ai_truncated` (HTTP 502) com mensagem própria. Tente uma pergunta mais curta ou
  peça ao administrador para rever `AI_MAX_TOKENS`.
- `content` vazio por qualquer outra razão: `ai_bad_response`.
- O campo `reasoning` **nunca** é lido, registado nem enviado ao navegador.
- O sistema **não** envia `reasoning_effort` nem `include_reasoning` e **não** aumenta `AI_MAX_TOKENS` sozinho: o comportamento
  dos outros modelos e provedores não mudou.

Se o `ai_truncated` for frequente, as opções são subir `AI_MAX_TOKENS` (até 2000; mais tokens = mais quota e mais latência,
mantendo o timeout em mente) ou enviar `reasoning_effort: low`. A segunda exige uma nova variável de configuração e
**ainda não foi implementada**, por ser uma decisão de arquitetura pendente de aprovação.

Quotas: os limites do plano gratuito **não foram confirmados** aqui. A página oficial de limites mostra apenas o plano
*Developer* (para `openai/gpt-oss-20b`: 30 RPM, 1K RPD, 8K TPM e 200K TPD) e informa que os limites são por organização.
Confirme os seus em *Settings → Limits* no console. Com um histórico grande, um único pedido pode consumir uma parte
relevante de um limite de tokens por minuto, e o excesso aparece como `ai_quota`.

## Vercel

- O `vercel.json` já encaminha `/ai/...` ao Flask. **Configure `AI_*` nas variáveis de ambiente do projeto** (nunca no repositório).
- **Duração da função:** segundo a documentação atual da Vercel, com *Fluid Compute* (padrão) a duração padrão e máxima no
  plano Hobby é de 300 s (Pro: padrão 300 s, máximo 800 s), então o timeout de 20 s cabe. Este projeto usa a configuração
  antiga `builds` do `vercel.json`; confirme no painel (*Settings → Functions*) o limite efetivo do seu projeto. Se for
  preciso ajustá-lo, é uma mudança de configuração do deploy, a fazer numa etapa própria.

### Rate limit e IP real (pendente)

O limite de `/ai/chat` usa `flask_limiter.util.get_remote_address`, isto é, `request.remote_addr`. O código **não** usa
`ProxyFix` nem lê `X-Forwarded-For`. Segundo a [documentação da Vercel](https://vercel.com/docs/headers/request-headers), a
Vercel envia o IP público do cliente em `x-forwarded-for` (e em `x-real-ip` / `x-vercel-forwarded-for`), **sobrescreve**
o `X-Forwarded-For` enviado pelo cliente para evitar falsificação, e só aceita um IP personalizado no plano Enterprise com
*trusted proxy*. A documentação não diz o que o runtime `@vercel/python` coloca em `request.remote_addr`.

Por isso **não foi feita nenhuma alteração**: configurar `ProxyFix` sem saber quantos proxies existem à frente do Flask, ou
confiar em `X-Forwarded-For` sem essa garantia, permitiria que um cliente falsificasse o IP e fugisse ao limite (ou, no
sentido inverso, que todos partilhassem o mesmo contador). Para resolver numa etapa própria é preciso medir no ambiente real:
num deploy de *preview*, registe só `request.remote_addr` e o número de entradas de `X-Forwarded-For` (nunca os valores
completos nem outros cabeçalhos) e confirme se `remote_addr` é o IP do cliente ou o de um proxy.

## Limitações conhecidas

- Sem *streaming*: a resposta aparece inteira quando fica pronta.
- A conversa não é guardada: recarregar a página ou clicar em **Limpar conversa** a descarta.
- Respostas longas do assistente são cortadas a 2000 caracteres apenas na cópia reenviada como histórico.
- A lista do seletor de tarefas traz até 100 tarefas, as mais recentemente atualizadas.
- Nesta versão a IA não executa ações no sistema.
