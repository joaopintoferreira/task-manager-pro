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
    setupUser();
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

    document.getElementById('user-avatar').textContent = initials;
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

        if (res.status === 401) {
            const ok = await tryRefresh();
            if (ok) return apiRequest(endpoint, options);
            doLogout();
            return null;
        }

        return res;
    } catch (err) {
        console.error('API error:', err);
        toast('Erro de conexão com o servidor', 'error');
        return null;
    }
}

async function tryRefresh() {
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
async function loadAll() {
    await Promise.all([loadTasks(), loadCategories(), loadStats()]);
}

// ══════════════════════════════════════════════
// TAREFAS
// ══════════════════════════════════════════════
async function loadTasks() {
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

    const res = await apiRequest(`/tasks?${params}`);
    if (!res || !res.ok) return;

    const data = await res.json();
    renderTasks(data.tasks);
    renderPagination(data);
    updateNavCounts(data);
}

function renderTasks(tasks) {
    const area = document.getElementById('tasks-area');

    if (!tasks.length) {
        area.innerHTML = `
            <div class="empty-state">
                <i class="bi bi-inbox"></i>
                <h4>Nenhuma tarefa encontrada</h4>
                <p>Tente outros filtros ou crie uma nova tarefa.</p>
                <button class="btn-primary-sm" onclick="openTaskModal()">
                    <i class="bi bi-plus-lg"></i> Nova tarefa
                </button>
            </div>`;
        return;
    }

    area.innerHTML = tasks.map(t => {
        const overdue   = t.is_overdue;
        const done      = t.status === 'completed';
        const dueStr    = t.due_date ? formatDate(t.due_date) : '';
        const catBadge  = t.category
            ? `<span class="badge-cat" style="background:${t.category.color}">${esc(t.category.name)}</span>`
            : '';
        const overdueBadge = overdue
            ? `<span class="task-meta overdue-tag"><i class="bi bi-exclamation-circle"></i> Atrasada</span>`
            : '';

        return `
        <div class="task-card priority-${t.priority} ${overdue ? 'is-overdue' : ''} ${done ? 'is-completed' : ''}">
            <input type="checkbox" class="task-check" data-id="${t.id}" ${done ? 'checked' : ''}
                   onchange="toggleStatus(${t.id}, this.checked)">
            <div class="task-body">
                <div class="task-title ${done ? 'done' : ''}">
                    ${esc(t.title)}
                    <span class="badge-priority ${t.priority}">${priorityLabel(t.priority)}</span>
                    <span class="badge-status ${t.status}">${statusLabel(t.status)}</span>
                    ${catBadge}
                </div>
                ${t.description ? `<div class="task-desc">${esc(t.description)}</div>` : ''}
                <div class="task-meta">
                    ${dueStr ? `<span><i class="bi bi-calendar3"></i>${dueStr}</span>` : ''}
                    <span><i class="bi bi-clock"></i>${timeAgo(t.created_at)}</span>
                    ${overdueBadge}
                </div>
            </div>
            <div class="task-actions">
                <button class="btn-icon"        title="Editar"   onclick="openTaskModal(${t.id})"><i class="bi bi-pencil"></i></button>
                <button class="btn-icon danger" title="Excluir"  onclick="deleteTask(${t.id})"><i class="bi bi-trash"></i></button>
            </div>
        </div>`;
    }).join('');
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
async function openTaskModal(taskId = null) {
    const modal = new bootstrap.Modal(document.getElementById('taskModal'));

    if (taskId) {
        document.getElementById('taskModalLabel').textContent = 'Editar Tarefa';
        const res = await apiRequest(`/tasks/${taskId}`);
        if (!res || !res.ok) { toast('Erro ao carregar tarefa', 'error'); return; }
        const t = await res.json();

        document.getElementById('taskId').value          = t.id;
        document.getElementById('taskTitle').value       = t.title;
        document.getElementById('taskDescription').value = t.description || '';
        document.getElementById('taskDueDate').value     = t.due_date ? t.due_date.slice(0,16) : '';
        document.getElementById('taskPriority').value    = t.priority;
        document.getElementById('taskStatus').value      = t.status;
        document.getElementById('taskCategory').value    = t.category_id || '';
    } else {
        document.getElementById('taskModalLabel').textContent = 'Nova Tarefa';
        document.getElementById('taskId').value          = '';
        document.getElementById('taskTitle').value       = '';
        document.getElementById('taskDescription').value = '';
        document.getElementById('taskDueDate').value     = '';
        document.getElementById('taskPriority').value    = 'medium';
        document.getElementById('taskStatus').value      = 'pending';
        document.getElementById('taskCategory').value    = '';
    }

    modal.show();
}

async function saveTask() {
    const taskId = document.getElementById('taskId').value;
    const title  = document.getElementById('taskTitle').value.trim();

    if (!title) { toast('Título é obrigatório', 'error'); return; }

    const body = {
        title,
        description: document.getElementById('taskDescription').value.trim(),
        due_date:    document.getElementById('taskDueDate').value || null,
        priority:    document.getElementById('taskPriority').value,
        status:      document.getElementById('taskStatus').value,
        category_id: document.getElementById('taskCategory').value || null,
    };

    const method   = taskId ? 'PUT' : 'POST';
    const endpoint = taskId ? `/tasks/${taskId}` : '/tasks';

    const res = await apiRequest(endpoint, { method, body: JSON.stringify(body) });
    if (!res) return;

    if (res.ok) {
        bootstrap.Modal.getInstance(document.getElementById('taskModal')).hide();
        toast(taskId ? 'Tarefa atualizada!' : 'Tarefa criada!', 'success');
        await loadAll();
    } else {
        const data = await res.json();
        toast(data.message || 'Erro ao salvar tarefa', 'error');
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
        await loadTasks();
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
        await loadTasks();
    } else {
        toast('Erro ao excluir categoria', 'error');
    }
}

// ══════════════════════════════════════════════
// NOTIFICAÇÕES
// ══════════════════════════════════════════════
async function loadNotifications() {
    const res = await apiRequest('/notifications?limit=20');
    if (!res || !res.ok) return;
    const data = await res.json();

    const badge = document.getElementById('notif-badge');
    if (data.unread_count > 0) {
        badge.textContent  = data.unread_count > 99 ? '99+' : data.unread_count;
        badge.style.display = '';
    } else {
        badge.style.display = 'none';
    }

    renderNotifications(data.notifications);
}

function renderNotifications(notifs) {
    const list = document.getElementById('notif-list');
    if (!notifs.length) {
        list.innerHTML = `<div class="text-center text-muted py-3" style="font-size:.875rem;">Nenhuma notificação</div>`;
        return;
    }

    const icons = {
        task_created:    'bi-plus-circle',
        task_completed:  'bi-check-circle',
        task_deleted:    'bi-trash',
        task_assignment: 'bi-people',
        due_date_reminder:'bi-alarm',
    };

    list.innerHTML = notifs.map(n => `
        <div class="notif-item ${n.is_read ? '' : 'unread'}" onclick="readNotif(${n.id}, this)">
            <div class="notif-icon">
                <i class="bi ${icons[n.type] || 'bi-bell'}"></i>
            </div>
            <div class="notif-content">
                <div class="notif-title">${esc(n.title)}</div>
                <div class="notif-msg">${esc(n.message)}</div>
                <div class="notif-time">${timeAgo(n.created_at)}</div>
            </div>
        </div>`).join('');
}

function toggleNotifPanel() {
    const panel = document.getElementById('notif-panel');
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
function setFilter(filter, el) {
    if (el) {
        document.querySelectorAll('.nav-link[data-filter]').forEach(a => a.classList.remove('active'));
        el.classList.add('active');
    }

    const titles = {
        all: 'Todas as Tarefas', pending: 'Pendentes',
        in_progress: 'Em Progresso', completed: 'Concluídas',
    };
    document.getElementById('page-title').textContent = titles[filter] || 'Tarefas';

    state.filter = filter;
    state.page   = 1;
    loadTasks();
    if (window.innerWidth < 768) toggleSidebar();
}

function filterByCategory(catId) {
    state.category = state.category == catId ? '' : catId;
    state.page = 1;
    document.getElementById('filter-category').value = state.category;
    loadTasks();
    loadCategories();
    if (window.innerWidth < 768) toggleSidebar();
}

function onSearch(val) {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
        state.search = val;
        state.page   = 1;
        loadTasks();
    }, 350);
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

function onSortChange(val) {
    state.sortBy = val;
    state.page   = 1;
    loadTasks();
}

function changePage(dir) {
    state.page += dir;
    loadTasks();
    window.scrollTo({ top: 0, behavior: 'smooth' });
}

// ══════════════════════════════════════════════
// SIDEBAR MOBILE
// ══════════════════════════════════════════════
function toggleSidebar() {
    document.getElementById('sidebar').classList.toggle('open');
    document.getElementById('sidebar-overlay').classList.toggle('open');
}

// ══════════════════════════════════════════════
// LOGOUT
// ══════════════════════════════════════════════
async function doLogout() {
    try {
        await apiRequest('/auth/logout', {
            method: 'POST',
            body: JSON.stringify({ refresh_token: refreshToken }),
        });
    } catch { /* segue */ }
    localStorage.clear();
    window.location.href = 'auth.html';
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

function formatDate(iso) {
    const d = new Date(iso);
    return d.toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function timeAgo(iso) {
    const diff  = Date.now() - new Date(iso).getTime();
    const mins  = Math.floor(diff / 60000);
    const hours = Math.floor(diff / 3600000);
    const days  = Math.floor(diff / 86400000);
    if (mins  < 1)  return 'agora mesmo';
    if (mins  < 60) return `${mins}min atrás`;
    if (hours < 24) return `${hours}h atrás`;
    if (days  < 7)  return `${days}d atrás`;
    return new Date(iso).toLocaleDateString('pt-BR');
}
