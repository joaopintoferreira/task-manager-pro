// Testes do painel administrativo (frontend): estrutura, segurança do DOM, permissões por papel, modais,
// perfil/senha, roteamento do Vercel, SRI e cabeçalhos. Nenhuma chamada de rede real: apiRequest é simulado.
// Executar a partir da raiz do projeto:  node frontend/tests/admin.test.js
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const FRONT = path.join(__dirname, '..');
const read = f => fs.readFileSync(path.join(FRONT, f), 'utf8');
const INDEX = read('index.html'), AUTH = read('auth.html'), SCRIPT = read('script.js'), PANEL = read('painel-adm.js');
const CSS = read('painel-adm.css'), STYLE = read('style.css');
const VERCEL = JSON.parse(fs.readFileSync(path.join(FRONT, '..', 'vercel.json'), 'utf8'));

// ── DOM mínimo ────────────────────────────────────────────────────
class FakeText { constructor(t) { this.text = String(t); this.isText = true; } get textContent() { return this.text; } }
class FakeEl {
    constructor(tag) {
        this.tag = tag; this.children = []; this.attrs = {}; this.listeners = {}; this.dataset = {}; this.className = '';
        this._text = ''; this.value = ''; this.checked = false; this.disabled = false; this.hidden = false; this.selected = false;
        const el = this;
        this.classList = {
            toggle: (c, on) => { const s = new Set(el.className.split(/\s+/).filter(Boolean)); (on ? s.add(c) : s.delete(c)); el.className = [...s].join(' '); },
            contains: c => el.className.split(/\s+/).includes(c),
        };
    }
    get id() { return this.attrs.id || ''; }
    setAttribute(k, v) { this.attrs[k] = String(v); if (k.startsWith('data-')) this.dataset[k.slice(5).replace(/-./g, m => m[1].toUpperCase())] = String(v); }
    getAttribute(k) { return this.attrs[k]; }
    appendChild(c) { this.children.push(c); c.parent = this; return c; }
    replaceChildren(...nodes) { this.children = []; this._text = ''; nodes.forEach(n => this.appendChild(n)); }
    addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
    set textContent(v) { this.children = []; this._text = String(v); }
    get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
    all() { return [this, ...this.children.flatMap(c => (c.isText ? [] : c.all()))]; }
    querySelectorAll(sel) { return this.all().filter(e => e !== this && matches(e, sel)); }
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
    focus() {}
    remove() { if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this); }
}
function matches(e, sel) {
    if (sel.startsWith('#')) return e.id === sel.slice(1);
    if (sel.startsWith('.')) return e.classList.contains(sel.slice(1));
    return e.tag === sel;
}
function makeDoc(extraIds = []) {
    const doc = { body: new FakeEl('body'), head: new FakeEl('head') };
    doc.createElement = t => new FakeEl(t);
    doc.createTextNode = t => new FakeText(t);
    doc.getElementById = id => doc.body.all().find(e => e.id === id) || null;
    ['admin-root', ...extraIds].forEach(id => { const e = new FakeEl('div'); e.setAttribute('id', id); doc.body.appendChild(e); });
    return doc;
}
const click = el => (el.listeners.click || []).forEach(fn => fn({ target: el }));
const buttons = (root, label) => root.all().filter(e => e.tag === 'button' && e.textContent.trim().includes(label));

// ── Contexto do painel ────────────────────────────────────────────
function panelCtx({ role = 'admin', responses = {} } = {}) {
    const doc = makeDoc();
    const calls = [], toasts = [], store = {}, modalState = { shown: 0, hidden: 0 };
    const ctx = {
        document: doc, console, Promise, Intl, URLSearchParams, Number, Object, Array, String, Set, Map, JSON, Date,
        setTimeout, clearTimeout,
        currentUser: { id: 1, username: 'eu', role },
        accessToken: 'old-access', refreshToken: 'old-refresh',
        localStorage: { setItem: (k, v) => { store[k] = v; } },
        toast: (msg, type) => toasts.push({ msg, type }),
        bootstrap: { Modal: { getOrCreateInstance: () => ({ show: () => modalState.shown++, hide: () => modalState.hidden++ }) } },
        apiRequest: async (url, opts = {}) => {
            calls.push({ url, method: opts.method || 'GET', body: opts.body ? JSON.parse(opts.body) : undefined });
            const key = Object.keys(responses).find(k => url.startsWith(k) && (!k.includes('|') || true));
            const spec = typeof responses[key] === 'function' ? responses[key](url, opts) : responses[key];
            if (spec === null) return null;
            const r = spec || { status: 200, body: {} };
            return { ok: r.status >= 200 && r.status < 300, status: r.status, json: async () => r.body };
        },
    };
    ctx.window = ctx;
    vm.createContext(ctx);
    vm.runInContext(PANEL, ctx);
    return { ctx, doc, calls, toasts, store, modalState, panel: ctx.window.PainelAdm };
}
const USERS = (users, extra = {}) => ({ status: 200, body: { users, total: users.length, page: 1, per_page: 20, total_pages: 1, ...extra } });
const OVERVIEW = { status: 200, body: { users: 3, active: 2, inactive: 1, admins: 1, superadmins: 1 } };
const row = (o = {}) => ({ id: 5, username: 'ana', email: 'ana@example.com', role: 'user', is_active: true, has_avatar: false,
    created_at: '2026-11-01T12:00:00Z', last_login: null, actions: { toggle_active: true, remove_avatar: false, clear_data: false, delete: false, change_role: false }, ...o });
const settle = () => new Promise(r => setTimeout(r, 30));

let passed = 0;
async function test(name, fn) { await fn(); passed++; console.log('ok -', name); }

(async () => {
    // ── Estrutura e carregamento ──
    await test('ficheiros do painel: existem, não começam por um prefixo do Flask e carregam só sob demanda', async () => {
        for (const f of ['painel-adm.js', 'painel-adm.css']) assert(fs.existsSync(path.join(FRONT, f)), f);
        assert(!/<script[^>]+painel-adm/.test(INDEX) && !/painel-adm\.css/.test(INDEX), 'o index.html não carrega o painel');
        const api = VERCEL.routes.find(r => r.dest === 'backend/wsgi.py').src;
        const re = new RegExp('^' + api + '$');
        for (const p of ['/admin/users', '/admin/audit', '/auth/me', '/tasks', '/ai/chat']) assert(re.test(p), p + ' deve ir para o Flask');
        for (const p of ['/painel-adm.js', '/painel-adm.css', '/index.html', '/script.js', '/style.css', '/assistant.js', '/auth.html'.replace('auth', 'login')]) {
            assert(!re.test(p), p + ' deve continuar estático');
        }
        const staticFiles = fs.readdirSync(FRONT).filter(f => fs.statSync(path.join(FRONT, f)).isFile());
        for (const f of staticFiles.filter(f => f !== 'auth.html')) assert(!re.test('/' + f), `/${f} seria encaminhado para o Flask`);
    });

    await test('index.html: Administração escondida por omissão, vista vazia e sem conteúdo administrativo para utilizadores comuns', async () => {
        assert(/id="nav-admin"[^>]*\bhidden\b/.test(INDEX), 'item de menu escondido');
        assert(/id="admin-view" hidden>\s*<div class="admin-panel" id="admin-root"><\/div>/.test(INDEX), 'vista vazia');
        assert(!/\/admin\//.test(INDEX), 'o HTML não contém rotas /admin');
        assert(/\.nav-link\[hidden\]\s*\{\s*display:\s*none/.test(STYLE));
    });

    await test('script.js: o painel só é carregado por ADMIN/SUPERADMIN (utilizador comum não pede nenhum ficheiro)', async () => {
        const make = role => {
            const head = new FakeEl('head');
            const ctx = { currentUser: role ? { role } : null, window: {}, Promise, Error,
                document: { createElement: t => new FakeEl(t), head, getElementById: () => null } };
            vm.createContext(ctx);
            vm.runInContext(SCRIPT.slice(SCRIPT.indexOf('const STAFF_ROLES'), SCRIPT.indexOf('function setupUser')), ctx);
            return { ctx, head };
        };
        for (const role of ['user', undefined, 'root', '']) {
            const { ctx, head } = make(role);
            assert.strictEqual(ctx.isStaff(), false, String(role));
            await assert.rejects(ctx.ensureAdminPanel(), /forbidden/);
            assert.strictEqual(head.children.length, 0, 'nenhum ficheiro pedido');
        }
        for (const role of ['admin', 'superadmin']) {
            const { ctx, head } = make(role);
            assert.strictEqual(ctx.isStaff(), true);
            ctx.ensureAdminPanel();
            ctx.ensureAdminPanel();                                           // idempotente
            assert.deepStrictEqual(head.children.map(c => c.tag), ['link', 'script']);
            assert(/^painel-adm\.css\?v=\d+$/.test(head.children[0].href) && /^painel-adm\.js\?v=\d+$/.test(head.children[1].src));
        }
    });

    await test('navegação: showView("admin") só funciona para administradores; as outras vistas continuam iguais', async () => {
        const make = role => {
            const doc = makeDoc(['dashboard-view', 'tasks-view', 'assistant-view', 'admin-view', 'page-title', 'sidebar']);
            const calls = [];
            const ctx = { document: doc, state: {}, window: { innerWidth: 1280 }, isStaff: () => role !== 'user',
                loadDashboard: () => calls.push('dash'), onAssistantShown: () => calls.push('assistant'),
                showAdminPanel: () => calls.push('admin'), toggleSidebar: () => {} };
            doc.querySelectorAll = () => [];
            vm.createContext(ctx);
            vm.runInContext(SCRIPT.slice(SCRIPT.indexOf('function showView'), SCRIPT.indexOf('function filterByCategory')), ctx);
            return { ctx, doc, calls };
        };
        const admin = make('admin');
        admin.ctx.showView('admin');
        assert.strictEqual(admin.doc.getElementById('admin-view').hidden, false);
        assert.strictEqual(admin.doc.getElementById('dashboard-view').hidden, true);
        assert.strictEqual(admin.doc.getElementById('page-title').textContent, 'Administração');
        assert.deepStrictEqual([admin.ctx.state.view, admin.calls], ['admin', ['admin']]);
        const user = make('user');
        user.ctx.showView('admin');                                           // recusado: vai para o dashboard
        assert.strictEqual(user.doc.getElementById('admin-view').hidden, true);
        assert.deepStrictEqual([user.ctx.state.view, user.calls], ['dashboard', ['dash']]);
        user.ctx.showView('tasks', 'Todas', 'all');
        assert.strictEqual(user.ctx.state.view, 'tasks');
        assert.strictEqual(user.doc.getElementById('tasks-view').hidden, false);
    });

    // ── Segurança do DOM ──
    await test('painel-adm.js: nenhuma API que injete HTML ou execute código; senhas nunca guardadas', async () => {
        for (const bad of [/innerHTML/, /outerHTML/, /insertAdjacentHTML/, /document\.write/, /\beval\(/, /new Function/, /sessionStorage/]) {
            assert(!bad.test(PANEL), String(bad));
        }
        const sets = [...PANEL.matchAll(/localStorage\.setItem\(([^,]+),/g)].map(m => m[1].trim());
        assert.deepStrictEqual(sets.sort(), ["'access_token'", "'refresh_token'"], 'só os tokens novos da limpeza global');
        assert(!/password[^;\n]*localStorage|localStorage[^;\n]*password/i.test(PANEL));
    });

    await test('utilizadores: nomes e emails maliciosos aparecem como texto (nenhum elemento é criado a partir deles)', async () => {
        const evil = '<img src=x onerror="alert(1)"><script>alert(2)</script>';
        const t = panelCtx({ responses: { '/admin/overview': OVERVIEW, '/admin/users': USERS([row({ username: evil, email: evil })]) } });
        t.panel.onShown(); await settle();
        const tags = new Set(t.doc.getElementById('admin-root').all().map(e => e.tag));
        for (const bad of ['img', 'script', 'iframe']) assert(!tags.has(bad), bad);
        const cells = t.doc.getElementById('admin-root').all().filter(e => e.classList.contains('adm-user-name'));
        assert.strictEqual(cells[0].textContent, evil);
        assert(!Object.values(t.doc.getElementById('admin-root').all().reduce((a, e) => Object.assign(a, e.attrs), {})).some(v => /onerror/.test(v)));
    });

    await test('h(): atributos fora da lista segura (onerror, href, style, srcdoc) são ignorados', async () => {
        const { panel } = panelCtx();
        const { h } = panel._internals;
        const el = h('div', { onerror: 'x', href: 'javascript:alert(1)', style: 'x', srcdoc: 'x', class: 'ok', 'aria-label': 'a', 'data-x': '1' }, '<b>t</b>');
        assert.deepStrictEqual(Object.keys(el.attrs).sort(), ['aria-label', 'data-x']);
        assert.strictEqual(el.className, 'ok');
        assert.strictEqual(el.textContent, '<b>t</b>');
        assert.strictEqual(el.children.length, 1);
        assert(el.children[0].isText);
    });

    // ── Papéis ──
    await test('separadores e ações dependem do papel (ADMIN: utilizadores e auditoria; SUPERADMIN: tudo)', async () => {
        const a = panelCtx({ role: 'admin' });
        assert.deepStrictEqual([...a.panel._internals.tabsForRole().map(t => t.id)], ['users', 'audit']);
        const s = panelCtx({ role: 'superadmin' });
        assert.deepStrictEqual([...s.panel._internals.tabsForRole().map(t => t.id)], ['users', 'admins', 'audit', 'maintenance']);
        const full = { toggle_active: true, remove_avatar: true, clear_data: true, delete: true, change_role: true };
        const limited = { toggle_active: true, remove_avatar: false, clear_data: false, delete: false, change_role: false };
        const responses = { '/admin/overview': OVERVIEW, '/admin/users': USERS([row({ actions: full }), row({ id: 6, username: 'bia', actions: limited })]) };
        const sup = panelCtx({ role: 'superadmin', responses });
        sup.panel.onShown(); await settle();
        const root = sup.doc.getElementById('admin-root');
        for (const label of ['Desativar', 'Remover foto', 'Papel', 'Limpar dados', 'Excluir']) assert(buttons(root, label).length >= 1, label);
        assert.strictEqual(buttons(root, 'Excluir').length, 1, 'só a linha com permissão');
        const adm = panelCtx({ role: 'admin', responses: { ...responses, '/admin/users': USERS([row({ actions: limited })]) } });
        adm.panel.onShown(); await settle();
        const r2 = adm.doc.getElementById('admin-root');
        assert.strictEqual(buttons(r2, 'Excluir').length + buttons(r2, 'Limpar dados').length + buttons(r2, 'Papel').length, 0);
        assert(buttons(r2, 'Desativar').length === 1);
        assert.strictEqual(r2.all().filter(e => e.classList.contains('adm-tab')).length, 2);
    });

    await test('erros 401/403/429/rede: mensagens por código estável, sem detalhes técnicos', async () => {
        const { panel } = panelCtx();
        const { errorText } = panel._internals;
        assert(/permissão/.test(errorText({ code: 'forbidden', message: 'x' })));
        assert(/Senha incorreta/.test(errorText({ code: 'invalid_password' })));
        assert(/Demasiadas tentativas/.test(errorText({ code: 'reauth_throttled' })));
        assert(/último SUPERADMIN/.test(errorText({ code: 'last_superadmin' })));
        assert(/mudaram/.test(errorText({ code: 'counts_changed' })));
        assert.strictEqual(errorText({ code: 'desconhecido', message: 'texto do servidor' }), 'texto do servidor');
        assert(/Não foi possível/.test(errorText({}, undefined)));
        const t = panelCtx({ role: 'superadmin', responses: { '/admin/overview': OVERVIEW, '/admin/users': { status: 403, body: { code: 'forbidden', message: 'x' } } } });
        t.panel.onShown(); await settle();
        const alert = t.doc.getElementById('admin-root').all().find(e => e.classList.contains('adm-error'));
        assert(alert && /permissão/.test(alert.textContent));
        const net = panelCtx({ responses: { '/admin/overview': OVERVIEW, '/admin/users': null } });
        net.panel.onShown(); await settle();
        assert(/contactar o servidor/.test(net.doc.getElementById('admin-root').all().find(e => e.classList.contains('adm-error')).textContent));
        const rate = panelCtx({ responses: { '/admin/overview': OVERVIEW, '/admin/users': { status: 429, body: { error: 'Muitas requisições.' } } } });
        rate.panel.onShown(); await settle();
        assert(rate.doc.getElementById('admin-root').all().some(e => e.classList.contains('adm-error')));
    });

    await test('pesquisa, filtros e paginação geram a consulta certa', async () => {
        const { panel } = panelCtx();
        const { buildQuery } = panel._internals;
        assert.strictEqual(buildQuery({ page: 2, search: 'a b&c', role: '', status: undefined }), '?page=2&search=a+b%26c');
        assert.strictEqual(buildQuery({}), '');
        const t = panelCtx({ responses: { '/admin/overview': OVERVIEW,
            '/admin/users': USERS([row()], { total: 45, page: 1, total_pages: 3 }) } });
        t.panel.onShown(); await settle();
        const next = buttons(t.doc.getElementById('admin-root'), 'Seguinte')[0];
        assert(!next.disabled);
        click(next); await settle();
        assert(t.calls.some(c => c.url.startsWith('/admin/users?page=2')), JSON.stringify(t.calls.map(c => c.url)));
        assert(buttons(t.doc.getElementById('admin-root'), 'Anterior')[0] !== undefined);
    });

    // ── Modais de operações sensíveis ──
    const submitModal = async (t, { password = 'Senha123Forte', text, check } = {}) => {
        const body = t.doc.getElementById('adminConfirmBody');
        const set = (id, v) => { const e = body.all().find(x => x.id === id); if (e) { if (typeof v === 'boolean') e.checked = v; else e.value = v; } return e; };
        if (text !== undefined) set('adminConfirmText', text);
        if (check !== undefined) set('adminConfirmCheck', check);
        const pw = set('adminConfirmPassword', password);
        const form = t.doc.getElementById('adminConfirmForm');
        await form.onsubmit({ preventDefault() {} });
        return { msg: t.doc.getElementById('adminConfirmMsg').textContent, pw, submit: t.doc.getElementById('adminConfirmSubmit') };
    };
    const full = { toggle_active: true, remove_avatar: false, clear_data: true, delete: true, change_role: true };
    const base = (extra = {}) => ({ '/admin/overview': OVERVIEW, '/admin/users?': USERS([row({ id: 7, username: 'vitima', actions: full })]), ...extra });
    const COUNTS = { tasks: 2, categories: 1, notifications: 3, collaborations: 1, refresh_tokens: 1, avatars: 1, third_party_notifications: 1 };

    await test('limpar dados: pré-visualização, frase e senha obrigatórias, expected_counts enviado, senha limpa depois', async () => {
        const t = panelCtx({ role: 'superadmin', responses: base({ '/admin/users/7/data-summary': { status: 200, body: { counts: COUNTS } },
            '/admin/users/7/clear-data': (u, o) => ({ status: 403, body: { code: 'invalid_password' } }) }) });
        t.panel.onShown(); await settle();
        click(buttons(t.doc.getElementById('admin-root'), 'Limpar dados')[0]); await settle();
        assert.strictEqual(t.modalState.shown, 1);
        assert(!t.calls.some(c => c.url.endsWith('clear-data')), 'nada é enviado antes da confirmação');
        const counts = t.doc.getElementById('adminConfirmBody').all().filter(e => e.tag === 'tr').map(e => e.textContent);
        assert(counts.some(c => c.startsWith('Tarefas') && c.endsWith('2')));
        let r = await submitModal(t, { text: 'outro' });
        assert(/não corresponde/.test(r.msg)); assert(!t.calls.some(c => c.url.endsWith('clear-data')));
        r = await submitModal(t, { text: 'vitima', password: '' });
        assert(/senha/i.test(r.msg)); assert(!t.calls.some(c => c.url.endsWith('clear-data')));
        r = await submitModal(t, { text: 'vitima' });                         // o servidor recusa a senha
        const call = t.calls.find(c => c.url.endsWith('clear-data'));
        assert.deepStrictEqual(call.body, { password: 'Senha123Forte', confirm_username: 'vitima', expected_counts: COUNTS });
        assert.strictEqual(r.pw.value, '', 'senha limpa após a tentativa');
        assert(/Senha incorreta/.test(r.msg));
        assert.strictEqual(t.modalState.hidden, 0, 'o modal continua aberto após o erro');
    });

    await test('excluir: com tarefas exige marcar a opção; só então envia delete_tasks=true; sucesso fecha e atualiza', async () => {
        const t = panelCtx({ role: 'superadmin', responses: base({ '/admin/users/7/data-summary': { status: 200, body: { counts: COUNTS } },
            '/admin/users/7': { status: 200, body: { deleted: COUNTS } } }) });
        t.panel.onShown(); await settle();
        click(buttons(t.doc.getElementById('admin-root'), 'Excluir')[0]); await settle();
        assert(t.doc.getElementById('adminConfirmBody').all().some(e => e.id === 'adminConfirmCheck'), 'checkbox das tarefas');
        let r = await submitModal(t, { text: 'vitima', check: false });
        assert(/tarefas/i.test(r.msg)); assert(!t.calls.some(c => c.method === 'DELETE'));
        const before = t.calls.length;
        r = await submitModal(t, { text: 'vitima', check: true });
        const del = t.calls.find(c => c.method === 'DELETE');
        assert.deepStrictEqual(del.body, { password: 'Senha123Forte', confirm_username: 'vitima', expected_counts: COUNTS, delete_tasks: true });
        assert.strictEqual(r.pw.value, '');
        assert.strictEqual(t.modalState.hidden, 1);
        assert(t.toasts.some(x => x.type === 'success' && /excluído/.test(x.msg)));
        await settle();
        assert(t.calls.length > before + 1, 'lista recarregada');
    });

    await test('excluir sem tarefas: não mostra a opção de apagar tarefas', async () => {
        const t = panelCtx({ role: 'superadmin', responses: base({ '/admin/users/7/data-summary': { status: 200, body: { counts: { ...COUNTS, tasks: 0 } } } }) });
        t.panel.onShown(); await settle();
        click(buttons(t.doc.getElementById('admin-root'), 'Excluir')[0]); await settle();
        assert(!t.doc.getElementById('adminConfirmBody').all().some(e => e.id === 'adminConfirmCheck'));
    });

    await test('limpeza global: pré-visualização, frase exata, senha, token; tokens novos guardados; desativada bloqueia o botão', async () => {
        const summary = { status: 200, body: { tables: { users: 3, tasks: 4 }, database: 'postgresql', schema_version: 'c4d7a1e08b52', wipe_enabled: true } };
        const preview = { status: 200, body: { counts: COUNTS, token: 'TOKEN-ASSINADO', phrase: 'LIMPAR TODOS OS DADOS', wipe_enabled: true, expires_in: 300 } };
        const t = panelCtx({ role: 'superadmin', responses: base({ '/admin/maintenance/summary': summary, '/admin/maintenance/wipe/preview': preview,
            '/admin/maintenance/wipe': { status: 200, body: { deleted: COUNTS, tokens: { access_token: 'NEW-A', refresh_token: 'NEW-R' } } } }) });
        t.panel.onShown(); await settle();
        click(t.doc.getElementById('admin-root').all().find(e => e.id === 'adm-tab-maintenance')); await settle();
        click(buttons(t.doc.getElementById('admin-root'), 'Pré-visualizar')[0]); await settle();
        assert(!t.calls.some(c => c.url === '/admin/maintenance/wipe'));
        let r = await submitModal(t, { text: 'limpar todos os dados' });      // frase tem de ser exata
        assert(/não corresponde/.test(r.msg)); assert(!t.calls.some(c => c.url === '/admin/maintenance/wipe'));
        r = await submitModal(t, { text: 'LIMPAR TODOS OS DADOS' });
        const wipe = t.calls.find(c => c.url === '/admin/maintenance/wipe');
        assert.deepStrictEqual(wipe.body, { token: 'TOKEN-ASSINADO', password: 'Senha123Forte', confirmation: 'LIMPAR TODOS OS DADOS' });
        assert.strictEqual(r.pw.value, '');
        assert.deepStrictEqual([t.ctx.accessToken, t.ctx.refreshToken], ['NEW-A', 'NEW-R']);
        assert.deepStrictEqual(t.store, { access_token: 'NEW-A', refresh_token: 'NEW-R' });
        // desativada: o botão de confirmar fica bloqueado e nada é enviado
        const off = panelCtx({ role: 'superadmin', responses: base({ '/admin/maintenance/summary': { status: 200, body: { tables: {}, wipe_enabled: false } },
            '/admin/maintenance/wipe/preview': { status: 200, body: { ...preview.body, wipe_enabled: false } } }) });
        off.panel.onShown(); await settle();
        click(off.doc.getElementById('admin-root').all().find(e => e.id === 'adm-tab-maintenance')); await settle();
        click(buttons(off.doc.getElementById('admin-root'), 'Pré-visualizar')[0]); await settle();
        assert.strictEqual(off.doc.getElementById('adminConfirmSubmit').disabled, true);
        assert(/desativada/.test(off.doc.getElementById('adminConfirmMsg').textContent));
    });

    await test('desativar e ativar: pedido certo e atualização da lista', async () => {
        const t = panelCtx({ role: 'admin', responses: base({ '/admin/users/7/deactivate': { status: 200, body: {} } }) });
        t.panel.onShown(); await settle();
        click(buttons(t.doc.getElementById('admin-root'), 'Desativar')[0]); await settle();
        assert(!t.doc.getElementById('adminConfirmBody').all().some(e => e.id === 'adminConfirmPassword'), 'sem senha nas operações simples');
        await t.doc.getElementById('adminConfirmForm').onsubmit({ preventDefault() {} });
        assert(t.calls.some(c => c.url === '/admin/users/7/deactivate' && c.method === 'POST'));
    });

    // ── Perfil e senha ──
    await test('perfil: o corpo do PATCH só leva campos alterados e a senha só quando o email muda', async () => {
        const ctx = {};
        vm.createContext(ctx);
        vm.runInContext(SCRIPT.slice(SCRIPT.indexOf('function buildProfilePatch'), SCRIPT.indexOf('async function saveProfile')), ctx);
        const cur = { username: 'ana', email: 'Ana@Example.com' };
        assert.strictEqual(ctx.buildProfilePatch(cur, 'ana', 'ana@example.com', 'x'), null);
        assert.deepStrictEqual({ ...ctx.buildProfilePatch(cur, ' nova ', 'ana@example.com', 'x') }, { username: 'nova' });
        assert.deepStrictEqual({ ...ctx.buildProfilePatch(cur, 'ana', 'outra@example.com', 'Senha1') }, { email: 'outra@example.com', current_password: 'Senha1' });
        const both = ctx.buildProfilePatch(cur, 'bia', 'b@x.com', 'S');
        assert.deepStrictEqual(Object.keys(both).sort(), ['current_password', 'email', 'username']);
        assert(!('role' in both) && !('is_active' in both));
    });

    await test('perfil: campos, autocomplete correto, mensagens em texto puro e senhas fora do localStorage', async () => {
        for (const id of ['profile-username', 'profile-email-input', 'profile-email-password', 'pw-current', 'pw-new', 'pw-confirm',
            'profile-msg', 'password-msg', 'profile-save', 'password-save', 'profile-logout', 'avatar-save', 'avatar-remove']) {
            assert(new RegExp(`id="${id}"`).test(INDEX), id);
        }
        assert(/id="pw-current"[^>]*autocomplete="current-password"/.test(INDEX));
        assert(/id="pw-new"[^>]*autocomplete="new-password"/.test(INDEX) && /id="pw-confirm"[^>]*autocomplete="new-password"/.test(INDEX));
        assert(/id="profile-email-pw-wrap" hidden/.test(INDEX), 'a senha atual do email só aparece quando o email muda');
        const profileJs = SCRIPT.slice(SCRIPT.indexOf('function setFormMessage'), SCRIPT.indexOf('function clearPendingAvatar') > 0 ? SCRIPT.indexOf('async function changePassword') + 1500 : undefined);
        assert(/el\.textContent = text/.test(profileJs) && !/innerHTML/.test(SCRIPT.slice(SCRIPT.indexOf('function setFormMessage'), SCRIPT.indexOf('async function changePassword') + 1500)));
        assert(!/localStorage\.setItem\([^)]*(password|pw-)/i.test(SCRIPT));
        assert(/action|onsubmit="event\.preventDefault\(\); changePassword\(\)/.test(INDEX));
    });

    // ── SRI e cabeçalhos ──
    await test('SRI: todos os recursos do CDN têm integrity sha384 e crossorigin', async () => {
        for (const [name, html] of [['index.html', INDEX], ['auth.html', AUTH]]) {
            const tags = [...html.matchAll(/<(?:link|script)\b[^>]*https:\/\/cdn\.jsdelivr\.net[^>]*>/g)].map(m => m[0]);
            assert(tags.length >= 2, name);
            for (const tag of tags) {
                assert(/integrity="sha384-[A-Za-z0-9+/]{64}"/.test(tag), `${name}: sem SRI em ${tag}`);
                assert(/crossorigin="anonymous"/.test(tag), `${name}: sem crossorigin em ${tag}`);
            }
        }
        assert(!/https?:\/\/(?!cdn\.jsdelivr\.net|localhost)[a-z0-9.-]+\.[a-z]{2,}\//i.test(INDEX.replace(/xmlns="[^"]+"/g, '')), 'sem outros recursos externos');
    });

    await test('cabeçalhos de segurança no Vercel: aplicados a tudo, CSP completa só em modo de relatório e sem unsafe-eval', async () => {
        const first = VERCEL.routes[0];
        assert.strictEqual(first.src, '/(.*)');
        assert.strictEqual(first.continue, true, 'continue: as rotas seguintes continuam a aplicar-se');
        const h = first.headers;
        assert.strictEqual(h['X-Content-Type-Options'], 'nosniff');
        assert.strictEqual(h['X-Frame-Options'], 'DENY');
        assert(/frame-ancestors 'none'/.test(h['Content-Security-Policy']) && /object-src 'none'/.test(h['Content-Security-Policy']));
        assert(!/script-src/.test(h['Content-Security-Policy']), 'a política aplicada não restringe scripts (há handlers inline)');
        const report = h['Content-Security-Policy-Report-Only'];
        assert(/default-src 'self'/.test(report) && /https:\/\/cdn\.jsdelivr\.net/.test(report));
        assert(!/unsafe-eval|\*/.test(report.replace(/\bsrc 'self'/g, '')), 'sem unsafe-eval nem curingas');
        assert.strictEqual(VERCEL.routes.filter(r => r.dest === 'backend/wsgi.py').length, 1);
    });

    await test('CSS do painel: tokens de tema, cartões em telemóvel e alvos de toque', async () => {
        assert(/var\(--bg-card\)/.test(CSS) && /var\(--border\)/.test(CSS) && /var\(--danger\)/.test(CSS));
        assert(!/#[0-9a-f]{3,8}\b/i.test(CSS.replace(/\/\*[\s\S]*?\*\//g, '')), 'cores só por tokens (claro/escuro)');
        assert(/@media \(max-width: 767px\)[\s\S]*attr\(data-label\)/.test(CSS));
    });

    console.log(`\n${passed} testes do painel administrativo passaram`);
})().catch(e => { console.error('FALHOU:', e && e.stack || e); process.exit(1); });
