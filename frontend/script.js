'use strict';

// ══════════════════════════════════════════════
// CONFIGURAÇÃO
// ══════════════════════════════════════════════
const API = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1'
    ? 'http://localhost:5000'
    : '';   // mesmo domínio no Vercel

const COLORS = ['#667eea','#764ba2','#ef4444','#f59e0b','#22c55e','#3b82f6','#ec4899','#8b5cf6','#14b8a6','#6b7280'];

// ══════════════════════════════════════════════
// ESTADO
// ══════════════════════════════════════════════
let accessToken  = null;
let refreshToken = null;
let currentUser  = null;

let state = {
    view:     'dashboard',
    filter:   'all',
    priority: '',
    category: '',
    search:   '',
    sortBy:   'created_at',
    order:    'desc',
    page:     1,
    perPage:  20,
};

let searchTimer = null;
let categoriesById = {};
let tasksById = {};       // tarefas da lista atual (o painel de detalhes reutiliza estes dados)
let panelTaskId = null;   // tarefa aberta no painel de detalhes
let panelSnapshot = '';   // JSON da tarefa exibida (evita redesenhos desnecessários)
let panelOpener = null;   // elemento que abriu o painel (para devolver o foco)
let userInitials = '?';
let notifOpen   = false;

// ══════════════════════════════════════════════
// INICIALIZAÇÃO
// ══════════════════════════════════════════════
document.addEventListener('DOMContentLoaded', init);

function init() {
    accessToken  = localStorage.getItem('access_token');
    refreshToken = localStorage.getItem('refresh_token');
    const userData = localStorage.getItem('user');

    if (!accessToken || !userData) {
        window.location.href = 'auth.html';
        return;
    }

    currentUser = JSON.parse(userData);
    applyTheme(getStoredTheme());
    syncSortControls();
    setupUser();
    loadAvatar();
    syncCurrentUser();
    startRelativeTimeUpdates();
    setupColorPicker();
    loadAll();
    loadNotifications();

    // Fechar painel de notificações ao clicar fora
    document.addEventListener('click', e => {
        const panel = document.getElementById('notif-panel');
        const btn   = document.querySelector('.notif-btn');
        if (notifOpen && !panel.contains(e.target) && !btn.contains(e.target)) {
            closeNotifPanel();
        }
    });
}

function setupUser() {
    const initials = currentUser.username
        ? currentUser.username.slice(0, 2).toUpperCase()
        : (currentUser.email || '?').slice(0, 2).toUpperCase();

    userInitials = initials;
    refreshAvatars();
    document.getElementById('user-name').textContent   = currentUser.username || currentUser.email;
    document.getElementById('user-email').textContent  = currentUser.email || '';
}

function setupColorPicker(activeColor = '#667eea') {
    const picker = document.getElementById('color-picker');
    if (!picker) return;
    picker.innerHTML = COLORS.map(c => `
        <div class="color-swatch ${c === activeColor ? 'active' : ''}"
             style="background:${c}"
             onclick="selectColor('${c}', this)"
             title="${c}"></div>
    `).join('');
}

function selectColor(color, el) {
    document.getElementById('categoryColor').value = color;
    document.querySelectorAll('.color-swatch').forEach(s => s.classList.remove('active'));
    el.classList.add('active');
}

// ══════════════════════════════════════════════
// API HELPER
// ══════════════════════════════════════════════
async function apiRequest(endpoint, options = {}) {
    const headers = {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${accessToken}`,
        ...(options.headers || {}),
    };

    try {
        const res = await fetch(`${API}${endpoint}`, { ...options, headers });

        if (res.status === 401 && !options.skipRefresh) {
            const ok = await tryRefresh();
            if (ok) return apiRequest(endpoint, options);
            doLogout();
            return null;
        }

        if (res.status === 429) {
            notifyOnce('Muitas requisições. Aguarde alguns minutos e tente novamente.');
        }
        return res;
    } catch (err) {
        // fetch rejeita sem resposta HTTP (rede, CORS, preflight recusado, servidor offline…):
        // a causa exata não é conhecida aqui, por isso a mensagem é neutra.
        console.error('API error:', err);
        notifyOnce('Não foi possível contactar o servidor. Verifique a ligação e tente novamente.');
        return null;
    }
}

// Evita empilhar o mesmo aviso de erro quando várias requisições falham ao mesmo tempo.
const recentErrorToasts = new Map();
function notifyOnce(msg, windowMs = 5000) {
    const now = Date.now();
    if (now - (recentErrorToasts.get(msg) || 0) < windowMs) return;
    recentErrorToasts.set(msg, now);
    toast(msg, 'error');
}

// Requisições simultâneas que recebem 401 compartilham uma única renovação
// (o refresh token é de uso único; renovar em paralelo deslogaria o usuário).
let refreshPromise = null;

function tryRefresh() {
    if (!refreshPromise) {
        refreshPromise = doRefresh().finally(() => { refreshPromise = null; });
    }
    return refreshPromise;
}

async function doRefresh() {
    try {
        const res  = await fetch(`${API}/auth/refresh`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ refresh_token: refreshToken }),
        });
        if (!res.ok) return false;
        const data = await res.json();
        accessToken  = data.tokens.access_token;
        refreshToken = data.tokens.refresh_token;
        localStorage.setItem('access_token',  accessToken);
        localStorage.setItem('refresh_token', refreshToken);
        return true;
    } catch { return false; }
}

// ══════════════════════════════════════════════
// CARREGAR TUDO
// ══════════════════════════════════════════════
// Carrega só o que a vista ativa mostra (categorias e estatísticas são partilhadas).
async function loadAll() {
    await Promise.all([loadCategories(), loadStats(), refreshActiveView()]);
}

function refreshActiveView() {
    return state.view === 'tasks' ? loadTasks() : loadRecentTasks();
}

// ══════════════════════════════════════════════
// TAREFAS
// ══════════════════════════════════════════════
async function loadTasks() {
    renderFilterChips();
    const params = new URLSearchParams({
        page:     state.page,
        per_page: state.perPage,
        sort_by:  state.sortBy,
        order:    state.order,
    });

    if (state.filter !== 'all') params.set('status', state.filter);
    if (state.priority)         params.set('priority', state.priority);
    if (state.category)         params.set('category_id', state.category);
    if (state.search)           params.set('search', state.search);

    const area = document.getElementById('tasks-area');
    // Primeira carga (ou vinda de um estado vazio): esqueleto. Nas seguintes, mantém a lista
    // anterior (sem salto de layout) e só a atenua enquanto carrega.
    if (area.querySelector('.task-card')) {
        area.classList.add('is-loading');
        area.setAttribute('aria-busy', 'true');
    } else {
        renderTasksSkeleton(area);
    }

    try {
        const res = await apiRequest(`/tasks?${params}`);
        if (!res || !res.ok) {
            if (area.querySelector('.skeleton-list')) area.innerHTML = '';
            return;
        }

        const data = await res.json();
        renderTasks(data.tasks);
        renderPagination(data);
        updateNavCounts(data);
        renderResultsCount(data.total);
    } finally {
        area.classList.remove('is-loading');
        area.removeAttribute('aria-busy');
    }
}

function renderTasksSkeleton(area, n = 5) {
    const card = `
        <div class="skeleton-card" aria-hidden="true">
            <span class="skeleton skeleton-check"></span>
            <div class="skeleton-body">
                <span class="skeleton skeleton-line w-60"></span>
                <span class="skeleton skeleton-line w-90"></span>
                <span class="skeleton skeleton-line w-30"></span>
            </div>
        </div>`;
    area.innerHTML = `
        <div class="skeleton-list" role="status">
            <span class="visually-hidden">Carregando tarefas…</span>
            ${card.repeat(n)}
        </div>`;
}

// Estado do prazo de uma tarefa: { cls, icon, label } ou null (sem prazo).
// Atrasada vem da API (is_overdue); "hoje/amanhã" compara dias no fuso da aplicação.
function dueInfo(t) {
    if (!t.due_date) return null;
    const when = formatDate(t.due_date);
    if (t.status === 'completed') return { cls: 'is-done', icon: 'calendar-check', label: when };
    if (t.is_overdue)             return { cls: 'is-overdue', icon: 'exclamation-circle', label: `Atrasada · ${when}` };

    const day  = d => new Intl.DateTimeFormat('en-CA', { timeZone: APP_TZ }).format(d);   // AAAA-MM-DD
    const diff = Math.round((Date.parse(day(parseServerDate(t.due_date))) - Date.parse(day(new Date()))) / 86400000);
    if (diff === 0) return { cls: 'is-today', icon: 'alarm', label: `Hoje · ${when}` };
    if (diff === 1) return { cls: 'is-soon',  icon: 'calendar-event', label: `Amanhã · ${when}` };
    return { cls: '', icon: 'calendar3', label: when };
}

function renderTasks(tasks) {
    const area = document.getElementById('tasks-area');
    tasksById = Object.fromEntries(tasks.map(t => [t.id, t]));

    if (!tasks.length) {
        const filters = activeFilters();
        area.innerHTML = filters.length
            ? `
            <div class="empty-state">
                <i class="bi bi-funnel"></i>
                <h4>Nenhum resultado</h4>
                <p>${state.search && filters.length === 1
                    ? `Nenhuma tarefa corresponde a “${esc(state.search)}”.`
                    : 'Nenhuma tarefa corresponde aos filtros atuais.'}</p>
                <button class="btn-secondary-sm" onclick="clearFilters()">
                    <i class="bi bi-x-circle"></i> Limpar filtros
                </button>
            </div>`
            : `
            <div class="empty-state">
                <i class="bi bi-inbox"></i>
                <h4>Ainda não tem tarefas</h4>
                <p>Crie a sua primeira tarefa para começar a organizar o trabalho.</p>
                <button class="btn-primary-sm" onclick="openTaskModal()">
                    <i class="bi bi-plus-lg"></i> Nova tarefa
                </button>
            </div>`;
        syncPanelWithTasks();
        return;
    }

    area.innerHTML = tasks.map(t => {
        const done = t.status === 'completed';
        const due  = dueInfo(t);
        const acc  = taskAccess(t);
        const catBadge = t.category
            ? `<span class="badge-cat" style="background:${t.category.color}">${esc(t.category.name)}</span>`
            : '';

        return `
        <article class="task-card priority-${t.priority} ${t.is_overdue ? 'is-overdue' : ''} ${done ? 'is-completed' : ''}"
                 data-id="${t.id}" onclick="onTaskCardClick(event, ${t.id})">
            <input type="checkbox" class="task-check" data-id="${t.id}" ${done ? 'checked' : ''}
                   ${acc.canEdit ? '' : 'disabled title="Somente leitura"'}
                   aria-label="Concluir tarefa: ${esc(t.title)}"
                   onchange="toggleStatus(${t.id}, this.checked)">
            <div class="task-body">
                <div class="task-title ${done ? 'done' : ''}">
                    <button type="button" class="task-title-btn" onclick="openTaskPanel(${t.id}, this)"
                            aria-haspopup="dialog" title="Ver detalhes">${esc(t.title)}</button>
                </div>
                ${t.description ? `<div class="task-desc">${esc(t.description)}</div>` : ''}
                <div class="task-meta">
                    <span class="badge-priority ${t.priority}">${priorityLabel(t.priority)}</span>
                    <span class="badge-status ${t.status}">${statusLabel(t.status)}</span>
                    ${catBadge}
                    ${accessBadges(acc, t)}
                    ${due ? `<span class="task-due ${due.cls}"><i class="bi bi-${due.icon}"></i>${due.label}</span>` : ''}
                    <span class="task-created"><i class="bi bi-clock"></i>${relTimeHtml(t.created_at)}</span>
                </div>
            </div>
            ${acc.canEdit || acc.canDelete ? `
            <div class="task-actions">
                ${acc.canEdit ? `<button class="btn-icon" title="Editar" aria-label="Editar tarefa" onclick="openTaskModal(${t.id})"><i class="bi bi-pencil"></i></button>` : ''}
                ${acc.canDelete ? `<button class="btn-icon danger" title="Excluir" aria-label="Excluir tarefa" onclick="deleteTask(${t.id})"><i class="bi bi-trash"></i></button>` : ''}
            </div>` : ''}
        </article>`;
    }).join('');
    syncPanelWithTasks();
}

function renderPagination(data) {
    const wrap = document.getElementById('pagination-wrap');
    if (data.total_pages <= 1) { wrap.style.display = 'none'; return; }
    wrap.style.display = 'flex';

    document.getElementById('page-info').textContent =
        `Página ${data.page} de ${data.total_pages} (${data.total} tarefas)`;
    document.getElementById('btn-prev').disabled = !data.has_prev;
    document.getElementById('btn-next').disabled = !data.has_next;
}

function updateNavCounts(data) {
    // Atualiza os contadores da sidebar com os dados paginados
    document.getElementById('cnt-all').textContent = data.total ?? '';
}

// ══════════════════════════════════════════════
// STATS (sidebar)
// ══════════════════════════════════════════════
async function loadStats() {
    const res = await apiRequest('/tasks/stats');
    if (!res || !res.ok) return;
    const s = await res.json();

    document.getElementById('stat-pending-n').textContent  = s.pending ?? 0;
    document.getElementById('stat-overdue-n').textContent  = s.overdue ?? 0;
    document.getElementById('cnt-pending').textContent     = s.pending     ?? '';
    document.getElementById('cnt-in_progress').textContent = s.in_progress ?? '';
    document.getElementById('cnt-completed').textContent   = s.completed   ?? '';
    document.getElementById('cnt-all').textContent         = s.total       ?? '';

    renderDashboardStats(s);
}

// ══════════════════════════════════════════════
// DASHBOARD
// ══════════════════════════════════════════════
function loadDashboard() {
    return Promise.all([loadStats(), loadRecentTasks()]);
}

function renderDashboardStats(s) {
    const set = (id, v) => { document.getElementById(id).textContent = v; };
    set('dash-total',       s.total       ?? 0);
    set('dash-pending',     s.pending     ?? 0);
    set('dash-in_progress', s.in_progress ?? 0);
    set('dash-completed',   s.completed   ?? 0);
    set('dash-overdue',     s.overdue     ?? 0);
    document.getElementById('dash-overdue-card').classList.toggle('has-value', (s.overdue ?? 0) > 0);

    const rate = Math.round(s.completion_rate ?? 0);
    set('dash-rate', `${rate}%`);
    set('dash-rate-caption', s.total
        ? `${s.completed ?? 0} de ${s.total} tarefas concluídas`
        : 'Nenhuma tarefa criada ainda');
    document.getElementById('dash-rate-bar').style.width = `${rate}%`;

    const pb  = s.priority_breakdown || {};
    const max = Math.max(1, pb.high ?? 0, pb.medium ?? 0, pb.low ?? 0);
    ['high', 'medium', 'low'].forEach(p => {
        const n = pb[p] ?? 0;
        set(`dash-prio-${p}`, n);
        document.getElementById(`dash-prio-${p}-bar`).style.width = `${(n / max) * 100}%`;
    });
}

async function loadRecentTasks() {
    const params = new URLSearchParams({ page: 1, per_page: 5, sort_by: 'created_at', order: 'desc' });
    const res = await apiRequest(`/tasks?${params}`);
    if (!res || !res.ok) return;
    const data = await res.json();
    renderRecentTasks(data.tasks);
}

function renderRecentTasks(tasks) {
    const box = document.getElementById('dash-recent');
    if (!tasks.length) {
        box.innerHTML = `
            <div class="empty-state">
                <i class="bi bi-inbox"></i>
                <h4>Nenhuma tarefa ainda</h4>
                <p>Crie a sua primeira tarefa para começar.</p>
                <button class="btn-primary-sm" onclick="openTaskModal()">
                    <i class="bi bi-plus-lg"></i> Nova tarefa
                </button>
            </div>`;
        return;
    }

    box.innerHTML = tasks.map(t => {
        const done = t.status === 'completed';
        return `
        <div class="recent-item">
            <span class="recent-prio ${t.priority}" title="Prioridade ${priorityLabel(t.priority)}"></span>
            <div class="recent-main">
                <button class="recent-title ${done ? 'done' : ''}" onclick="openTaskModal(${t.id})">${esc(t.title)}</button>
                <div class="recent-meta">
                    <span class="badge-status ${t.status}">${statusLabel(t.status)}</span>
                    ${accessBadges(taskAccess(t), t)}
                    ${t.due_date ? `<span><i class="bi bi-calendar3"></i>${formatDate(t.due_date)}</span>` : ''}
                    <span><i class="bi bi-clock"></i>${relTimeHtml(t.created_at)}</span>
                    ${t.is_overdue ? `<span class="overdue-tag"><i class="bi bi-exclamation-circle"></i>Atrasada</span>` : ''}
                </div>
            </div>
            <span class="badge-priority ${t.priority}">${priorityLabel(t.priority)}</span>
        </div>`;
    }).join('');
}

// ══════════════════════════════════════════════
// CATEGORIAS
// ══════════════════════════════════════════════
async function loadCategories() {
    const res = await apiRequest('/categories');
    if (!res || !res.ok) return;
    const cats = await res.json();
    renderCategoriesSidebar(cats);
    renderCategoriesDropdowns(cats);
}

function renderCategoriesSidebar(cats) {
    const nav = document.getElementById('categories-nav');
    if (!cats.length) {
        nav.innerHTML = `<div class="text-muted small ps-2" style="opacity:.6">Nenhuma categoria</div>`;
        return;
    }
    nav.innerHTML = cats.map(c => `
        <div class="cat-nav-item ${state.category == c.id ? 'active' : ''}">
            <span class="cat-dot" style="background:${c.color}; flex-shrink:0;"></span>
            <span class="cat-name" onclick="filterByCategory(${c.id})" style="flex:1; cursor:pointer; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">
                ${esc(c.name)}
            </span>
            <span class="nav-badge">${c.task_count ?? 0}</span>
            <button class="cat-action-btn" onclick="openCategoryModal(${c.id}, '${esc(c.name)}', '${c.color}')" title="Editar">
                <i class="bi bi-pencil"></i>
            </button>
            <button class="cat-action-btn danger" onclick="deleteCategory(${c.id}, '${esc(c.name)}')" title="Excluir">
                <i class="bi bi-trash"></i>
            </button>
        </div>`).join('');
}

function renderCategoriesDropdowns(cats) {
    categoriesById = Object.fromEntries(cats.map(c => [String(c.id), c]));
    renderFilterChips();
    const opts = '<option value="">Sem categoria</option>' +
        cats.map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join('');

    const taskCat = document.getElementById('taskCategory');
    if (taskCat) taskCat.innerHTML = opts;

    const filterCat = document.getElementById('filter-category');
    if (filterCat) {
        filterCat.innerHTML = '<option value="">Categoria</option>' +
            cats.map(c => `<option value="${c.id}" ${state.category == c.id ? 'selected' : ''}>${esc(c.name)}</option>`).join('');
    }
}

// ══════════════════════════════════════════════
// CRUD TAREFAS
// ══════════════════════════════════════════════
// Estado do formulário aberto: o servidor decide o que é permitido; aqui só se adapta a interface.
let taskForm = { readOnly: false, canChangeCategory: true, titleTouched: false };
const TITLE_MAX = 200;

async function openTaskModal(taskId = null) {
    const modal = new bootstrap.Modal(document.getElementById('taskModal'));
    const $ = id => document.getElementById(id);
    let t = null;

    if (taskId) {
        const res = await apiRequest(`/tasks/${taskId}`);
        if (!res || !res.ok) { toast('Erro ao carregar tarefa', 'error'); return; }
        t = await res.json();
    }

    const acc = t ? taskAccess(t) : { isOwner: true, canEdit: true };
    taskForm = { readOnly: !acc.canEdit, canChangeCategory: acc.isOwner, titleTouched: false,
                 foreignColor: t && !acc.isOwner && t.category ? t.category.color : '' };

    // A categoria pertence ao dono: quem não é dono não a vê entre as suas, por isso mostra-se a da tarefa
    $('taskCategory').querySelectorAll('option[data-foreign]').forEach(o => o.remove());
    if (t && !acc.isOwner && t.category) {
        const opt = new Option(`${t.category.name} (do dono)`, t.category_id);
        opt.dataset.foreign = '1';
        $('taskCategory').add(opt);
    }

    $('taskModalLabel').textContent = !t ? 'Nova Tarefa' : (acc.canEdit ? 'Editar Tarefa' : 'Detalhes da Tarefa');
    $('taskId').value          = t ? t.id : '';
    $('taskTitle').value       = t ? t.title : '';
    $('taskDescription').value = t ? (t.description || '') : '';
    $('taskDueDate').value     = t && t.due_date ? isoToInputValue(t.due_date) : '';
    $('taskPriority').value    = t ? t.priority : 'medium';
    $('taskStatus').value      = t ? t.status : 'pending';
    $('taskCategory').value    = t ? (t.category_id || '') : '';

    // Aviso de partilha / só leitura
    const note = $('taskModalNote');
    note.hidden = !t || acc.isOwner;
    note.className = 'form-note' + (acc.canEdit ? '' : ' is-readonly');
    note.innerHTML = acc.canEdit
        ? '<i class="bi bi-people"></i> Tarefa partilhada consigo. Pode editá-la, mas não excluí-la.'
        : '<i class="bi bi-lock"></i> Tem acesso somente leitura a esta tarefa.';

    const hint = $('taskCategoryHint');
    hint.hidden = acc.isOwner;
    hint.textContent = 'A categoria só pode ser alterada pelo dono da tarefa.';

    // Campos desativados para quem não pode editar (e a categoria para quem não é dono)
    document.querySelectorAll('#taskModal .modal-body input:not([type=hidden]), #taskModal .modal-body textarea, #taskModal .modal-body select')
        .forEach(el => { if (el.id !== 'taskPriority' && el.id !== 'taskStatus') el.disabled = taskForm.readOnly; });
    $('taskCategory').disabled = taskForm.readOnly || !taskForm.canChangeCategory;
    $('taskSaveBtn').hidden    = taskForm.readOnly;
    $('taskCancelBtn').textContent = taskForm.readOnly ? 'Fechar' : 'Cancelar';

    syncTaskSegments();
    updateCategoryDot();
    renderTaskTitleState();
    modal.show();
}

// ── Seletores segmentados (prioridade/estado): o <select> oculto continua a ser a fonte do valor ──
const SEGMENT_SELECT = { taskPriorityRadio: 'taskPriority', taskStatusRadio: 'taskStatus' };

function onSegmentChange(radio) {
    document.getElementById(SEGMENT_SELECT[radio.name]).value = radio.value;
}

function syncTaskSegments() {
    Object.entries(SEGMENT_SELECT).forEach(([name, selectId]) => {
        const value = document.getElementById(selectId).value;
        document.querySelectorAll(`input[name="${name}"]`).forEach(r => { r.checked = r.value === value; });
    });
}

function updateCategoryDot() {
    const sel = document.getElementById('taskCategory');
    const dot = document.getElementById('taskCategoryDot');
    const foreign = sel.selectedOptions[0] && sel.selectedOptions[0].dataset.foreign;
    const cat = foreign ? null : categoriesById[String(sel.value)];
    const color = foreign ? taskForm.foreignColor : (cat ? cat.color : '');
    dot.style.background = color;
    dot.classList.toggle('has-color', !!color);
}

// ── Validação do título ──
function taskTitleError() {
    const len = document.getElementById('taskTitle').value.trim().length;
    if (!len) return 'O título é obrigatório.';
    if (len > TITLE_MAX) return `O título deve ter no máximo ${TITLE_MAX} caracteres (tem ${len}).`;
    return '';
}

function renderTaskTitleState() {
    const input = document.getElementById('taskTitle');
    const len   = input.value.trim().length;
    const err   = taskTitleError();
    // Mostra o erro depois de a pessoa tocar no campo ou tentar guardar; o excesso de caracteres, logo
    const show  = !!err && (taskForm.titleTouched || len > TITLE_MAX);
    const count = document.getElementById('taskTitleCount');
    count.textContent = `${len}/${TITLE_MAX}`;
    count.classList.toggle('is-over', len > TITLE_MAX);
    document.getElementById('taskTitleError').textContent = show ? err : '';
    input.classList.toggle('is-invalid', show);
    input.setAttribute('aria-invalid', show ? 'true' : 'false');
}

function onTaskTitleInput() { renderTaskTitleState(); }
function onTaskTitleBlur()  { taskForm.titleTouched = true; renderTaskTitleState(); }

async function saveTask() {
    if (taskForm.readOnly) return;
    const taskId = document.getElementById('taskId').value;

    taskForm.titleTouched = true;
    renderTaskTitleState();
    if (taskTitleError()) { document.getElementById('taskTitle').focus(); return; }

    const body = {
        title:       document.getElementById('taskTitle').value.trim(),
        description: document.getElementById('taskDescription').value.trim(),
        due_date:    document.getElementById('taskDueDate').value
                     ? inputValueToIso(document.getElementById('taskDueDate').value) : null,
        priority:    document.getElementById('taskPriority').value,
        status:      document.getElementById('taskStatus').value,
    };
    // Só o dono altera a categoria: omitir o campo evita apagar a categoria do dono por engano
    if (taskForm.canChangeCategory) body.category_id = document.getElementById('taskCategory').value || null;

    const method   = taskId ? 'PUT' : 'POST';
    const endpoint = taskId ? `/tasks/${taskId}` : '/tasks';

    const saveBtn = document.getElementById('taskSaveBtn');
    saveBtn.disabled = true;      // evita envio duplo
    try {
        const res = await apiRequest(endpoint, { method, body: JSON.stringify(body) });
        if (!res) return;

        if (res.ok) {
            bootstrap.Modal.getInstance(document.getElementById('taskModal')).hide();
            toast(taskId ? 'Tarefa atualizada!' : 'Tarefa criada!', 'success');
            await loadAll();
        } else {
            const data = await res.json().catch(() => ({}));
            toast(data.message || 'Erro ao salvar tarefa', 'error');
        }
    } finally {
        saveBtn.disabled = false;
    }
}

async function toggleStatus(taskId, checked) {
    const status = checked ? 'completed' : 'pending';
    const res = await apiRequest(`/tasks/${taskId}`, {
        method: 'PUT',
        body: JSON.stringify({ status }),
    });
    if (res && res.ok) {
        if (checked) toast('Tarefa concluída! 🎉', 'success');
        await loadAll();
    } else if (res) {
        // O servidor recusou (ex.: acesso somente leitura): informa e repõe a lista
        const data = await res.json().catch(() => ({}));
        toast(data.message || 'Não foi possível alterar o estado da tarefa', 'error');
        await loadAll();
    }
}

async function deleteTask(taskId) {
    if (!confirm('Excluir esta tarefa? Esta ação não pode ser desfeita.')) return;
    const res = await apiRequest(`/tasks/${taskId}`, { method: 'DELETE' });
    if (res && res.ok) {
        toast('Tarefa excluída', 'info');
        await loadAll();
        loadNotifications();
    }
}

// ══════════════════════════════════════════════
// CRUD CATEGORIAS
// ══════════════════════════════════════════════
function openCategoryModal(id = null, name = '', color = '#667eea') {
    document.getElementById('categoryId').value    = id || '';
    document.getElementById('categoryName').value  = name;
    document.getElementById('categoryColor').value = color;
    document.getElementById('categoryModalTitle').textContent = id ? 'Editar Categoria' : 'Nova Categoria';
    setupColorPicker(color);
    new bootstrap.Modal(document.getElementById('categoryModal')).show();
}

async function saveCategory() {
    const id    = document.getElementById('categoryId').value;
    const name  = document.getElementById('categoryName').value.trim();
    const color = document.getElementById('categoryColor').value;

    if (!name) { toast('Nome é obrigatório', 'error'); return; }

    const method   = id ? 'PUT' : 'POST';
    const endpoint = id ? `/categories/${id}` : '/categories';

    const res = await apiRequest(endpoint, {
        method,
        body: JSON.stringify({ name, color }),
    });
    if (!res) return;

    if (res.ok) {
        bootstrap.Modal.getInstance(document.getElementById('categoryModal')).hide();
        toast(id ? 'Categoria atualizada!' : 'Categoria criada!', 'success');
        await loadCategories();
        await refreshActiveView();
    } else {
        const data = await res.json();
        toast(data.message || 'Erro ao salvar categoria', 'error');
    }
}

async function deleteCategory(id, name) {
    if (!confirm(`Excluir a categoria "${name}"?\nAs tarefas dessa categoria não serão excluídas.`)) return;

    const res = await apiRequest(`/categories/${id}`, { method: 'DELETE' });
    if (res && res.ok) {
        if (state.category == id) { state.category = ''; }
        toast('Categoria excluída!', 'info');
        await loadCategories();
        await refreshActiveView();
    } else {
        toast('Erro ao excluir categoria', 'error');
    }
}

// ══════════════════════════════════════════════
// NOTIFICAÇÕES
// ══════════════════════════════════════════════
let notifUnread = 0;   // não lidas conforme a última resposta do servidor (ajustado localmente ao remover)

function setNotifBadge(count) {
    notifUnread = Math.max(0, count);
    const badge = document.getElementById('notif-badge');
    if (notifUnread > 0) {
        badge.textContent  = notifUnread > 99 ? '99+' : notifUnread;
        badge.style.display = '';
    } else {
        badge.style.display = 'none';
    }
}

async function loadNotifications() {
    const res = await apiRequest('/notifications?limit=20');
    if (!res || !res.ok) return;
    const data = await res.json();

    setNotifBadge(data.unread_count);
    renderNotifications(data.notifications);
}

const NOTIF_EMPTY_HTML = '<div class="text-center text-muted py-3" style="font-size:.875rem;">Nenhuma notificação</div>';

function renderNotifications(notifs) {
    const list = document.getElementById('notif-list');
    if (!notifs.length) {
        list.innerHTML = NOTIF_EMPTY_HTML;
        return;
    }

    const icons = {
        task_created:    'bi-plus-circle',
        task_completed:  'bi-check-circle',
        task_deleted:    'bi-trash',
        task_assignment: 'bi-people',
        due_date_reminder:'bi-alarm',
        task_permission_changed: 'bi-person-gear',
        task_access_removed:     'bi-person-dash',
    };

    list.innerHTML = notifs.map(n => `
        <div class="notif-item ${n.is_read ? '' : 'unread'}" onclick="readNotif(${n.id}, this)">
            <div class="notif-icon">
                <i class="bi ${icons[n.type] || 'bi-bell'}"></i>
            </div>
            <div class="notif-content">
                <div class="notif-title">${esc(n.title)}</div>
                <div class="notif-msg">${esc(n.message)}</div>
                <div class="notif-time">${relTimeHtml(n.created_at)}</div>
            </div>
            <button type="button" class="notif-delete" title="Remover notificação"
                    aria-label="Remover notificação: ${esc(n.title)}"
                    onclick="deleteNotif(${n.id}, this, event)">
                <i class="bi bi-trash"></i>
            </button>
        </div>`).join('');
}

// Remove só esta notificação (independente de marcá-la como lida). Remove da lista de imediato e
// repõe o item se o servidor falhar; 404 = já tinha sido removida, conta como sucesso.
async function deleteNotif(id, btn, event) {
    if (event) event.stopPropagation();          // não dispara "marcar como lida" nem fecha o painel
    const item = btn.closest('.notif-item');
    if (!item || item.classList.contains('is-removing')) return;

    const wasUnread = item.classList.contains('unread');
    const neighbour = item.nextElementSibling || item.previousElementSibling;
    item.classList.add('is-removing');
    btn.disabled = true;

    const res = await apiRequest(`/notifications/${id}`, { method: 'DELETE' });

    if (res && (res.ok || res.status === 404)) {
        item.remove();
        if (wasUnread) setNotifBadge(notifUnread - 1);
        const list = document.getElementById('notif-list');
        if (!list.querySelector('.notif-item')) list.innerHTML = NOTIF_EMPTY_HTML;
        // devolve o foco ao teclado: próximo item, ou o botão de marcar tudo como lido
        const next = neighbour && neighbour.isConnected && neighbour.querySelector('.notif-delete');
        (next || document.querySelector('#notif-panel .notif-header .btn-text')).focus();
        return;
    }

    // Falhou: o item volta. Falha de rede e 429 já foram avisadas pelo apiRequest.
    item.classList.remove('is-removing');
    btn.disabled = false;
    if (res && res.status !== 429) toast('Não foi possível remover a notificação.', 'error');
    btn.focus();
}

function toggleNotifPanel() {
    const panel = document.getElementById('notif-panel');
    if (!notifOpen) closeTaskPanel(false);   // não sobrepor os dois painéis
    notifOpen = !notifOpen;
    panel.style.display = notifOpen ? 'flex' : 'none';
    if (notifOpen) loadNotifications();
}

function closeNotifPanel() {
    notifOpen = false;
    document.getElementById('notif-panel').style.display = 'none';
}

async function readNotif(id, el) {
    el.classList.remove('unread');
    await apiRequest(`/notifications/${id}/read`, { method: 'POST' });
    loadNotifications();
}

async function markAllRead() {
    await apiRequest('/notifications/read-all', { method: 'POST' });
    toast('Notificações marcadas como lidas', 'info');
    loadNotifications();
}

// ══════════════════════════════════════════════
// FILTROS E NAVEGAÇÃO
// ══════════════════════════════════════════════
const FILTER_TITLES = {
    all: 'Todas as Tarefas', pending: 'Pendentes',
    in_progress: 'Em Progresso', completed: 'Concluídas',
};

function setFilter(filter) {
    state.filter = filter;
    state.page   = 1;
    showView('tasks', FILTER_TITLES[filter], filter);
    loadTasks();
}

// Alterna entre a vista do dashboard e a lista de tarefas.
// `filter` indica qual item da navegação fica ativo na vista de lista.
function showView(view, title, filter) {
    const isDash = view === 'dashboard';
    state.view = isDash ? 'dashboard' : 'tasks';
    document.getElementById('dashboard-view').hidden = !isDash;
    document.getElementById('tasks-view').hidden     = isDash;
    document.getElementById('page-title').textContent = isDash ? 'Dashboard' : (title || 'Tarefas');

    document.querySelectorAll('.nav-link').forEach(a => {
        const active = isDash ? a.dataset.view === 'dashboard' : a.dataset.filter === filter;
        a.classList.toggle('active', active);
    });

    if (isDash) loadDashboard();
    if (window.innerWidth < 768 && document.getElementById('sidebar').classList.contains('open')) toggleSidebar();
}

function filterByCategory(catId) {
    state.category = state.category == catId ? '' : catId;
    state.page = 1;
    showView('tasks', FILTER_TITLES[state.filter], state.filter);
    document.getElementById('filter-category').value = state.category;
    loadTasks();
    loadCategories();
}

function onSearch(val) {
    document.getElementById('search-clear').hidden = !val;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
        state.search = val.trim();
        state.page   = 1;
        loadTasks();
    }, 350);
}

function clearSearch() {
    const input = document.getElementById('search-input');
    if (!input.value && !state.search) return;
    input.value = '';
    document.getElementById('search-clear').hidden = true;
    clearTimeout(searchTimer);
    state.search = '';
    state.page   = 1;
    loadTasks();
    input.focus();
}

function onPriorityChange(val) {
    state.priority = val;
    state.page     = 1;
    loadTasks();
}

function onCategoryChange(val) {
    state.category = val;
    state.page     = 1;
    loadTasks();
    loadCategories();
}

// Direção padrão por campo: texto/prazo/prioridade crescente; datas de criação/atualização decrescente.
const DEFAULT_ORDER = { created_at: 'desc', updated_at: 'desc', due_date: 'asc', priority: 'asc', title: 'asc' };

function onSortChange(val) {
    state.sortBy = val;
    state.order  = DEFAULT_ORDER[val] || 'desc';
    state.page   = 1;
    syncSortControls();
    loadTasks();
}

function toggleSortOrder() {
    state.order = state.order === 'asc' ? 'desc' : 'asc';
    state.page  = 1;
    syncSortControls();
    loadTasks();
}

function syncSortControls() {
    document.getElementById('sort-by').value = state.sortBy;
    const asc = state.order === 'asc';
    const btn = document.getElementById('sort-order');
    const label = asc ? 'Ordem crescente (clique para inverter)' : 'Ordem decrescente (clique para inverter)';
    btn.title = label;
    btn.setAttribute('aria-label', label);
    document.getElementById('sort-order-icon').className = asc ? 'bi bi-sort-up' : 'bi bi-sort-down';
}

// Quais filtros estão ativos (a ordenação não conta como filtro)
function activeFilters() {
    const list = [];
    if (state.filter !== 'all') list.push({ kind: 'status', label: FILTER_TITLES[state.filter] });
    if (state.priority)         list.push({ kind: 'priority', label: `Prioridade: ${priorityLabel(state.priority)}` });
    if (state.category) {
        const c = categoriesById[String(state.category)];
        list.push({ kind: 'category', label: `Categoria: ${c ? c.name : '…'}` });
    }
    if (state.search)           list.push({ kind: 'search', label: `Busca: “${state.search}”` });
    return list;
}

function renderFilterChips() {
    const filters = activeFilters();
    document.getElementById('filter-chips').innerHTML = filters.map(f => `
        <span class="filter-chip">${esc(f.label)}
            <button type="button" onclick="removeFilter('${f.kind}')" aria-label="Remover filtro: ${esc(f.label)}" title="Remover filtro">
                <i class="bi bi-x"></i>
            </button>
        </span>`).join('');
    document.getElementById('clear-filters').hidden = !filters.length;
}

function renderResultsCount(total) {
    const n = total ?? 0;
    document.getElementById('results-count').textContent = `${n} ${n === 1 ? 'tarefa' : 'tarefas'}`;
}

function removeFilter(kind) {
    if (kind === 'status')   { setFilter('all'); return; }
    if (kind === 'search')   { clearSearch(); return; }
    if (kind === 'priority') {
        state.priority = '';
        document.getElementById('filter-priority').value = '';
    } else if (kind === 'category') {
        state.category = '';
        document.getElementById('filter-category').value = '';
        loadCategories();
    }
    state.page = 1;
    loadTasks();
}

function clearFilters() {
    clearTimeout(searchTimer);
    state.search = ''; state.priority = ''; state.category = ''; state.filter = 'all'; state.page = 1;
    document.getElementById('search-input').value = '';
    document.getElementById('search-clear').hidden = true;
    document.getElementById('filter-priority').value = '';
    document.getElementById('filter-category').value = '';
    showView('tasks', FILTER_TITLES.all, 'all');
    loadTasks();
    loadCategories();
}

function changePage(dir) {
    state.page += dir;
    loadTasks();
    window.scrollTo({ top: 0, behavior: 'smooth' });
}

// ══════════════════════════════════════════════
// PAINEL LATERAL DE DETALHES
// Usa os dados já carregados na lista (sem pedidos extras). As permissões aqui só decidem
// o que mostrar: quem decide de fato é o servidor (403 em editar/excluir sem permissão).
// ══════════════════════════════════════════════
function taskAccess(t) {
    const me      = currentUser && currentUser.id;
    const isOwner = t.user_id === me;
    const mine    = (t.collaborators || []).find(c => c.user_id === me);
    return {
        isOwner,
        isShared:  !isOwner,
        canEdit:   isOwner || (!!mine && mine.role === 'editor'),
        canDelete: isOwner,
    };
}

const COLLAB_ROLE_LABEL = { editor: 'Editor', viewer: 'Leitor' };

// Validação local do email de partilha (o servidor valida de novo). Devolve '' se estiver ok.
function shareEmailError(email, task, user) {
    if (!email) return 'Indique o email do utilizador.';
    if (email.length > 120 || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) return 'Indique um email válido.';
    if (user && user.email && email === String(user.email).toLowerCase()) return 'Já é o dono desta tarefa.';
    if ((task.collaborators || []).some(c => String(c.email).toLowerCase() === email)) {
        return 'Este utilizador já tem acesso a esta tarefa.';
    }
    return '';
}

// Mensagem mostrada quando o servidor recusa adicionar um colaborador.
// 404 cobre email inexistente e conta inativa: a resposta é neutra para não revelar qual dos casos é.
function shareErrorMessage(status, serverMessage) {
    if (status === 404) return 'Não foi possível partilhar com este email. Confirme o endereço ou tente outro.';
    if ((status === 400 || status === 403) && serverMessage) return serverMessage;
    if (status === 429) return 'Muitas requisições. Aguarde alguns minutos e tente novamente.';
    return 'Não foi possível concluir a operação. Tente novamente.';
}

// Nome do dono a mostrar (o servidor envia owner.username; nunca se usa o email como substituto)
const OWNER_UNKNOWN = 'Proprietário desconhecido';
function ownerName(t) {
    const name = t && t.owner && typeof t.owner.username === 'string' ? t.owner.username.trim() : '';
    return name || '';
}
function ownerLabel(t, user) {
    if (t.user_id === (user && user.id)) return `Você${user.username ? ` (${user.username})` : ''}`;
    return ownerName(t) || OWNER_UNKNOWN;
}

// Indicadores "Partilhada" / "Só leitura" (vazio para o dono)
function accessBadges(acc, t) {
    if (acc.isOwner) return '';
    const by = t && ownerName(t);
    return `<span class="badge-share"${by ? ` title="Partilhada por ${esc(by)}"` : ''}><i class="bi bi-people"></i>Partilhada</span>`
         + (acc.canEdit ? '' : `<span class="badge-share readonly"><i class="bi bi-lock"></i>Só leitura</span>`);
}

function onTaskCardClick(e, taskId) {
    if (e.target.closest('button, input, a, select, label')) return;   // controlos próprios do cartão
    if (window.getSelection && window.getSelection().toString()) return; // usuário está selecionando texto
    openTaskPanel(taskId, e.currentTarget.querySelector('.task-title-btn'));
}

function openTaskPanel(taskId, opener) {
    const t = tasksById[taskId];
    if (!t) return;
    if (notifOpen) closeNotifPanel();

    if (panelTaskId !== taskId) shareFormOpen = false;
    panelTaskId = taskId;
    panelOpener = opener || document.activeElement;
    renderTaskPanel(t);

    document.getElementById('task-panel').classList.add('open');
    document.getElementById('task-panel').setAttribute('aria-hidden', 'false');
    document.getElementById('task-panel-backdrop').classList.add('open');
    document.body.classList.add('panel-open');
    // Só é possível focar depois de o painel ficar visível (a visibilidade muda no início da
    // transição, mas em alguns navegadores só no frame seguinte): tenta de novo ao fim da animação.
    const focusClose = () => {
        const btn = document.getElementById('task-panel-close');
        if (panelTaskId === taskId && document.activeElement !== btn) btn.focus();
    };
    requestAnimationFrame(focusClose);
    setTimeout(focusClose, 250);
}

function closeTaskPanel(restoreFocus = true) {
    if (panelTaskId === null) return;
    const id = panelTaskId;
    panelTaskId = null;
    panelSnapshot = '';
    shareFormOpen = false;

    document.getElementById('task-panel').classList.remove('open');
    document.getElementById('task-panel').setAttribute('aria-hidden', 'true');
    document.getElementById('task-panel-backdrop').classList.remove('open');
    document.body.classList.remove('panel-open');

    if (restoreFocus) {
        // A lista pode ter sido redesenhada: procura o cartão atual antes de usar o elemento guardado
        const target = document.querySelector(`.task-card[data-id="${id}"] .task-title-btn`)
            || (panelOpener && document.contains(panelOpener) ? panelOpener : null);
        if (target) target.focus();
    }
    panelOpener = null;
}

// Depois de a lista ser recarregada: atualiza o painel se a tarefa mudou, fecha se desapareceu
function syncPanelWithTasks() {
    if (panelTaskId === null) return;
    const t = tasksById[panelTaskId];
    if (!t) { closeTaskPanel(false); return; }
    if (JSON.stringify(t) !== panelSnapshot) renderTaskPanel(t);
}

function editFromPanel() {
    const id = panelTaskId;
    if (id === null) return;
    closeTaskPanel(false);   // o modal assume o foco
    openTaskModal(id);
}

async function deleteFromPanel() {
    const id = panelTaskId;
    if (id === null) return;
    await deleteTask(id);    // se for excluída, o recarregamento da lista fecha o painel
}

function renderTaskPanel(t) {
    panelSnapshot = JSON.stringify(t);
    const acc  = taskAccess(t);
    const due  = dueInfo(t);
    const done = t.status === 'completed';

    document.getElementById('task-panel-title').textContent = t.title;

    const owner = esc(ownerLabel(t, currentUser));
    const by    = ownerName(t);
    const access = acc.isOwner ? '' : `
        <div class="panel-note">
            <i class="bi bi-people"></i>
            <span>${by ? `Tarefa partilhada por <strong>${esc(by)}</strong>` : 'Tarefa partilhada consigo'} · ${acc.canEdit ? 'pode editar' : 'só leitura'}</span>
        </div>`;

    const row = (label, value) => `<div class="detail-row"><dt>${label}</dt><dd>${value}</dd></div>`;
    const when = iso => iso ? `${formatDate(iso)} <span class="detail-rel">· ${relTimeHtml(iso)}</span>` : '—';

    document.getElementById('task-panel-body').innerHTML = `
        <div class="detail-badges">
            <span class="badge-status ${t.status}">${statusLabel(t.status)}</span>
            <span class="badge-priority ${t.priority}">${priorityLabel(t.priority)}</span>
            ${t.category ? `<span class="badge-cat" style="background:${t.category.color}">${esc(t.category.name)}</span>` : ''}
            ${t.is_overdue ? '<span class="task-due is-overdue"><i class="bi bi-exclamation-circle"></i>Atrasada</span>' : ''}
            ${accessBadges(acc, t)}
        </div>
        ${access}
        <section class="detail-section">
            <h3>Descrição</h3>
            ${t.description
                ? `<p class="detail-desc">${esc(t.description)}</p>`
                : '<p class="detail-empty">Sem descrição.</p>'}
        </section>
        <section class="detail-section">
            <h3>Informações</h3>
            <dl class="detail-list">
                ${row('Prazo', due
                    ? `<span class="task-due ${due.cls}"><i class="bi bi-${due.icon}"></i>${due.label}</span>`
                    : '<span class="detail-empty">Sem prazo</span>')}
                ${row('Estado', statusLabel(t.status))}
                ${row('Prioridade', priorityLabel(t.priority))}
                ${row('Categoria', t.category
                    ? `<span class="cat-dot" style="background:${t.category.color}"></span> ${esc(t.category.name)}`
                    : '<span class="detail-empty">Sem categoria</span>')}
                ${row('Dono', owner)}
                ${row('Criada', when(t.created_at))}
                ${row('Atualizada', when(t.updated_at))}
            </dl>
        </section>
        <section class="detail-section" id="panel-collabs">${collabsSectionHtml(t)}</section>`;

    const actions = [];
    if (acc.canEdit)   actions.push('<button type="button" class="btn-secondary-sm" onclick="editFromPanel()"><i class="bi bi-pencil"></i> Editar</button>');
    if (acc.canDelete) actions.push('<button type="button" class="btn-secondary-sm danger" onclick="deleteFromPanel()"><i class="bi bi-trash"></i> Excluir</button>');
    document.getElementById('task-panel-foot').innerHTML = actions.join('');
    document.getElementById('task-panel-foot').hidden = !actions.length;
    document.getElementById('task-panel-body').scrollTop = 0;
}

// ── Gestão de colaboradores (só o dono vê os controlos; o servidor valida cada operação) ──
let shareFormOpen = false;   // formulário "Partilhar tarefa" aberto no painel

function collabsSectionHtml(t) {
    const acc     = taskAccess(t);
    const collabs = t.collaborators || [];
    const nameOf  = c => c.username || c.email;
    const initial = c => esc(String(nameOf(c) || '?').slice(0, 2).toUpperCase());

    const head = `
        <div class="detail-section-head">
            <h3>Colaboradores</h3>
            ${acc.isOwner ? `
            <button type="button" class="btn-text" id="share-toggle" aria-expanded="${shareFormOpen}"
                    aria-controls="share-form" onclick="toggleShareForm()">
                <i class="bi bi-person-plus"></i> Partilhar tarefa
            </button>` : ''}
        </div>`;

    const form = acc.isOwner && shareFormOpen ? `
        <form class="share-form" id="share-form" onsubmit="submitShare(event)" novalidate>
            <label class="form-label" for="share-email">Email do utilizador</label>
            <input type="email" class="form-control" id="share-email" placeholder="nome@exemplo.com"
                   autocomplete="off" autocapitalize="off" spellcheck="false" maxlength="120"
                   aria-describedby="share-error" oninput="clearShareError()">
            <span class="form-label d-block mt-3" id="share-role-label">Perfil</span>
            <div class="segmented" role="radiogroup" aria-labelledby="share-role-label">
                <label class="segment seg-in_progress"><input type="radio" name="shareRoleRadio" value="editor"><span>Editor</span></label>
                <label class="segment"><input type="radio" name="shareRoleRadio" value="viewer" checked><span>Leitor</span></label>
            </div>
            <div class="form-text">Editor altera a tarefa e o estado; Leitor só consulta. Nenhum pode excluí-la.</div>
            <div class="field-error" id="share-error" role="alert"></div>
            <div class="share-actions">
                <button type="submit" class="btn-primary-sm" id="share-submit">Adicionar</button>
                <button type="button" class="btn-secondary-sm" onclick="toggleShareForm(false)">Cancelar</button>
            </div>
        </form>` : '';

    if (!collabs.length) {
        return head + form + `<p class="detail-empty">${acc.isOwner ? 'Ainda não partilhou esta tarefa.' : 'Sem colaboradores.'}</p>`;
    }

    const ownerRow = acc.isOwner ? `
        <li>
            <span class="user-avatar collab-avatar">${esc(userInitials)}</span>
            <span class="collab-info"><span class="collab-name">Você</span></span>
            <span class="collab-role">Dono</span>
        </li>` : '';

    const rows = collabs.map(c => `
        <li data-user-id="${c.user_id}">
            <span class="user-avatar collab-avatar">${initial(c)}</span>
            <span class="collab-info">
                <span class="collab-name">${esc(nameOf(c))}</span>
                ${c.username && c.email ? `<span class="collab-email">${esc(c.email)}</span>` : ''}
            </span>
            ${acc.isOwner ? `
            <span class="collab-controls">
                <select class="form-select form-select-sm collab-role-select" aria-label="Perfil de ${esc(nameOf(c))}"
                        onchange="changeCollaboratorRole(${c.user_id}, this)">
                    <option value="editor" ${c.role === 'editor' ? 'selected' : ''}>Editor</option>
                    <option value="viewer" ${c.role !== 'editor' ? 'selected' : ''}>Leitor</option>
                </select>
                <button type="button" class="btn-icon danger" title="Remover acesso"
                        aria-label="Remover acesso de ${esc(nameOf(c))}" onclick="removeCollaborator(${c.user_id})">
                    <i class="bi bi-person-dash"></i>
                </button>
            </span>` : `<span class="collab-role">${COLLAB_ROLE_LABEL[c.role] || 'Leitor'}</span>`}
        </li>`).join('');

    return head + form + `<ul class="collab-list">${ownerRow}${rows}</ul>`;
}

// Redesenha só a secção (o resto do painel e o foco mantêm-se); preserva o que foi escrito no formulário
function refreshCollabsSection(t) {
    const box = document.getElementById('panel-collabs');
    if (!box || !t) return;
    const prevEmail = document.getElementById('share-email');
    const draft = prevEmail ? { email: prevEmail.value, role: (document.querySelector('input[name="shareRoleRadio"]:checked') || {}).value } : null;
    box.innerHTML = collabsSectionHtml(t);
    if (draft && document.getElementById('share-email')) {
        document.getElementById('share-email').value = draft.email;
        const r = document.querySelector(`input[name="shareRoleRadio"][value="${draft.role}"]`);
        if (r) r.checked = true;
    }
}

function afterCollabChange(t) {
    panelSnapshot = JSON.stringify(t);   // o painel já reflete esta versão: evita redesenho completo
    refreshCollabsSection(t);
}

function toggleShareForm(force) {
    const t = tasksById[panelTaskId];
    if (!t || !taskAccess(t).isOwner) return;
    shareFormOpen = typeof force === 'boolean' ? force : !shareFormOpen;
    refreshCollabsSection(t);
    const target = document.getElementById(shareFormOpen ? 'share-email' : 'share-toggle');
    if (target) target.focus();
}

function showShareError(msg) {
    const el = document.getElementById('share-error');
    const input = document.getElementById('share-email');
    if (el) el.textContent = msg;
    if (input) { input.classList.toggle('is-invalid', !!msg); input.setAttribute('aria-invalid', msg ? 'true' : 'false'); }
}
function clearShareError() { showShareError(''); }

// Bloqueia os controlos enquanto uma operação está a decorrer (evita pedidos duplicados)
function setCollabBusy(busy, submitLabel) {
    const box = document.getElementById('panel-collabs');
    if (!box) return;
    box.classList.toggle('is-busy', busy);
    if (busy) box.setAttribute('aria-busy', 'true'); else box.removeAttribute('aria-busy');
    box.querySelectorAll('button, select, input').forEach(el => { el.disabled = busy; });
    const submit = document.getElementById('share-submit');
    if (submit && submitLabel) submit.textContent = busy ? submitLabel : 'Adicionar';
}

async function readJson(res) { try { return await res.json(); } catch { return {}; } }

// Só atualiza a interface se o painel ainda mostrar a mesma tarefa
const panelStillShows = id => panelTaskId === id;

async function submitShare(e) {
    e.preventDefault();
    const taskId = panelTaskId;
    const t = tasksById[taskId];
    if (!t || !taskAccess(t).isOwner) return;

    const input = document.getElementById('share-email');
    const email = input.value.trim().toLowerCase();
    const role  = (document.querySelector('input[name="shareRoleRadio"]:checked') || {}).value || 'viewer';

    const invalid = shareEmailError(email, t, currentUser);
    if (invalid) { showShareError(invalid); input.focus(); return; }
    showShareError('');

    setCollabBusy(true, 'A adicionar…');
    const res = await apiRequest(`/tasks/${taskId}/collaborators`, { method: 'POST', body: JSON.stringify({ email, role }) });
    if (!panelStillShows(taskId)) return;
    setCollabBusy(false);

    if (!res) return;   // falha de rede: o apiRequest já avisou
    const data = await readJson(res);
    if (res.ok && data.collaborator) {
        const cur = tasksById[taskId];
        cur.collaborators = [...(cur.collaborators || []), data.collaborator];
        shareFormOpen = true;
        const keepRole = role;
        afterCollabChange(cur);
        document.getElementById('share-email').value = '';
        const r = document.querySelector(`input[name="shareRoleRadio"][value="${keepRole}"]`);
        if (r) r.checked = true;
        document.getElementById('share-email').focus();
        toast(`${data.collaborator.username || data.collaborator.email} agora tem acesso (${COLLAB_ROLE_LABEL[data.collaborator.role]}).`, 'success');
    } else {
        showShareError(shareErrorMessage(res.status, data.message));
        document.getElementById('share-email').focus();
    }
}

async function changeCollaboratorRole(userId, selectEl) {
    const taskId = panelTaskId;
    const t = tasksById[taskId];
    const collab = t && (t.collaborators || []).find(c => c.user_id === userId);
    if (!collab || !taskAccess(t).isOwner) return;

    const previous = collab.role;
    const role = selectEl.value;
    if (role === previous) return;

    setCollabBusy(true);
    const res = await apiRequest(`/tasks/${taskId}/collaborators/${userId}`, { method: 'PUT', body: JSON.stringify({ role }) });
    if (!panelStillShows(taskId)) return;
    setCollabBusy(false);

    const name = collab.username || collab.email;
    if (res && res.ok) {
        const data = await readJson(res);
        const cur = tasksById[taskId];
        cur.collaborators = cur.collaborators.map(c => (c.user_id === userId ? { ...c, ...(data.collaborator || { role }) } : c));
        panelSnapshot = JSON.stringify(cur);   // o seletor já mostra o valor novo (e mantém o foco)
        toast(`Perfil de ${name} alterado para ${COLLAB_ROLE_LABEL[role]}.`, 'success');
    } else {
        selectEl.value = previous;               // repõe o valor anterior
        if (res) {
            const data = await readJson(res);
            toast(res.status === 404 ? 'Esta partilha já não existe.' : (data.message || 'Não foi possível alterar o perfil.'), 'error');
            if (res.status === 404) loadTasks();  // volta a sincronizar a lista
        }
        selectEl.focus();
    }
}

async function removeCollaborator(userId) {
    const taskId = panelTaskId;
    const t = tasksById[taskId];
    const collab = t && (t.collaborators || []).find(c => c.user_id === userId);
    if (!collab || !taskAccess(t).isOwner) return;

    const name = collab.username || collab.email;
    if (!confirm(`Remover o acesso de ${name}?\nDeixará de ver esta tarefa.`)) return;

    setCollabBusy(true);
    const res = await apiRequest(`/tasks/${taskId}/collaborators/${userId}`, { method: 'DELETE' });
    if (!panelStillShows(taskId)) return;
    setCollabBusy(false);

    if (res && (res.ok || res.status === 404)) {   // 404: já tinha sido removido noutro lado
        const cur = tasksById[taskId];
        cur.collaborators = (cur.collaborators || []).filter(c => c.user_id !== userId);
        afterCollabChange(cur);
        toast(res.ok ? `Acesso de ${name} removido.` : 'Esta partilha já não existia.', res.ok ? 'success' : 'info');
        const next = document.getElementById('share-toggle');
        if (next) next.focus();
    } else if (res) {
        const data = await readJson(res);
        toast(data.message || 'Não foi possível remover o acesso.', 'error');
    }
}

// Esc fecha o painel (a menos que um modal Bootstrap esteja aberto, que trata do seu próprio Esc);
// Tab fica preso dentro do painel enquanto ele está aberto.
document.addEventListener('keydown', e => {
    if (panelTaskId === null) return;
    if (document.querySelector('.modal.show')) return;
    if (e.key === 'Escape') { e.preventDefault(); closeTaskPanel(); return; }
    if (e.key !== 'Tab') return;

    const items = [...document.querySelectorAll('#task-panel button:not([disabled]), #task-panel input:not([disabled]), #task-panel select:not([disabled]), #task-panel a[href], #task-panel [tabindex]:not([tabindex="-1"])')]
        .filter(el => el.offsetParent !== null);
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first)  { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    else if (!document.getElementById('task-panel').contains(document.activeElement)) { e.preventDefault(); first.focus(); }
});

// ══════════════════════════════════════════════
// TEMA (claro/escuro) — o valor inicial é aplicado por um script no <head> do index.html
// ══════════════════════════════════════════════
function getStoredTheme() {
    try { return localStorage.getItem('theme') === 'dark' ? 'dark' : 'light'; }
    catch (e) { return 'light'; }
}

// Mantém data-theme (CSS próprio) e data-bs-theme (Bootstrap) sempre iguais.
function applyTheme(theme) {
    const t = theme === 'dark' ? 'dark' : 'light';
    document.documentElement.setAttribute('data-theme', t);
    document.documentElement.setAttribute('data-bs-theme', t);
    syncThemeButton();
}

function toggleTheme() {
    const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    try { localStorage.setItem('theme', next); } catch (e) { /* armazenamento indisponível */ }
    applyTheme(next);
}

function syncThemeButton() {
    const dark = document.documentElement.getAttribute('data-theme') === 'dark';
    const btn  = document.getElementById('theme-toggle');
    if (!btn) return;
    const label = dark ? 'Mudar para tema claro' : 'Mudar para tema escuro';
    btn.setAttribute('aria-pressed', String(dark));
    btn.setAttribute('aria-label', label);
    btn.title = label;
}

// Reaplica o tema guardado quando a página volta do cache (botão "voltar") ou
// quando outra aba do mesmo site o altera.
window.addEventListener('pageshow', e => { if (e.persisted) applyTheme(getStoredTheme()); });
window.addEventListener('storage',  e => { if (e.key === 'theme') applyTheme(getStoredTheme()); });

// ══════════════════════════════════════════════
// SIDEBAR MOBILE
// ══════════════════════════════════════════════
function toggleSidebar() {
    document.getElementById('sidebar').classList.toggle('open');
    document.getElementById('sidebar-overlay').classList.toggle('open');
}

// ══════════════════════════════════════════════
// PERFIL / FOTO
// A foto é guardada no banco (PUT/GET/DELETE /auth/avatar). Como <img> não envia o
// cabeçalho Authorization, ela é baixada via fetch e exibida por um blob URL.
// ══════════════════════════════════════════════
const AVATAR_TYPES           = ['image/png', 'image/jpeg', 'image/webp', 'image/gif'];
const AVATAR_MAX_INPUT_BYTES = 5 * 1024 * 1024;   // arquivo escolhido (é reduzido antes do envio)
const AVATAR_SIZE            = 256;               // lado, em px, da imagem enviada

let avatarUrl     = null;   // blob URL da foto salva
let pendingAvatar = null;   // Blob selecionado, ainda não salvo
let pendingUrl    = null;   // blob URL da prévia

function paintAvatar(el, url, initials) {
    if (!el) return;
    if (url) {
        el.style.backgroundImage = `url("${url}")`;
        el.classList.add('has-photo');
        el.textContent = '';
    } else {
        el.style.backgroundImage = '';
        el.classList.remove('has-photo');
        el.textContent = initials;
    }
}

function refreshAvatars() {
    paintAvatar(document.getElementById('user-avatar'), avatarUrl, userInitials);
    paintAvatar(document.getElementById('profile-avatar'), pendingUrl || avatarUrl, userInitials);
    const save   = document.getElementById('avatar-save');
    const remove = document.getElementById('avatar-remove');
    if (save)   save.disabled   = !pendingAvatar;
    if (remove) remove.disabled = !(currentUser && currentUser.has_avatar) || !!pendingAvatar;
}

function setAvatarUrl(url) {
    if (avatarUrl) URL.revokeObjectURL(avatarUrl);
    avatarUrl = url;
    refreshAvatars();
}

async function loadAvatar() {
    if (!currentUser || !currentUser.has_avatar) { setAvatarUrl(null); return; }
    const res = await apiRequest('/auth/avatar');
    if (res && res.ok) setAvatarUrl(URL.createObjectURL(await res.blob()));
    else setAvatarUrl(null);
}

// Atualiza os dados do usuário (o localStorage pode estar desatualizado, ex.: foto trocada em outro dispositivo)
async function syncCurrentUser() {
    const res = await apiRequest('/auth/me');
    if (!res || !res.ok) return;
    const { user } = await res.json();
    const changed = user.avatar_updated_at !== currentUser.avatar_updated_at;
    currentUser = user;
    localStorage.setItem('user', JSON.stringify(user));
    setupUser();
    if (changed) loadAvatar();
}

function clearPendingAvatar() {
    if (pendingUrl) URL.revokeObjectURL(pendingUrl);
    pendingAvatar = null;
    pendingUrl    = null;
    const input = document.getElementById('avatar-input');
    if (input) input.value = '';
}

function openProfileModal() {
    clearPendingAvatar();
    document.getElementById('profile-name').textContent  = currentUser.username || '';
    document.getElementById('profile-email').textContent = currentUser.email || '';
    refreshAvatars();
    bootstrap.Modal.getOrCreateInstance(document.getElementById('profileModal')).show();
}

// Recorta no centro e reduz para AVATAR_SIZE x AVATAR_SIZE (JPEG): envio leve e dentro do limite do servidor
function resizeImage(file) {
    return new Promise((resolve, reject) => {
        const url = URL.createObjectURL(file);
        const img = new Image();
        img.onload = () => {
            URL.revokeObjectURL(url);
            const side = Math.min(img.width, img.height);
            const canvas = document.createElement('canvas');
            canvas.width = canvas.height = AVATAR_SIZE;
            const ctx = canvas.getContext('2d');
            ctx.fillStyle = '#ffffff';
            ctx.fillRect(0, 0, AVATAR_SIZE, AVATAR_SIZE);
            ctx.drawImage(img, (img.width - side) / 2, (img.height - side) / 2, side, side, 0, 0, AVATAR_SIZE, AVATAR_SIZE);
            canvas.toBlob(b => b ? resolve(b) : reject(new Error('Falha ao processar a imagem.')), 'image/jpeg', 0.88);
        };
        img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('Não foi possível ler a imagem.')); };
        img.src = url;
    });
}

async function onAvatarPicked(input) {
    const file = input.files && input.files[0];
    if (!file) return;
    if (!AVATAR_TYPES.includes(file.type)) {
        toast('Formato inválido. Use PNG, JPEG, WebP ou GIF.', 'error');
        clearPendingAvatar(); refreshAvatars(); return;
    }
    if (file.size > AVATAR_MAX_INPUT_BYTES) {
        toast('Imagem muito grande (máximo 5 MB).', 'error');
        clearPendingAvatar(); refreshAvatars(); return;
    }
    try {
        const blob = await resizeImage(file);
        clearPendingAvatar();
        pendingAvatar = blob;
        pendingUrl    = URL.createObjectURL(blob);
        refreshAvatars();
    } catch (err) {
        toast(err.message || 'Não foi possível processar a imagem.', 'error');
        clearPendingAvatar(); refreshAvatars();
    }
}

async function saveAvatar() {
    if (!pendingAvatar) return;
    const btn = document.getElementById('avatar-save');
    btn.disabled = true;
    const res = await apiRequest('/auth/avatar', {
        method:  'PUT',
        headers: { 'Content-Type': pendingAvatar.type },
        body:    pendingAvatar,
    });
    if (!res) { refreshAvatars(); return; }
    const data = await res.json().catch(() => ({}));
    if (res.ok) {
        currentUser = data.user;
        localStorage.setItem('user', JSON.stringify(currentUser));
        clearPendingAvatar();
        await loadAvatar();
        toast('Foto atualizada!', 'success');
    } else {
        toast(data.message || 'Falha ao enviar a foto', 'error');
        refreshAvatars();
    }
}

async function removeAvatar() {
    if (!confirm('Remover sua foto de perfil?')) return;
    const res = await apiRequest('/auth/avatar', { method: 'DELETE' });
    if (!res) return;
    if (res.ok) {
        currentUser = (await res.json()).user;
        localStorage.setItem('user', JSON.stringify(currentUser));
        clearPendingAvatar();
        setAvatarUrl(null);
        toast('Foto removida', 'info');
    } else {
        toast('Falha ao remover a foto', 'error');
    }
}

// ══════════════════════════════════════════════
// LOGOUT
// ══════════════════════════════════════════════
async function doLogout() {
    stopRelativeTimeUpdates();
    try {
        // skipRefresh evita loop infinito quando a sessão já expirou (401 -> logout -> 401 ...)
        await apiRequest('/auth/logout', {
            method: 'POST',
            skipRefresh: true,
            body: JSON.stringify({ refresh_token: refreshToken }),
        });
    } catch { /* segue */ }
    localStorage.clear();
    window.location.href = '/';
}

// ══════════════════════════════════════════════
// TOAST
// ══════════════════════════════════════════════
function toast(msg, type = 'info') {
    const icons = { success: 'bi-check-circle-fill', error: 'bi-x-circle-fill', info: 'bi-info-circle-fill' };
    const wrap  = document.getElementById('toast-wrap');
    const el    = document.createElement('div');
    el.className = `toast-item ${type}`;
    el.innerHTML = `<i class="bi ${icons[type] || icons.info}"></i><span>${esc(msg)}</span>`;
    wrap.appendChild(el);
    setTimeout(() => el.remove(), 3500);
}

// ══════════════════════════════════════════════
// UTILITÁRIOS
// ══════════════════════════════════════════════
function esc(str) {
    if (!str) return '';
    return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function priorityLabel(p) {
    return { high: 'Alta', medium: 'Média', low: 'Baixa' }[p] || p;
}

function statusLabel(s) {
    return { pending: 'Pendente', in_progress: 'Em progresso', completed: 'Concluída' }[s] || s;
}

// ══════════════════════════════════════════════
// DATAS E FUSO HORÁRIO
// A API trabalha em UTC (ISO 8601 com 'Z'); a exibição usa America/Sao_Paulo.
// ══════════════════════════════════════════════
const APP_TZ = 'America/Sao_Paulo';

function parseServerDate(iso) {
    // Valores sem fuso (legado) são UTC; sem isso o navegador os trataria como horário local
    const hasZone = /(Z|[+-]\d{2}:?\d{2})$/i.test(iso);
    return new Date(hasZone || !iso.includes('T') ? iso : iso + 'Z');
}

function formatDate(iso) {
    return parseServerDate(iso).toLocaleString('pt-BR', {
        timeZone: APP_TZ, day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit',
    });
}

// Diferença (ms) entre o horário de parede de `tz` e UTC no instante `date`
function tzOffsetMs(date, tz) {
    const p = Object.fromEntries(new Intl.DateTimeFormat('en-US', {
        timeZone: tz, hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', second: '2-digit',
    }).formatToParts(date).map(x => [x.type, x.value]));
    return Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second) - Math.floor(date.getTime() / 1000) * 1000;
}

// UTC (ISO) -> valor de <input type="datetime-local"> em America/Sao_Paulo
function isoToInputValue(iso) {
    const d = parseServerDate(iso);
    return new Date(d.getTime() + tzOffsetMs(d, APP_TZ)).toISOString().slice(0, 16);
}

// valor de <input type="datetime-local"> (America/Sao_Paulo) -> UTC ISO
function inputValueToIso(value) {
    const guess = new Date(value + ':00Z');
    let utc = guess.getTime() - tzOffsetMs(guess, APP_TZ);
    utc = guess.getTime() - tzOffsetMs(new Date(utc), APP_TZ);   // reajuste na virada de horário de verão
    return new Date(utc).toISOString();
}

// ══════════════════════════════════════════════
// TEMPO RELATIVO ("há 2 minutos") — atualização automática
// ══════════════════════════════════════════════
const RELATIVE_TIME_INTERVAL_MS = 30000;
let relativeTimeTimer = null;

function plural(n, one, many) {
    return `há ${n} ${n === 1 ? one : many}`;
}

function timeAgo(iso, now = Date.now()) {
    const diff  = now - parseServerDate(iso).getTime();
    const mins  = Math.floor(diff / 60000);
    const hours = Math.floor(diff / 3600000);
    const days  = Math.floor(diff / 86400000);
    if (mins  < 1)  return 'agora mesmo';        // inclui pequenas diferenças de relógio (diff negativo)
    if (mins  < 60) return plural(mins,  'minuto', 'minutos');
    if (hours < 24) return plural(hours, 'hora',   'horas');
    if (days  < 7)  return plural(days,  'dia',    'dias');
    return parseServerDate(iso).toLocaleDateString('pt-BR', { timeZone: APP_TZ });
}

// Marcação usada nos cards e notificações; o temporizador atualiza todos os .time-ago
function relTimeHtml(iso) {
    return `<span class="time-ago" data-ts="${esc(iso)}">${timeAgo(iso)}</span>`;
}

function refreshRelativeTimes() {
    document.querySelectorAll('.time-ago[data-ts]').forEach(el => {
        const text = timeAgo(el.dataset.ts);
        if (el.textContent !== text) el.textContent = text;
    });
}

// Um único temporizador para toda a página (idempotente)
function startRelativeTimeUpdates() {
    if (relativeTimeTimer !== null) return;
    relativeTimeTimer = setInterval(refreshRelativeTimes, RELATIVE_TIME_INTERVAL_MS);
}

function stopRelativeTimeUpdates() {
    if (relativeTimeTimer === null) return;
    clearInterval(relativeTimeTimer);
    relativeTimeTimer = null;
}

// Aba oculta: pausa o temporizador; ao voltar, atualiza na hora e retoma
document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
        stopRelativeTimeUpdates();
    } else if (accessToken) {
        refreshRelativeTimes();
        startRelativeTimeUpdates();
    }
});
window.addEventListener('pagehide', stopRelativeTimeUpdates);
