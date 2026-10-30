// Testes do Assistente de IA (frontend/assistant.js) com um DOM mínimo simulado e sem rede.
// Executar a partir da raiz do projeto:  node frontend/tests/assistant.test.js
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const read = f => fs.readFileSync(path.join(__dirname, '..', f), 'utf8');
const ASSISTANT_SRC = read('assistant.js');
const SCRIPT_SRC = read('script.js');
const INDEX_SRC = read('index.html');

// ── DOM mínimo ──────────────────────────────────────────────────
class FakeEl {
    constructor(doc, tag, id) {
        this.doc = doc; this.tagName = tag; this.id = id || ''; this.children = []; this.parent = null;
        this.className = ''; this.hidden = false; this.disabled = false; this.style = {};
        this.attrs = {}; this._text = ''; this.value = ''; this.scrollTop = 0; this.scrollHeight = 100;
        this.innerHTMLWrites = 0; this.handlers = {};
    }
    set textContent(v) { this._text = String(v); this.children = []; }
    get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
    set innerHTML(v) { this.innerHTMLWrites++; this.children = []; this._text = ''; }
    appendChild(c) { c.parent = this; this.children.push(c); return c; }
    add(c) { this.appendChild(c); }
    get options() { return this.children; }
    remove(index) {
        if (typeof index === 'number') { this.children.splice(index, 1); return; }   // <select>.remove(i)
        if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this);
    }
    setAttribute(k, v) { this.attrs[k] = String(v); }
    getAttribute(k) { return this.attrs[k] ?? null; }
    focus() { this.doc.activeElement = this; }
    querySelectorAll(sel) {
        const cls = sel.replace('.', ''), out = [];
        const walk = n => n.children.forEach(c => { if (c.className.split(/\s+/).includes(cls)) out.push(c); walk(c); });
        walk(this);
        return out;
    }
    all() { return [this, ...this.children.flatMap(c => c.all())]; }
}

function makeDocument() {
    const doc = { activeElement: null };
    doc.body = new FakeEl(doc, 'body');
    const ids = ['assistant-log', 'assistant-welcome', 'assistant-send', 'assistant-error', 'assistant-error-text',
        'assistant-retry', 'assistant-input', 'assistant-chip', 'assistant-chip-title', 'assistant-consent',
        'assistant-consent-accept', 'assistant-task', 'dashboard-view', 'tasks-view', 'assistant-view', 'page-title',
        'sidebar'];
    ids.forEach(id => doc.body.appendChild(new FakeEl(doc, 'div', id)));
    doc.createElement = tag => new FakeEl(doc, tag);
    doc.getElementById = id => doc.body.all().find(e => e.id === id) || null;
    doc.byId = id => doc.getElementById(id);
    // estado inicial igual ao index.html
    doc.byId('assistant-error').hidden = true;
    doc.byId('assistant-chip').hidden = true;
    doc.byId('assistant-consent').hidden = true;
    doc.byId('assistant-view').hidden = true;
    doc.byId('assistant-task').appendChild(Object.assign(new FakeEl(doc, 'option'), { value: '', _text: 'Sem tarefa' }));
    return doc;
}

const response = (status, body) => ({ ok: status >= 200 && status < 300, status, json: async () => body });
const ok = reply => response(200, { reply, context: null });

function makeContext(apiImpl) {
    const document = makeDocument();
    const calls = [];
    const stubs = [];
    const ctx = {
        document, console, Date, Number, String, Promise, JSON, Math, Object, Array,
        get localStorage() { throw new Error('localStorage não deve ser usado'); },
        get sessionStorage() { throw new Error('sessionStorage não deve ser usado'); },
        apiRequest: async (url, opts) => { calls.push({ url, opts, body: opts && opts.body ? JSON.parse(opts.body) : null }); return apiImpl ? apiImpl(url, opts, calls) : ok('Olá!'); },
        tasksById: { 5: { id: 5, title: 'Preparar relatório' } }, panelTaskId: null,
        closeTaskPanel: (...a) => stubs.push(['closeTaskPanel', a]),
        showView: (...a) => stubs.push(['showView', a]),
    };
    vm.createContext(ctx);
    vm.runInContext(ASSISTANT_SRC, ctx);
    return { ctx, document, calls, stubs, run: code => vm.runInContext(code, ctx) };
}

const msgs = d => d.byId('assistant-log').querySelectorAll('.assistant-msg').filter(m => !m.className.includes('assistant-typing'));
const typing = d => d.getElementById('assistant-typing');
const type = (d, text) => { d.byId('assistant-input').value = text; };

let passed = 0;
async function test(name, fn) { await fn(); passed++; console.log('ok -', name); }

(async () => {
    // ── funções puras ──
    await test('histórico enviado: últimas 10 mensagens, 2000 caracteres cada, 8000 no total', async () => {
        const { ctx } = makeContext();
        const many = Array.from({ length: 14 }, (_, i) => ({ role: i % 2 ? 'assistant' : 'user', content: 'm' + i }));
        const p = ctx.assistantHistoryPayload(many);
        assert.strictEqual(p.length, 10);
        assert.strictEqual(p[p.length - 1].content, 'm13');
        const long = [{ role: 'assistant', content: 'a'.repeat(5000) }, { role: 'user', content: 'u' }];
        assert.strictEqual(ctx.assistantHistoryPayload(long)[0].content.length, 2000);     // resposta longa cortada só na cópia
        const big = Array.from({ length: 6 }, (_, i) => ({ role: i % 2 ? 'assistant' : 'user', content: 'x'.repeat(1900) }));
        const q = ctx.assistantHistoryPayload(big);
        assert(q.reduce((n, m) => n + m.content.length, 0) <= 8000);
        assert.strictEqual(q[q.length - 1], q[q.length - 1]);
        assert.strictEqual(q.length, 4);                                                   // descartou as mais antigas
    });

    await test('Enter envia; Shift+Enter, Ctrl+Enter e composição IME não enviam', async () => {
        const { ctx } = makeContext();
        assert.strictEqual(ctx.assistantShouldSend({ key: 'Enter' }), true);
        assert.strictEqual(ctx.assistantShouldSend({ key: 'Enter', shiftKey: true }), false);
        assert.strictEqual(ctx.assistantShouldSend({ key: 'Enter', ctrlKey: true }), false);
        assert.strictEqual(ctx.assistantShouldSend({ key: 'Enter', isComposing: true }), false);
        assert.strictEqual(ctx.assistantShouldSend({ key: 'Enter', keyCode: 229 }), false);
        assert.strictEqual(ctx.assistantShouldSend({ key: 'a' }), false);
    });

    await test('teclado: Enter envia e cancela a quebra de linha; Shift+Enter deixa o padrão', async () => {
        const c = makeContext();
        type(c.document, 'Olá');
        let prevented = 0;
        c.ctx.assistantKeydown({ key: 'Enter', preventDefault: () => prevented++ });
        await new Promise(r => setTimeout(r, 10));
        assert.strictEqual(prevented, 1);
        assert.strictEqual(c.calls.length, 1);
        type(c.document, 'Linha 1');
        c.ctx.assistantKeydown({ key: 'Enter', shiftKey: true, preventDefault: () => prevented++ });
        await new Promise(r => setTimeout(r, 10));
        assert.strictEqual(prevented, 1);
        assert.strictEqual(c.calls.length, 1);
    });

    await test('mapa de erros: mensagens compreensíveis, sem detalhes técnicos', async () => {
        const { ctx } = makeContext();
        for (const code of ['ai_not_configured', 'ai_auth', 'ai_model', 'ai_rejected', 'ai_quota', 'ai_timeout', 'ai_unavailable', 'ai_bad_response', 'ai_truncated', 'invalid_request']) {
            const t = ctx.assistantErrorInfo(502, code).text;
            assert(t.length > 20 && !/traceback|stack|exception|http \d{3}|api[_ ]?key|bearer/i.test(t), code);
        }
        assert(/configurado/.test(ctx.assistantErrorInfo(503, 'ai_not_configured').text));
        assert(/limite de uso|quota/i.test(ctx.assistantErrorInfo(503, 'ai_quota').text));
        assert(/demorou/.test(ctx.assistantErrorInfo(504, 'ai_timeout').text));
        assert(/depressa/.test(ctx.assistantErrorInfo(429).text));
        assert(/contactar o servidor/.test(ctx.assistantErrorInfo(0).text));
        assert.strictEqual(ctx.assistantErrorInfo(404, 'task_not_found').dropContext, true);
    });

    // ── envio, carregamento, resposta ──
    await test('envio: mensagem do utilizador, indicador de carregamento e resposta real', async () => {
        let release;
        const c = makeContext(() => new Promise(r => { release = () => r(ok('Comece pelas urgentes.')); }));
        const d = c.document;
        type(d, 'Como priorizo?');
        const pending = c.ctx.assistantSend();
        assert.strictEqual(msgs(d).length, 1);
        assert.strictEqual(msgs(d)[0].children[1].textContent, 'Como priorizo?');
        assert.strictEqual(d.byId('assistant-send').disabled, true, 'envio desativado durante a requisição');
        assert(typing(d), 'indicador de carregamento visível');
        assert.strictEqual(d.byId('assistant-log').getAttribute('aria-busy'), 'true');
        assert.strictEqual(d.byId('assistant-welcome').hidden, true);
        await c.ctx.assistantSend();                                   // segundo envio enquanto ocupado: ignorado
        assert.strictEqual(c.calls.length, 1);
        release(); await pending;
        assert(!typing(d));
        assert.strictEqual(d.byId('assistant-send').disabled, false);
        assert.strictEqual(msgs(d).length, 2);
        assert.strictEqual(msgs(d)[1].children[1].textContent, 'Comece pelas urgentes.');
        assert.strictEqual(c.calls[0].url, '/ai/chat');
        assert.strictEqual(c.calls[0].opts.method, 'POST');
        assert.deepStrictEqual(c.calls[0].body, { messages: [{ role: 'user', content: 'Como priorizo?' }], task_id: null });
        assert.strictEqual(d.byId('assistant-input').value, '');
    });

    await test('envio: mensagem vazia ou só espaços não faz pedido', async () => {
        const c = makeContext();
        type(c.document, '   ');
        await c.ctx.assistantSend();
        assert.strictEqual(c.calls.length, 0);
        assert.strictEqual(msgs(c.document).length, 0);
    });

    await test('o histórico acumula e é enviado nos pedidos seguintes', async () => {
        const c = makeContext();
        type(c.document, 'primeira'); await c.ctx.assistantSend();
        type(c.document, 'segunda');  await c.ctx.assistantSend();
        assert.deepStrictEqual(c.calls[1].body.messages.map(m => m.role), ['user', 'assistant', 'user']);
        assert.strictEqual(c.calls[1].body.messages[2].content, 'segunda');
    });

    // ── erros e tentar novamente ──
    for (const [label, res, expected] of [
        ['configuração', response(503, { code: 'ai_not_configured', message: 'x' }), /não está configurado/],
        ['quota', response(503, { code: 'ai_quota', message: 'x' }), /limite de uso/],
        ['timeout', response(504, { code: 'ai_timeout', message: 'x' }), /demorou/],
        ['falha de rede', null, /contactar o servidor/],
        ['limite de pedidos (429)', response(429, { error: 'x' }), /depressa/],
    ]) {
        await test(`erro (${label}): mensagem compreensível, mensagem do utilizador mantida e "Tentar novamente" disponível`, async () => {
            const c = makeContext(() => res);
            type(c.document, 'Pergunta');
            await c.ctx.assistantSend();
            assert.strictEqual(c.document.byId('assistant-error').hidden, false);
            assert(expected.test(c.document.byId('assistant-error-text').textContent), c.document.byId('assistant-error-text').textContent);
            assert.strictEqual(c.document.activeElement.id, 'assistant-retry');
            assert.strictEqual(msgs(c.document).length, 1);
            assert.strictEqual(c.document.byId('assistant-send').disabled, false);
            assert(!typing(c.document));
            assert.strictEqual(c.run('assistant.failed'), true);
        });
    }

    await test('429 do próprio aplicativo: mensagem de ritmo (sem texto do servidor) e "Tentar novamente" funciona', async () => {
        let n = 0;
        const c = makeContext(() => (++n === 1
            ? response(429, { error: 'Muitas requisições. Aguarde e tente novamente.' })
            : ok('Resposta depois da espera.')));
        type(c.document, 'Pergunta');
        await c.ctx.assistantSend();
        const text = c.document.byId('assistant-error-text').textContent;
        assert(/depressa/.test(text) && !/Muitas requisições/.test(text), text);
        assert(!/limite de uso|quota/i.test(text), 'não confundir com a quota do provedor');
        assert.strictEqual(c.run('assistant.failed'), true);
        await c.ctx.assistantRetry();
        assert.strictEqual(c.calls.length, 2);
        assert.strictEqual(c.document.byId('assistant-error').hidden, true);
        assert.strictEqual(msgs(c.document).length, 2);
        assert.strictEqual(c.run('assistant.failed'), false);
    });

    await test('429 do provedor (ai_quota) tem mensagem própria, distinta do 429 do aplicativo', async () => {
        const { ctx } = makeContext();
        assert(/limite de uso/.test(ctx.assistantErrorInfo(503, 'ai_quota').text));
        assert(!/limite de uso/.test(ctx.assistantErrorInfo(429, undefined).text));
    });

    await test('tentar novamente: reenvia sem duplicar a mensagem e mostra a resposta', async () => {
        let n = 0;
        const c = makeContext(() => (++n === 1 ? response(502, { code: 'ai_unavailable' }) : ok('Agora sim.')));
        type(c.document, 'Pergunta única');
        await c.ctx.assistantSend();
        await c.ctx.assistantRetry();
        assert.strictEqual(c.calls.length, 2);
        assert.deepStrictEqual(c.calls[1].body, c.calls[0].body);              // mesmo histórico, uma só vez
        assert.strictEqual(msgs(c.document).filter(m => m.className.includes('is-user')).length, 1);
        assert.strictEqual(msgs(c.document).length, 2);
        assert.strictEqual(c.document.byId('assistant-error').hidden, true);
        assert.strictEqual(c.run('assistant.failed'), false);
    });

    // ── limpar ──
    await test('limpar conversa: remove mensagens, erro e contexto, e mostra a mensagem inicial', async () => {
        const c = makeContext(() => response(503, { code: 'ai_quota' }));
        type(c.document, 'oi'); await c.ctx.assistantSend();
        c.ctx.assistantSelectTask('5');
        c.ctx.assistantAcceptConsent();
        c.ctx.assistantClear();
        assert.strictEqual(msgs(c.document).length, 0);
        assert.strictEqual(c.run('assistant.messages.length'), 0);
        assert.strictEqual(c.document.byId('assistant-welcome').hidden, false);
        assert.strictEqual(c.document.byId('assistant-error').hidden, true);
        assert.strictEqual(c.document.byId('assistant-chip').hidden, true);
        assert.strictEqual(c.run('assistant.taskId'), null);
        assert.strictEqual(c.run('assistant.consent'), false, 'nova conversa exige novo consentimento');
        assert.strictEqual(c.document.byId('assistant-task').value, '');
    });

    await test('limpar durante o carregamento ignora a resposta atrasada', async () => {
        let release;
        const c = makeContext(() => new Promise(r => { release = () => r(ok('resposta antiga')); }));
        type(c.document, 'oi');
        const pending = c.ctx.assistantSend();
        c.ctx.assistantClear();
        release(); await pending;
        assert.strictEqual(msgs(c.document).length, 0);
        assert.strictEqual(c.run('assistant.messages.length'), 0);
        assert(!typing(c.document));
    });

    // ── contexto e consentimento ──
    await test('consentimento: escolher tarefa mostra o aviso e NÃO ativa o contexto nem envia dados', async () => {
        const c = makeContext();
        const d = c.document;
        c.ctx.assistantSelectTask('5');
        assert.strictEqual(d.byId('assistant-consent').hidden, false);
        assert.strictEqual(d.byId('assistant-chip').hidden, true);
        assert.strictEqual(c.run('assistant.taskId'), null);
        assert.strictEqual(d.activeElement.id, 'assistant-consent-accept');
        type(d, 'Pergunta antes do aceite');
        await c.ctx.assistantSend();
        assert.strictEqual(c.calls[0].body.task_id, null, 'nenhum task_id antes do aceite explícito');
    });

    await test('consentimento: aceitar ativa o contexto (chip) e o task_id passa a seguir nos pedidos', async () => {
        const c = makeContext();
        const d = c.document;
        c.ctx.assistantSelectTask('5');
        c.ctx.assistantAcceptConsent();
        assert.strictEqual(d.byId('assistant-consent').hidden, true);
        assert.strictEqual(d.byId('assistant-chip').hidden, false);
        assert.strictEqual(d.byId('assistant-chip-title').textContent, 'Preparar relatório');
        type(d, 'E agora?'); await c.ctx.assistantSend();
        assert.strictEqual(c.calls[0].body.task_id, 5);
        assert.deepStrictEqual(Object.keys(c.calls[0].body).sort(), ['messages', 'task_id']);   // só o id, nunca o conteúdo da tarefa
        assert(!JSON.stringify(c.calls[0].body).includes('Preparar relatório'));
    });

    await test('consentimento: recusar não envia nada e repõe o seletor', async () => {
        const c = makeContext();
        c.ctx.assistantSelectTask('5');
        c.ctx.assistantDeclineConsent();
        assert.strictEqual(c.document.byId('assistant-consent').hidden, true);
        assert.strictEqual(c.document.byId('assistant-chip').hidden, true);
        assert.strictEqual(c.document.byId('assistant-task').value, '');
        assert.strictEqual(c.run('assistant.taskId'), null);
        assert.strictEqual(c.run('assistant.pendingTaskId'), null);
        assert.strictEqual(c.calls.length, 0);
    });

    await test('remover contexto: pedidos seguintes voltam a não levar task_id; o consentimento vale na sessão', async () => {
        const c = makeContext();
        c.ctx.assistantSelectTask('5'); c.ctx.assistantAcceptConsent();
        c.ctx.assistantRemoveContext();
        assert.strictEqual(c.document.byId('assistant-chip').hidden, true);
        type(c.document, 'geral'); await c.ctx.assistantSend();
        assert.strictEqual(c.calls[0].body.task_id, null);
        c.ctx.assistantSelectTask('5');                                  // mesma sessão: sem novo aviso
        assert.strictEqual(c.document.byId('assistant-consent').hidden, true);
        assert.strictEqual(c.run('assistant.taskId'), 5);
    });

    await test('tarefa deixou de estar acessível (404 do servidor): contexto removido e aviso claro', async () => {
        const c = makeContext(() => response(404, { code: 'task_not_found', message: 'Tarefa não encontrada.' }));
        c.ctx.assistantSelectTask('5'); c.ctx.assistantAcceptConsent();
        type(c.document, 'oi'); await c.ctx.assistantSend();
        assert.strictEqual(c.run('assistant.taskId'), null);
        assert.strictEqual(c.document.byId('assistant-chip').hidden, true);
        assert(/já não está disponível|deixou de ter acesso/.test(c.document.byId('assistant-error-text').textContent));
    });

    await test('botão "Perguntar à IA" do painel: abre o assistente com a tarefa pendente de consentimento, sem pedidos', async () => {
        const c = makeContext();
        c.ctx.panelTaskId = 5;
        c.run('panelTaskId = 5');
        c.ctx.askAiAboutTask();
        assert.deepStrictEqual(c.stubs.map(s => s[0]), ['closeTaskPanel', 'showView']);
        assert.deepStrictEqual(c.stubs[0][1], [false]);
        assert.deepStrictEqual(c.stubs[1][1], ['assistant']);
        assert.strictEqual(c.document.byId('assistant-consent').hidden, false);
        assert.strictEqual(c.run('assistant.pendingTaskId'), 5);
        assert.strictEqual(c.run('assistant.taskId'), null);
        assert.strictEqual(c.document.byId('assistant-task').value, '5');
        assert.strictEqual(c.calls.length, 0, 'nada é enviado antes do aceite');
    });

    await test('seletor de tarefas: 1 pedido, reutilizado, com títulos como texto', async () => {
        const c = makeContext(() => response(200, { tasks: [{ id: 1, title: '<b>Titulo</b>' }, { id: 2, title: 'x'.repeat(100) }] }));
        await c.ctx.assistantLoadTasks();
        await c.ctx.assistantLoadTasks();
        assert.strictEqual(c.calls.length, 1);
        assert.strictEqual(c.calls[0].url, '/tasks?per_page=100&sort_by=updated_at&order=desc');
        const options = c.document.byId('assistant-task').options;
        assert.strictEqual(options.length, 3);
        assert.strictEqual(options[1].textContent, '<b>Titulo</b>');
        assert(options[2].textContent.length <= 70);
        assert.strictEqual(c.document.byId('assistant-task').innerHTMLWrites, 0);
    });

    // ── segurança da renderização e armazenamento ──
    await test('respostas da IA são texto escapado: nenhum HTML é interpretado', async () => {
        const evil = '<img src=x onerror="alert(1)"><script>alert(2)</script><b>negrito</b>';
        const c = makeContext(() => ok(evil));
        type(c.document, '<i>minha pergunta</i>');
        await c.ctx.assistantSend();
        const [mine, reply] = msgs(c.document);
        assert.strictEqual(reply.children[1].textContent, evil);
        assert.strictEqual(mine.children[1].textContent, '<i>minha pergunta</i>');
        const tags = new Set(c.document.byId('assistant-log').all().map(e => e.tagName));
        for (const bad of ['img', 'script', 'b', 'i']) assert(!tags.has(bad), `não pode existir <${bad}>`);
        assert(c.document.body.all().every(e => e.innerHTMLWrites === 0 || e.id === ''), 'sem innerHTML');
        assert.strictEqual(c.document.byId('assistant-log').innerHTMLWrites, 0);
    });

    await test('nada é guardado no navegador: sem localStorage/sessionStorage/IndexedDB/cookies no código nem na execução', async () => {
        const code = ASSISTANT_SRC.replace(/\/\/.*$/gm, '');          // ignora comentários
        assert(!/localStorage|sessionStorage|indexedDB|document\.cookie|caches\./.test(code));
        assert(!/innerHTML/.test(code), 'assistant.js não usa innerHTML');
        const c = makeContext(() => ok('resposta'));       // o contexto lança erro se o armazenamento for tocado
        type(c.document, 'oi');
        await c.ctx.assistantSend();
        c.ctx.assistantSelectTask('5'); c.ctx.assistantAcceptConsent(); c.ctx.assistantClear();
    });

    // ── integração com a navegação existente ──
    const viewCtx = (extra = {}) => {
        const document = makeDocument();
        document.body.appendChild(Object.assign(new FakeEl(document, 'a'), { className: 'nav-link', dataset: { view: 'dashboard' } }));
        document.body.appendChild(Object.assign(new FakeEl(document, 'a'), { className: 'nav-link', dataset: { view: 'assistant' } }));
        document.body.appendChild(Object.assign(new FakeEl(document, 'a'), { className: 'nav-link', dataset: { filter: 'all' } }));
        document.querySelectorAll = sel => document.body.querySelectorAll(sel);
        const calls = [];
        const ctx = { document, state: {}, window: { innerWidth: 1280 }, ...extra,
            loadDashboard: () => calls.push('loadDashboard'), onAssistantShown: () => calls.push('onAssistantShown'),
            toggleSidebar: () => calls.push('toggleSidebar') };
        // classList mínimo para os .nav-link
        document.body.all().filter(e => e.className === 'nav-link').forEach(e => {
            e.classes = new Set(); e.classList = { toggle: (c, on) => (on ? e.classes.add(c) : e.classes.delete(c)) };
        });
        vm.createContext(ctx);
        const a = SCRIPT_SRC.indexOf('function showView'), b = SCRIPT_SRC.indexOf('function filterByCategory');
        vm.runInContext(SCRIPT_SRC.slice(a, b), ctx);
        return { ctx, document, calls };
    };
    const active = d => d.body.all().filter(e => e.className === 'nav-link' && e.classes.has('active')).map(e => e.dataset.view || e.dataset.filter);

    await test('navegação: showView("assistant") mostra a vista, ativa o item de menu e avisa o assistente', async () => {
        const v = viewCtx();
        v.ctx.showView('assistant');
        assert.strictEqual(v.document.byId('assistant-view').hidden, false);
        assert.strictEqual(v.document.byId('dashboard-view').hidden, true);
        assert.strictEqual(v.document.byId('tasks-view').hidden, true);
        assert.strictEqual(v.document.byId('page-title').textContent, 'Assistente de IA');
        assert.deepStrictEqual(active(v.document), ['assistant']);
        assert.deepStrictEqual(v.calls, ['onAssistantShown']);
        assert.strictEqual(v.ctx.state.view, 'assistant');
    });

    await test('navegação: dashboard e lista de tarefas continuam a funcionar como antes', async () => {
        const v = viewCtx();
        v.ctx.showView('dashboard');
        assert.strictEqual(v.document.byId('dashboard-view').hidden, false);
        assert.strictEqual(v.document.byId('assistant-view').hidden, true);
        assert.strictEqual(v.document.byId('page-title').textContent, 'Dashboard');
        assert.deepStrictEqual(active(v.document), ['dashboard']);
        assert.deepStrictEqual(v.calls, ['loadDashboard']);
        v.ctx.showView('tasks', 'Todas as Tarefas', 'all');
        assert.strictEqual(v.document.byId('tasks-view').hidden, false);
        assert.strictEqual(v.document.byId('dashboard-view').hidden, true);
        assert.strictEqual(v.document.byId('assistant-view').hidden, true);
        assert.strictEqual(v.document.byId('page-title').textContent, 'Todas as Tarefas');
        assert.deepStrictEqual(active(v.document), ['all']);
        assert.strictEqual(v.ctx.state.view, 'tasks');
    });

    await test('classes das mensagens não colidem com o contentor .assistant (layout)', async () => {
        const c = makeContext(() => ok('resposta'));
        type(c.document, 'oi'); await c.ctx.assistantSend();
        for (const m of msgs(c.document)) {
            assert(!m.className.split(/\s+/).includes('assistant'), m.className);
            assert(/is-(user|assistant)/.test(m.className));
        }
        const css = fs.readFileSync(path.join(__dirname, '..', 'assistant.css'), 'utf8');
        assert(!/\.assistant-msg\.(user|assistant)\b/.test(css));
    });

    await test('estrutura: menu, vista, scripts, botão do painel e rota do Vercel', async () => {
        assert(/data-view="assistant"[^>]*showView\('assistant'\)/.test(INDEX_SRC), 'item de menu');
        assert(/id="assistant-view"/.test(INDEX_SRC) && /id="assistant-log"[^>]*role="log"[^>]*aria-live="polite"/.test(INDEX_SRC));
        assert(/<script src="assistant\.js/.test(INDEX_SRC) && /href="assistant\.css/.test(INDEX_SRC));
        assert(INDEX_SRC.indexOf('script.js') < INDEX_SRC.indexOf('assistant.js'), 'assistant.js depois de script.js');
        assert(/for="assistant-input"/.test(INDEX_SRC) && /for="assistant-task"/.test(INDEX_SRC), 'rótulos acessíveis');
        const consent = INDEX_SRC.match(/id="assistant-consent-text"[\s\S]*?<\/p>/)[0].replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ');
        assert(/conteúdo da tarefa selecionada/.test(consent) && /será enviado ao provedor externo de IA/.test(consent), 'envio ao provedor');
        assert(/tarefas? de outro usuário/.test(consent) && /compartilhadas? com você/.test(consent), 'tarefa compartilhada');
        assert(/indicador de atraso/.test(consent) && /calculado a partir do prazo e do estado/.test(consent), 'indicador de atraso');
        for (const campo of ['título', 'descrição', 'estado', 'prioridade', 'prazo', 'categoria', 'papel na tarefa']) {
            assert(consent.includes(campo), `campo enviado ausente do consentimento: ${campo}`);
        }
        assert(/Evite incluir informações pessoais ou confidenciais\./.test(consent));
        assert(/askAiAboutTask\(\)[^<]*>[\s\S]{0,60}Perguntar à IA/.test(SCRIPT_SRC), 'botão no painel de detalhes');
        const vercel = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'vercel.json'), 'utf8'));
        const api = vercel.routes.find(r => r.dest === 'backend/wsgi.py').src;
        const re = new RegExp('^' + api + '$');
        for (const p of ['/ai/chat', '/auth/login', '/tasks', '/tasks/1', '/categories', '/notifications/read-all']) assert(re.test(p), p);
        for (const p of ['/assistant.js', '/assistant.css', '/index.html', '/style.css', '/script.js']) assert(!re.test(p), p + ' deve continuar estático');
    });

    console.log(`\n${passed} testes do assistente passaram`);
})().catch(e => { console.error('FALHOU:', e && e.stack || e); process.exit(1); });
