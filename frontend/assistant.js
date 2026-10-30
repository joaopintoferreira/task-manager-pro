'use strict';

// ══════════════════════════════════════════════════════════════════
// ASSISTENTE DE IA
// A conversa vive só na memória desta página: nada é guardado no navegador (nem localStorage/
// sessionStorage) nem no servidor. O frontend envia apenas o texto das mensagens e, se o utilizador
// aceitar, o `task_id` da tarefa; quem verifica o acesso e monta o contexto é o backend.
// As respostas do modelo são texto não confiável: só entram no DOM via textContent.
// ══════════════════════════════════════════════════════════════════
const ASSISTANT_MAX_MESSAGES = 10;     // histórico enviado (o servidor também limita)
const ASSISTANT_MAX_CHARS    = 2000;   // por mensagem
const ASSISTANT_MAX_TOTAL    = 8000;   // histórico inteiro
const ASSISTANT_TASKS_TTL_MS = 5 * 60 * 1000;

const assistant = {
    messages: [],        // [{role: 'user' | 'assistant', content}]
    busy: false,
    failed: false,       // a última requisição falhou (a mensagem do utilizador continua no histórico)
    taskId: null,        // contexto ativo (só depois do consentimento)
    taskTitle: '',
    pendingTaskId: null, // tarefa escolhida à espera de consentimento
    consent: false,      // consentimento desta sessão da interface (não é guardado em lado nenhum)
    epoch: 0,            // incrementa ao limpar: respostas atrasadas da conversa antiga são ignoradas
    tasksLoadedAt: 0,
    taskTitles: {},
};

const assistantEl = id => document.getElementById(id);

// ── Funções puras (testadas sem DOM) ──────────────────────────────

// Histórico a enviar: últimas 10 mensagens, cada uma até 2000 caracteres, no máximo 8000 no total
// (descarta as mais antigas). As respostas do assistente podem ser mais longas que 2000: são cortadas
// só na cópia enviada como contexto.
function assistantHistoryPayload(messages) {
    const list = messages.slice(-ASSISTANT_MAX_MESSAGES)
        .map(m => ({ role: m.role, content: String(m.content).slice(0, ASSISTANT_MAX_CHARS) }));
    let total = list.reduce((n, m) => n + m.content.length, 0);
    while (list.length > 1 && total > ASSISTANT_MAX_TOTAL) total -= list.shift().content.length;
    return list;
}

// Enter envia; Shift+Enter quebra a linha; durante a composição de texto (IME) Enter confirma a composição
function assistantShouldSend(e) {
    return e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.altKey && !e.metaKey
        && !e.isComposing && e.keyCode !== 229;
}

// Mensagem para o utilizador a partir da resposta do servidor (nunca mostra detalhes técnicos)
function assistantErrorInfo(status, code) {
    const byCode = {
        ai_not_configured: 'O assistente de IA ainda não está configurado neste servidor. Peça ao administrador para definir o provedor, a chave e o modelo.',
        ai_auth:           'O provedor de IA recusou as credenciais configuradas. Avise o administrador.',
        ai_model:          'O modelo de IA configurado não está disponível. Avise o administrador.',
        ai_rejected:       'O provedor de IA recusou o pedido. Avise o administrador para verificar o modelo configurado.',
        ai_quota:          'O limite de uso do provedor de IA foi atingido (quota ou saldo). Tente mais tarde.',
        ai_timeout:        'O assistente demorou demasiado a responder. Tente novamente.',
        ai_unavailable:    'O serviço de IA está indisponível neste momento. Tente novamente em instantes.',
        ai_bad_response:   'O serviço de IA devolveu uma resposta inválida. Tente novamente.',
        invalid_request:   'Não foi possível enviar esta mensagem. Verifique o tamanho e tente novamente.',
    };
    if (code === 'task_not_found') {
        return { text: 'A tarefa selecionada já não está disponível ou deixou de ter acesso a ela. O contexto foi removido.', dropContext: true };
    }
    if (byCode[code]) return { text: byCode[code] };
    if (status === 429) return { text: 'Enviou mensagens demasiado depressa. Aguarde um momento e tente novamente.' };
    if (status === 0 || status == null) return { text: 'Não foi possível contactar o servidor. Verifique a ligação e tente novamente.' };
    return { text: 'Não foi possível obter uma resposta do assistente. Tente novamente.' };
}

// ── Interface ─────────────────────────────────────────────────────

function assistantScrollToEnd() {
    const log = assistantEl('assistant-log');
    log.scrollTop = log.scrollHeight;
}

function assistantAppendMessage(role, text) {
    const item = document.createElement('div');
    item.className = `assistant-msg is-${role}`;
    const who = document.createElement('span');
    who.className = 'assistant-msg-who';
    who.textContent = role === 'user' ? 'Você' : 'Assistente';
    const body = document.createElement('p');
    body.className = 'assistant-msg-text';
    body.textContent = text;                       // texto puro: nunca HTML
    item.appendChild(who);
    item.appendChild(body);
    assistantEl('assistant-log').appendChild(item);
    assistantEl('assistant-welcome').hidden = true;
    assistantScrollToEnd();
}

function assistantSetTyping(on) {
    const log = assistantEl('assistant-log');
    let box = assistantEl('assistant-typing');
    if (on && !box) {
        box = document.createElement('div');
        box.id = 'assistant-typing';
        box.className = 'assistant-msg is-assistant assistant-typing';
        box.setAttribute('role', 'status');
        const hidden = document.createElement('span');
        hidden.className = 'visually-hidden';
        hidden.textContent = 'O assistente está a responder…';
        box.appendChild(hidden);
        for (let i = 0; i < 3; i++) box.appendChild(document.createElement('i'));
        log.appendChild(box);
        assistantScrollToEnd();
    } else if (!on && box) {
        box.remove();
    }
}

function assistantSetBusy(busy) {
    assistant.busy = busy;
    assistantEl('assistant-send').disabled = busy;
    assistantEl('assistant-log').setAttribute('aria-busy', String(busy));
    assistantSetTyping(busy);
}

function assistantShowError(text) {
    assistantEl('assistant-error-text').textContent = text;
    assistantEl('assistant-error').hidden = false;
}

function assistantHideError() {
    assistantEl('assistant-error').hidden = true;
    assistantEl('assistant-error-text').textContent = '';
}

function assistantOnInput() {
    const input = assistantEl('assistant-input');
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 160) + 'px';
}

function assistantKeydown(e) {
    if (!assistantShouldSend(e)) return;
    e.preventDefault();
    assistantSend();
}

function assistantUseExample(text) {
    const input = assistantEl('assistant-input');
    input.value = text;
    assistantOnInput();
    input.focus();
}

// ── Envio ─────────────────────────────────────────────────────────

async function assistantSend() {
    if (assistant.busy) return;
    const input = assistantEl('assistant-input');
    const text = input.value.trim();
    if (!text) return;
    if (text.length > ASSISTANT_MAX_CHARS) {
        assistantShowError(`A mensagem tem ${text.length} caracteres; o máximo é ${ASSISTANT_MAX_CHARS}.`);
        return;
    }
    assistant.messages.push({ role: 'user', content: text });
    assistantAppendMessage('user', text);
    input.value = '';
    assistantOnInput();
    await assistantRequest();
}

// Tentar novamente reenvia o mesmo histórico: a mensagem do utilizador não é duplicada
async function assistantRetry() {
    if (assistant.busy) return;
    const last = assistant.messages[assistant.messages.length - 1];
    if (!last || last.role !== 'user') return;
    await assistantRequest();
}

async function assistantRequest() {
    const epoch = assistant.epoch;
    assistantHideError();
    assistant.failed = false;
    assistantSetBusy(true);

    const res = await apiRequest('/ai/chat', {
        method: 'POST',
        body: JSON.stringify({
            messages: assistantHistoryPayload(assistant.messages),
            task_id: assistant.taskId,          // o servidor volta a verificar o acesso a cada pedido
        }),
    });
    if (epoch !== assistant.epoch) return;      // a conversa foi limpa enquanto esperava
    assistantSetBusy(false);

    const data = res ? await res.json().catch(() => ({})) : {};
    if (res && res.ok && typeof data.reply === 'string' && data.reply) {
        assistant.messages.push({ role: 'assistant', content: data.reply });
        assistantAppendMessage('assistant', data.reply);
        return;
    }

    assistant.failed = true;
    const info = assistantErrorInfo(res ? res.status : 0, data.code);
    if (info.dropContext) assistantRemoveContext();
    assistantShowError(info.text);
    assistantEl('assistant-retry').focus();
}

// ── Contexto de tarefa e consentimento ───────────────────────────

function assistantRenderContext() {
    const active = assistant.taskId !== null;
    assistantEl('assistant-chip').hidden = !active;
    assistantEl('assistant-chip-title').textContent = assistant.taskTitle;
    assistantEl('assistant-consent').hidden = assistant.pendingTaskId === null;
    assistantEl('assistant-task').value = active ? String(assistant.taskId)
        : (assistant.pendingTaskId !== null ? String(assistant.pendingTaskId) : '');
}

function assistantTitleFor(id) {
    return assistant.taskTitles[id] || (typeof tasksById !== 'undefined' && tasksById[id] ? tasksById[id].title : 'Tarefa');
}

// O contexto só fica ativo depois do consentimento explícito desta sessão
function assistantSelectTask(value) {
    const id = value ? Number(value) : null;
    assistantHideError();
    if (id === null) { assistantRemoveContext(); return; }
    if (assistant.consent) { assistantActivateContext(id); return; }
    assistant.taskId = null;
    assistant.taskTitle = '';
    assistant.pendingTaskId = id;
    assistantRenderContext();
    assistantEl('assistant-consent-accept').focus();
}

function assistantActivateContext(id) {
    assistant.pendingTaskId = null;
    assistant.taskId = id;
    assistant.taskTitle = assistantTitleFor(id);
    assistantRenderContext();
}

function assistantAcceptConsent() {
    if (assistant.pendingTaskId === null) return;
    assistant.consent = true;
    assistantActivateContext(assistant.pendingTaskId);
    assistantEl('assistant-input').focus();
}

function assistantDeclineConsent() {
    assistant.pendingTaskId = null;
    assistantRenderContext();
    assistantEl('assistant-task').focus();
}

function assistantRemoveContext() {
    assistant.taskId = null;
    assistant.taskTitle = '';
    assistant.pendingTaskId = null;
    assistantRenderContext();
}

// Botão "Perguntar à IA" do painel de detalhes: abre o assistente com a tarefa como contexto pendente
function askAiAboutTask() {
    if (typeof panelTaskId === 'undefined' || panelTaskId === null) return;
    const id = panelTaskId;
    if (tasksById[id]) assistant.taskTitles[id] = tasksById[id].title;
    closeTaskPanel(false);
    showView('assistant');
    assistantEnsureTaskOption(id, assistant.taskTitles[id]);
    assistantSelectTask(String(id));
}

function assistantEnsureTaskOption(id, title) {
    const select = assistantEl('assistant-task');
    if ([...select.options].some(o => o.value === String(id))) return;
    const option = document.createElement('option');
    option.value = String(id);
    option.textContent = title || 'Tarefa';
    select.appendChild(option);
}

// ── Lista de tarefas para o seletor (1 pedido, reutilizado durante 5 minutos) ──

async function assistantLoadTasks() {
    if (Date.now() - assistant.tasksLoadedAt < ASSISTANT_TASKS_TTL_MS) return;
    assistant.tasksLoadedAt = Date.now();
    const res = await apiRequest('/tasks?per_page=100&sort_by=updated_at&order=desc');
    if (!res || !res.ok) { assistant.tasksLoadedAt = 0; return; }
    const data = await res.json();
    const select = assistantEl('assistant-task');
    const keep = assistant.taskId !== null ? String(assistant.taskId)
        : (assistant.pendingTaskId !== null ? String(assistant.pendingTaskId) : '');
    while (select.options.length > 1) select.remove(1);
    (data.tasks || []).forEach(t => {
        assistant.taskTitles[t.id] = t.title;
        const option = document.createElement('option');
        option.value = String(t.id);
        option.textContent = t.title.length > 70 ? t.title.slice(0, 67) + '…' : t.title;
        select.appendChild(option);
    });
    if (keep) assistantEnsureTaskOption(keep, assistant.taskTitles[keep]);
    select.value = keep;
}

// ── Limpar e entrar na vista ─────────────────────────────────────

function assistantClear() {
    assistant.epoch++;                 // ignora respostas ainda em curso
    assistant.messages = [];
    assistant.failed = false;
    assistant.consent = false;         // nova conversa = novo consentimento
    assistantSetBusy(false);
    assistantHideError();
    const log = assistantEl('assistant-log');
    [...log.querySelectorAll('.assistant-msg')].forEach(n => n.remove());
    assistantEl('assistant-welcome').hidden = false;
    assistantEl('assistant-input').value = '';
    assistantOnInput();
    assistantRemoveContext();
    assistantEl('assistant-input').focus();
}

// Chamada por showView('assistant')
function onAssistantShown() {
    assistantLoadTasks();
    if (assistant.pendingTaskId === null) assistantEl('assistant-input').focus();
}
