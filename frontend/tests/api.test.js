// Testes do tratamento de erros de apiRequest e do carregamento por vista em frontend/script.js.
// Executar a partir da raiz do projeto:  node frontend/tests/api.test.js
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const src = fs.readFileSync(path.join(__dirname, '..', 'script.js'), 'utf8');
const slice = (from, to) => {
    const a = src.indexOf(from), b = src.indexOf(to);
    assert(a >= 0 && b > a, `marcadores não encontrados: ${from}`);
    return src.slice(a, b);
};

function makeSandbox(fetchImpl) {
    const toasts = [];
    const ctx = {
        API: '', accessToken: 't', refreshToken: 'r', console: { error() {} },
        Date, Map, Promise, setTimeout, localStorage: { clear() {} },
        toast: (msg, type) => toasts.push({ msg, type }),
        fetch: fetchImpl, state: { view: 'dashboard' },
        calls: [],
    };
    vm.createContext(ctx);
    vm.runInContext(slice('async function apiRequest', '// Requisições simultâneas'), ctx);
    return { ctx, toasts };
}

let passed = 0;
async function test(name, fn) { await fn(); passed++; console.log('ok -', name); }

(async () => {
    await test('429 com resposta acessível mostra mensagem de excesso de requisições', async () => {
        const { ctx, toasts } = makeSandbox(async () => ({ status: 429, ok: false }));
        const res = await ctx.apiRequest('/tasks');
        assert.strictEqual(res.status, 429);
        assert.strictEqual(toasts.length, 1);
        assert(/Muitas requisições/.test(toasts[0].msg));
    });

    await test('falha de fetch sem resposta usa mensagem neutra e devolve null', async () => {
        const { ctx, toasts } = makeSandbox(async () => { throw new TypeError('NetworkError'); });
        assert.strictEqual(await ctx.apiRequest('/tasks'), null);
        assert.strictEqual(toasts.length, 1);
        assert(/Não foi possível contactar/.test(toasts[0].msg));
        assert(!/servidor está|falha do servidor/i.test(toasts[0].msg));
    });

    await test('avisos idênticos simultâneos não se empilham', async () => {
        const { ctx, toasts } = makeSandbox(async () => { throw new TypeError('x'); });
        await Promise.all([ctx.apiRequest('/a'), ctx.apiRequest('/b'), ctx.apiRequest('/c')]);
        assert.strictEqual(toasts.length, 1);
    });

    await test('resposta 200 não gera aviso', async () => {
        const { ctx, toasts } = makeSandbox(async () => ({ status: 200, ok: true }));
        assert.strictEqual((await ctx.apiRequest('/tasks')).ok, true);
        assert.strictEqual(toasts.length, 0);
    });

    // loadAll: só carrega o que a vista ativa precisa
    const loadCtx = view => {
        const calls = [];
        const ctx = { state: { view }, Promise };
        for (const n of ['loadTasks', 'loadRecentTasks', 'loadCategories', 'loadStats']) ctx[n] = async () => calls.push(n);
        vm.createContext(ctx);
        vm.runInContext(slice('async function loadAll', 'async function loadTasks'), ctx);
        return { ctx, calls };
    };

    await test('loadAll no Dashboard não pede a lista paginada', async () => {
        const { ctx, calls } = loadCtx('dashboard');
        await ctx.loadAll();
        assert.deepStrictEqual([...calls].sort(), ['loadCategories', 'loadRecentTasks', 'loadStats']);
    });

    await test('loadAll na vista de tarefas pede a lista e não as recentes', async () => {
        const { ctx, calls } = loadCtx('tasks');
        await ctx.loadAll();
        assert.deepStrictEqual([...calls].sort(), ['loadCategories', 'loadStats', 'loadTasks']);
    });

    // Permissões mostradas no painel (o servidor continua a decidir de fato)
    const access = (task, user) => {
        const ctx = { currentUser: user };
        vm.createContext(ctx);
        vm.runInContext(slice('function taskAccess', 'function onTaskCardClick'), ctx);
        return ctx.taskAccess(task);
    };
    const me = { id: 1 };

    await test('painel: dono pode editar e excluir', async () => {
        const a = access({ user_id: 1, collaborators: [] }, me);
        assert.deepStrictEqual([a.isOwner, a.canEdit, a.canDelete], [true, true, true]);
    });

    await test('painel: colaborador editor edita mas não exclui', async () => {
        const a = access({ user_id: 2, collaborators: [{ user_id: 1, role: 'editor' }] }, me);
        assert.deepStrictEqual([a.isOwner, a.canEdit, a.canDelete], [false, true, false]);
    });

    await test('painel: colaborador leitor não edita nem exclui', async () => {
        const a = access({ user_id: 2, collaborators: [{ user_id: 1, role: 'viewer' }] }, me);
        assert.deepStrictEqual([a.isOwner, a.canEdit, a.canDelete], [false, false, false]);
    });

    // Gestão de colaboradores: validação do email e mensagens (neutras) de erro
    const shareCtx = () => {
        const ctx = { currentUser: { id: 1, email: 'Dono@Exemplo.com' } };
        vm.createContext(ctx);
        vm.runInContext(slice('function taskAccess', 'function accessBadges'), ctx);
        return ctx;
    };
    const task = { user_id: 1, collaborators: [{ user_id: 2, email: 'Ana@exemplo.com', role: 'viewer' }] };

    await test('partilha: validação local do email', async () => {
        const c = shareCtx();
        const err = e => c.shareEmailError(e, task, c.currentUser);
        assert(err('').length > 0);
        assert(err('sem-arroba').length > 0);
        assert(err('a@b').length > 0);                         // sem domínio com ponto
        assert(err('x'.repeat(115) + '@a.com').length > 0);    // > 120 caracteres
        assert(/dono/i.test(err('dono@exemplo.com')));         // o próprio (sem distinguir maiúsculas)
        assert(/já tem acesso/i.test(err('ana@exemplo.com'))); // já é colaborador
        assert.strictEqual(err('novo@exemplo.com'), '');
    });

    await test('partilha: erro 404 é neutro e não revela se o email existe', async () => {
        const c = shareCtx();
        const msg = c.shareErrorMessage(404, 'Usuário não encontrado!');
        assert(!/não encontrado|não existe|inexistente|inativ|registad/i.test(msg), msg);
        assert.strictEqual(c.shareErrorMessage(404, 'qualquer coisa'), msg);
    });

    await test('partilha: 400/403 mostram a mensagem do servidor; outros erros são genéricos', async () => {
        const c = shareCtx();
        assert.strictEqual(c.shareErrorMessage(403, 'Apenas o dono pode adicionar colaboradores!'), 'Apenas o dono pode adicionar colaboradores!');
        assert.strictEqual(c.shareErrorMessage(400, 'Usuário já é colaborador desta tarefa!'), 'Usuário já é colaborador desta tarefa!');
        assert(/Muitas requisições/.test(c.shareErrorMessage(429)));
        assert(/Tente novamente/.test(c.shareErrorMessage(500, 'detalhe interno')));
        assert(!/detalhe interno/.test(c.shareErrorMessage(500, 'detalhe interno')));
    });

    // Remoção individual de notificações (DOM mínimo simulado)
    const notifCtx = (apiImpl, { unread = true, siblings = false } = {}) => {
        const classes = new Set(unread ? ['notif-item', 'unread'] : ['notif-item']);
        const state = { removed: false, focused: [], toasts: [], badge: { textContent: '', style: {} },
                        list: { innerHTML: 'ANTES', querySelector() { return state.removed && !siblings ? null : {}; } } };
        const item = {
            classList: { add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c) },
            remove() { state.removed = true; }, isConnected: true,
            nextElementSibling: null, previousElementSibling: null, querySelector: () => null,
        };
        const btn = { closest: () => item, disabled: false, focus() { state.focused.push('btn'); } };
        const ctx = {
            state, item, btn, classes, event: { stopPropagation() { state.stopped = true; } },
            document: {
                getElementById: id => (id === 'notif-badge' ? state.badge : state.list),
                querySelector: () => ({ focus() { state.focused.push('markall'); } }),
            },
            apiRequest: async (...a) => { state.calls = (state.calls || []).concat([a]); return apiImpl(); },
            toast: (m, t) => state.toasts.push({ m, t }),
            esc: x => x, relTimeHtml: x => x, Math,
        };
        vm.createContext(ctx);
        vm.runInContext(slice('let notifUnread', 'function toggleNotifPanel'), ctx);
        vm.runInContext('notifUnread = 3', ctx);
        return ctx;
    };

    await test('notificação: remover usa DELETE, não marca como lida e atualiza o selo', async () => {
        const c = notifCtx(async () => ({ ok: true, status: 200 }));
        await c.deleteNotif(7, c.btn, c.event);
        assert.strictEqual(c.state.calls.length, 1);
        assert.strictEqual(c.state.calls[0][0], '/notifications/7');
        assert.strictEqual(c.state.calls[0][1].method, 'DELETE');
        assert(c.state.stopped, 'o clique não deve propagar (marcar como lida / fechar painel)');
        assert(c.state.removed);
        assert.strictEqual(vm.runInContext('notifUnread', c), 2);
        assert.strictEqual(c.state.badge.textContent, 2);
        assert.strictEqual(c.state.list.innerHTML, vm.runInContext('NOTIF_EMPTY_HTML', c));   // lista ficou vazia
        assert.deepStrictEqual(c.state.focused, ['markall']);
    });

    await test('notificação: remover uma já lida não altera o selo', async () => {
        const c = notifCtx(async () => ({ ok: true, status: 200 }), { unread: false });
        await c.deleteNotif(7, c.btn, c.event);
        assert.strictEqual(vm.runInContext('notifUnread', c), 3);
    });

    await test('notificação: 404 (já removida) conta como sucesso', async () => {
        const c = notifCtx(async () => ({ ok: false, status: 404 }));
        await c.deleteNotif(7, c.btn, c.event);
        assert(c.state.removed);
        assert.strictEqual(c.state.toasts.length, 0);
    });

    await test('notificação: falha de rede repõe o item sem aviso duplicado', async () => {
        const c = notifCtx(async () => null);          // apiRequest já mostra o aviso de rede
        await c.deleteNotif(7, c.btn, c.event);
        assert(!c.state.removed);
        assert(!c.classes.has('is-removing') && c.classes.has('unread'));
        assert.strictEqual(c.btn.disabled, false);
        assert.strictEqual(vm.runInContext('notifUnread', c), 3);
        assert.strictEqual(c.state.toasts.length, 0);
    });

    await test('notificação: 429 repõe o item (o aviso é do apiRequest); 403/500 mostram aviso próprio', async () => {
        let c = notifCtx(async () => ({ ok: false, status: 429 }));
        await c.deleteNotif(7, c.btn, c.event);
        assert(!c.state.removed && c.state.toasts.length === 0);
        for (const status of [403, 500]) {
            c = notifCtx(async () => ({ ok: false, status }));
            await c.deleteNotif(7, c.btn, c.event);
            assert(!c.state.removed);
            assert.strictEqual(c.state.toasts.length, 1);
            assert(/Não foi possível remover/.test(c.state.toasts[0].m));
        }
    });

    await test('notificação: duplo clique durante o pedido só envia um DELETE', async () => {
        let release;
        const c = notifCtx(() => new Promise(r => { release = () => r({ ok: true, status: 200 }); }));
        const first = c.deleteNotif(7, c.btn, c.event);
        await c.deleteNotif(7, c.btn, c.event);        // segundo clique enquanto o primeiro decorre
        release(); await first;
        assert.strictEqual(c.state.calls.length, 1);
    });

    // Nome do dono (Fase 2): nunca o email; fallback quando falta
    await test('dono: o próprio vê «Você (nome)»; o colaborador vê o nome real', async () => {
        const c = shareCtx();
        const user = { id: 1, username: 'joão', email: 'a@b.c' };
        assert.strictEqual(c.ownerLabel({ user_id: 1, owner: { id: 1, username: 'joão' } }, user), 'Você (joão)');
        assert.strictEqual(c.ownerLabel({ user_id: 7, owner: { id: 7, username: 'Marta' } }, user), 'Marta');
        assert.strictEqual(c.ownerLabel({ user_id: 1 }, { id: 1 }), 'Você');
    });

    await test('dono: sem nome ou com dados incompletos usa «Proprietário desconhecido» (nunca o email)', async () => {
        const c = shareCtx();
        const user = { id: 1, username: 'joão' };
        for (const t of [{ user_id: 7 }, { user_id: 7, owner: null }, { user_id: 7, owner: {} },
                         { user_id: 7, owner: { id: 7, username: '   ' } },
                         { user_id: 7, owner: { id: 7, username: null, email: 'x@y.z' } },
                         { user_id: 7, owner: { id: 7, email: 'x@y.z' } }]) {
            const label = c.ownerLabel(t, user);
            assert.strictEqual(label, 'Proprietário desconhecido');
            assert(!label.includes('@'));
        }
        assert.strictEqual(c.ownerName(undefined), '');
    });

    console.log(`\n${passed} testes de api/frontend passaram`);
})().catch(e => { console.error('FALHOU:', e.message); process.exit(1); });
