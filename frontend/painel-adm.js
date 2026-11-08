'use strict';

// ══════════════════════════════════════════════════════════════════
// PAINEL ADMINISTRATIVO (ADMIN e SUPERADMIN)
//
// Carregado por script.js só para quem é ADMIN/SUPERADMIN. Isto é interface: quem decide é o backend, que lê o
// papel da base de dados em cada pedido e autoriza cada rota /admin. Esconder um botão NÃO é segurança.
//
// Regras deste ficheiro:
//   • todo o texto dinâmico entra no DOM com textContent (nunca como HTML, nem construído com dados da API);
//   • classes CSS vêm sempre de tabelas fixas, nunca de dados da API;
//   • senhas e confirmações vivem só em campos do modal: são limpas após cada tentativa e nunca guardadas.
//
// Usa de script.js: apiRequest, toast, currentUser, accessToken/refreshToken, bootstrap (Modal).
// ══════════════════════════════════════════════════════════════════
(function () {
    const ROLE_LABEL = { user: 'Utilizador', admin: 'Administrador', superadmin: 'Superadministrador' };
    const ROLE_BADGE = { user: 'is-neutral', admin: 'is-primary', superadmin: 'is-danger' };
    const RESULT_LABEL = { success: 'Sucesso', denied: 'Negado', failed: 'Falhou' };
    const RESULT_BADGE = { success: 'is-success', denied: 'is-warning', failed: 'is-danger' };
    const ACTION_LABEL = {
        'user.activate': 'Ativar conta', 'user.deactivate': 'Desativar conta', 'user.avatar_removed': 'Remover foto',
        'user.data_cleared': 'Limpar dados do utilizador', 'user.deleted': 'Excluir utilizador',
        'role.changed': 'Alterar papel', 'role.bootstrap_superadmin': 'Primeiro SUPERADMIN',
        'maintenance.wipe': 'Limpeza global', 'maintenance.wipe_preview': 'Pré-visualização da limpeza global',
    };
    const COUNT_LABEL = {
        tasks: 'Tarefas', categories: 'Categorias', notifications: 'Notificações', collaborations: 'Colaborações',
        refresh_tokens: 'Sessões (refresh tokens)', avatars: 'Fotos de perfil',
        third_party_notifications: 'Notificações de outros utilizadores sobre estas tarefas (ficam, sem tarefa)',
    };
    // Mensagens por código estável do backend (o texto do servidor é só um recurso para códigos desconhecidos)
    const ERROR_TEXT = {
        forbidden: 'Não tem permissão para esta operação.',
        self_action: 'Esta operação não pode ser aplicada à sua própria conta.',
        invalid_password: 'Senha incorreta.',
        reauth_throttled: 'Demasiadas tentativas de confirmação. Aguarde alguns minutos.',
        confirmation_mismatch: 'A confirmação escrita não corresponde.',
        counts_changed: 'Os dados mudaram desde a pré-visualização. Nada foi apagado: feche e reveja as contagens.',
        has_tasks: 'O utilizador tem tarefas. Marque a opção de as apagar para excluir a conta.',
        last_superadmin: 'Não é possível alterar o último SUPERADMIN ativo.',
        no_change: 'Nada para alterar: já está neste estado.',
        invalid_transition: 'Transição de papel não permitida (passe por Administrador).',
        inactive_target: 'Ative a conta antes de a promover.',
        inactive_actor: 'A sua conta foi desativada.',
        wipe_disabled: 'A limpeza global está desativada neste ambiente.',
        token_expired: 'A pré-visualização expirou. Gere uma nova.',
        token_invalid: 'Pré-visualização inválida. Gere uma nova.',
        token_used: 'Esta pré-visualização já foi utilizada. Gere uma nova.',
        no_active_superadmin: 'Não existe nenhum SUPERADMIN ativo. Nada foi apagado.',
        not_found: 'Utilizador não encontrado.',
        no_avatar: 'O utilizador não tem foto de perfil.',
        internal_error: 'Não foi possível concluir a operação. Nada foi alterado.',
    };

    const state = {
        built: false,
        tab: 'users',
        users: { page: 1, search: '', role: '', status: '', timer: null },
        audit: { page: 1, action: '', result: '', actor: '', from: '', to: '' },
        lookup: null,
    };
    let root = null;
    let modal = null;

    // ── Utilitários ───────────────────────────────────────────────
    const isSuper = () => !!currentUser && currentUser.role === 'superadmin';
    const asText = v => (v === null || v === undefined ? '' : String(v));
    const asInt = v => (Number.isInteger(v) ? v : 0);
    const roleLabel = r => ROLE_LABEL[r] || 'Desconhecido';

    const SAFE_ATTRS = new Set(['id', 'type', 'value', 'title', 'role', 'for', 'placeholder', 'autocomplete', 'maxlength',
        'min', 'max', 'name', 'scope', 'colspan', 'rel']);
    // Criador de elementos: o conteúdo é sempre texto (textContent); atributos só da lista segura ou aria-/data-
    function h(tag, props, ...children) {
        const el = document.createElement(tag);
        Object.entries(props || {}).forEach(([key, value]) => {
            if (value === null || value === undefined || value === false) return;
            if (key === 'class') el.className = asText(value);
            else if (key === 'text') el.textContent = asText(value);
            else if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2), value);
            else if (key === 'disabled' || key === 'hidden' || key === 'checked' || key === 'required') { if (value) el[key] = true; }
            else if (SAFE_ATTRS.has(key) || key.startsWith('aria-') || key.startsWith('data-')) el.setAttribute(key, asText(value));
        });
        children.flat().forEach(child => {
            if (child === null || child === undefined || child === false) return;
            el.appendChild(typeof child === 'object' ? child : document.createTextNode(asText(child)));
        });
        return el;
    }

    function formatDate(iso) {
        if (!iso) return '—';
        const d = new Date(iso);
        if (Number.isNaN(d.getTime())) return '—';
        return new Intl.DateTimeFormat('pt-BR', { timeZone: 'America/Sao_Paulo', dateStyle: 'short', timeStyle: 'short' }).format(d);
    }

    function errorText(data, fallback) {
        if (data && ERROR_TEXT[data.code]) return ERROR_TEXT[data.code];
        return (data && typeof data.message === 'string' && data.message) || fallback || 'Não foi possível concluir a operação.';
    }

    async function api(path, options) {
        const res = await apiRequest(path, options);
        if (!res) return { ok: false, status: 0, data: {} };
        const data = await res.json().catch(() => ({}));
        return { ok: res.ok, status: res.status, data };
    }

    const jsonBody = obj => ({ body: JSON.stringify(obj) });

    function buildQuery(params) {
        const q = new URLSearchParams();
        Object.entries(params).forEach(([k, v]) => { if (v !== '' && v !== null && v !== undefined) q.set(k, v); });
        const s = q.toString();
        return s ? `?${s}` : '';
    }

    const badge = (text, cls) => h('span', { class: `adm-badge ${cls || 'is-neutral'}`, text });
    const roleBadge = role => badge(roleLabel(role), ROLE_BADGE[role] || 'is-neutral');
    const statusBadge = active => badge(active ? 'Ativa' : 'Inativa', active ? 'is-success' : 'is-warning');

    function emptyState(text) {
        return h('div', { class: 'adm-empty' }, h('i', { class: 'bi bi-inbox', 'aria-hidden': 'true' }), h('p', { text }));
    }

    function loading() {
        return h('div', { class: 'adm-loading', role: 'status' }, h('span', { class: 'spinner-border spinner-border-sm' }),
            h('span', { text: ' A carregar…' }));
    }

    function actionButton(label, icon, handler, kind) {
        return h('button', { type: 'button', class: `adm-btn ${kind || ''}`, onclick: handler },
            h('i', { class: `bi ${icon}`, 'aria-hidden': 'true' }), h('span', { text: ` ${label}` }));
    }

    function pager(info, go) {
        const total = asInt(info.total_pages) || 1;
        const page = asInt(info.page) || 1;
        return h('div', { class: 'adm-pager' },
            h('button', { type: 'button', class: 'adm-btn', disabled: page <= 1, onclick: () => go(page - 1) }, '‹ Anterior'),
            h('span', { class: 'adm-pager-info', text: `Página ${page} de ${total} (${asInt(info.total)})` }),
            h('button', { type: 'button', class: 'adm-btn', disabled: page >= total, onclick: () => go(page + 1) }, 'Seguinte ›'));
    }

    // ── Modal de confirmação reutilizável ─────────────────────────
    // opções: title, lines[], counts{}, danger, confirmText (frase a escrever), askPassword, checkbox{label, required},
    //         select{label, options[]}, confirmLabel, submit(values) -> {ok,data,status}, onDone(), disabledReason
    function ensureModal() {
        if (modal) return modal;
        const el = h('div', { class: 'modal fade', id: 'adminConfirmModal', tabindex: '-1', 'aria-labelledby': 'adminConfirmTitle' },
            h('div', { class: 'modal-dialog modal-dialog-centered' },
                h('form', { class: 'modal-content', id: 'adminConfirmForm', novalidate: true },
                    h('div', { class: 'modal-header' },
                        h('h5', { class: 'modal-title', id: 'adminConfirmTitle' }),
                        h('button', { type: 'button', class: 'btn-close', 'data-bs-dismiss': 'modal', 'aria-label': 'Fechar' })),
                    h('div', { class: 'modal-body', id: 'adminConfirmBody' }),
                    h('div', { class: 'modal-footer' },
                        h('button', { type: 'button', class: 'btn btn-secondary btn-sm', 'data-bs-dismiss': 'modal' }, 'Cancelar'),
                        h('button', { type: 'submit', class: 'btn btn-danger btn-sm', id: 'adminConfirmSubmit' })))));
        document.body.appendChild(el);
        // Ao fechar, limpa tudo (senhas incluídas)
        el.addEventListener('hidden.bs.modal', () => { document.getElementById('adminConfirmBody').replaceChildren(); });
        modal = el;
        return el;
    }

    function openConfirm(opts) {
        const el = ensureModal();
        const body = el.querySelector('#adminConfirmBody');
        const submitBtn = el.querySelector('#adminConfirmSubmit');
        const form = el.querySelector('#adminConfirmForm');
        el.querySelector('#adminConfirmTitle').textContent = opts.title;
        submitBtn.textContent = opts.confirmLabel || 'Confirmar';
        submitBtn.className = `btn btn-sm ${opts.danger === false ? 'btn-primary' : 'btn-danger'}`;
        submitBtn.disabled = !!opts.disabledReason;

        const fields = {};
        const nodes = [];
        (opts.lines || []).forEach(line => nodes.push(h('p', { class: 'adm-modal-line', text: line })));
        if (opts.counts) {
            nodes.push(h('table', { class: 'adm-counts' }, h('tbody', null, Object.keys(COUNT_LABEL)
                .filter(k => k in opts.counts)
                .map(k => h('tr', null, h('th', { scope: 'row', text: COUNT_LABEL[k] }), h('td', { text: asInt(opts.counts[k]) }))))));
        }
        if (opts.select) {
            fields.select = h('select', { class: 'form-select form-select-sm', id: 'adminConfirmSelect' },
                opts.select.options.map(o => h('option', { value: o.value, text: o.label })));
            nodes.push(h('label', { class: 'form-label', for: 'adminConfirmSelect', text: opts.select.label }), fields.select);
        }
        if (opts.checkbox) {
            fields.checkbox = h('input', { type: 'checkbox', class: 'form-check-input', id: 'adminConfirmCheck' });
            nodes.push(h('div', { class: 'form-check my-2' }, fields.checkbox,
                h('label', { class: 'form-check-label', for: 'adminConfirmCheck', text: opts.checkbox.label })));
        }
        if (opts.confirmText) {
            fields.text = h('input', { type: 'text', class: 'form-control form-control-sm', id: 'adminConfirmText',
                autocomplete: 'off', placeholder: opts.confirmText });
            nodes.push(h('label', { class: 'form-label mt-2', for: 'adminConfirmText', text: opts.confirmPrompt || `Escreva «${opts.confirmText}» para confirmar` }), fields.text);
        }
        if (opts.askPassword !== false) {
            fields.password = h('input', { type: 'password', class: 'form-control form-control-sm', id: 'adminConfirmPassword',
                autocomplete: 'current-password' });
            nodes.push(h('label', { class: 'form-label mt-2', for: 'adminConfirmPassword', text: 'A sua senha (confirmação)' }), fields.password);
        }
        const msg = h('div', { class: 'adm-modal-msg', id: 'adminConfirmMsg', role: 'alert' });
        if (opts.disabledReason) msg.textContent = opts.disabledReason;
        nodes.push(msg);
        body.replaceChildren(...nodes);

        const wipePassword = () => { if (fields.password) fields.password.value = ''; };
        form.onsubmit = async ev => {
            ev.preventDefault();
            msg.textContent = '';
            if (opts.confirmText && (fields.text.value.trim() !== opts.confirmText)) { msg.textContent = ERROR_TEXT.confirmation_mismatch; return; }
            if (fields.password && !fields.password.value) { msg.textContent = 'Indique a sua senha para confirmar.'; return; }
            if (opts.checkbox && opts.checkbox.required && !fields.checkbox.checked) { msg.textContent = opts.checkbox.requiredMessage; return; }
            const values = {
                password: fields.password ? fields.password.value : undefined,
                confirmText: fields.text ? fields.text.value.trim() : undefined,
                checked: fields.checkbox ? fields.checkbox.checked : false,
                selected: fields.select ? fields.select.value : undefined,
            };
            submitBtn.disabled = true;
            const result = await opts.submit(values);
            wipePassword();                                   // a senha nunca fica no campo depois da tentativa
            submitBtn.disabled = !!opts.disabledReason;
            if (result.ok) {
                bootstrap.Modal.getOrCreateInstance(el).hide();
                if (opts.successMessage) toast(opts.successMessage, 'success');
                if (opts.onDone) opts.onDone(result.data);
            } else {
                msg.textContent = errorText(result.data);
            }
        };
        bootstrap.Modal.getOrCreateInstance(el).show();
        setTimeout(() => (fields.text || fields.password || fields.select || submitBtn).focus(), 250);
    }

    // ── Estrutura ─────────────────────────────────────────────────
    function tabsForRole() {
        const tabs = [{ id: 'users', label: 'Utilizadores', icon: 'bi-people' }];
        if (isSuper()) tabs.push({ id: 'admins', label: 'Administradores', icon: 'bi-shield-check' });
        tabs.push({ id: 'audit', label: 'Auditoria', icon: 'bi-journal-text' });
        if (isSuper()) tabs.push({ id: 'maintenance', label: 'Manutenção', icon: 'bi-tools' });
        return tabs;
    }

    function build() {
        root = document.getElementById('admin-root');
        const tabs = tabsForRole();
        root.replaceChildren(
            h('header', { class: 'adm-head' },
                h('div', null,
                    h('h2', { class: 'adm-title', text: 'Painel administrativo' }),
                    h('p', { class: 'adm-sub', text: isSuper()
                        ? 'Controlo completo: utilizadores, administradores, auditoria e manutenção. As operações destrutivas pedem a sua senha.'
                        : 'Gestão de contas de utilizadores comuns e consulta das suas próprias ações.' })),
                badge(roleLabel(currentUser && currentUser.role), ROLE_BADGE[currentUser && currentUser.role] || 'is-neutral')),
            h('div', { class: 'adm-cards', id: 'adm-cards', 'aria-label': 'Resumo' }),
            h('div', { class: 'adm-tabs', role: 'tablist', 'aria-label': 'Secções da administração' },
                tabs.map(t => h('button', { type: 'button', class: 'adm-tab', role: 'tab', id: `adm-tab-${t.id}`,
                    'aria-selected': 'false', 'aria-controls': 'adm-panel', 'data-tab': t.id, onclick: () => selectTab(t.id) },
                    h('i', { class: `bi ${t.icon}`, 'aria-hidden': 'true' }), h('span', { text: ` ${t.label}` })))),
            h('div', { class: 'adm-body', id: 'adm-panel', role: 'tabpanel', tabindex: '0' }));
        state.built = true;
    }

    function selectTab(id) {
        if (!tabsForRole().some(t => t.id === id)) id = 'users';
        state.tab = id;
        root.querySelectorAll('.adm-tab').forEach(btn => {
            const on = btn.dataset.tab === id;
            btn.classList.toggle('active', on);
            btn.setAttribute('aria-selected', String(on));
        });
        document.getElementById('adm-panel').setAttribute('aria-labelledby', `adm-tab-${id}`);
        ({ users: loadUsers, admins: loadAdmins, audit: loadAudit, maintenance: loadMaintenance })[id]();
    }

    async function loadOverview() {
        const r = await api('/admin/overview');
        const box = document.getElementById('adm-cards');
        if (!box) return;
        if (!r.ok) { box.replaceChildren(); return; }
        const d = r.data;
        const card = (label, value) => h('div', { class: 'adm-card' }, h('span', { class: 'adm-card-value', text: asInt(value) }),
            h('span', { class: 'adm-card-label', text: label }));
        box.replaceChildren(card('Utilizadores', d.users), card('Ativos', d.active), card('Inativos', d.inactive),
            card('Administradores', d.admins), card('Superadministradores', d.superadmins));
    }

    function refreshAll() {
        loadOverview();
        selectTab(state.tab);
    }

    function setBody(...nodes) {
        const body = document.getElementById('adm-panel');
        if (body) body.replaceChildren(...nodes);
    }

    function failure(r) {
        return h('div', { class: 'adm-error', role: 'alert' }, h('i', { class: 'bi bi-exclamation-triangle', 'aria-hidden': 'true' }),
            h('span', { text: ` ${errorText(r.data, r.status === 0 ? 'Não foi possível contactar o servidor.' : undefined)}` }));
    }

    // ── Utilizadores ──────────────────────────────────────────────
    async function loadUsers() {
        setBody(loading());
        const u = state.users;
        const r = await api(`/admin/users${buildQuery({ page: u.page, search: u.search, role: u.role, status: u.status })}`);
        if (state.tab !== 'users') return;
        const filters = usersToolbar();
        if (!r.ok) { setBody(filters, failure(r)); return; }
        const rows = Array.isArray(r.data.users) ? r.data.users : [];
        setBody(filters, rows.length ? usersTable(rows) : emptyState('Nenhum utilizador encontrado.'), pager(r.data, p => { u.page = p; loadUsers(); }));
    }

    function usersToolbar() {
        const u = state.users;
        const select = (id, label, value, options, onchange) => h('label', { class: 'adm-filter' }, h('span', { class: 'visually-hidden', text: label }),
            h('select', { class: 'form-select form-select-sm', id, onchange },
                options.map(([val, text]) => { const o = h('option', { value: val, text }); if (val === value) o.selected = true; return o; })));
        return h('div', { class: 'adm-toolbar' },
            h('label', { class: 'adm-filter adm-search' }, h('span', { class: 'visually-hidden', text: 'Pesquisar utilizadores' }),
                h('input', { type: 'search', class: 'form-control form-control-sm', id: 'adm-search', maxlength: 100,
                    placeholder: 'Pesquisar por nome ou email', value: u.search, oninput: ev => {
                        clearTimeout(u.timer);
                        const value = ev.target.value;
                        u.timer = setTimeout(() => { u.search = value.trim(); u.page = 1; loadUsers(); }, 350);
                    } })),
            select('adm-role', 'Filtrar por papel', u.role, [['', 'Todos os papéis'], ['user', 'Utilizadores'], ['admin', 'Administradores'], ['superadmin', 'Superadministradores']],
                ev => { u.role = ev.target.value; u.page = 1; loadUsers(); }),
            select('adm-status', 'Filtrar por estado', u.status, [['', 'Todos os estados'], ['active', 'Ativas'], ['inactive', 'Inativas']],
                ev => { u.status = ev.target.value; u.page = 1; loadUsers(); }));
    }

    function userCell(label, ...content) {
        return h('td', { 'data-label': label }, ...content);
    }

    function usersTable(rows) {
        const head = ['Utilizador', 'Papel', 'Estado', 'Criado em', 'Último acesso', 'Ações'];
        return h('div', { class: 'adm-table-wrap' }, h('table', { class: 'adm-table' },
            h('thead', null, h('tr', null, head.map(t => h('th', { scope: 'col', text: t })))),
            h('tbody', null, rows.map(user => h('tr', null,
                userCell('Utilizador', h('div', { class: 'adm-user' },
                    h('span', { class: 'adm-user-name', text: asText(user.username) }),
                    h('span', { class: 'adm-user-email', text: asText(user.email) }))),
                userCell('Papel', roleBadge(user.role)),
                userCell('Estado', statusBadge(user.is_active)),
                userCell('Criado em', formatDate(user.created_at)),
                userCell('Último acesso', formatDate(user.last_login)),
                userCell('Ações', userActions(user)))))));
    }

    function userActions(user) {
        const a = user.actions || {};
        const buttons = [];
        if (a.toggle_active) {
            buttons.push(user.is_active
                ? actionButton('Desativar', 'bi-person-slash', () => deactivate(user), 'is-warning')
                : actionButton('Ativar', 'bi-person-check', () => activate(user)));
        }
        if (a.remove_avatar) buttons.push(actionButton('Remover foto', 'bi-image', () => removeAvatar(user)));
        if (a.change_role) buttons.push(actionButton('Papel', 'bi-shield', () => changeRole(user)));
        if (a.clear_data) buttons.push(actionButton('Limpar dados', 'bi-eraser', () => clearData(user), 'is-danger'));
        if (a.delete) buttons.push(actionButton('Excluir', 'bi-trash', () => deleteUser(user), 'is-danger'));
        return buttons.length ? h('div', { class: 'adm-actions' }, buttons) : h('span', { class: 'adm-muted', text: '—' });
    }

    async function simpleAction(path, options, okMessage) {
        const r = await api(path, options);
        if (r.ok) { toast(okMessage, 'success'); refreshAll(); }
        else toast(errorText(r.data), 'error');
    }

    const activate = user => simpleAction(`/admin/users/${user.id}/activate`, { method: 'POST' }, 'Conta ativada.');

    function deactivate(user) {
        openConfirm({
            title: 'Desativar conta', askPassword: false, confirmLabel: 'Desativar',
            lines: [`A conta «${asText(user.username)}» deixa de poder iniciar sessão e as sessões abertas deixam de funcionar. Os dados são mantidos e pode reativá-la a qualquer momento.`],
            submit: () => api(`/admin/users/${user.id}/deactivate`, { method: 'POST' }),
            successMessage: 'Conta desativada.', onDone: refreshAll,
        });
    }

    function removeAvatar(user) {
        openConfirm({
            title: 'Remover foto de perfil', askPassword: false, confirmLabel: 'Remover foto',
            lines: [`A foto de «${asText(user.username)}» será removida. O utilizador pode enviar outra.`],
            submit: () => api(`/admin/users/${user.id}/avatar`, { method: 'DELETE' }),
            successMessage: 'Foto removida.', onDone: refreshAll,
        });
    }

    function changeRole(user) {
        const options = { user: ['admin'], admin: ['user', 'superadmin'], superadmin: ['admin'] }[user.role] || [];
        if (!options.length) return;
        openConfirm({
            title: 'Alterar papel', confirmLabel: 'Alterar papel',
            lines: [`Utilizador: ${asText(user.username)} (${roleLabel(user.role)}). Quem fica sem privilégios perde o acesso ao painel de imediato.`],
            select: { label: 'Novo papel', options: options.map(r => ({ value: r, label: roleLabel(r) })) },
            submit: v => api(`/admin/users/${user.id}/role`, { method: 'PUT', ...jsonBody({ role: v.selected, password: v.password }) }),
            successMessage: 'Papel atualizado.', onDone: refreshAll,
        });
    }

    async function clearData(user) {
        const r = await api(`/admin/users/${user.id}/data-summary`);
        if (!r.ok) { toast(errorText(r.data), 'error'); return; }
        const counts = r.data.counts || {};
        openConfirm({
            title: 'Limpar dados do utilizador', confirmLabel: 'Limpar dados', confirmText: asText(user.username),
            lines: [`Vão ser apagados os dados de «${asText(user.username)}»: tarefas, categorias, notificações e colaborações. A conta, o papel, a foto e as sessões são mantidos.`,
                'Esta operação não pode ser desfeita. Contagens atuais:'],
            counts,
            submit: v => api(`/admin/users/${user.id}/clear-data`, { method: 'POST',
                ...jsonBody({ password: v.password, confirm_username: v.confirmText, expected_counts: counts }) }),
            successMessage: 'Dados do utilizador removidos.', onDone: refreshAll,
        });
    }

    async function deleteUser(user) {
        const r = await api(`/admin/users/${user.id}/data-summary`);
        if (!r.ok) { toast(errorText(r.data), 'error'); return; }
        const counts = r.data.counts || {};
        const tasks = asInt(counts.tasks);
        openConfirm({
            title: 'Excluir utilizador', confirmLabel: 'Excluir definitivamente', confirmText: asText(user.username),
            lines: [`A conta «${asText(user.username)}» e todos os seus dados serão apagados de forma definitiva. Contagens atuais:`],
            counts,
            checkbox: tasks > 0 ? { label: `Apagar também as ${tasks} tarefa(s) deste utilizador`, required: true,
                requiredMessage: ERROR_TEXT.has_tasks } : null,
            submit: v => api(`/admin/users/${user.id}`, { method: 'DELETE',
                ...jsonBody({ password: v.password, confirm_username: v.confirmText, expected_counts: counts, delete_tasks: v.checked }) }),
            successMessage: 'Utilizador excluído.', onDone: refreshAll,
        });
    }

    // ── Administradores (SUPERADMIN) ──────────────────────────────
    async function loadAdmins() {
        setBody(loading());
        const r = await api('/admin/admins');
        if (state.tab !== 'admins') return;
        if (!r.ok) { setBody(failure(r)); return; }
        const rows = Array.isArray(r.data.admins) ? r.data.admins : [];
        setBody(promoteBox(), h('h3', { class: 'adm-section', text: 'Administradores atuais' }),
            rows.length ? usersTable(rows) : emptyState('Nenhum administrador.'));
    }

    function promoteBox() {
        const input = h('input', { type: 'email', class: 'form-control form-control-sm', id: 'adm-lookup', maxlength: 120,
            placeholder: 'email do utilizador existente', autocomplete: 'off' });
        const result = h('div', { class: 'adm-lookup-result', id: 'adm-lookup-result', 'aria-live': 'polite' });
        const search = async ev => {
            ev.preventDefault();
            result.replaceChildren();
            const email = input.value.trim();
            if (!email) return;
            const r = await api(`/admin/users/lookup${buildQuery({ email })}`);
            if (!r.ok) { result.replaceChildren(h('p', { class: 'adm-error', text: errorText(r.data) })); return; }
            const user = r.data.user || {};
            const canPromote = user.role === 'user' && user.is_active;
            result.replaceChildren(h('div', { class: 'adm-lookup-card' },
                h('div', { class: 'adm-user' }, h('span', { class: 'adm-user-name', text: asText(user.username) }),
                    h('span', { class: 'adm-user-email', text: asText(user.email) })),
                roleBadge(user.role), statusBadge(user.is_active),
                canPromote ? actionButton('Promover a Administrador', 'bi-shield-plus', () => promote(user))
                    : h('span', { class: 'adm-muted', text: user.role !== 'user' ? 'Já tem privilégios.' : 'Conta inativa: ative-a primeiro.' })));
        };
        return h('section', { class: 'adm-promote' },
            h('h3', { class: 'adm-section', text: 'Promover utilizador existente' }),
            h('p', { class: 'adm-muted', text: 'Procure o utilizador pelo email, confira os dados e confirme com a sua senha.' }),
            h('form', { class: 'adm-inline-form', onsubmit: search }, h('label', { class: 'visually-hidden', for: 'adm-lookup', text: 'Email do utilizador' }),
                input, h('button', { type: 'submit', class: 'adm-btn' }, h('i', { class: 'bi bi-search', 'aria-hidden': 'true' }), ' Procurar')),
            result);
    }

    function promote(user) {
        openConfirm({
            title: 'Promover a Administrador', confirmLabel: 'Promover', danger: false,
            lines: [`«${asText(user.username)}» (${asText(user.email)}) passará a ter acesso ao painel de administração, limitado a utilizadores comuns.`],
            submit: v => api('/admin/admins', { method: 'POST', ...jsonBody({ user_id: user.id, password: v.password }) }),
            successMessage: 'Utilizador promovido a Administrador.', onDone: refreshAll,
        });
    }

    // ── Auditoria ─────────────────────────────────────────────────
    async function loadAudit() {
        setBody(loading());
        const f = state.audit;
        const r = await api(`/admin/audit${buildQuery({ page: f.page, action: f.action, result: f.result, actor_id: isSuper() ? f.actor : '', from: f.from, to: f.to })}`);
        if (state.tab !== 'audit') return;
        const filters = auditToolbar();
        if (!r.ok) { setBody(filters, failure(r)); return; }
        const rows = Array.isArray(r.data.entries) ? r.data.entries : [];
        setBody(filters,
            h('p', { class: 'adm-muted', text: r.data.scope === 'all' ? 'A mostrar o histórico completo.' : 'A mostrar apenas as suas próprias ações.' }),
            rows.length ? auditTable(rows) : emptyState('Sem registos para estes filtros.'),
            pager(r.data, p => { f.page = p; loadAudit(); }));
    }

    function auditToolbar() {
        const f = state.audit;
        const field = (label, node) => h('label', { class: 'adm-filter' }, h('span', { class: 'adm-filter-label', text: label }), node);
        const sel = (value, options, onchange) => h('select', { class: 'form-select form-select-sm', onchange },
            options.map(([val, text]) => { const o = h('option', { value: val, text }); if (val === value) o.selected = true; return o; }));
        let draft = { ...f };
        const apply = ev => { ev.preventDefault(); Object.assign(f, draft, { page: 1 }); loadAudit(); };
        const actions = [['', 'Todas as ações'], ...Object.entries(ACTION_LABEL)];
        return h('form', { class: 'adm-toolbar adm-audit-filters', onsubmit: apply },
            field('Ação', sel(f.action, actions, ev => { draft.action = ev.target.value; })),
            field('Resultado', sel(f.result, [['', 'Todos'], ['success', 'Sucesso'], ['denied', 'Negado'], ['failed', 'Falhou']], ev => { draft.result = ev.target.value; })),
            isSuper() ? field('Administrador (ID)', h('input', { type: 'number', min: 1, class: 'form-control form-control-sm', value: f.actor,
                oninput: ev => { draft.actor = ev.target.value; } })) : null,
            field('De', h('input', { type: 'date', class: 'form-control form-control-sm', value: f.from, oninput: ev => { draft.from = ev.target.value; } })),
            field('Até', h('input', { type: 'date', class: 'form-control form-control-sm', value: f.to, oninput: ev => { draft.to = ev.target.value; } })),
            h('button', { type: 'submit', class: 'adm-btn' }, h('i', { class: 'bi bi-funnel', 'aria-hidden': 'true' }), ' Filtrar'));
    }

    function detailsText(details) {
        if (!details || typeof details !== 'object') return '';
        return Object.entries(details).filter(([, v]) => typeof v !== 'object')
            .map(([k, v]) => `${k}: ${asText(v)}`).join(' · ');
    }

    function auditTable(rows) {
        const head = ['Quando', 'Administrador', 'Ação', 'Alvo', 'Resultado', 'Detalhes'];
        return h('div', { class: 'adm-table-wrap' }, h('table', { class: 'adm-table' },
            h('thead', null, h('tr', null, head.map(t => h('th', { scope: 'col', text: t })))),
            h('tbody', null, rows.map(e => h('tr', null,
                userCell('Quando', formatDate(e.created_at)),
                userCell('Administrador', asText(e.actor_label) || (e.actor_id ? `#${asInt(e.actor_id)}` : 'Sistema')),
                userCell('Ação', ACTION_LABEL[e.action] || asText(e.action)),
                userCell('Alvo', e.target_id ? `${asText(e.target_label) || 'utilizador'} (#${asInt(e.target_id)})` : '—'),
                userCell('Resultado', badge(RESULT_LABEL[e.result] || 'Desconhecido', RESULT_BADGE[e.result] || 'is-neutral')),
                userCell('Detalhes', h('span', { class: 'adm-details', text: detailsText(e.details) })))))));
    }

    // ── Manutenção (SUPERADMIN) ───────────────────────────────────
    async function loadMaintenance() {
        setBody(loading());
        const r = await api('/admin/maintenance/summary');
        if (state.tab !== 'maintenance') return;
        if (!r.ok) { setBody(failure(r)); return; }
        const d = r.data;
        const tables = d.tables || {};
        const names = { users: 'Utilizadores', tasks: 'Tarefas', categories: 'Categorias', notifications: 'Notificações',
            task_collaborators: 'Colaborações', refresh_tokens: 'Sessões (refresh tokens)', user_avatars: 'Fotos de perfil', admin_audit_log: 'Registos de auditoria' };
        setBody(
            h('section', null, h('h3', { class: 'adm-section', text: 'Resumo da base de dados' }),
                h('table', { class: 'adm-counts adm-counts-wide' }, h('tbody', null,
                    Object.keys(names).map(k => h('tr', null, h('th', { scope: 'row', text: names[k] }), h('td', { text: asInt(tables[k]) }))))),
                h('p', { class: 'adm-muted', text: `Motor: ${asText(d.database)} · Versão do esquema: ${asText(d.schema_version) || 'desconhecida'}` })),
            h('section', { class: 'adm-danger-zone' },
                h('h3', { class: 'adm-section', text: 'Limpeza global de dados' }),
                h('p', { text: 'Apaga tarefas, categorias, notificações, colaborações, sessões e fotos de TODOS os utilizadores. Mantém as contas, os administradores, o esquema e a auditoria. Não pode ser desfeita.' }),
                h('p', { class: 'adm-muted', text: d.wipe_enabled ? 'Ativada neste ambiente.' : 'Desativada neste ambiente (variável ADMIN_ALLOW_GLOBAL_WIPE). A pré-visualização continua disponível.' }),
                actionButton('Pré-visualizar limpeza global', 'bi-eye', () => previewWipe(d), 'is-danger')));
    }

    async function previewWipe(summary) {
        const r = await api('/admin/maintenance/wipe/preview', { method: 'POST' });
        if (!r.ok) { toast(errorText(r.data), 'error'); return; }
        const p = r.data;
        openConfirm({
            title: 'Limpeza global de dados', confirmLabel: 'Apagar todos os dados', confirmText: asText(p.phrase),
            confirmPrompt: `Escreva «${asText(p.phrase)}» para confirmar`,
            lines: ['Vão ser apagados, em todas as contas, os dados abaixo. A pré-visualização é válida por poucos minutos.'],
            counts: p.counts || {},
            disabledReason: p.wipe_enabled ? '' : ERROR_TEXT.wipe_disabled,
            submit: v => api('/admin/maintenance/wipe', { method: 'POST',
                ...jsonBody({ token: p.token, password: v.password, confirmation: v.confirmText }) }),
            successMessage: 'Dados operacionais removidos. Contas e auditoria foram preservadas.',
            onDone: data => {
                // A limpeza revoga todos os refresh tokens; o servidor devolve tokens novos para esta sessão continuar
                if (data && data.tokens) {
                    accessToken = data.tokens.access_token;
                    refreshToken = data.tokens.refresh_token;
                    localStorage.setItem('access_token', accessToken);
                    localStorage.setItem('refresh_token', refreshToken);
                }
                refreshAll();
            },
        });
    }

    // ── API pública do painel ─────────────────────────────────────
    function onShown() {
        if (!state.built || !document.getElementById('adm-panel')) build();
        loadOverview();
        selectTab(tabsForRole().some(t => t.id === state.tab) ? state.tab : 'users');
    }

    window.PainelAdm = {
        onShown,
        _internals: { h, asText, buildQuery, roleLabel, errorText, detailsText, formatDate, tabsForRole, ERROR_TEXT, ACTION_LABEL },
    };
})();
