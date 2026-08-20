// ─── STATE ────────────────────────────────────────────────────────────────────
let studentsData = [];
let classesData = [];
let activityData = [];
let repoFilter = false;
let alertFilter = null;         // 'overdue' | 'soon' | null
let activityFilter = 'tudo';

const MONTHS_PT = ['Jan', 'Fev', 'Mar', 'Abr', 'Mai', 'Jun', 'Jul', 'Ago', 'Set', 'Out', 'Nov', 'Dez'];
const CAT_LABEL = {
    presenca: 'Presença', falta: 'Falta', reposicao: 'Reposição', pagamento: 'Pagamento',
    aluno: 'Aluno', turma: 'Turma', matricula: 'Matrícula', exclusao: 'Exclusão', info: 'Sistema'
};
const FILTER_MAP = {
    frequencia: ['presenca', 'falta', 'reposicao'],
    pagamento: ['pagamento'],
    aluno: ['aluno'],
    turma: ['turma', 'matricula'],
    exclusao: ['exclusao'],
};

// ─── INIT ─────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    loadAll();
    setTodayDate();
    trapBackGesture();
});

// No celular, o gesto "voltar" (deslizar pro lado) NÃO pode deslogar. Se tiver um
// modal aberto, fecha ele; senão, não sai da tela (mantém o app "preso").
function trapBackGesture() {
    try {
        history.pushState({ amp: 1 }, '');
        window.addEventListener('popstate', () => {
            const aberto = Array.from(document.querySelectorAll('.modal-overlay'))
                .some(m => getComputedStyle(m).display !== 'none');
            if (aberto && typeof closeModals === 'function') closeModals();
            history.pushState({ amp: 1 }, '');   // re-prende (nunca sai/desloga)
        });
    } catch (e) {}
}

function hideAppLoading() {
    document.getElementById('app-loading')?.classList.add('hidden');
}

// Data de HOJE no fuso LOCAL (não usar toISOString, que é UTC e vira "amanhã"
// depois das 21h no Brasil — bagunçava a data padrão de início/pagamento).
function todayLocalISO() {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function setTodayDate() {
    const today = todayLocalISO();
    const dateInput = document.getElementById('startDate');
    const paymentDateInput = document.getElementById('paymentDate');
    if (dateInput) { dateInput.value = today; autoCalculateStudentData(); }
    if (paymentDateInput) paymentDateInput.value = today;
}

function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// ─── AUTO-CALCULATE ───────────────────────────────────────────────────────────
const PLANO_MESES = { Mensal: 1, Trimestral: 3, Semestral: 6, Anual: 12 };
const MESES_LONGOS = ['Janeiro', 'Fevereiro', 'Março', 'Abril', 'Maio', 'Junho',
    'Julho', 'Agosto', 'Setembro', 'Outubro', 'Novembro', 'Dezembro'];

function planMonths(plan) {
    if (PLANO_MESES[plan]) return PLANO_MESES[plan];
    const m = String(plan || '').match(/\d+/);
    return m ? Math.max(parseInt(m[0]), 1) : 1;
}

function lastDayOfMonth(y, m) { return new Date(y, m + 1, 0).getDate(); } // m: 0-based

// Espelha o _competencias do backend: [{mesRef:'YYYY-MM', venc:'YYYY-MM-DD'}]
function competenciasJS(startStr, dia, meses) {
    if (!startStr) return [];
    const start = new Date(startStr + 'T12:00:00');
    let y = start.getFullYear(), m = start.getMonth(); // m 0-based
    if (start.getDate() > dia) { m++; if (m > 11) { m = 0; y++; } }
    const out = [];
    for (let i = 0; i < meses; i++) {
        const yy = y + Math.floor((m + i) / 12);
        const mm = (m + i) % 12; // 0-based
        const d = Math.min(dia, lastDayOfMonth(yy, mm));
        const mms = String(mm + 1).padStart(2, '0'), ds = String(d).padStart(2, '0');
        out.push({ mesRef: `${yy}-${mms}`, venc: `${yy}-${mms}-${ds}`, y: yy, m: mm });
    }
    return out;
}

function autoCalculateStudentData() {
    const startInput = document.getElementById('startDate').value;
    const plan = document.getElementById('plan').value;
    const aulasSemana = parseInt(document.getElementById('classesPerWeek').value) || 2;
    const paymentDay = parseInt(document.getElementById('paymentDay').value) || 30;
    const months = planMonths(plan);

    document.getElementById('saldoAulas').value = aulasSemana * months * 4;

    if (startInput) {
        const comps = competenciasJS(startInput, paymentDay, months);
        if (comps.length) document.getElementById('endDate').value = comps[comps.length - 1].venc;
    }
    renderParcelaPreview();
}

// Prévia das parcelas geradas no cadastro
function renderParcelaPreview() {
    const list = document.getElementById('parcPreviewList');
    const sum = document.getElementById('parcPreviewSum');
    if (!list) return;
    const startInput = document.getElementById('startDate').value;
    const plan = document.getElementById('plan').value;
    const paymentDay = parseInt(document.getElementById('paymentDay').value) || 30;
    const price = document.getElementById('price').value.trim() || '0,00';
    const firstPaid = document.getElementById('firstPaid')?.checked;
    const months = planMonths(plan);
    const comps = competenciasJS(startInput, paymentDay, months);

    list.innerHTML = '';
    comps.forEach((c, i) => {
        const paid = (i === 0 && firstPaid);
        const dv = c.venc.slice(8, 10) + '/' + c.venc.slice(5, 7);
        list.innerHTML += `
            <div class="parc-preview-item ${paid ? 'paid' : ''}">
                <span class="pl">${i + 1}ª · ${MESES_LONGOS[c.m]}/${c.y}</span>
                <span class="pr">${paid ? '<i class="fa-solid fa-check"></i> Paga' : 'vence ' + dv}</span>
            </div>`;
    });
    if (sum) sum.innerText = comps.length ? `${comps.length}x de R$ ${price}` : '';
}

function enforceClassLimit(checkbox) {
    const max = parseInt(document.getElementById('classesPerWeek').value) || 2;
    const checkedCount = document.querySelectorAll('input[name="selectedClasses"]:checked').length;
    if (checkedCount > max) {
        alert(`O plano permite apenas ${max} aula(s) por semana!`);
        checkbox.checked = false;
    }
}

// ─── LOAD ─────────────────────────────────────────────────────────────────────
async function loadAll() {
    try {
        await fetchClasses();          // turmas primeiro (os alunos usam os nomes)
        await fetchStudents();
        await Promise.all([fetchAlerts(), fetchActivity()]);  // independentes → em paralelo
    } finally {
        hideAppLoading();
    }
}

async function fetchClasses() {
    try {
        const res = await fetch('/api/classes');
        classesData = await res.json();
        renderClassGrid();
    } catch (e) { console.error(e); }
}

async function fetchStudents() {
    try {
        const res = await fetch('/api/students');
        studentsData = await res.json();
        renderStudentTable();
        updateStats();
    } catch (e) { console.error(e); }
}

async function fetchAlerts() {
    try {
        const res = await fetch('/api/alerts');
        const data = await res.json();
        const overdueEl = document.getElementById('countOverdue');
        const soonEl = document.getElementById('countSoon');
        const overdueCard = document.getElementById('alertOverdueCard');
        const soonCard = document.getElementById('alertSoonCard');
        if (overdueEl) overdueEl.innerText = data.overdue.length;
        if (soonEl) soonEl.innerText = data.due_soon.length;
        if (overdueCard) overdueCard.style.display = data.overdue.length > 0 ? 'flex' : 'none';
        if (soonCard) soonCard.style.display = data.due_soon.length > 0 ? 'flex' : 'none';
    } catch (e) { console.error(e); }
}

async function fetchActivity() {
    try {
        const res = await fetch('/api/activity');
        activityData = await res.json();
        renderActivity();
    } catch (e) { console.error(e); }
}

// ─── RENDER CLASS GRID ────────────────────────────────────────────────────────
function renderClassGrid() {
    const grid = document.getElementById('classGrid');
    if (!grid) return;
    grid.innerHTML = '';
    const daysOrder = ['Segunda', 'Terça', 'Quarta', 'Quinta', 'Sexta', 'Sábado'];

    daysOrder.forEach(day => {
        const classesToday = classesData.filter(c => c.day === day).sort((a, b) => a.time.localeCompare(b.time));
        if (classesToday.length > 0) {
            const col = document.createElement('div');
            col.className = 'day-column';
            col.innerHTML = `<h3>${day}</h3>`;
            classesToday.forEach(c => {
                const percent = (c.student_count / c.capacity) * 100;
                const isFull = c.student_count >= c.capacity;
                const statusColor = isFull ? 'var(--danger)' : 'var(--lime)';
                col.innerHTML += `
                <div class="class-card" onclick="openClassDetails(${c.id})" style="cursor:pointer;">
                    <div class="btn-del-class" onclick="event.stopPropagation(); deleteClass(${c.id}, this)"><i class="fa-solid fa-trash"></i></div>
                    <div class="class-header"><span class="class-time">${esc(c.time)}</span><span class="class-prof">${esc(c.professor)}</span></div>
                    <div class="class-meta">
                        <span><i class="fa-solid fa-user-group"></i> ${c.student_count}/${c.capacity}</span>
                        <span style="color:${statusColor}; font-weight:700; font-size:0.72rem; font-family:var(--disp); letter-spacing:.5px;">${isFull ? 'LOTADO' : 'DISPONÍVEL'}</span>
                    </div>
                    <div class="progress-bar"><div class="progress-fill ${isFull ? 'full' : ''}" style="width:${Math.min(percent, 100)}%"></div></div>
                </div>`;
            });
            grid.appendChild(col);
        }
    });
}

// ─── RENDER STUDENT LIST ──────────────────────────────────────────────────────
function renderStudentTable() {
    const list = document.getElementById('studentList');
    if (!list) return;
    list.innerHTML = '';
    let arr = [...studentsData];

    const searchVal = document.getElementById('search')?.value?.toLowerCase();
    if (searchVal) arr = arr.filter(s => s.name.toLowerCase().includes(searchVal));
    if (repoFilter) arr = arr.filter(s => s.reposicoes_count > 0);
    if (alertFilter === 'overdue') arr = arr.filter(s => s.payment_overdue);
    if (alertFilter === 'soon') arr = arr.filter(s => s.payment_alert && !s.payment_overdue);

    arr.sort((a, b) => (a.active === b.active) ? 0 : a.active ? -1 : 1);

    if (arr.length === 0) {
        list.innerHTML = '<div class="activity-empty">Nenhum aluno encontrado.</div>';
        return;
    }

    arr.forEach(s => {
        const rowClass = s.active ? (s.payment_overdue ? 'due' : '') : 'off';

        let badges = '';
        if (!s.active) badges += '<span class="badge badge-muted">Inativo</span>';
        if (s.active && s.payment_overdue) badges += '<span class="badge badge-danger">Vencido</span>';
        else if (s.active && s.payment_alert) badges += '<span class="badge badge-warning">Vence em breve</span>';
        if (s.active && s.plan_expiring) badges += '<span class="badge badge-warning">Plano expirando</span>';

        const repoLine = (s.reposicoes_count > 0 && s.active)
            ? `<div class="st-repo"><i class="fa-solid fa-rotate"></i> ${s.reposicoes_count} reposição(ões) pendente(s)</div>` : '';

        let payHtml;
        if (s.active) {
            const total = s.parcelasTotal || 0;
            const pagas = s.parcelasPagas || 0;
            const venceClass = s.payment_overdue ? 'over' : '';
            if (total > 0) {
                const resumo = `<small class="parc">${pagas} de ${total} meses pagos</small>`;
                if (pagas >= total) {
                    // plano quitado → oferecer renovação
                    payHtml = `
                        <b class="paid">Plano quitado</b>
                        ${resumo}
                        <button onclick="openRenovarModal(${s.id})" class="btn-renew"><i class="fa-solid fa-rotate"></i> Renovar plano</button>`;
                } else {
                    const venceLabel = s.payment_overdue ? 'Vencido desde' : 'Próx. venc';
                    payHtml = `
                        <b class="${venceClass}">${venceLabel} ${formatDate(s.nextPayment)}</b>
                        ${resumo}
                        <button onclick="openPaymentModal(${s.id})" class="btn-pay" style="margin-top:6px;"><i class="fa-solid fa-money-bill-wave"></i> Registrar Pgto</button>`;
                }
            } else {
                const paidLine = s.lastPayment
                    ? `<small class="paid"><i class="fa-solid fa-check"></i> Pago ${formatDate(s.lastPayment)}</small>`
                    : '<small>Sem pagamento</small>';
                payHtml = `
                    <b class="${venceClass}">Vence ${formatDate(s.nextPayment)}</b>
                    <small>todo dia ${s.paymentDay || 30}</small>
                    ${paidLine}
                    <button onclick="openPaymentModal(${s.id})" class="btn-pay" style="margin-top:6px;"><i class="fa-solid fa-money-bill-wave"></i> Registrar Pgto</button>`;
            }
        } else {
            payHtml = '<b>—</b><small>contrato encerrado</small>';
        }

        const plusRepos = (s.reposicoes_count > 0) ? `<span class="plus">+${s.reposicoes_count} repos.</span>` : '';
        const priceFormatted = formatPrice(s.price);

        const row = document.createElement('div');
        row.className = 'st-row ' + rowClass;
        row.innerHTML = `
            <div class="st-info">
                <div class="st-name">${esc(s.name)} ${badges}</div>
                <div class="st-sub">${esc(s.classes_desc) || '<span style="color:#94A3B0">Sem turma fixa</span>'} &nbsp;·&nbsp; ${esc(s.plan)} · ${s.classesPerWeek || 2}x/sem · <b>${priceFormatted}</b></div>
                ${repoLine}
            </div>
            <div class="st-pay">${payHtml}</div>
            <div class="saldo-box"><em>SALDO</em><b>${s.credits || 0}</b>${plusRepos}</div>
            <div class="st-actions">
                <button class="btn-secondary" style="font-size:0.72rem; padding:7px 11px;" onclick="openActionModal(${s.id})"><i class="fa-solid fa-list-check"></i> Gerenciar</button>
                <div class="icons">
                    <button class="icon-btn ${s.active ? 'on' : ''}" title="${s.active ? 'Inativar' : 'Ativar'}" onclick="toggleStudentStatus(${s.id}, this)"><i class="fa-solid fa-toggle-${s.active ? 'on' : 'off'}"></i></button>
                    <button class="icon-btn edit" onclick="editStudent(${s.id})"><i class="fa-solid fa-pen"></i></button>
                    <button class="icon-btn del" onclick="deleteStudent(${s.id}, this)"><i class="fa-solid fa-trash"></i></button>
                </div>
            </div>`;
        list.appendChild(row);
    });
}

// ─── ACTIVITY FEED ────────────────────────────────────────────────────────────
function setActivityFilter(filter, el) {
    activityFilter = filter;
    document.querySelectorAll('#activityFilters .filter-chip').forEach(c => c.classList.remove('active'));
    if (el) el.classList.add('active');
    renderActivity();
}

function dayLabel(isoDate) {
    const d = new Date(isoDate + 'T12:00:00');
    const today = new Date(); today.setHours(12, 0, 0, 0);
    const diff = Math.round((today - d) / 86400000);
    const dm = `${String(d.getDate()).padStart(2, '0')} ${MONTHS_PT[d.getMonth()]}`;
    if (diff === 0) return `Hoje · ${dm}`;
    if (diff === 1) return `Ontem · ${dm}`;
    return `${dm} ${d.getFullYear()}`;
}

function renderActivity() {
    const feed = document.getElementById('activityFeed');
    if (!feed) return;

    let arr = [...activityData];
    if (activityFilter !== 'tudo') {
        const cats = FILTER_MAP[activityFilter] || [];
        arr = arr.filter(a => cats.includes(a.category));
    }

    if (arr.length === 0) {
        feed.innerHTML = '<div class="activity-empty">Nenhuma atividade registrada ainda.</div>';
        return;
    }

    let html = '';
    let lastDay = null;
    arr.forEach(a => {
        const dt = (a.datetime || '').split(' ');
        const dayPart = dt[0] || '';
        const timePart = (dt[1] || '').slice(0, 5);
        const label = dayLabel(dayPart);
        if (label !== lastDay) {
            html += `<div class="day-label">${label}</div>`;
            lastDay = label;
        }
        const cat = a.category || 'info';
        const catLabel = CAT_LABEL[cat] || 'Sistema';
        let desc = esc(a.description);
        const dash = desc.indexOf(' — ');
        if (dash > -1) desc = `<b>${desc.slice(0, dash)}</b>${desc.slice(dash)}`;
        html += `
            <div class="ev ${cat}">
                <div class="ev-time">${timePart}</div>
                <div><span class="ev-cat ${cat}">${catLabel}</span><span class="ev-desc">${desc}</span></div>
                <div class="ev-user"><i class="fa-regular fa-user"></i> ${esc(a.user)}</div>
            </div>`;
    });
    feed.innerHTML = html;
}

// ─── ACTION MODAL (frequência) ────────────────────────────────────────────────
function openActionModal(id) {
    const s = studentsData.find(st => st.id === id);
    if (!s) return;
    renderActionModalContent(s);
    document.getElementById('modalActions').style.display = 'flex';
}

function renderActionModalContent(s) {
    const body = document.getElementById('actionModalBody');

    let historyHtml = '<div style="text-align:center; padding:10px; color:var(--muted); font-size:0.8rem;">Nenhum histórico ainda.</div>';
    if (s.history && s.history.length > 0) {
        const dotColor = { presenca: 'var(--lime)', falta_aviso: 'var(--amber)', falta_sem_aviso: 'var(--danger)', usar_reposicao: 'var(--blue)', anular_reposicao: 'var(--muted)', pagamento: 'var(--primary)' };
        historyHtml = s.history.map(h => {
            const canUndo = ['presenca', 'falta_aviso', 'falta_sem_aviso', 'usar_reposicao', 'anular_reposicao'].includes(h.action_type);
            return `
            <div class="he">
                <div class="he-dot" style="background:${dotColor[h.action_type] || 'var(--muted)'}"></div>
                <div style="flex:1;">
                    <div class="he-tm"><i class="fa-regular fa-clock"></i> ${esc(h.date)}</div>
                    <div class="he-ds">${esc(h.desc)}</div>
                </div>
                ${canUndo ? `<button class="he-undo" onclick="undoHistoryEntry(${s.id}, ${h.id}, this)"><i class="fa-solid fa-rotate-left"></i> Desfazer</button>` : ''}
            </div>`;
        }).join('');
    }

    const priceFormatted = formatPrice(s.price);
    const disabled = !s.active ? 'disabled' : '';
    const disabledCls = !s.active ? 'disabled' : '';

    let alertHtml = '';
    if (s.payment_overdue) alertHtml = `<div class="am-alert over">Pagamento VENCIDO desde ${formatDate(s.nextPayment)}</div>`;
    else if (s.payment_alert) alertHtml = `<div class="am-alert soon">Pagamento vence em breve: ${formatDate(s.nextPayment)}</div>`;

    const repoBlock = (s.reposicoes_count > 0) ? `
        <button class="am-btn use" ${disabled} onclick="studentAction(this, ${s.id}, 'usar_reposicao')">
            <span class="lf"><i class="fa-solid fa-hand-sparkles"></i> Usar Reposição</span>
            <span class="am-pill">−1 REPOSIÇÃO</span>
        </button>
        <button class="am-btn void" ${disabled} onclick="studentAction(this, ${s.id}, 'anular_reposicao')">
            <span class="lf"><i class="fa-solid fa-xmark"></i> Anular Reposição (falta na repos.)</span>
        </button>` : '';

    body.innerHTML = `
        <div class="am-who">
            <h4>${esc(s.name)}</h4>
            <div class="am-chip">${esc(s.plan)} · ${s.classesPerWeek}x/sem · ${priceFormatted}</div>
            <div class="am-dates"><i class="fa-regular fa-calendar"></i> ${s.startDate ? formatDate(s.startDate) : '—'} → ${s.endDate ? formatDate(s.endDate) : '—'} &nbsp;|&nbsp; Venc. todo dia <strong>${s.paymentDay || 30}</strong></div>
            ${alertHtml}
        </div>
        <div class="am-tiles">
            <div class="am-tile g"><em>Saldo de Aulas</em><b>${s.credits || 0}</b></div>
            <div class="am-tile ${s.reposicoes_count > 0 ? 'r' : 'n'}"><em>Reposições</em><b>${s.reposicoes_count}</b></div>
        </div>
        <div class="court-line"></div>
        <div class="am-actions ${disabledCls}">
            <button class="am-btn pres" ${disabled} onclick="studentAction(this, ${s.id}, 'presenca')">
                <span class="lf"><i class="fa-solid fa-circle-check"></i> Presença Normal</span>
                <span class="am-pill">−1 AULA</span>
            </button>
            <button class="am-btn avi" ${disabled} onclick="studentAction(this, ${s.id}, 'falta_com_reposicao')">
                <span class="lf"><i class="fa-solid fa-user-clock"></i> Falta com Aviso Prévio</span>
                <span class="am-pill">−1 AULA<br>+1 REPOSIÇÃO</span>
            </button>
            <button class="am-btn sem" ${disabled} onclick="studentAction(this, ${s.id}, 'falta_sem_aviso')">
                <span class="lf"><i class="fa-solid fa-ban"></i> Falta sem Aviso Prévio</span>
                <span class="am-pill">PERDE AULA</span>
            </button>
            ${repoBlock}
        </div>
        <div class="am-hist">
            <div class="am-hist-title"><i class="fa-solid fa-clock-rotate-left"></i> Histórico do Aluno</div>
            <div class="hist-scroll">${historyHtml}</div>
        </div>`;
}

// ─── UNDO HISTORY ENTRY ───────────────────────────────────────────────────────
async function undoHistoryEntry(studentId, historyId, btnEl) {
    if (!confirm('Desfazer este registro? O saldo de aulas será revertido se aplicável.')) return;
    btnEl.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
    btnEl.disabled = true;
    try {
        const res = await fetch(`/api/students/${studentId}/history/${historyId}/delete`, { method: 'DELETE' });
        const data = await res.json();
        if (data.success) {
            await fetchStudents();
            const updated = studentsData.find(st => st.id === studentId);
            if (updated) renderActionModalContent(updated);
        }
    } catch (e) {
        alert('Erro ao desfazer.');
        btnEl.disabled = false;
    }
}

// ─── PAYMENT MODAL (parcelas) ─────────────────────────────────────────────────
let currentPaymentStudentId = null;
let selectedParcelaId = null;

function openPaymentModal(studentId) {
    const s = studentsData.find(st => st.id === studentId);
    if (!s) return;
    currentPaymentStudentId = studentId;
    selectedParcelaId = null;
    document.getElementById('paymentStudentId').value = s.id;
    renderPaymentModal(s);
    document.getElementById('modalPayment').style.display = 'flex';
}

function openRenovarModal(studentId) {
    openPaymentModal(studentId);
    toggleRenovPanel(true);
}

function renderPaymentModal(s) {
    const mens = s.mensalidades || [];
    const pagas = mens.filter(m => m.status === 'paga').length;
    const vencidas = mens.filter(m => m.status === 'vencida').length;
    const aVencer = mens.filter(m => m.status === 'a_vencer').length;

    let listHtml = mens.map(m => {
        const comp = `${MESES_LONGOS[parseInt(m.mesRef.slice(5, 7)) - 1]}/${m.mesRef.slice(0, 4)}`;
        if (m.status === 'paga') {
            return `
            <div class="am-btn paga">
                <span class="lf"><i class="fa-solid fa-circle-check"></i> ${m.numero}ª · ${comp}</span>
                <span style="display:flex; align-items:center; gap:8px;">
                    <span class="am-pill">Paga · ${formatDate(m.pagoEm)}</span>
                    <button class="parc-estorno" onclick="estornarParcela(${m.id})" title="Estornar pagamento"><i class="fa-solid fa-rotate-left"></i></button>
                </span>
            </div>`;
        }
        const cls = m.status === 'vencida' ? 'venc' : 'aberta';
        const ic = m.status === 'vencida' ? 'fa-triangle-exclamation' : 'fa-clock';
        const lbl = (m.status === 'vencida' ? 'Vencida' : 'Vence') + ' · ' + formatDate(m.vencimento);
        const selCls = (selectedParcelaId === m.id) ? ' sel' : '';
        return `
            <div class="am-btn ${cls}${selCls}" onclick="selectParcela(${m.id})">
                <span class="lf"><i class="fa-solid ${ic}"></i> ${m.numero}ª · ${comp}</span>
                <span class="am-pill">${lbl}</span>
            </div>`;
    }).join('');
    if (!mens.length) listHtml = '<div style="text-align:center; color:var(--muted); padding:12px; font-size:0.85rem;">Sem parcelas geradas.</div>';

    const body = document.getElementById('paymentModalBody');
    body.innerHTML = `
        <input type="hidden" id="paymentStudentId" value="${s.id}">
        <div class="am-who">
            <h4>${esc(s.name)}</h4>
            <div class="am-chip">${esc(s.plan)} · vence dia ${s.paymentDay || 30} · ${formatPrice(s.price)}</div>
            <div class="am-dates"><i class="fa-regular fa-calendar"></i> Início ${formatDate(s.startDate)} &nbsp;·&nbsp; Fim ${formatDate(s.endDate)}</div>
        </div>
        <div class="am-tiles">
            <div class="am-tile g"><em>Pagas</em><b>${pagas}</b></div>
            <div class="am-tile ${vencidas > 0 ? 'r' : 'n'}"><em>Vencidas</em><b>${vencidas}</b></div>
            <div class="am-tile n"><em>A vencer</em><b>${aVencer}</b></div>
        </div>
        <div class="am-hist-title" style="margin:14px 0 9px;"><i class="fa-solid fa-layer-group"></i> Parcelas do plano — toque pra dar baixa</div>
        <div class="parc-list">${listHtml}</div>

        <div class="pay-panel" id="payPanel" style="display:none;">
            <div class="sec"><i class="fa-solid fa-money-bill-wave"></i> <span id="payPanelTitle">Informar pagamento</span></div>
            <div class="sheet-2col" style="display:flex; gap:10px;">
                <div style="flex:1;"><label>Data do pagamento</label><input type="date" id="paymentDate"></div>
                <div style="flex:1;"><label>Valor recebido (R$)</label><input type="text" id="paymentAmount"></div>
            </div>
            <div style="display:flex; gap:10px; justify-content:flex-end; margin-top:12px;">
                <button class="btn-secondary" onclick="closePayPanel()">Cancelar</button>
                <button class="btn-primary" onclick="confirmParcelaPayment()"><i class="fa-solid fa-check"></i> Confirmar</button>
            </div>
        </div>

        <div class="pay-panel renov" id="renovPanel" style="display:none;">
            <div class="sec"><i class="fa-solid fa-rotate"></i> Renovar plano — por quantos meses?</div>
            <div class="sheet-2col" style="display:flex; gap:10px;">
                <div style="flex:1;">
                    <label>Duração</label>
                    <select id="renovMeses">
                        <option value="1">Mensal — 1 mês</option>
                        <option value="3">Trimestral — 3 meses</option>
                        <option value="6" selected>Semestral — 6 meses</option>
                        <option value="12">Anual — 12 meses</option>
                    </select>
                </div>
                <div style="flex:1;"><label>Valor da mensalidade (R$)</label><input type="text" id="renovValor" value="${formatPriceInput(s.price)}"></div>
            </div>
            <div style="display:flex; gap:10px; justify-content:flex-end; margin-top:12px;">
                <button class="btn-secondary" onclick="toggleRenovPanel(false)">Cancelar</button>
                <button class="btn-primary" onclick="confirmRenovar()"><i class="fa-solid fa-check"></i> Gerar parcelas</button>
            </div>
        </div>

        <div style="text-align:center; margin-top:14px;">
            <button class="btn-renew" onclick="toggleRenovPanel(true)"><i class="fa-solid fa-rotate"></i> Renovar plano</button>
        </div>`;
}

function selectParcela(mid) {
    selectedParcelaId = mid;
    const s = studentsData.find(st => st.id === currentPaymentStudentId);
    const m = (s?.mensalidades || []).find(x => x.id === mid);
    if (!m) return;
    renderPaymentModal(s); // re-render marca a parcela selecionada (classe .sel)
    const comp = `${MESES_LONGOS[parseInt(m.mesRef.slice(5, 7)) - 1]}/${m.mesRef.slice(0, 4)}`;
    document.getElementById('payPanelTitle').innerText = `Informar pagamento — ${m.numero}ª parcela (${comp})`;
    document.getElementById('paymentDate').value = todayLocalISO();
    document.getElementById('paymentAmount').value = formatPriceInput(m.valor || s.price);
    document.getElementById('payPanel').style.display = 'block';
    document.getElementById('payPanel').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function closePayPanel() {
    selectedParcelaId = null;
    const s = studentsData.find(st => st.id === currentPaymentStudentId);
    if (s) renderPaymentModal(s);
}

function toggleRenovPanel(show) {
    const p = document.getElementById('renovPanel');
    const pay = document.getElementById('payPanel');
    if (!p) return;
    if (pay) pay.style.display = 'none';
    p.style.display = show ? 'block' : 'none';
    if (show) p.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

async function confirmParcelaPayment() {
    const id = currentPaymentStudentId;
    const date = document.getElementById('paymentDate').value;
    const amount = document.getElementById('paymentAmount').value;
    if (!date) { alert('Preencha a data do pagamento.'); return; }
    const btn = document.querySelector('#payPanel .btn-primary');
    const orig = btn.innerHTML;
    btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Salvando...';
    btn.disabled = true;
    try {
        const res = await fetch(`/api/students/${id}/register_payment`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ paymentDate: date, amount: amount, mensalidadeId: selectedParcelaId })
        });
        if (!res.ok) { alert(await errMsg(res, 'Erro ao registrar pagamento.')); return; }
        selectedParcelaId = null;
        await refreshAndRerenderPayment(id);
    } catch (e) {
        alert('Sem conexão com o sistema. Tente novamente.');
    } finally {
        btn.innerHTML = orig;
        btn.disabled = false;
    }
}

async function estornarParcela(mid) {
    if (!confirm('Estornar o pagamento desta parcela? Ela volta a ficar em aberto.')) return;
    const id = currentPaymentStudentId;
    try {
        const res = await fetch(`/api/students/${id}/mensalidades/${mid}/estornar`, { method: 'POST' });
        if (!res.ok) { alert(await errMsg(res, 'Erro ao estornar.')); return; }
        await refreshAndRerenderPayment(id);
    } catch (e) { alert('Sem conexão com o sistema.'); }
}

async function confirmRenovar() {
    const id = currentPaymentStudentId;
    const meses = document.getElementById('renovMeses').value;
    const valor = document.getElementById('renovValor').value;
    const btn = document.querySelector('#renovPanel .btn-primary');
    const orig = btn.innerHTML;
    btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Gerando...';
    btn.disabled = true;
    try {
        const res = await fetch(`/api/students/${id}/renovar`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ meses: parseInt(meses), valor })
        });
        if (!res.ok) { alert(await errMsg(res, 'Erro ao renovar.')); return; }
        await refreshAndRerenderPayment(id);
    } catch (e) {
        alert('Sem conexão com o sistema.');
    } finally {
        btn.innerHTML = orig;
        btn.disabled = false;
    }
}

async function refreshAndRerenderPayment(id) {
    await fetchStudents();
    await fetchAlerts();
    await fetchActivity();
    const s = studentsData.find(st => st.id === id);
    if (s && document.getElementById('modalPayment').style.display === 'flex') renderPaymentModal(s);
}

async function errMsg(res, fallback) {
    try { const e = await res.json(); if (e && e.error) return e.error; } catch (_) {}
    return fallback;
}

// ─── TOGGLE STATUS ────────────────────────────────────────────────────────────
async function toggleStudentStatus(id, btnElement) {
    btnElement.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
    btnElement.disabled = true;
    try {
        await fetch(`/api/students/${id}/toggle_status`, { method: 'POST' });
        await fetchStudents();
        await fetchAlerts();
        await fetchActivity();
    } catch (e) {
        alert('Erro ao alterar status!');
        btnElement.disabled = false;
    }
}

// ─── STUDENT ACTION ───────────────────────────────────────────────────────────
let isProcessingAction = false;
async function studentAction(btnElement, id, actionStr) {
    if (isProcessingAction) return;
    isProcessingAction = true;
    const originalContent = btnElement.innerHTML;
    btnElement.style.opacity = '0.5';
    try {
        await fetch(`/api/students/${id}/action`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: actionStr })
        });
        await fetchStudents();
        await fetchActivity();
        const updatedStudent = studentsData.find(st => st.id === id);
        if (updatedStudent && document.getElementById('modalActions').style.display === 'flex') {
            renderActionModalContent(updatedStudent);
        }
    } catch (e) {
        alert('Erro na conexão!');
        btnElement.innerHTML = originalContent;
        btnElement.style.opacity = '1';
    } finally {
        isProcessingAction = false;
    }
}

// ─── EDIT STUDENT ─────────────────────────────────────────────────────────────
function editStudent(id) {
    const student = studentsData.find(s => s.id === id);
    if (!student) return;
    openStudentModal();
    document.getElementById('studentId').value = student.id;
    document.getElementById('name').value = student.name;
    document.getElementById('plan').value = student.plan;
    document.getElementById('startDate').value = student.startDate;
    document.getElementById('endDate').value = student.endDate || '';
    document.getElementById('price').value = formatPriceInput(student.price);
    document.getElementById('classesPerWeek').value = student.classesPerWeek || 2;
    document.getElementById('lastPayment').value = student.lastPayment || '';
    document.getElementById('nextPayment').value = student.nextPayment || '';
    document.getElementById('saldoAulas').value = student.credits || 0;
    if (document.getElementById('paymentDay')) document.getElementById('paymentDay').value = student.paymentDay || 30;
    const fp = document.getElementById('firstPaid'); if (fp) fp.checked = false;
    document.getElementById('studentModalTitle').innerText = 'Editar Aluno';
    if (student.class_ids) {
        student.class_ids.forEach(clsId => {
            const cb = document.querySelector(`input[name="selectedClasses"][value="${clsId}"]`);
            if (cb) cb.checked = true;
        });
    }
    renderParcelaPreview();
}

// ─── OPEN STUDENT MODAL ───────────────────────────────────────────────────────
function openStudentModal() {
    const container = document.getElementById('classSelector');
    container.innerHTML = '';
    document.getElementById('studentId').value = '';
    document.getElementById('studentModalTitle').innerText = 'Novo Aluno';
    document.getElementById('studentForm').reset();
    setTodayDate();

    const daysOrder = ['Segunda', 'Terça', 'Quarta', 'Quinta', 'Sexta', 'Sábado'];
    const sortedClasses = [...classesData].sort((a, b) =>
        daysOrder.indexOf(a.day) - daysOrder.indexOf(b.day) || a.time.localeCompare(b.time));

    sortedClasses.forEach(c => {
        const isFull = c.student_count >= c.capacity;
        const statusText = isFull
            ? `<span style="color:var(--danger); font-weight:bold">(${c.student_count}/${c.capacity} LOTADO)</span>`
            : `(${c.student_count}/${c.capacity})`;
        container.innerHTML += `
            <label class="check-item" style="${isFull ? 'background:#FEF6F4' : ''}">
                <input type="checkbox" value="${c.id}" name="selectedClasses" onchange="enforceClassLimit(this)">
                <div>
                    <span style="font-weight:700; font-size:0.8rem">${esc(c.day)} - ${esc(c.time)}</span>
                    <div style="font-size:0.73rem; color:var(--muted)">${esc(c.professor)} ${statusText}</div>
                </div>
            </label>`;
    });
    document.getElementById('modalStudent').style.display = 'flex';
}

// ─── CLASS DETAILS MODAL ──────────────────────────────────────────────────────
function openClassDetails(classId) {
    const cls = classesData.find(c => c.id === classId);
    if (!cls) return;
    document.getElementById('currentClassId').value = cls.id;
    document.getElementById('detailClassTitle').innerText = `${cls.day} - ${cls.time} (${cls.professor})`;
    renderEnrolledList(cls);
    renderStudentSelect(cls);
    document.getElementById('modalClassDetails').style.display = 'flex';
}

function renderEnrolledList(cls) {
    const list = document.getElementById('enrolledList');
    list.innerHTML = '';
    if (!cls.students || cls.students.length === 0) {
        list.innerHTML = '<div style="color:var(--muted); font-size:0.85rem; text-align:center; padding:12px;">Nenhum aluno.</div>';
        return;
    }
    cls.students.forEach(s => {
        list.innerHTML += `
            <div class="enrolled-item">
                <span style="font-weight:600;">${esc(s.name)}</span>
                <button onclick="removeStudentFromClass(${s.id}, this)" style="color:var(--danger); background:none; border:none; cursor:pointer; font-size:1rem;"><i class="fa-solid fa-user-minus"></i></button>
            </div>`;
    });
}

function renderStudentSelect(cls) {
    const select = document.getElementById('studentToAdd');
    select.innerHTML = '<option value="">Selecione um aluno...</option>';
    const enrolledIds = cls.students ? cls.students.map(s => s.id) : [];
    [...studentsData].sort((a, b) => a.name.localeCompare(b.name)).forEach(s => {
        if (!enrolledIds.includes(s.id)) select.innerHTML += `<option value="${s.id}">${esc(s.name)}</option>`;
    });
}

// ─── FORM SUBMISSIONS ─────────────────────────────────────────────────────────
document.getElementById('studentForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const submitBtn = e.target.querySelector('button[type="submit"]');
    if (submitBtn.disabled) return;
    const originalBtnText = submitBtn.innerHTML;
    submitBtn.disabled = true;
    submitBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Salvando...';
    try {
        const id = document.getElementById('studentId').value;
        const isEdit = !!id;
        const classIds = Array.from(document.querySelectorAll('input[name="selectedClasses"]:checked')).map(cb => parseInt(cb.value));
        const startInput = document.getElementById('startDate').value;
        const endInput = document.getElementById('endDate').value;
        const plan = document.getElementById('plan').value;
        const paymentDay = parseInt(document.getElementById('paymentDay').value) || 30;

        let endDate = endInput;
        if (!endDate) {
            const start = new Date(startInput + 'T12:00:00');
            const months = plan === 'Mensal' ? 1 : plan === 'Trimestral' ? 3 : 6;
            const endObj = new Date(start);
            endObj.setMonth(endObj.getMonth() + months);
            endDate = endObj.toISOString().split('T')[0];
        }

        const firstPaid = document.getElementById('firstPaid')?.checked || false;
        const data = {
            name: document.getElementById('name').value, plan,
            price: document.getElementById('price').value,
            startDate: startInput, endDate, paymentDay,
            firstPaid,
            lastPayment: document.getElementById('lastPayment').value,
            classesPerWeek: document.getElementById('classesPerWeek').value,
            saldoAulas: document.getElementById('saldoAulas').value, classIds
        };
        const url = isEdit ? `/api/students/${id}/update` : '/api/students';
        const method = isEdit ? 'PUT' : 'POST';
        const res = await fetch(url, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
        if (!res.ok) { alert(await errMsg(res, 'Não foi possível salvar o aluno.')); return; }
        closeModals();
        await loadAll();
        e.target.reset();
        setTodayDate();
    } catch (error) {
        alert('Sem conexão com o sistema. Tente novamente.');
    } finally {
        submitBtn.disabled = false;
        submitBtn.innerHTML = originalBtnText;
    }
});

document.getElementById('classForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const submitBtn = e.target.querySelector('button[type="submit"]');
    if (submitBtn.disabled) return;
    submitBtn.disabled = true;
    const orig = submitBtn.innerHTML;
    submitBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Criando...';
    try {
        const data = {
            day: document.getElementById('classDay').value,
            time: document.getElementById('classTime').value,
            capacity: document.getElementById('classCapacity').value,
            professor: document.getElementById('classProf').value
        };
        await fetch('/api/classes', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
        closeModals();
        await fetchClasses();
        await fetchActivity();
        e.target.reset();
    } catch (error) {
        alert('Erro ao criar turma.');
    } finally {
        submitBtn.disabled = false;
        submitBtn.innerHTML = orig;
    }
});

// ─── CLASS STUDENT MANAGEMENT ─────────────────────────────────────────────────
let isAddingStudent = false;
async function addStudentToCurrentClass() {
    if (isAddingStudent) return;
    const classId = document.getElementById('currentClassId').value;
    const studentId = document.getElementById('studentToAdd').value;
    if (!studentId) return;
    isAddingStudent = true;
    const btn = document.querySelector('#modalClassDetails .btn-primary');
    const orig = btn.innerHTML;
    btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
    try {
        await fetch(`/api/classes/${classId}/add_student`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ student_id: studentId })
        });
        await loadAll();
        const updated = classesData.find(c => c.id == classId);
        if (updated) { renderEnrolledList(updated); renderStudentSelect(updated); }
    } finally {
        isAddingStudent = false;
        btn.innerHTML = orig;
    }
}

async function removeStudentFromClass(studentId, btnElement) {
    if (!confirm('Remover da aula?')) return;
    btnElement.style.opacity = '0.3';
    btnElement.style.pointerEvents = 'none';
    const classId = document.getElementById('currentClassId').value;
    await fetch(`/api/classes/${classId}/remove_student`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ student_id: studentId })
    });
    await loadAll();
    const updated = classesData.find(c => c.id == classId);
    if (updated) { renderEnrolledList(updated); renderStudentSelect(updated); }
}

// ─── DELETE ───────────────────────────────────────────────────────────────────
async function deleteStudent(id, btnElement) {
    if (confirm('Excluir aluno definitivamente?')) {
        btnElement.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
        await fetch(`/api/students/${id}/delete`, { method: 'DELETE' });
        loadAll();
    }
}

async function deleteClass(id, btnElement) {
    if (confirm('Excluir esta turma?')) {
        btnElement.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
        await fetch(`/api/classes/${id}`, { method: 'DELETE' });
        loadAll();
    }
}

// ─── UI HELPERS ───────────────────────────────────────────────────────────────
function switchTab(tab) {
    document.querySelectorAll('.view-section').forEach(el => el.classList.remove('active'));
    const target = document.getElementById(`view-${tab}`);
    if (target) target.classList.add('active');
    document.querySelectorAll('.nav-btn').forEach(btn => {
        btn.classList.remove('active');
        if (btn.getAttribute('onclick')?.includes(`'${tab}'`)) btn.classList.add('active');
    });
    if (tab === 'activity') fetchActivity();
    if (window.innerWidth <= 820) document.getElementById('sidebar')?.classList.remove('active');
}

function toggleRepoFilter() {
    repoFilter = !repoFilter;
    alertFilter = null;
    document.getElementById('repoFilterCard').style.outline = repoFilter ? '2px solid var(--primary)' : 'none';
    clearAlertOutlines();
    switchTab('students');
    renderStudentTable();
}

function clearAlertOutlines() {
    ['alertOverdueCard', 'alertSoonCard'].forEach(t => {
        const c = document.getElementById(t);
        if (c) c.style.outline = 'none';
    });
}

function toggleAlertFilter(type) {
    alertFilter = alertFilter === type ? null : type;
    repoFilter = false;
    document.getElementById('repoFilterCard').style.outline = 'none';
    clearAlertOutlines();
    const card = document.getElementById(type === 'overdue' ? 'alertOverdueCard' : 'alertSoonCard');
    if (card && alertFilter === type) card.style.outline = '2px solid var(--primary)';
    switchTab('students');
    renderStudentTable();
}

function updateStats() {
    if (!studentsData) return;
    const totalRepos = studentsData.reduce((acc, s) => acc + (s.reposicoes_count || 0), 0);
    const el = document.getElementById('totalReposicoes');
    if (el) el.innerText = totalRepos;
    const sub = document.getElementById('studentsSub');
    if (sub) {
        const ativos = studentsData.filter(s => s.active).length;
        sub.innerText = `${ativos} ativo(s) · ${totalRepos} reposição(ões) pendente(s)`;
    }
}

function formatDate(d) {
    if (!d) return '-';
    const p = d.split('-');
    if (p.length < 3) return d;
    return `${p[2]}/${p[1]}`;
}

function formatPrice(val) {
    if (val === undefined || val === null) return '';
    const n = parseFloat(val);
    return 'R$ ' + n.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function formatPriceInput(val) {
    if (val === undefined || val === null) return '';
    const n = parseFloat(val);
    return n.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function toggleMenu() { document.getElementById('sidebar')?.classList.toggle('active'); }
function openClassModal() { document.getElementById('modalClass').style.display = 'flex'; }
function closeModals() { document.querySelectorAll('.modal-overlay').forEach(m => m.style.display = 'none'); }
// Busca com debounce: não re-renderiza a lista inteira a cada tecla (trava no
// celular). Espera 140ms parar de digitar.
let _searchTimer = null;
function filterStudents() {
    clearTimeout(_searchTimer);
    _searchTimer = setTimeout(renderStudentTable, 140);
}

document.querySelectorAll('.modal-overlay').forEach(overlay => {
    overlay.addEventListener('click', (e) => { if (e.target === overlay) closeModals(); });
});
