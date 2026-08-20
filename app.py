import os
import re
import sys
import calendar
import socket
import uuid
import threading
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, redirect, url_for, send_from_directory, abort
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.orm import declared_attr
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename


# Modo dev só é aceito rodando via interpretador Python puro. Um .exe compilado
# (Nuitka/PyInstaller, o que é distribuído aos clientes) tem sys.frozen=True —
# então NENHUM valor de AMP_DEV no ambiente consegue reativar o modo dev nele,
# mesmo que o repositório seja público. Sem essa trava, "set AMP_DEV=1" antes
# de abrir o .exe instalado liberava todos os módulos sem licença nenhuma.
DEV_MODE = bool(os.environ.get('AMP_DEV')) and not getattr(sys, 'frozen', False)


def _resource_path(rel):
    """Acha templates/static rodando normal, no PyInstaller ou no Nuitka (compilado)."""
    candidates = []
    if hasattr(sys, '_MEIPASS'):
        candidates.append(sys._MEIPASS)              # PyInstaller
    candidates.append(os.path.dirname(sys.executable))  # Nuitka standalone (dados ao lado do .exe)
    candidates.append(os.path.dirname(os.path.abspath(__file__)))
    for base in candidates:
        pth = os.path.join(base, rel)
        if os.path.exists(pth):
            return pth
    return os.path.join(candidates[-1], rel)


def _local_data_dir():
    """Pasta gravável p/ o banco quando instalado no PC do cliente."""
    base = os.environ.get('APPDATA') or os.path.expanduser('~')
    d = os.path.join(base, 'ArenaAMP')
    os.makedirs(d, exist_ok=True)
    return d


def _get_secret_key():
    """Usa SECRET_KEY do ambiente (hospedado) ou gera uma aleatória por instalação (local)."""
    env = os.environ.get('SECRET_KEY')
    if env:
        return env
    path = os.path.join(_local_data_dir(), 'secret.key')
    try:
        if os.path.exists(path):
            return open(path).read().strip()
        import secrets
        k = secrets.token_hex(32)
        with open(path, 'w') as f:
            f.write(k)
        return k
    except Exception:
        return 'chave_local_fallback'


app = Flask(__name__,
            template_folder=_resource_path('templates'),
            static_folder=_resource_path('static'))
app.config['SECRET_KEY'] = _get_secret_key()

db_url = os.environ.get('DATABASE_URL')
if db_url and db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
if not db_url:
    # Instalação local: banco em %APPDATA%\ArenaAMP\arena.db (não some, é gravável)
    _db_path = os.path.join(_local_data_dir(), 'arena.db').replace('\\', '/')
    db_url = 'sqlite:///' + _db_path
app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
# "Lembrar meu acesso": cookie persistente de 1 ano (o perfil do WebView é
# fixo por instalação, então o login se mantém entre aberturas do app).
app.config['REMEMBER_COOKIE_DURATION'] = timedelta(days=365)
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=365)

enrollments = db.Table('enrollments',
    db.Column('student_id', db.Integer, db.ForeignKey('student.id'), primary_key=True),
    db.Column('class_session_id', db.Integer, db.ForeignKey('class_session.id'), primary_key=True)
)

ALL_MODULES = ['aulas', 'ranking', 'comandas', 'relatorios', 'config', 'assinatura']

class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), nullable=True, default='user')   # 'adm' vê tudo; 'user' segue perms
    perms = db.Column(db.String(200), nullable=True, default='')     # csv de módulos liberados
    photo = db.Column(db.String(200), nullable=True)                 # nome do arquivo em uploads/
    # colunas de sync (usuários sincronizam pro login na nuvem) — ver PLANO_ACESSO_REMOTO.md
    sync_uid = db.Column(db.String(36), index=True)
    updated_at = db.Column(db.String(26))
    deleted_at = db.Column(db.String(26), nullable=True)
    def set_password(self, p): self.password_hash = generate_password_hash(p)
    def check_password(self, p): return check_password_hash(self.password_hash, p)
    @property
    def is_adm(self):
        return (self.role or 'user') == 'adm'
    def perm_list(self):
        return [p for p in (self.perms or '').split(',') if p]
    def can(self, module):
        return self.is_adm or module in self.perm_list()

class Setting(db.Model):
    """Chave/valor para identidade global da arena (nome, logo, cor)."""
    __tablename__ = 'settings'
    key = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.Text, nullable=True)

class SyncMixin:
    """Colunas de sincronização (Fase de acesso remoto). São ADITIVAS: os ids
    autoincrement continuam iguais; o `sync_uid` (UUID) é a identidade estável
    entre PC e nuvem. Carimbadas automaticamente por eventos (ver _stamp_sync).
    Ver PLANO_ACESSO_REMOTO.md."""
    @declared_attr
    def sync_uid(cls):
        return db.Column(db.String(36), index=True)
    @declared_attr
    def updated_at(cls):
        return db.Column(db.String(26))     # 'YYYY-MM-DD HH:MM:SS.ffffff' (microssegundos)
    @declared_attr
    def deleted_at(cls):
        return db.Column(db.String(26), nullable=True)  # soft-delete (usado na Fase de sync)

class ClassSession(SyncMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    day = db.Column(db.String(20), nullable=False)
    time = db.Column(db.String(10), nullable=False)
    professor = db.Column(db.String(50), nullable=False)
    capacity = db.Column(db.Integer, default=6)
    def to_dict(self):
        return {
            'id': self.id, 'day': self.day, 'time': self.time,
            'professor': self.professor, 'capacity': self.capacity,
            'student_count': len(self.students),
            'students': [{'id': s.id, 'name': s.name} for s in self.students]
        }

class Replacement(SyncMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('student.id'), nullable=False)
    created_at = db.Column(db.String(10), nullable=False)
    expires_at = db.Column(db.String(10), nullable=False)

class StudentHistory(SyncMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('student.id'), nullable=False)
    description = db.Column(db.String(200), nullable=False)
    date_str = db.Column(db.String(20), nullable=False)
    action_type = db.Column(db.String(30), nullable=True, default='info')
    credit_delta = db.Column(db.Integer, nullable=True, default=0)

class ActivityLog(SyncMixin, db.Model):
    """Registro geral de tudo que acontece no ecossistema (auditoria global).
    Sincroniza PC↔nuvem: assim o dono vê no 'Atividades' o que a funcionária fez
    pelo celular (e vice-versa)."""
    id = db.Column(db.Integer, primary_key=True)
    system = db.Column(db.String(20), nullable=True, default='aulas')     # aulas/comandas/ranking/config
    category = db.Column(db.String(20), nullable=False, default='info')  # presenca/falta/reposicao/pagamento/aluno/turma/matricula/exclusao/...
    action_type = db.Column(db.String(30), nullable=True, default='info')
    description = db.Column(db.String(300), nullable=False)
    user = db.Column(db.String(50), nullable=True, default='sistema')
    date_str = db.Column(db.String(20), nullable=False)     # exibição: dd/mm/YYYY HH:MM
    created_at = db.Column(db.String(20), nullable=False)    # ordenação/agrupamento: YYYY-MM-DD HH:MM:SS

    def to_dict(self):
        return {
            'id': self.id,
            'system': self.system or 'aulas',
            'category': self.category,
            'action_type': self.action_type,
            'description': self.description,
            'user': self.user or 'sistema',
            'date': self.date_str,
            'datetime': self.created_at,
        }

class Mensalidade(SyncMixin, db.Model):
    """Uma parcela mensal do plano do aluno. Cada mês do plano vira uma linha.
    O status (paga/vencida/a vencer) é DERIVADO de pago_em + vencimento — nunca
    guardado, pra não ficar dessincronizado."""
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('student.id'), nullable=False)
    ciclo = db.Column(db.Integer, nullable=False, default=1)   # 1 = plano original; +1 a cada renovação
    numero = db.Column(db.Integer, nullable=False, default=1)  # nº da parcela dentro do ciclo
    mes_ref = db.Column(db.String(7), nullable=False)          # 'YYYY-MM' (competência)
    vencimento = db.Column(db.String(10), nullable=False)      # 'YYYY-MM-DD'
    valor = db.Column(db.Float, nullable=False, default=0.0)
    pago_em = db.Column(db.String(10), nullable=True)          # 'YYYY-MM-DD' quando paga

    def status(self, today_iso=None):
        if self.pago_em:
            return 'paga'
        if today_iso is None:
            today_iso = datetime.now().strftime('%Y-%m-%d')
        return 'vencida' if self.vencimento < today_iso else 'a_vencer'

    def to_dict(self, today_iso=None):
        return {
            'id': self.id, 'ciclo': self.ciclo, 'numero': self.numero,
            'mesRef': self.mes_ref, 'vencimento': self.vencimento,
            'valor': self.valor, 'pagoEm': self.pago_em,
            'status': self.status(today_iso),
        }

class Student(SyncMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    plan = db.Column(db.String(20), nullable=False)
    start_date = db.Column(db.String(10), nullable=False)
    end_date = db.Column(db.String(10), nullable=False)
    payment_day = db.Column(db.Integer, nullable=False, default=30)
    next_payment = db.Column(db.String(10), nullable=False)
    last_payment = db.Column(db.String(10), nullable=True)
    classes_per_week = db.Column(db.Integer, default=2)
    credits = db.Column(db.Integer, default=0)
    price = db.Column(db.Float, nullable=False)
    active = db.Column(db.Boolean, default=True)

    replacements = db.relationship('Replacement', backref='student', lazy=True, cascade="all, delete-orphan")
    classes = db.relationship('ClassSession', secondary=enrollments, lazy='subquery',
                              backref=db.backref('students', lazy=True))
    history_logs = db.relationship('StudentHistory', backref='student', lazy=True,
                                   cascade="all, delete-orphan",
                                   order_by="desc(StudentHistory.id)")
    mensalidades = db.relationship('Mensalidade', backref='student', lazy=True,
                                   cascade="all, delete-orphan",
                                   order_by="Mensalidade.vencimento")

    def to_dict(self):
        classes_str = ", ".join([f"{c.day[:3]} {c.time}" for c in self.classes])
        today = datetime.now().date()
        today_iso = today.strftime('%Y-%m-%d')
        payment_alert = False
        payment_overdue = False
        plan_expiring = False

        mens = list(self.mensalidades)
        parcelas_pagas = sum(1 for m in mens if m.pago_em)
        parcelas_total = len(mens)

        if mens:
            # Modelo NOVO: status vem das parcelas. Vencido = existe parcela
            # vencida e não paga. Próx. vencimento = 1ª parcela em aberto.
            abertas = [m for m in mens if not m.pago_em]
            for m in abertas:
                if m.vencimento < today_iso:
                    payment_overdue = True
                    break
            prox = min(abertas, key=lambda m: m.vencimento) if abertas else None
            next_payment_val = prox.vencimento if prox else (mens[-1].vencimento if mens else self.next_payment)
            if prox and not payment_overdue:
                try:
                    delta = (datetime.strptime(prox.vencimento, '%Y-%m-%d').date() - today).days
                    if 0 <= delta <= 7:
                        payment_alert = True
                except:
                    pass
        else:
            # Fallback (aluno sem parcelas geradas): lógica antiga por next_payment.
            next_payment_val = self.next_payment
            if self.next_payment:
                try:
                    delta = (datetime.strptime(self.next_payment, '%Y-%m-%d').date() - today).days
                    if delta < 0:
                        payment_overdue = True
                    elif delta <= 7:
                        payment_alert = True
                except:
                    pass

        if self.end_date:
            try:
                end = datetime.strptime(self.end_date, '%Y-%m-%d').date()
                if (end - today).days <= 14:
                    plan_expiring = True
            except:
                pass

        return {
            'id': self.id,
            'name': self.name,
            'plan': self.plan,
            'startDate': self.start_date,
            'endDate': self.end_date,
            'paymentDay': self.payment_day,
            'nextPayment': next_payment_val,
            'lastPayment': self.last_payment,
            'classesPerWeek': self.classes_per_week,
            'credits': self.credits,
            'price': self.price,
            'active': self.active,
            'classes_desc': classes_str,
            'class_ids': [c.id for c in self.classes],
            'reposicoes_count': len(self.replacements),
            'reposicoes_details': [{'id': r.id, 'expires': r.expires_at} for r in self.replacements],
            'history': [{'id': h.id, 'desc': h.description, 'date': h.date_str,
                         'action_type': h.action_type, 'credit_delta': h.credit_delta}
                        for h in self.history_logs],
            'payment_alert': payment_alert,
            'payment_overdue': payment_overdue,
            'plan_expiring': plan_expiring,
            'mensalidades': [m.to_dict(today_iso) for m in mens],
            'parcelasPagas': parcelas_pagas,
            'parcelasTotal': parcelas_total,
        }

# ── Carimbo de sync ───────────────────────────────────────────────────────────
# Seta sync_uid (uma vez) + updated_at (sempre) em todo insert/update dos modelos
# sincronizáveis. NÃO mexe em PK. É a base pro sync PC↔nuvem (ver PLANO_ACESSO_REMOTO.md).
import contextlib
_SYNC_MODELS = (User, Student, ClassSession, Replacement, StudentHistory, Mensalidade, ActivityLog)
_SYNC_APPLYING = {'on': False}

def _stamp_sync(mapper, connection, target):
    if _SYNC_APPLYING['on']:
        return  # aplicando sync: preserva sync_uid/updated_at vindos do peer
    if not getattr(target, 'sync_uid', None):
        target.sync_uid = str(uuid.uuid4())
    target.updated_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')

@contextlib.contextmanager
def applying():
    _SYNC_APPLYING['on'] = True
    try:
        yield
    finally:
        _SYNC_APPLYING['on'] = False

for _m in _SYNC_MODELS:
    event.listen(_m, 'before_insert', _stamp_sync)
    event.listen(_m, 'before_update', _stamp_sync)

# Especificação das tabelas pro motor de sync (mesma ordem/nomes do arena-sync).
import sync_core
SPECS = [
    sync_core.TableSpec('users', User, ['username', 'password_hash', 'role', 'perms', 'photo'],
                        natural_key='username'),
    sync_core.TableSpec('class_session', ClassSession, ['day', 'time', 'professor', 'capacity']),
    sync_core.TableSpec('student', Student, ['name', 'plan', 'start_date', 'end_date', 'payment_day',
                                             'next_payment', 'last_payment', 'classes_per_week',
                                             'credits', 'price', 'active'],
                        m2m={'class_uids': ('classes', 'class_session')}),
    sync_core.TableSpec('replacement', Replacement, ['created_at', 'expires_at'], {'student_id': 'student'}),
    sync_core.TableSpec('student_history', StudentHistory, ['description', 'date_str', 'action_type',
                                                            'credit_delta'], {'student_id': 'student'}),
    sync_core.TableSpec('mensalidade', Mensalidade, ['ciclo', 'numero', 'mes_ref', 'vencimento',
                                                     'valor', 'pago_em'], {'student_id': 'student'}),
    sync_core.TableSpec('activity_log', ActivityLog, ['system', 'category', 'action_type',
                                                      'description', 'user', 'date_str', 'created_at']),
]


@login_manager.user_loader
def load_user(uid):
    return db.session.get(User, int(uid))

def add_history(student_id, description, action_type='info', credit_delta=0):
    now_str = datetime.now().strftime('%d/%m/%Y %H:%M')
    log = StudentHistory(student_id=student_id, description=description,
                         date_str=now_str, action_type=action_type,
                         credit_delta=credit_delta)
    db.session.add(log)

def add_activity(description, category='info', action_type='info', system='aulas'):
    """Registra uma ação no log geral do ecossistema, guardando quem fez e em qual sistema."""
    try:
        uname = current_user.username if getattr(current_user, 'is_authenticated', False) else 'sistema'
    except Exception:
        uname = 'sistema'
    now = datetime.now()
    db.session.add(ActivityLog(
        system=system, category=category, action_type=action_type, description=description,
        user=uname, date_str=now.strftime('%d/%m/%Y %H:%M'),
        created_at=now.strftime('%Y-%m-%d %H:%M:%S')))

# Disponibiliza para os módulos (comandas/ranking) registrarem no mesmo log global
app.add_activity = add_activity

def compute_next_payment(payment_day: int, reference_date: datetime = None) -> str:
    """Calcula o próximo vencimento a partir de reference_date (a data do pagamento).

    Avança de mês em mês até cair numa data que já passou TANTO da data do
    pagamento QUANTO de hoje. Isso garante que registrar um pagamento sempre
    tira o aluno de 'vencido' — antes, avançava só 1 mês e, se a data informada
    fosse retroativa (ou o dia de vencimento fosse cedo no mês), o próximo
    vencimento caía antes de hoje e o aluno continuava aparecendo como vencido.
    """
    if reference_date is None:
        reference_date = datetime.now()
    try:
        pd = int(payment_day)
    except (TypeError, ValueError):
        pd = 30
    day = min(max(pd, 1), 28)  # dia fixo, capado em 28 pra nunca estourar em mês curto
    ref_d = reference_date.date()
    today = datetime.now().date()
    try:
        candidate = reference_date.replace(day=day)
    except ValueError:
        candidate = reference_date.replace(day=28)
    # trava de segurança: nunca laçar infinito (o servidor é single-thread e
    # um loop preso derrubaria o app inteiro). 120 iterações = 10 anos, folga de sobra.
    for _ in range(120):
        cand_d = candidate.date()
        if cand_d > ref_d and cand_d > today:
            break
        if candidate.month == 12:
            candidate = candidate.replace(year=candidate.year + 1, month=1)
        else:
            candidate = candidate.replace(month=candidate.month + 1)
    return candidate.strftime('%Y-%m-%d')


def parse_brl_money(value, default=0.0) -> float:
    """Lê um valor em reais tolerante ao que o usuário digita ou ao que o front manda:
    'R$ 1.200,50', '1.200,50', '150,90', '150.90', '150', 150.9, '', None → float.
    Nunca levanta exceção — em caso de dúvida devolve `default`. Isso evita que um
    campo mal digitado derrube o backend (que é single-thread) no meio de um commit."""
    if value is None:
        return float(default)
    if isinstance(value, (int, float)):
        return float(value)
    s = re.sub(r'[^\d,.\-]', '', str(value).strip())  # tira 'R$', espaços, letras
    if not s or s in ('-', '.', ','):
        return float(default)
    if ',' in s and '.' in s:
        s = s.replace('.', '').replace(',', '.')  # BR: ponto = milhar, vírgula = decimal
    elif ',' in s:
        s = s.replace(',', '.')                    # só vírgula = decimal
    elif '.' in s:
        # só ponto: pode ser decimal ('150.90') ou milhar BR ('1.200', '1.234.567').
        # Se TODO grupo após um ponto tiver exatamente 3 dígitos, é separador de milhar.
        parts = s.split('.')
        if len(parts) > 1 and all(len(p) == 3 for p in parts[1:]):
            s = s.replace('.', '')
    try:
        return float(s)
    except ValueError:
        return float(default)


def _parse_date_flex(value):
    """Aceita 'YYYY-MM-DD', 'DD/MM/YYYY' ou 'DD-MM-YYYY'. Devolve None se vazio/inválido."""
    if not value:
        return None
    value = str(value).strip()
    for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y'):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None

# ── PARCELAS / MENSALIDADES ───────────────────────────────────────────────────

PLANO_MESES = {'Mensal': 1, 'Trimestral': 3, 'Semestral': 6, 'Anual': 12}

def plan_months(plan) -> int:
    """Quantos meses (parcelas) um plano tem. Aceita nome ('Semestral') ou já
    um número ('6 meses' / 6). Default 1 (nunca 0, pra não gerar plano vazio)."""
    if isinstance(plan, (int, float)):
        return max(int(plan), 1)
    if plan in PLANO_MESES:
        return PLANO_MESES[plan]
    m = re.search(r'\d+', str(plan or ''))
    return max(int(m.group()), 1) if m else 1

def plano_por_meses(n) -> str:
    """Nome do plano a partir do nº de meses (pra renovação com duração custom)."""
    for nome, meses in PLANO_MESES.items():
        if meses == n:
            return nome
    return f"{n} meses"

def _last_day_of_month(year, month) -> int:
    return calendar.monthrange(year, month)[1]

def _dia_venc(year, month, dia) -> str:
    """Vencimento no dia fixo, respeitando meses curtos (dia 30 em fev → 28/29)."""
    d = min(max(int(dia), 1), _last_day_of_month(year, month))
    return f"{year:04d}-{month:02d}-{d:02d}"

def _competencias(start_dt, dia, meses):
    """Lista de (mes_ref 'YYYY-MM', vencimento 'YYYY-MM-DD') a partir do MÊS DE
    INÍCIO. A 1ª parcela é SEMPRE no mês em que o aluno começou (quem começou em
    fevereiro tem a 1ª parcela em fevereiro). O vencimento é o dia fixo do mês,
    mas a 1ª nunca vence antes da data de início — pra não nascer já vencida antes
    de o aluno começar. Se não foi paga, aparece pendente/vencida normalmente
    (o plano só fica 'quitado' quando o operador registra todos os meses)."""
    y, m = start_dt.year, start_dt.month
    start_iso = start_dt.strftime('%Y-%m-%d')
    out = []
    for i in range(meses):
        yy = y + (m - 1 + i) // 12
        mm = (m - 1 + i) % 12 + 1
        venc = _dia_venc(yy, mm, dia)
        if i == 0 and venc < start_iso:
            venc = start_iso
        out.append((f"{yy:04d}-{mm:02d}", venc))
    return out

def gerar_mensalidades(student, meses, valor, dia, ciclo, start_dt,
                       primeira_paga=False, primeira_pago_em=None):
    """Cria as parcelas de um ciclo do plano. Faz append na relação (não seta
    student_id na mão) pra que student.mensalidades fique em dia na memória mesmo
    quando a coleção já tinha sido carregada — senão end_date/next_payment não
    enxergam as parcelas recém-criadas numa renovação."""
    comps = _competencias(start_dt, dia, meses)
    criadas = []
    for i, (mes_ref, venc) in enumerate(comps, start=1):
        pago = None
        if i == 1 and primeira_paga:
            pago = primeira_pago_em or datetime.now().strftime('%Y-%m-%d')
        mm = Mensalidade(ciclo=ciclo, numero=i, mes_ref=mes_ref, vencimento=venc,
                         valor=float(valor or 0.0), pago_em=pago)
        student.mensalidades.append(mm)
        criadas.append(mm)
    return criadas

def sync_pagamento_cache(student):
    """Mantém next_payment/last_payment do aluno em dia com as parcelas — assim o
    dashboard e qualquer leitor antigo continuam funcionando sem alteração."""
    mens = list(student.mensalidades)
    if not mens:
        return
    abertas = sorted([m for m in mens if not m.pago_em], key=lambda m: m.vencimento)
    pagas = sorted([m for m in mens if m.pago_em], key=lambda m: m.pago_em)
    student.next_payment = abertas[0].vencimento if abertas else mens[-1].vencimento
    student.last_payment = pagas[-1].pago_em if pagas else (student.last_payment or '')

# ── IDENTIDADE GLOBAL (Configurações) ─────────────────────────────────────────

DEFAULT_IDENTITY = {'arena_name': 'Arena AMP', 'accent': '#FF7A1A', 'logo': ''}

def _uploads_dir():
    d = os.path.join(_local_data_dir(), 'uploads')
    os.makedirs(d, exist_ok=True)
    return d

def get_setting(key, default=''):
    try:
        s = db.session.get(Setting, key)
        return s.value if (s and s.value not in (None, '')) else default
    except Exception:
        return default

def set_setting(key, value):
    s = db.session.get(Setting, key)
    if not s:
        s = Setting(key=key)
        db.session.add(s)
    s.value = value

def get_identity():
    return {
        'arena_name': get_setting('arena_name', DEFAULT_IDENTITY['arena_name']),
        'accent':     get_setting('accent',     DEFAULT_IDENTITY['accent']),
        'logo':       get_setting('logo',        DEFAULT_IDENTITY['logo']),
    }

@app.context_processor
def inject_identity():
    """Deixa a identidade da arena disponível em todos os templates como `arena`."""
    try:
        return {'arena': get_identity()}
    except Exception:
        return {'arena': dict(DEFAULT_IDENTITY)}

@app.route('/uploads/<path:filename>')
@login_required
def uploaded_file(filename):
    return send_from_directory(_uploads_dir(), filename)

@app.route('/api/identity')
@login_required
def api_identity():
    """Identidade da arena para os módulos aplicarem cor/nome/logo (qualquer usuário logado)."""
    ident = get_identity()
    ident['logo_url'] = url_for('uploaded_file', filename=ident['logo']) if ident['logo'] else None
    return jsonify(ident)


# ── PLANO / ASSINATURA ────────────────────────────────────────────────────────
CONTATO = {'whatsapp': '(11) 97244-7927', 'whatsapp_link': 'https://wa.me/5511972447927',
           'email': 'fehgodinho98@gmail.com'}

# Link do site do sistema (card "Site" no Hub) — só abre pra quem tem o add-on
# de site contratado. Placeholder genérico por enquanto; troque quando tiver
# o site de verdade (um lugar só).
SITE_URL = 'https://www.google.com'

def _plan_info():
    """Estado do plano do cliente. EM PRODUÇÃO deve vir do payload ASSINADO da licença
    (license_client) — nunca de um flag local, senão é trivial de burlar.
    Em modo dev, lê a Setting 'dev_plan' só para pré-visualizar os estados."""
    plan, dias, has_site, site_url = 'free', None, False, ''
    if DEV_MODE:
        plan = get_setting('dev_plan', 'free')
        if plan == 'demo':
            dias = int(get_setting('dev_demo_dias', '7') or 7)
        has_site = get_setting('dev_has_site', '') == '1'
        site_url = get_setting('dev_site_url', '') or SITE_URL
    else:
        try:
            import license_client
            info = license_client.get_plan()   # {'plan', 'demo_days_left', 'has_site', 'site_url'}
            plan = info.get('plan', 'free')
            dias = info.get('demo_days_left')
            has_site = bool(info.get('has_site'))
            site_url = info.get('site_url', '') or ''
        except Exception:
            plan = 'free'
    return {'plan': plan, 'demo_days_left': dias, 'is_premium': plan == 'premium',
            'is_demo': plan == 'demo', 'has_site': has_site, 'site_url': site_url, 'contato': CONTATO}

@app.route('/api/plan')
@login_required
def api_plan():
    return jsonify(_plan_info())

def _banner_text():
    if DEV_MODE:
        return get_setting('dev_banner', '')
    try:
        import license_client
        return license_client.get_banner()
    except Exception:
        return ''

@app.route('/api/banner')
@login_required
def api_banner():
    return jsonify({'banner': _banner_text()})

@app.route('/api/promo')
@login_required
def api_promo():
    """Banners promocionais GLOBAIS, configurados no /admin do license-server.
    Valem pra todos os produtos (arena, oficina...)."""
    try:
        import license_client
        return jsonify(license_client.get_promo())
    except Exception:
        return jsonify({'banners': [], 'image_url': '', 'wa_text': ''})

@app.route('/api/prices')
@login_required
def api_prices():
    """Preços vigentes (sistema/site) definidos no /admin do license-server,
    pra o modal de planos exibir o valor certo. {} = usa o padrão embutido."""
    if os.environ.get('AMP_DEV'):
        return jsonify({})
    try:
        import license_client
        return jsonify(license_client.get_prices())
    except Exception:
        return jsonify({})


@app.route('/api/brand')
@login_required
def api_brand():
    """Identidade central do estúdio (PG System) + nome/logo deste cliente,
    definidos no /admin. O app usa pra assinar 'por <marca>' e mostrar o contato
    de suporte de forma variável. Fallback embutido se o servidor não responder."""
    fb = {'brand_name': 'PG SYSTEMS', 'brand_legal': 'Fernando Prestes Godinho',
          'brand_contact': 'WhatsApp (11) 97244-7927 · fehgodinho98@gmail.com',
          'product_name': '', 'product_logo': ''}
    if os.environ.get('AMP_DEV'):
        return jsonify(fb)
    try:
        import license_client
        data = license_client.get_brand() or {}
        for k, v in fb.items():
            data.setdefault(k, v)
        return jsonify(data)
    except Exception:
        return jsonify(fb)

@app.route('/api/manutencao', methods=['POST'])
@login_required
def api_manutencao():
    """Manutenção preventiva: faz backup, remove registros de atividade antigos
    (>180 dias) e otimiza o banco (VACUUM/ANALYZE). NÃO apaga alunos, comandas,
    pagamentos etc. — só limpeza técnica pra manter o sistema rápido."""
    if not adm_required():
        return jsonify({'error': 'Acesso restrito ao administrador.'}), 403
    # Segurança: um backup antes de qualquer limpeza.
    try:
        auto_backup()
    except Exception:
        pass
    removed = 0
    try:
        corte = (datetime.now() - timedelta(days=180)).strftime('%Y-%m-%d %H:%M:%S')
        q = ActivityLog.query.filter(ActivityLog.created_at.isnot(None),
                                     ActivityLog.created_at < corte)
        removed = q.count()
        q.delete(synchronize_session=False)
        db.session.commit()
    except Exception:
        db.session.rollback()
    freed = 0
    try:
        if db.engine.url.drivername.startswith('sqlite') and db.engine.url.database:
            import sqlite3
            path = db.engine.url.database
            before = os.path.getsize(path) if os.path.exists(path) else 0
            db.session.close()
            db.engine.dispose()   # solta o lock antes do VACUUM
            con = sqlite3.connect(path)
            con.execute('VACUUM')
            con.execute('ANALYZE')
            con.close()
            after = os.path.getsize(path) if os.path.exists(path) else 0
            freed = max(0, before - after)
    except Exception:
        pass
    return jsonify({'ok': True, 'logs_removidos': removed,
                    'espaco_liberado_kb': round(freed / 1024.0, 1)})


@app.route('/api/factory-reset', methods=['POST'])
@login_required
def api_factory_reset():
    """Zera os DADOS operacionais (alunos, turmas, comandas, ranking, atividade)
    de TODOS os módulos, mantendo apenas os usuários/login e a identidade da
    arena (nome, logo, cor). Faz backup antes. Ação destrutiva — exige confirmação."""
    if not adm_required():
        return jsonify({'error': 'Acesso restrito ao administrador.'}), 403
    body = request.get_json(silent=True) or {}
    if (body.get('confirm') or '').strip().upper() != 'ZERAR':
        return jsonify({'error': 'Confirmação inválida.'}), 400
    try:
        auto_backup()
    except Exception:
        pass
    keep = {'users', 'settings'}   # preserva login e identidade da arena
    apagados = 0
    try:
        # ordem reversa de dependência (filhos primeiro) evita conflito de FK
        for table in reversed(db.metadata.sorted_tables):
            if table.name in keep:
                continue
            apagados += db.session.execute(table.delete()).rowcount or 0
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': 'Falha ao zerar: ' + str(e)}), 500
    # otimiza depois de esvaziar
    try:
        if db.engine.url.drivername.startswith('sqlite') and db.engine.url.database:
            import sqlite3
            path = db.engine.url.database
            db.session.close()
            db.engine.dispose()
            con = sqlite3.connect(path)
            con.execute('VACUUM')
            con.close()
    except Exception:
        pass
    try:
        add_activity('Sistema zerado (padrão de fábrica) pelo administrador.',
                     category='config', action_type='config', system='config')
        db.session.commit()
    except Exception:
        pass
    return jsonify({'ok': True, 'registros_apagados': apagados})


@app.route('/api/plan/dev', methods=['POST'])
@login_required
def api_plan_dev():
    """Alterna o plano SOMENTE em modo dev (para testar o visual premium/demo)."""
    if not DEV_MODE or not adm_required():
        return jsonify({'error': 'Indisponível.'}), 403
    p = (request.get_json(silent=True) or {}).get('plan', 'free')
    set_setting('dev_plan', p if p in ('free', 'premium', 'demo') else 'free')
    db.session.commit()
    return jsonify(_plan_info())


@app.route('/api/checkout', methods=['POST'])
@login_required
def api_checkout():
    """Inicia um checkout de pagamento avulso no Mercado Pago pra renovar
    a licença deste PC. Envolve dinheiro — só admin decide."""
    if not adm_required():
        return jsonify({'error': 'Acesso restrito ao administrador.'}), 403
    if DEV_MODE:
        return jsonify({'error': 'Indisponível em modo de desenvolvimento.'}), 400
    data = request.get_json(silent=True) or {}
    try:
        import license_client
        res = license_client.create_checkout(data.get('plan'), bool(data.get('site')))
    except Exception:
        res = {'error': 'Falha ao conectar ao servidor de licenças.'}
    return jsonify(res), (200 if res.get('checkout_url') else 502)


@app.route('/api/subscribe', methods=['POST'])
@login_required
def api_subscribe():
    """Inicia uma ASSINATURA recorrente (cartão, renova sozinha). Só admin."""
    if not adm_required():
        return jsonify({'error': 'Acesso restrito ao administrador.'}), 403
    if DEV_MODE:
        return jsonify({'error': 'Indisponível em modo de desenvolvimento.'}), 400
    data = request.get_json(silent=True) or {}
    try:
        import license_client
        res = license_client.create_subscription(data.get('plan'), bool(data.get('site')), data.get('email'))
    except Exception:
        res = {'error': 'Falha ao conectar ao servidor de licenças.'}
    return jsonify(res), (200 if res.get('init_point') else 502)


@app.route('/api/subscription/cancel', methods=['POST'])
@login_required
def api_subscription_cancel():
    """Cancela a assinatura recorrente. Só admin."""
    if not adm_required():
        return jsonify({'error': 'Acesso restrito ao administrador.'}), 403
    if DEV_MODE:
        return jsonify({'error': 'Indisponível em modo de desenvolvimento.'}), 400
    try:
        import license_client
        res = license_client.cancel_subscription()
    except Exception:
        res = {'error': 'Falha ao conectar ao servidor de licenças.'}
    return jsonify(res), (200 if res.get('ok') else 502)


@app.route('/api/checkout/verify', methods=['POST'])
@login_required
def api_checkout_verify():
    """Força uma revalidação ONLINE (não só o cache local) da licença, pra
    refletir um pagamento recém-aprovado sem precisar fechar e reabrir o app."""
    if not DEV_MODE:
        try:
            import license_client
            license_client.check_license()
        except Exception:
            pass
    return jsonify(_plan_info())


def _save_upload(file_storage, prefix):
    """Salva um upload (logo/foto) e devolve o nome do arquivo, ou None."""
    if not file_storage or not file_storage.filename:
        return None
    ext = os.path.splitext(file_storage.filename)[1].lower()
    if ext not in ('.png', '.jpg', '.jpeg', '.webp', '.gif', '.svg', '.ico'):
        return None
    name = f"{prefix}_{datetime.now().strftime('%Y%m%d%H%M%S')}{ext}"
    file_storage.save(os.path.join(_uploads_dir(), secure_filename(name)))
    return secure_filename(name)

def adm_required():
    """True se o usuário atual é ADM; caso contrário deixa a rota barrar."""
    return bool(getattr(current_user, 'is_adm', False))

@app.before_request
def _aulas_api_guard():
    """APIs de dados do Aulas exigem a permissão do módulo (o Hub esconde,
    mas chamada direta também precisa ser barrada)."""
    p = request.path
    if (p.startswith('/api/students') or p.startswith('/api/classes') or p.startswith('/api/alerts')):
        if getattr(current_user, 'is_authenticated', False) and not current_user.can('aulas'):
            return jsonify({'error': 'Acesso restrito: sem permissão para o Aulas.'}), 403
    return None

# ── BACKUP AUTOMÁTICO SEMANAL ─────────────────────────────────────────────────

def auto_backup():
    """Copia o arena.db inteiro para %APPDATA%\\ArenaAMP\\backups\\ no máximo 1x
    por semana (verificado a cada inicialização). Mantém as 8 cópias mais recentes.
    O .db é um backup completo de TODOS os módulos (aulas/comandas/ranking/config)."""
    try:
        if os.environ.get('DATABASE_URL'):
            return  # hospedado (Postgres) não usa backup local
        src = os.path.join(_local_data_dir(), 'arena.db')
        if not os.path.exists(src):
            return
        bdir = os.path.join(_local_data_dir(), 'backups')
        os.makedirs(bdir, exist_ok=True)
        existentes = sorted(f for f in os.listdir(bdir) if f.startswith('arena_') and f.endswith('.db'))
        if existentes:
            try:
                ultimo = datetime.strptime(existentes[-1][6:16], '%Y-%m-%d')
                if (datetime.now() - ultimo).days < 7:
                    return
            except ValueError:
                pass
        import sqlite3
        dst = os.path.join(bdir, f"arena_{datetime.now().strftime('%Y-%m-%d')}.db")
        con_src = sqlite3.connect(src)
        con_dst = sqlite3.connect(dst)
        with con_dst:
            con_src.backup(con_dst)   # cópia consistente, mesmo com o app aberto
        con_dst.close(); con_src.close()
        todas = sorted(f for f in os.listdir(bdir) if f.startswith('arena_') and f.endswith('.db'))
        for f in todas[:-8]:
            os.remove(os.path.join(bdir, f))
        try:
            with app.app_context():
                add_activity('Backup automático semanal criado (backups/' + os.path.basename(dst) + ')',
                             category='config', action_type='config', system='config')
                db.session.commit()
        except Exception:
            pass
        print(f"[backup] semanal salvo em {dst}")
    except Exception as e:
        print(f"[backup] automático falhou: {e}")

# ── AUTH ──────────────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('shell'))
    error = None
    if request.method == 'POST':
        remember = bool(request.form.get('remember'))
        user = User.query.filter_by(username=request.form['username']).first()
        if user and user.check_password(request.form['password']):
            login_user(user, remember=remember)
            # Grava a preferência: se marcou "lembrar", a portaria (/_gate) não
            # força logout na próxima abertura e o usuário já entra logado.
            set_setting('remember_login', '1' if remember else '')
            set_setting('remember_username', user.username if remember else '')
            db.session.commit()
            return redirect(url_for('shell'))
        else:
            error = "Usuário ou Senha incorretos"
    return render_template('login.html', error=error,
                           remember_username=get_setting('remember_username', ''),
                           remember_default=(get_setting('remember_login', '') == '1'))

@app.route('/logout')
@login_required
def logout():
    logout_user()
    # Sair de propósito desliga o "manter conectado" — senão a próxima
    # abertura entraria logada de novo.
    set_setting('remember_login', '')
    db.session.commit()
    return redirect(url_for('login'))

def _enabled_modules():
    """Módulos liberados pela licença (lista). Relatórios é recurso da plataforma
    (liberado junto quando há licença válida). Licença inválida/bloqueada → lista
    vazia (Hub tranca tudo) — NUNCA liberar por falha, senão vira brecha."""
    if DEV_MODE:
        return ['aulas', 'comandas', 'ranking', 'relatorios']
    try:
        import license_client
        mods = list(license_client.get_modules())
    except Exception:
        mods = []
    if mods and 'relatorios' not in mods:
        mods.append('relatorios')
    return mods


SHELL_MODULE_DEFS = [
    {'key': 'aulas',      'name': 'Aulas',         'icon': 'fa-users',      'url': '/aulas'},
    {'key': 'ranking',    'name': 'Ranking',       'icon': 'fa-trophy',     'url': '/ranking/'},
    {'key': 'comandas',   'name': 'Comandas',      'icon': 'fa-receipt',    'url': '/comandas/'},
    {'key': 'relatorios', 'name': 'Relatórios',    'icon': 'fa-chart-line', 'url': '/relatorios/'},
]

def _shell_modules():
    """Módulos que o usuário atual pode abrir em abas (licença + permissão)."""
    mods = _enabled_modules()
    out = [m for m in SHELL_MODULE_DEFS if m['key'] in mods and current_user.can(m['key'])]
    if current_user.can('config'):
        out.append({'key': 'config', 'name': 'Configurações', 'icon': 'fa-gear', 'url': '/configuracoes'})
    return out

@app.route('/')
@login_required
def shell():
    # Força trocar a senha se ainda for a padrão de fábrica (segurança na entrega).
    try:
        must_pw = bool(current_user.is_adm and current_user.check_password('admin123'))
    except Exception:
        must_pw = False
    # Termos agora vêm do license-server (editáveis no /admin) e o aceite é
    # decidido no shell.html via /api/terms — não passamos mais terms_ok aqui.
    return render_template('shell.html', user=current_user, shell_modules=_shell_modules(),
                           plan=_plan_info(), is_dev=bool(DEV_MODE),
                           must_set_password=must_pw)


@app.route('/api/account/password', methods=['POST'])
@login_required
def api_account_password():
    """Troca a senha do usuário logado. Usado pra forçar a saída da senha padrão
    no 1º acesso (segurança) e também disponível pra troca voluntária."""
    data = request.get_json(silent=True) or {}
    nova = (data.get('nova') or '')
    if len(nova) < 6:
        return jsonify({'error': 'A senha precisa ter ao menos 6 caracteres.'}), 400
    if nova == 'admin123':
        return jsonify({'error': 'Escolha uma senha diferente da padrão.'}), 400
    current_user.set_password(nova)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/terms')
@login_required
def api_terms():
    """Termos de Uso vigentes (do license-server) + a versão que ESTE cliente já
    aceitou. O shell compara: se a versão mudou, reexibe o modal de aceite."""
    if DEV_MODE:
        return jsonify({'version': '0', 'html': '<p>Termos (modo dev).</p>',
                        'accepted': get_setting('terms_accepted', '')})
    try:
        import license_client
        t = license_client.get_terms() or {}
    except Exception:
        t = {}
    t['accepted'] = get_setting('terms_accepted', '')
    return jsonify(t)


@app.route('/api/terms/accept', methods=['POST'])
@login_required
def api_terms_accept():
    """Registra localmente o aceite (versão vinda do servidor) e reporta ao
    license-server pra você ver no /admin quem aceitou e quando."""
    version = ((request.get_json(silent=True) or {}).get('version') or '').strip()
    if not version:
        return jsonify({'error': 'versão ausente'}), 400
    set_setting('terms_accepted', version)
    set_setting('terms_accepted_at', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    set_setting('terms_accepted_by', current_user.username)
    db.session.commit()
    if not DEV_MODE:
        try:
            import license_client
            license_client.report_terms_accept(version)
        except Exception:
            pass
    return jsonify({'ok': True})

@app.route('/hub')
@login_required
def hub():
    try:
        from version import APP_VERSION
    except Exception:
        APP_VERSION = ''
    return render_template('hub.html', user=current_user, modules=_enabled_modules(),
                           plan=_plan_info(), site_url=SITE_URL, app_version=APP_VERSION)


@app.route('/assinatura')
@login_required
def assinatura():
    if not current_user.can('assinatura'):
        return render_template('nao_autorizado.html'), 403
    return render_template('assinatura.html', user=current_user, plan=_plan_info())


@app.route('/api/assinatura')
@login_required
def api_assinatura():
    """Dados da assinatura pra página. Em dev não há licença real → devolve o
    básico do _plan_info; em produção relaia o license-server."""
    if not current_user.can('assinatura'):
        return jsonify({'error': 'Acesso restrito.'}), 403
    if DEV_MODE:
        info = _plan_info()
        # Stub de dev com um site JÁ ALINHADO ao sistema, pra visualizar o aviso.
        from datetime import datetime, timezone, timedelta
        venc = (datetime.now(timezone.utc) + timedelta(days=18)).isoformat()
        return jsonify({'plan': info['plan'], 'active': info['is_premium'],
                        'expires_at': venc, 'ciclo': 'Mensal', 'desde': None,
                        'has_site': True, 'site_ativo': True, 'site_alinhado': True,
                        'site_expires_at': venc, 'site_started_at': venc,
                        'pagamentos': []})
    try:
        import license_client
        return jsonify(license_client.subscription_info())
    except Exception:
        return jsonify({'error': 'Falha ao consultar a assinatura.'}), 502


@app.route('/aulas')
@login_required
def index():
    if 'aulas' not in _enabled_modules() or not current_user.can('aulas'):
        return render_template('nao_autorizado.html'), 403
    return render_template('index.html', user=current_user)


@app.route('/manifest.json')
def manifest():
    """PWA manifest — atalho na tela inicial do celular com a logo da arena."""
    ident = get_identity()
    nome = (ident.get('arena_name') if isinstance(ident, dict) else getattr(ident, 'arena_name', None)) or 'Arena AMP'
    accent = (ident.get('accent') if isinstance(ident, dict) else getattr(ident, 'accent', None)) or '#FF7A1A'
    logo = url_for('static', filename='logo.png')
    return jsonify({
        'name': nome, 'short_name': nome[:12], 'start_url': '/aulas',
        'display': 'standalone', 'background_color': '#132539', 'theme_color': '#132539',
        'icons': [
            {'src': logo, 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any'},
            {'src': logo, 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any'},
        ],
    })


@app.route('/configuracoes')
@login_required
def configuracoes():
    if not current_user.can('config'):
        return render_template('nao_autorizado.html'), 403
    return render_template('configuracoes.html', user=current_user,
                           identity=get_identity(), all_modules=ALL_MODULES,
                           modules=_enabled_modules())


# ── API CONFIGURAÇÕES (somente ADM) ───────────────────────────────────────────

def _deny_if_not_adm():
    if not adm_required():
        return jsonify({'error': 'Acesso restrito ao administrador.'}), 403
    return None

MODULE_LABELS = {'aulas': 'Aulas', 'ranking': 'Ranking', 'comandas': 'Comandas', 'relatorios': 'Relatórios'}

@app.route('/api/config/identity', methods=['GET', 'POST'])
@login_required
def config_identity():
    guard = _deny_if_not_adm()
    if guard: return guard
    if request.method == 'GET':
        return jsonify(get_identity())
    name = (request.form.get('arena_name') or '').strip()
    accent = (request.form.get('accent') or '').strip()
    if name:
        set_setting('arena_name', name[:60])
    if accent:
        set_setting('accent', accent[:9])
    logo = _save_upload(request.files.get('logo'), 'logo')
    if logo:
        set_setting('logo', logo)
    db.session.commit()
    add_activity('Identidade da arena atualizada', category='config', action_type='config')
    return jsonify({'ok': True, **get_identity()})

def _local_ips():
    """IPs IPv4 da máquina (pra montar o link de acesso pelo celular). Inclui o
    IP da rede local e, se o Tailscale estiver instalado, também o 100.x dele."""
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ip = info[4][0]
            if ':' not in ip and not ip.startswith('127.'):
                ips.add(ip)
    except Exception:
        pass
    try:  # descobre o IP principal da LAN mesmo sem DNS do hostname
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    # Tailscale (100.64.0.0/10) primeiro — é o link recomendado
    return sorted(ips, key=lambda ip: (not ip.startswith('100.'), ip))

@app.route('/api/config/remote', methods=['GET', 'POST'])
@login_required
def config_remote():
    """Liga/desliga o acesso pelo celular (bind na rede) e mostra o link.
    Opt-in: vem DESLIGADO. Ligar só passa a valer ao reabrir o app."""
    guard = _deny_if_not_adm()
    if guard: return guard
    if request.method == 'POST':
        body = request.get_json(silent=True) or request.form
        val = body.get('enabled')
        enabled = val in (True, 1, '1', 'true', 'True', 'on')
        set_setting('remote_access', '1' if enabled else '')
        db.session.commit()
        add_activity('Acesso remoto (celular) ' + ('ligado' if enabled else 'desligado'),
                     category='config', action_type='config')
        return jsonify({'ok': True, 'enabled': enabled})
    enabled = get_setting('remote_access', '') == '1'
    port = request.host.split(':')[-1] if ':' in request.host else '80'
    ips = _local_ips()
    return jsonify({
        'enabled': enabled,
        'port': port,
        'ips': ips,
        'urls': [f'http://{ip}:{port}' for ip in ips],
    })

# ── CLIENTE DE SYNC COM A NUVEM (arena-sync) ──────────────────────────────────
# O app do PC empurra/puxa o módulo Aulas do serviço arena-sync (fallback com o
# PC desligado). Usa o motor compartilhado sync_core. Ver PLANO_ACESSO_REMOTO.md.

# URL do serviço arena-sync (nuvem). Default de produção — o cliente pode
# sobrescrever por setting `arena_sync_url` ou env ARENA_SYNC_URL (ex.: teste).
ARENA_SYNC_URL_DEFAULT = 'https://arena-sync-rh5a.onrender.com'

def _sync_cfg():
    base = (get_setting('arena_sync_url', '') or os.environ.get('ARENA_SYNC_URL', '')
            or ARENA_SYNC_URL_DEFAULT).rstrip('/')
    return base, get_setting('arena_token', ''), get_setting('sync_secret', '')

def _sync_post(url, body, secret=None, timeout=90):
    import json as _json
    from urllib import request as _rq
    headers = {'Content-Type': 'application/json'}
    if secret:
        headers['X-Sync-Secret'] = secret
    req = _rq.Request(url, data=_json.dumps(body).encode('utf-8'), method='POST', headers=headers)
    with _rq.urlopen(req, timeout=timeout) as r:
        return _json.loads(r.read().decode('utf-8'))

def sync_provision():
    """Registra a arena no arena-sync (uma vez). Manda a chave + o payload de
    licença JÁ ASSINADO (do cache) pro arena-sync verificar a assinatura, em vez
    de revalidar (que exigiria ser a máquina vinculada). Guarda token + segredo."""
    import license_client
    base, _, _ = _sync_cfg()
    if not base:
        raise RuntimeError('URL do arena-sync não configurada')
    lic = license_client.cached_license()
    if not lic.get('license_key'):
        raise RuntimeError('sem chave de licença')
    r = _sync_post(base + '/provision', {
        'license_key': lic['license_key'],
        'payload': lic.get('payload'),
        'signature': lic.get('signature'),
    })
    set_setting('arena_token', r['arena_token'])
    set_setting('sync_secret', r['sync_secret'])
    db.session.commit()
    return r

def sync_pull():
    """Puxa o estado do servidor e funde localmente (adota o que a funcionária
    fez com o PC desligado)."""
    base, token, secret = _sync_cfg()
    data = _sync_post(f'{base}/a/{token}/sync/pull', {}, secret)
    return sync_core.apply_state(db.session, SPECS, data, applying)

def sync_push():
    """Empurra o estado local pro servidor (o servidor funde, mais-recente-vence)."""
    base, token, secret = _sync_cfg()
    payload = sync_core.export_state(db.session, SPECS)
    return _sync_post(f'{base}/a/{token}/sync/push', payload, secret)

def sync_now():
    """Sincroniza nos dois sentidos: puxa primeiro (adota mudanças da nuvem),
    depois empurra. Devolve o resumo (pra 'Fulana fez X mudanças'). Se a arena não
    existir mais no servidor (recriado/limpo → 403), re-provisiona e tenta 1x."""
    if not get_setting('arena_token', ''):
        sync_provision()
    try:
        resumo = sync_pull()
        sync_push()
    except Exception:
        sync_provision()          # arena sumiu do servidor? recria + novo segredo
        resumo = sync_pull()
        sync_push()
    set_setting('sync_last', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    db.session.commit()
    return resumo

# Sync EM SEGUNDO PLANO: nunca trava a tela (o Render pode demorar ~40s no cold
# start). A UI dispara e depois consulta o status por /api/sync/auto.
_sync_lock = threading.Lock()

def _run_sync_bg():
    def job():
        with app.app_context():
            if not _sync_lock.acquire(blocking=False):
                return   # já tem uma sincronização rodando
            try:
                set_setting('sync_status', 'running'); set_setting('sync_msg', '')
                db.session.commit()
                resumo = sync_now()
                vindos = sum((t.get('novos', 0) + t.get('atualizados', 0)) for t in resumo.values())
                set_setting('sync_status', 'ok')
                set_setting('sync_msg', (f'{vindos} mudança(s) recebida(s) da nuvem.'
                                         if vindos else 'Tudo em dia.'))
                db.session.commit()
            except Exception as e:
                try:
                    db.session.rollback()
                except Exception:
                    pass
                set_setting('sync_status', 'error')
                set_setting('sync_msg', f'Falhou: {e}'[:200])
                try:
                    db.session.commit()
                except Exception:
                    pass
                app.logger.warning('sync em background falhou', exc_info=True)
            finally:
                _sync_lock.release()
    threading.Thread(target=job, daemon=True).start()

@app.route('/api/sync/now', methods=['POST'])
@login_required
def api_sync_now():
    guard = _deny_if_not_adm()
    if guard: return guard
    _run_sync_bg()      # dispara e volta na hora (não trava a tela)
    return jsonify({'ok': True, 'started': True})

def sync_auto(kind='both'):
    """Sync best-effort do auto-sync (run_desktop). Só age com o opt-in `cloud_sync`
    ligado E a URL configurada. NUNCA levanta exceção — não pode quebrar a abertura
    nem o fechamento do app."""
    try:
        if get_setting('cloud_sync', '') != '1':
            return
        base, token, secret = _sync_cfg()
        if not base:
            return
        if not token:
            sync_provision()
        if kind in ('pull', 'both'):
            sync_pull()
        if kind in ('push', 'both'):
            sync_push()
        set_setting('sync_last', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        app.logger.warning('auto-sync (best-effort) falhou', exc_info=True)

def _arena_link():
    """Link pronto pra dar pra funcionária (com o token embutido). Só existe
    depois de provisionar (arena_token setado)."""
    base, token, _ = _sync_cfg()   # base já cai no default de produção
    return f"{base}/a/{token}/" if (base and token) else ''

@app.route('/api/sync/auto', methods=['GET', 'POST'])
@login_required
def api_sync_auto():
    guard = _deny_if_not_adm()
    if guard: return guard
    if request.method == 'POST':
        body = request.get_json(silent=True) or request.form
        enabled = body.get('enabled') in (True, 1, '1', 'true', 'True', 'on')
        set_setting('cloud_sync', '1' if enabled else '')
        db.session.commit()
        if enabled:
            _run_sync_bg()   # provisiona + 1ª sincronização em segundo plano
        return jsonify({'ok': True, 'enabled': enabled})
    return jsonify({'enabled': get_setting('cloud_sync', '') == '1',
                    'last': get_setting('sync_last', ''), 'link': _arena_link(),
                    'status': get_setting('sync_status', ''),
                    'msg': get_setting('sync_msg', '')})

@app.route('/api/config/users', methods=['GET', 'POST'])
@login_required
def config_users():
    guard = _deny_if_not_adm()
    if guard: return guard
    if request.method == 'GET':
        users = User.query.order_by(User.id).all()
        return jsonify([{
            'id': u.id, 'username': u.username, 'role': u.role or 'user',
            'perms': u.perm_list(),
            'photo': url_for('uploaded_file', filename=u.photo) if u.photo else None,
        } for u in users])
    # POST — criar usuário
    username = (request.form.get('username') or '').strip()
    password = request.form.get('password') or ''
    role = 'adm' if request.form.get('role') == 'adm' else 'user'
    perms = ','.join([m for m in request.form.getlist('perms') if m in ALL_MODULES])
    if not username or not password:
        return jsonify({'error': 'Informe usuário e senha.'}), 400
    if User.query.filter_by(username=username).first():
        return jsonify({'error': 'Já existe um usuário com esse nome.'}), 400
    u = User(username=username, role=role, perms=perms)
    u.set_password(password)
    photo = _save_upload(request.files.get('photo'), 'user')
    if photo:
        u.photo = photo
    db.session.add(u)
    db.session.commit()
    add_activity(f"Usuário '{username}' criado", category='config', action_type='config')
    return jsonify({'ok': True, 'id': u.id})

@app.route('/api/config/users/<int:uid>', methods=['PUT', 'DELETE'])
@login_required
def config_user_detail(uid):
    guard = _deny_if_not_adm()
    if guard: return guard
    u = db.session.get(User, uid)
    if not u:
        return jsonify({'error': 'Usuário não encontrado.'}), 404
    if request.method == 'DELETE':
        if u.id == current_user.id:
            return jsonify({'error': 'Você não pode excluir a si mesmo.'}), 400
        if u.is_adm and User.query.filter_by(role='adm').count() <= 1:
            return jsonify({'error': 'Não é possível remover o único administrador.'}), 400
        name = u.username
        db.session.delete(u)
        db.session.commit()
        add_activity(f"Usuário '{name}' removido", category='config', action_type='config')
        return jsonify({'ok': True})
    # PUT — atualizar papel/permissões/senha/foto
    if 'role' in request.form:
        new_role = 'adm' if request.form.get('role') == 'adm' else 'user'
        # não deixar rebaixar o último ADM
        if u.is_adm and new_role != 'adm' and User.query.filter_by(role='adm').count() <= 1:
            return jsonify({'error': 'Deve existir ao menos um administrador.'}), 400
        u.role = new_role
    if 'perms' in request.form:
        u.perms = ','.join([m for m in request.form.getlist('perms') if m in ALL_MODULES])
    pw = request.form.get('password')
    if pw:
        u.set_password(pw)
    photo = _save_upload(request.files.get('photo'), 'user')
    if photo:
        u.photo = photo
    db.session.commit()
    add_activity(f"Usuário '{u.username}' atualizado", category='config', action_type='config')
    return jsonify({'ok': True})

# ── CLASSES ───────────────────────────────────────────────────────────────────

@app.route('/api/classes', methods=['GET', 'POST'])
@login_required
def manage_classes():
    if request.method == 'GET':
        return jsonify([c.to_dict() for c in ClassSession.query.all()])
    d = request.json
    new_c = ClassSession(day=d['day'], time=d['time'],
                         professor=d['professor'], capacity=int(d['capacity']))
    db.session.add(new_c)
    db.session.flush()
    add_activity(f"Turma {new_c.day} {new_c.time} criada (Prof. {new_c.professor})",
                 category='turma', action_type='turma_criada')
    db.session.commit()
    return jsonify(new_c.to_dict())

@app.route('/api/classes/<int:id>', methods=['DELETE'])
@login_required
def delete_class(id):
    c = db.session.get(ClassSession, id)
    if c:
        add_activity(f"Turma {c.day} {c.time} excluída", category='exclusao', action_type='turma_excluida')
        db.session.delete(c)
        db.session.commit()
    return jsonify({'msg': 'ok'})

@app.route('/api/classes/<int:class_id>/add_student', methods=['POST'])
@login_required
def add_student_to_class(class_id):
    c = db.session.get(ClassSession, class_id)
    s = db.session.get(Student, int(request.json.get('student_id')))
    if c and s and s not in c.students:
        c.students.append(s)
        s.updated_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')  # bump p/ sincronizar a matrícula
        add_activity(f"{s.name} matriculado na turma {c.day} {c.time}",
                     category='matricula', action_type='matricula')
        db.session.commit()
    return jsonify(c.to_dict())

@app.route('/api/classes/<int:class_id>/remove_student', methods=['POST'])
@login_required
def remove_student_from_class(class_id):
    c = db.session.get(ClassSession, class_id)
    s = db.session.get(Student, int(request.json.get('student_id')))
    if c and s and s in c.students:
        c.students.remove(s)
        s.updated_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')  # bump p/ sincronizar a matrícula
        add_activity(f"{s.name} removido da turma {c.day} {c.time}",
                     category='turma', action_type='desmatricula')
        db.session.commit()
    return jsonify(c.to_dict())

# ── STUDENTS ──────────────────────────────────────────────────────────────────

@app.route('/api/students', methods=['GET', 'POST'])
@login_required
def manage_students():
    if request.method == 'GET':
        return jsonify([s.to_dict() for s in Student.query.all()])

    try:
        d = request.json or {}
        payment_day = int(d.get('paymentDay', 30) or 30)
        start_dt = _parse_date_flex(d.get('startDate')) or datetime.now()
        start_str = start_dt.strftime('%Y-%m-%d')
        plan = d.get('plan', 'Mensal')
        meses = plan_months(plan)
        price = parse_brl_money(d.get('price'), 0.0)
        # Fim do plano = start + nº de meses (fonte da verdade, sem digitar errado)
        end_dt = _parse_date_flex(d.get('endDate'))
        if not end_dt:
            comps = _competencias(start_dt, payment_day, meses)
            end_dt = datetime.strptime(comps[-1][1], '%Y-%m-%d') if comps else start_dt
        end_str = end_dt.strftime('%Y-%m-%d')

        new_s = Student(
            name=d.get('name', 'Sem nome'),
            plan=plan,
            price=price,
            start_date=start_str,
            end_date=end_str,
            payment_day=payment_day,
            next_payment=start_str,   # ajustado pelo sync logo abaixo
            last_payment=d.get('lastPayment', '') or '',
            classes_per_week=int(d.get('classesPerWeek', 2) or 2),
            credits=int(d.get('saldoAulas', 0) or 0)
        )

        if 'classIds' in d:
            for cid in d['classIds']:
                try:
                    c = db.session.get(ClassSession, int(cid))
                    if c:
                        new_s.classes.append(c)
                except:
                    continue

        db.session.add(new_s)
        db.session.flush()

        # Gera as parcelas do plano (1º ciclo). Opção de já marcar a 1ª como paga
        # (comum na matrícula) — usa a data informada em lastPayment se houver.
        primeira_paga = bool(d.get('firstPaid'))
        gerar_mensalidades(new_s, meses, price, payment_day, ciclo=1, start_dt=start_dt,
                            primeira_paga=primeira_paga,
                            primeira_pago_em=(d.get('lastPayment') or start_str))
        db.session.flush()
        sync_pagamento_cache(new_s)

        add_history(new_s.id,
                    f"Plano {new_s.plan} iniciado ({meses}x). Início: {new_s.start_date} | Fim: {new_s.end_date} | Venc. todo dia {new_s.payment_day}",
                    action_type='info')
        add_activity(f"Aluno {new_s.name} cadastrado — plano {new_s.plan}",
                     category='aluno', action_type='aluno_criado')
        db.session.commit()
        return jsonify(new_s.to_dict())
    except Exception as e:
        db.session.rollback()
        app.logger.exception('criar aluno falhou')
        return jsonify({'error': f'Não foi possível cadastrar o aluno: {e}'}), 400

@app.route('/api/students/<int:id>/update', methods=['PUT'])
@login_required
def update_student_data(id):
    s = db.session.get(Student, id)
    if not s:
        return jsonify({'error': 'Not found'}), 404

    try:
        d = request.json or {}
        antigo = (s.plan, s.start_date, s.payment_day, s.price)

        s.name = d.get('name', s.name)
        s.plan = d.get('plan', s.plan)
        s.price = parse_brl_money(d.get('price'), s.price)
        start_dt = _parse_date_flex(d.get('startDate'))
        if start_dt:
            s.start_date = start_dt.strftime('%Y-%m-%d')
        s.payment_day = int(d.get('paymentDay', s.payment_day) or s.payment_day)
        s.classes_per_week = int(d.get('classesPerWeek', 2) or 2)

        # Fim = start + meses do plano (mantém a regra do cadastro).
        meses = plan_months(s.plan)
        end_dt = _parse_date_flex(d.get('endDate'))
        if not end_dt and start_dt:
            comps = _competencias(start_dt, s.payment_day, meses)
            end_dt = datetime.strptime(comps[-1][1], '%Y-%m-%d') if comps else start_dt
        if end_dt:
            s.end_date = end_dt.strftime('%Y-%m-%d')
        elif d.get('endDate'):
            s.end_date = d['endDate']

        new_credits = int(d.get('saldoAulas', s.credits) or 0)
        if s.credits != new_credits:
            add_history(s.id, f"Saldo ajustado manualmente: {s.credits} → {new_credits}",
                        action_type='info', credit_delta=new_credits - s.credits)
        s.credits = new_credits

        s.classes = []
        if 'classIds' in d:
            for cid in d['classIds']:
                try:
                    c = db.session.get(ClassSession, int(cid))
                    if c:
                        s.classes.append(c)
                except:
                    continue

        # Se o que define as parcelas mudou E nenhuma parcela foi paga ainda,
        # regenera o 1º ciclo (corrige um cadastro feito errado). Se já tem
        # pagamento, NÃO mexe — preserva o histórico; use "Renovar" pra estender.
        mudou_plano = (s.plan, s.start_date, s.payment_day, s.price) != antigo
        alguma_paga = any(m.pago_em for m in s.mensalidades)
        if mudou_plano and not alguma_paga and start_dt:
            for m in list(s.mensalidades):
                db.session.delete(m)
            db.session.flush()
            gerar_mensalidades(s, meses, s.price, s.payment_day, ciclo=1, start_dt=start_dt)
            db.session.flush()
        # mantém valor das parcelas em aberto alinhado ao preço, se mudou o preço
        elif s.price != antigo[3]:
            for m in s.mensalidades:
                if not m.pago_em:
                    m.valor = s.price
        sync_pagamento_cache(s)

        add_activity(f"Aluno {s.name} editado", category='aluno', action_type='aluno_editado')
        db.session.commit()
        return jsonify(s.to_dict())
    except Exception as e:
        db.session.rollback()
        app.logger.exception('editar aluno falhou')
        return jsonify({'error': f'Não foi possível salvar as alterações: {e}'}), 400

@app.route('/api/students/<int:id>/delete', methods=['DELETE'])
@login_required
def delete_student(id):
    s = db.session.get(Student, id)
    if s:
        add_activity(f"Aluno {s.name} excluído do sistema", category='exclusao', action_type='aluno_excluido')
        db.session.delete(s)
        db.session.commit()
    return jsonify({'msg': 'ok'})

@app.route('/api/students/<int:id>/toggle_status', methods=['POST'])
@login_required
def toggle_status(id):
    s = db.session.get(Student, id)
    if not s:
        return jsonify({'error': 'Not found'}), 404
    s.active = not s.active
    status_str = "ATIVADO" if s.active else "INATIVADO"
    add_history(s.id, f"Status alterado para {status_str}", action_type='info')
    add_activity(f"Aluno {s.name} {status_str.lower()}", category='aluno', action_type='status')
    db.session.commit()
    return jsonify(s.to_dict())

# ── ACTIONS ───────────────────────────────────────────────────────────────────

@app.route('/api/students/<int:id>/action', methods=['POST'])
@login_required
def student_action(id):
    s = db.session.get(Student, id)
    if not s:
        return jsonify({'error': 'Not found'}), 404

    action = request.json.get('action')

    if action == 'presenca':
        delta = -1 if s.credits > 0 else 0
        s.credits += delta
        add_history(s.id, "✅ Presença registrada", action_type='presenca', credit_delta=delta)
        add_activity(f"{s.name} — presença registrada" + (" (−1 aula)" if delta else ""), category='presenca', action_type='presenca')

    elif action == 'falta_com_reposicao':
        delta = -1 if s.credits > 0 else 0
        s.credits += delta
        exp = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d')
        db.session.add(Replacement(student_id=s.id,
                                   created_at=datetime.now().strftime('%Y-%m-%d'),
                                   expires_at=exp))
        add_history(s.id, "⚠️ Falta com aviso prévio — +1 Reposição gerada",
                    action_type='falta_aviso', credit_delta=delta)
        add_activity(f"{s.name} — falta com aviso (+1 reposição)", category='falta', action_type='falta_aviso')

    elif action == 'falta_sem_aviso':
        delta = -1 if s.credits > 0 else 0
        s.credits += delta
        add_history(s.id, "❌ Falta SEM aviso prévio — aula perdida",
                    action_type='falta_sem_aviso', credit_delta=delta)
        add_activity(f"{s.name} — falta sem aviso (aula perdida)", category='falta', action_type='falta_sem_aviso')

    elif action == 'usar_reposicao':
        if s.replacements:
            db.session.delete(s.replacements[0])
            add_history(s.id, "🔄 Reposição utilizada", action_type='usar_reposicao', credit_delta=0)
            add_activity(f"{s.name} — reposição utilizada (−1 reposição)", category='reposicao', action_type='usar_reposicao')

    elif action == 'anular_reposicao':
        if s.replacements:
            db.session.delete(s.replacements[0])
            add_history(s.id, "🚫 Reposição anulada (falta na reposição)",
                        action_type='anular_reposicao', credit_delta=0)
            add_activity(f"{s.name} — reposição anulada", category='reposicao', action_type='anular_reposicao')

    db.session.commit()
    return jsonify({'success': True, 'credits': s.credits, 'student': s.to_dict()})

# ── UNDO HISTORY ──────────────────────────────────────────────────────────────

@app.route('/api/students/<int:student_id>/history/<int:history_id>/delete', methods=['DELETE'])
@login_required
def delete_history_entry(student_id, history_id):
    s = db.session.get(Student, student_id)
    h = db.session.get(StudentHistory, history_id)
    if not s or not h or h.student_id != student_id:
        return jsonify({'error': 'Not found'}), 404

    # Reverse credit delta
    if h.credit_delta:
        s.credits = max(0, s.credits - h.credit_delta)

    # If it generated a replacement, remove it
    if h.action_type == 'falta_aviso' and s.replacements:
        db.session.delete(s.replacements[0])

    db.session.delete(h)
    db.session.commit()
    return jsonify({'success': True, 'student': s.to_dict()})

# ── PAYMENT ───────────────────────────────────────────────────────────────────

@app.route('/api/students/<int:id>/register_payment', methods=['POST'])
@login_required
def register_payment(id):
    s = db.session.get(Student, id)
    if not s:
        return jsonify({'error': 'Aluno não encontrado'}), 404

    try:
        d = request.json or {}
        payment_dt = _parse_date_flex(d.get('paymentDate')) or datetime.now()
        payment_date_str = payment_dt.strftime('%Y-%m-%d')

        # Escolhe a parcela: a informada (mensalidadeId) ou, na falta, a mais
        # antiga em aberto (vencida primeiro). Assim o botão "Registrar" simples
        # continua funcionando mesmo sem escolher o mês.
        mid = d.get('mensalidadeId')
        alvo = None
        if mid:
            alvo = db.session.get(Mensalidade, int(mid))
            if not alvo or alvo.student_id != s.id:
                return jsonify({'error': 'Parcela não encontrada'}), 404
        else:
            abertas = sorted([m for m in s.mensalidades if not m.pago_em],
                             key=lambda m: m.vencimento)
            alvo = abertas[0] if abertas else None

        amount = parse_brl_money(d.get('amount'), default=(alvo.valor if alvo else s.price) or 0.0)
        fmt = f"{amount:,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.')

        if alvo:
            if alvo.pago_em:
                return jsonify({'error': 'Essa parcela já está paga'}), 400
            alvo.pago_em = payment_date_str
            if amount:
                alvo.valor = amount
            comp = f"{alvo.mes_ref[5:7]}/{alvo.mes_ref[0:4]}"
            desc = f"💰 Pagamento recebido: R$ {fmt} (parcela {comp}) em {payment_dt.strftime('%d/%m/%Y')}"
        else:
            # aluno sem parcelas (fallback do modelo antigo)
            s.last_payment = payment_date_str
            s.next_payment = compute_next_payment(s.payment_day, payment_dt)
            desc = f"💰 Pagamento recebido: R$ {fmt} em {payment_dt.strftime('%d/%m/%Y')}"

        db.session.flush()
        sync_pagamento_cache(s)
        add_history(s.id, desc, action_type='pagamento', credit_delta=0)
        add_activity(f"{s.name} — R$ {fmt} recebido", category='pagamento', action_type='pagamento')

        db.session.commit()
        return jsonify({'success': True, 'student': s.to_dict()})
    except Exception as e:
        # nunca deixa a sessão poluída derrubar o app (single-thread): faz rollback
        # e devolve erro tratado, em vez de estourar 500 e travar tudo até reiniciar.
        db.session.rollback()
        app.logger.exception('register_payment falhou')
        return jsonify({'error': f'Não foi possível registrar o pagamento: {e}'}), 400

@app.route('/api/students/<int:id>/mensalidades/<int:mid>/estornar', methods=['POST'])
@login_required
def estornar_mensalidade(id, mid):
    """Desfaz o pagamento de uma parcela (marca de volta como em aberto)."""
    s = db.session.get(Student, id)
    m = db.session.get(Mensalidade, mid)
    if not s or not m or m.student_id != s.id:
        return jsonify({'error': 'Parcela não encontrada'}), 404
    try:
        if not m.pago_em:
            return jsonify({'error': 'Essa parcela não está paga'}), 400
        comp = f"{m.mes_ref[5:7]}/{m.mes_ref[0:4]}"
        m.pago_em = None
        db.session.flush()
        sync_pagamento_cache(s)
        add_history(s.id, f"↩️ Pagamento estornado (parcela {comp})",
                    action_type='estorno', credit_delta=0)
        add_activity(f"{s.name} — pagamento estornado (parcela {comp})",
                     category='pagamento', action_type='estorno')
        db.session.commit()
        return jsonify({'success': True, 'student': s.to_dict()})
    except Exception as e:
        db.session.rollback()
        app.logger.exception('estornar falhou')
        return jsonify({'error': f'Não foi possível estornar: {e}'}), 400

@app.route('/api/students/<int:id>/renovar', methods=['POST'])
@login_required
def renovar_plano(id):
    """Gera um novo ciclo de parcelas (renovação), pela duração escolhida,
    continuando a partir do fim do plano atual. Não recadastra o aluno."""
    s = db.session.get(Student, id)
    if not s:
        return jsonify({'error': 'Aluno não encontrado'}), 404
    try:
        d = request.json or {}
        meses = plan_months(d.get('meses') or d.get('plan') or 6)
        valor = parse_brl_money(d.get('valor'), default=s.price or 0.0)
        dia = int(d.get('paymentDay', s.payment_day) or s.payment_day)

        mens = list(s.mensalidades)
        ciclo = (max((m.ciclo for m in mens), default=0)) + 1
        # começa no mês seguinte ao último vencimento existente
        if mens:
            ultimo = max(m.vencimento for m in mens)
            base = datetime.strptime(ultimo, '%Y-%m-%d')
            y, mo = base.year, base.month + 1
            if mo > 12:
                mo = 1; y += 1
            start_dt = datetime(y, mo, 1, 12)
        else:
            start_dt = datetime.now()

        gerar_mensalidades(s, meses, valor, dia, ciclo=ciclo, start_dt=start_dt)
        db.session.flush()

        # atualiza plano/preço/fim conforme a renovação
        s.plan = plano_por_meses(meses)
        s.price = valor
        s.payment_day = dia
        novas = [m for m in s.mensalidades if m.ciclo == ciclo]
        if novas:
            s.end_date = max(m.vencimento for m in novas)
        sync_pagamento_cache(s)

        add_history(s.id, f"🔄 Plano renovado: +{meses} mês(es) — R$ {valor:.2f}".replace('.', ','),
                    action_type='renovacao', credit_delta=0)
        add_activity(f"{s.name} — plano renovado (+{meses} meses)",
                     category='aluno', action_type='renovacao')
        db.session.commit()
        return jsonify({'success': True, 'student': s.to_dict()})
    except Exception as e:
        db.session.rollback()
        app.logger.exception('renovar falhou')
        return jsonify({'error': f'Não foi possível renovar: {e}'}), 400

# ── ALERTS ────────────────────────────────────────────────────────────────────

@app.route('/api/alerts')
@login_required
def get_alerts():
    students = Student.query.filter_by(active=True).all()
    today = datetime.now().date()
    overdue, due_soon, expiring = [], [], []

    for s in students:
        if s.next_payment:
            try:
                np = datetime.strptime(s.next_payment, '%Y-%m-%d').date()
                delta = (np - today).days
                entry = {'id': s.id, 'name': s.name, 'next_payment': s.next_payment, 'days': delta}
                if delta < 0:
                    overdue.append(entry)
                elif delta <= 7:
                    due_soon.append(entry)
            except:
                pass
        if s.end_date:
            try:
                end = datetime.strptime(s.end_date, '%Y-%m-%d').date()
                delta = (end - today).days
                if 0 <= delta <= 14:
                    expiring.append({'id': s.id, 'name': s.name, 'end_date': s.end_date, 'days': delta})
            except:
                pass

    return jsonify({'overdue': overdue, 'due_soon': due_soon, 'expiring': expiring})

# ── ACTIVITY LOG (auditoria global) ───────────────────────────────────────────

@app.route('/api/activity')
@login_required
def get_activity():
    q = ActivityLog.query
    system = request.args.get('system')
    if system:
        if system == 'aulas':
            # registros antigos não têm a coluna preenchida — contam como aulas
            q = q.filter(db.or_(ActivityLog.system == 'aulas', ActivityLog.system.is_(None)))
        else:
            q = q.filter(ActivityLog.system == system)
    user_f = request.args.get('user')
    if user_f:
        q = q.filter(ActivityLog.user == user_f)
    logs = q.order_by(ActivityLog.id.desc()).limit(500).all()
    return jsonify([l.to_dict() for l in logs])


# ── MÓDULO RELATÓRIOS (frontend + relatório de Aulas) ────────────────────────

@app.route('/relatorios/')
@login_required
def relatorios_home():
    # Barra final obrigatória: os assets (app.js/style.css) são relativos à URL.
    # O Flask redireciona /relatorios -> /relatorios/ automaticamente.
    if not current_user.can('relatorios'):
        return render_template('nao_autorizado.html'), 403
    return send_from_directory(_resource_path('relatorios_static'), 'index.html')

@app.route('/relatorios/<path:fname>')
@login_required
def relatorios_asset(fname):
    return send_from_directory(_resource_path('relatorios_static'), fname)

@app.route('/api/relatorios/aulas', methods=['POST'])
@login_required
def relatorio_aulas():
    """Relatório do módulo Aulas: base de alunos, receita, ocupação e movimento do período."""
    if not current_user.can('relatorios'):
        return jsonify({'detail': 'Acesso restrito.'}), 403
    d = request.get_json(silent=True) or {}
    hoje_dt = datetime.now()
    di = (d.get('data_inicio') or hoje_dt.strftime('%Y-%m-%d'))[:10]
    df = (d.get('data_fim') or hoje_dt.strftime('%Y-%m-%d'))[:10]
    if di > df:
        di, df = df, di

    students = Student.query.all()
    ativos = [s for s in students if s.active]
    receita_mensal = sum(s.price or 0 for s in ativos)
    hoje = hoje_dt.strftime('%Y-%m-%d')
    em7 = (hoje_dt + timedelta(days=7)).strftime('%Y-%m-%d')
    vencidos = [s for s in ativos if s.next_payment and s.next_payment < hoje]
    vence_7d = [s for s in ativos if s.next_payment and hoje <= s.next_payment <= em7]

    # Alunos por plano
    planos = {}
    for s in ativos:
        planos[s.plan or 'Sem plano'] = planos.get(s.plan or 'Sem plano', 0) + 1

    # Ocupação por turma
    turmas = ClassSession.query.all()
    ocupacao = sorted([{
        'turma': f'{t.day} {t.time}', 'professor': t.professor,
        'alunos': len(t.students), 'capacidade': t.capacity or 6,
    } for t in turmas], key=lambda x: -x['alunos'])
    vagas_total = sum(x['capacidade'] for x in ocupacao)
    ocup_pct = round(sum(x['alunos'] for x in ocupacao) / vagas_total * 100, 1) if vagas_total else 0

    # Movimento do período (log de atividades do sistema Aulas)
    logs = (ActivityLog.query
            .filter(db.or_(ActivityLog.system == 'aulas', ActivityLog.system.is_(None)))
            .filter(ActivityLog.created_at >= di)
            .filter(ActivityLog.created_at <= df + ' 23:59:59')
            .all())
    cats = {}
    por_dia = {}
    for l in logs:
        cats[l.category] = cats.get(l.category, 0) + 1
        dia = (l.created_at or '')[:10]
        if dia:
            por_dia[dia] = por_dia.get(dia, 0) + 1

    return jsonify({
        'data_inicio': di, 'data_fim': df,
        'alunos_ativos': len(ativos),
        'alunos_inativos': len(students) - len(ativos),
        'receita_mensal_estimada': round(receita_mensal, 2),
        'ticket_medio': round(receita_mensal / len(ativos), 2) if ativos else 0,
        'ocupacao_pct': ocup_pct,
        'turmas': ocupacao,
        'planos': [{'plano': k, 'alunos': v} for k, v in sorted(planos.items(), key=lambda x: -x[1])],
        'vencidos': [{'nome': s.name, 'valor': s.price or 0, 'vencimento': s.next_payment} for s in vencidos],
        'vence_7d': [{'nome': s.name, 'valor': s.price or 0, 'vencimento': s.next_payment} for s in vence_7d],
        'movimento': {
            'presencas': cats.get('presenca', 0),
            'faltas': cats.get('falta', 0),
            'reposicoes': cats.get('reposicao', 0),
            'pagamentos': cats.get('pagamento', 0),
            'novos_alunos': cats.get('aluno', 0),
            'total_acoes': len(logs),
        },
        'atividade_por_dia': [{'dia': k, 'acoes': v} for k, v in sorted(por_dia.items())],
    })

# ── MIGRATE (safe — keeps all data) ──────────────────────────────────────────

def _auto_migrate():
    """Adiciona automaticamente as colunas que faltam em tabelas já existentes,
    comparando os modelos atuais com o banco. Roda no STARTUP, antes de qualquer
    consulta — senão, depois de uma atualização que inclui um campo novo, o app
    crasharia com 'no such column' (o db.create_all NÃO altera tabelas antigas).
    Só adiciona (nunca remove/renomeia); os dados do cliente ficam intactos."""
    try:
        from sqlalchemy import inspect as sa_inspect, text
        insp = sa_inspect(db.engine)
        existing = set(insp.get_table_names())
        with db.engine.begin() as conn:
            for table in db.metadata.sorted_tables:
                if table.name not in existing:
                    continue  # tabela nova → db.create_all() cria inteira depois
                have = {c['name'] for c in insp.get_columns(table.name)}
                for col in table.columns:
                    if col.name in have:
                        continue
                    try:
                        coltype = col.type.compile(dialect=db.engine.dialect)
                    except Exception:
                        coltype = 'VARCHAR'
                    # adiciona como NULLABLE (evita erro do SQLite com NOT NULL
                    # sem default); o modelo cuida do default em novos inserts.
                    try:
                        conn.execute(text('ALTER TABLE "%s" ADD COLUMN "%s" %s'
                                          % (table.name, col.name, coltype)))
                    except Exception:
                        continue
                    # aplica o default do modelo nas linhas ANTIGAS (só se for um
                    # valor simples), pra elas não ficarem com NULL indevido.
                    d = getattr(col, 'default', None)
                    if d is not None and getattr(d, 'is_scalar', False):
                        try:
                            conn.execute(
                                text('UPDATE "%s" SET "%s" = :v WHERE "%s" IS NULL'
                                     % (table.name, col.name, col.name)),
                                {'v': d.arg})
                        except Exception:
                            pass
    except Exception:
        pass


def _backfill_mensalidades():
    """Gera as parcelas dos alunos que ainda não têm nenhuma (roda no startup, é
    idempotente). Marca como PAGAS as competências até o último pagamento do aluno
    (last_payment); o resto fica vencida/a vencer. Aproximação segura — o dono da
    arena ajusta manualmente qualquer caso na tela."""
    try:
        alunos = Student.query.all()
        criou = False
        for s in alunos:
            if s.mensalidades:
                continue  # já migrado
            start_dt = _parse_date_flex(s.start_date) or datetime.now()
            meses = plan_months(s.plan)
            dia = s.payment_day or 30
            comps = _competencias(start_dt, dia, meses)
            # mês do último pagamento (YYYY-MM) — parcelas até aí entram como pagas
            lp = _parse_date_flex(s.last_payment)
            lp_mes = lp.strftime('%Y-%m') if lp else None
            for i, (mes_ref, venc) in enumerate(comps, start=1):
                pago = venc if (lp_mes and mes_ref <= lp_mes) else None
                s.mensalidades.append(Mensalidade(
                    ciclo=1, numero=i, mes_ref=mes_ref,
                    vencimento=venc, valor=float(s.price or 0.0), pago_em=pago))
            criou = True
        if criou:
            db.session.flush()
            for s in alunos:
                sync_pagamento_cache(s)
            db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception('backfill de mensalidades falhou')


def _backfill_fix_parcela_alignment():
    """Corrige as parcelas geradas pelo modelo antigo (que pulava 1 mês quando o
    dia de início já tinha passado do dia de vencimento). Agora a 1ª parcela fica
    SEMPRE no mês de início. Idempotente: só mexe no que está desalinhado, preserva
    quais foram pagas (as pagas continuam pagas, com o vencimento corrigido). Só
    trata o caso comum (1 ciclo, nº de parcelas == plano) pra não arriscar renovados.
    Roda 1x — protegida por flag em Settings."""
    try:
        if get_setting('parcelas_realinhadas') == '1':
            return 0
        mudou = 0
        for s in Student.query.all():
            mens = sorted(s.mensalidades, key=lambda m: (m.ciclo, m.numero))
            if not mens:
                continue
            meses = plan_months(s.plan)
            if len(set(m.ciclo for m in mens)) != 1 or len(mens) != meses:
                continue  # renovado / fora do padrão — não mexe
            start_dt = _parse_date_flex(s.start_date)
            if not start_dt:
                continue
            correct = _competencias(start_dt, s.payment_day or 30, meses)
            for parc, (mes_ref, venc) in zip(mens, correct):
                if parc.mes_ref != mes_ref or parc.vencimento != venc:
                    parc.mes_ref = mes_ref
                    parc.vencimento = venc
                    if parc.pago_em:            # backfill sintético: mantém "pago no vencimento"
                        parc.pago_em = venc
                    mudou += 1
            sync_pagamento_cache(s)
        set_setting('parcelas_realinhadas', '1')
        db.session.commit()
        return mudou
    except Exception:
        db.session.rollback()
        app.logger.exception('realinhamento de parcelas falhou')
        return 0


def _backfill_sync_fields():
    """Preenche sync_uid + updated_at nos registros que ainda não têm (1ª abertura
    após a atualização de sync). Idempotente e aditivo — não toca em PK nem apaga
    nada. Ver PLANO_ACESSO_REMOTO.md."""
    try:
        agora = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')
        mexeu = False
        for Model in _SYNC_MODELS:
            faltando = Model.query.filter(Model.sync_uid.is_(None)).all()
            for row in faltando:
                # atribuição direta (não via evento) pra controlar o backfill;
                # o before_update ainda carimba updated_at no flush.
                row.sync_uid = str(uuid.uuid4())
                if not row.updated_at:
                    row.updated_at = agora
                mexeu = True
        if mexeu:
            db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception('backfill de sync_uid falhou')


@app.route('/migrate')
@login_required
def migrate():
    """Adds new columns without dropping any data. Run once after deploying new code."""
    if not adm_required():
        return render_template('nao_autorizado.html'), 403
    try:
        from sqlalchemy import text, inspect as sa_inspect

        def col_exists(table, column):
            inspector = sa_inspect(db.engine)
            cols = [c['name'] for c in inspector.get_columns(table)]
            return column in cols

        results = []
        with db.engine.connect() as conn:

            # ── student ────────────────────────────────────────────────────────
            migrations = [
                ('student', 'payment_day',      "ALTER TABLE student ADD COLUMN payment_day INTEGER DEFAULT 30",
                 "UPDATE student SET payment_day = 30 WHERE payment_day IS NULL"),
                ('student', 'active',           "ALTER TABLE student ADD COLUMN active BOOLEAN DEFAULT TRUE",
                 "UPDATE student SET active = TRUE WHERE active IS NULL"),
                ('student', 'classes_per_week', "ALTER TABLE student ADD COLUMN classes_per_week INTEGER DEFAULT 2",
                 "UPDATE student SET classes_per_week = 2 WHERE classes_per_week IS NULL"),
                ('student', 'credits',          "ALTER TABLE student ADD COLUMN credits INTEGER DEFAULT 0",
                 "UPDATE student SET credits = 0 WHERE credits IS NULL"),
                ('student', 'last_payment',     "ALTER TABLE student ADD COLUMN last_payment VARCHAR(10)", None),
                ('student_history', 'action_type', "ALTER TABLE student_history ADD COLUMN action_type VARCHAR(30) DEFAULT 'info'", None),
                ('student_history', 'credit_delta', "ALTER TABLE student_history ADD COLUMN credit_delta INTEGER DEFAULT 0", None),
                ('users', 'role',  "ALTER TABLE users ADD COLUMN role VARCHAR(20) DEFAULT 'user'",
                 "UPDATE users SET role = 'user' WHERE role IS NULL"),
                ('users', 'perms', "ALTER TABLE users ADD COLUMN perms VARCHAR(200) DEFAULT ''", None),
                ('users', 'photo', "ALTER TABLE users ADD COLUMN photo VARCHAR(200)", None),
                ('activity_log', 'system', "ALTER TABLE activity_log ADD COLUMN system VARCHAR(20) DEFAULT 'aulas'",
                 "UPDATE activity_log SET system = 'aulas' WHERE system IS NULL"),
            ]

            for table, col, alter_sql, update_sql in migrations:
                try:
                    if not col_exists(table, col):
                        conn.execute(text(alter_sql))
                        if update_sql:
                            conn.execute(text(update_sql))
                        results.append(f"✅ {table}.{col} adicionada")
                    else:
                        results.append(f"⏭️  {table}.{col} já existe")
                except Exception as col_err:
                    results.append(f"⚠️  {table}.{col}: {col_err}")

            # end_date special case — try to compute from start_date + plan
            try:
                if not col_exists('student', 'end_date'):
                    conn.execute(text("ALTER TABLE student ADD COLUMN end_date VARCHAR(10) DEFAULT ''"))
                    try:
                        conn.execute(text("""
                            UPDATE student SET end_date =
                                CASE
                                    WHEN plan = 'Mensal'     THEN to_char(TO_DATE(start_date,'YYYY-MM-DD') + INTERVAL '1 month',  'YYYY-MM-DD')
                                    WHEN plan = 'Trimestral' THEN to_char(TO_DATE(start_date,'YYYY-MM-DD') + INTERVAL '3 months', 'YYYY-MM-DD')
                                    WHEN plan = 'Semestral'  THEN to_char(TO_DATE(start_date,'YYYY-MM-DD') + INTERVAL '6 months', 'YYYY-MM-DD')
                                    ELSE start_date
                                END
                            WHERE end_date IS NULL OR end_date = ''
                        """))
                    except:
                        pass  # SQLite doesn't support to_char; end_date stays empty
                    results.append("✅ student.end_date adicionada e calculada")
                else:
                    results.append("⏭️  student.end_date já existe")
            except Exception as e:
                results.append(f"⚠️  student.end_date: {e}")

            conn.commit()

        db.create_all()  # create any brand-new tables
        results.append("")
        results.append("🎉 Migração concluída! Seus dados estão intactos.")
        html = "<br>".join(results)
        return f"""
        <html><body style="font-family:monospace; padding:2rem; background:#f8fafc;">
        <h2 style="color:#1e293b;">🔧 Migração Arena AMP</h2>
        <div style="background:white; padding:1.5rem; border-radius:10px; border:1px solid #e2e8f0; line-height:2;">
        {html}
        </div>
        <br><a href="/" style="background:#FF914D; color:white; padding:10px 20px; border-radius:8px; text-decoration:none; font-weight:bold;">
        ← Voltar ao Sistema
        </a>
        </body></html>
        """
    except Exception as e:
        return f"<h1 style='color:red'>❌ Erro: {e}</h1>"


# ── BACKUP ────────────────────────────────────────────────────────────────────

@app.route('/api/backup')
@login_required
def backup():
    """Returns a full JSON backup of all data. Restrito ao administrador."""
    if not adm_required():
        return jsonify({'error': 'Backup é restrito ao administrador.'}), 403
    import json

    students = Student.query.all()
    classes = ClassSession.query.all()

    students_data = []
    for s in students:
        sd = s.to_dict()
        # Include raw replacements
        sd['_replacements_raw'] = [
            {'created_at': r.created_at, 'expires_at': r.expires_at}
            for r in s.replacements
        ]
        # Include full history
        sd['_history_raw'] = [
            {'description': h.description, 'date_str': h.date_str,
             'action_type': h.action_type or 'info',
             'credit_delta': h.credit_delta or 0}
            for h in s.history_logs
        ]
        # Include parcelas (mensalidades)
        sd['_mensalidades_raw'] = [
            {'ciclo': m.ciclo, 'numero': m.numero, 'mes_ref': m.mes_ref,
             'vencimento': m.vencimento, 'valor': m.valor, 'pago_em': m.pago_em}
            for m in s.mensalidades
        ]
        students_data.append(sd)

    classes_data = []
    for c in classes:
        cd = c.to_dict()
        classes_data.append(cd)

    # Enrollments map
    enrollments_data = []
    for s in students:
        for c in s.classes:
            enrollments_data.append({'student_id': s.id, 'class_id': c.id})

    backup_obj = {
        'version': '2.0',
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'students': students_data,
        'classes': classes_data,
        'enrollments': enrollments_data,
    }

    from flask import Response
    filename = f"arena_backup_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    return Response(
        json.dumps(backup_obj, ensure_ascii=False, indent=2),
        mimetype='application/json',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'}
    )


# ── RESTORE ───────────────────────────────────────────────────────────────────

@app.route('/restore', methods=['GET', 'POST'])
@login_required
def restore():
    """Upload a backup JSON file and restore all data (merges, doesn't wipe). Restrito ao ADM."""
    if not adm_required():
        return render_template('nao_autorizado.html'), 403
    import json

    if request.method == 'GET':
        return '''
        <html><body style="font-family:Inter,sans-serif; padding:2rem; background:#f8fafc; max-width:600px; margin:auto;">
        <h2 style="color:#1e293b;">📥 Restaurar Backup</h2>
        <div style="background:#fff7ed; border:1px solid #fed7aa; border-radius:10px; padding:1rem; margin-bottom:1.5rem;">
            <strong style="color:#c2410c;">⚠️ Atenção:</strong>
            O restore adiciona os dados do arquivo sem apagar os existentes.
            Para uma restauração limpa, acesse <code>/reset-banco-de-dados</code> antes.
        </div>
        <form method="POST" enctype="multipart/form-data"
              style="background:white; padding:1.5rem; border-radius:10px; border:1px solid #e2e8f0;">
            <label style="font-weight:600; color:#475569; display:block; margin-bottom:8px;">
                Selecione o arquivo de backup (.json):
            </label>
            <input type="file" name="backup_file" accept=".json" required
                   style="width:100%; padding:10px; border:1px solid #cbd5e1; border-radius:8px; margin-bottom:1rem;">
            <button type="submit"
                    style="background:#FF914D; color:white; border:none; padding:12px 24px; border-radius:8px; font-weight:bold; cursor:pointer; font-size:1rem;">
                🔄 Restaurar Dados
            </button>
        </form>
        <br>
        <a href="/" style="color:#64748b; font-size:0.9rem;">← Voltar ao Sistema</a>
        </body></html>
        '''

    # POST — process the uploaded file
    if 'backup_file' not in request.files:
        return "<h1>❌ Nenhum arquivo enviado.</h1>"

    file = request.files['backup_file']
    try:
        data = json.loads(file.read().decode('utf-8'))
    except Exception as e:
        return f"<h1>❌ Arquivo inválido: {e}</h1>"

    results = []
    class_id_map = {}   # old_id -> new ClassSession object
    student_id_map = {} # old_id -> new Student object

    try:
        # 1. Restore classes
        for cd in data.get('classes', []):
            existing = ClassSession.query.filter_by(day=cd['day'], time=cd['time']).first()
            if existing:
                class_id_map[cd['id']] = existing
                results.append(f"⏭️  Turma {cd['day']} {cd['time']} já existe")
            else:
                nc = ClassSession(day=cd['day'], time=cd['time'],
                                  professor=cd['professor'], capacity=cd.get('capacity', 6))
                db.session.add(nc)
                db.session.flush()
                class_id_map[cd['id']] = nc
                results.append(f"✅ Turma {cd['day']} {cd['time']} restaurada")

        # 2. Restore students
        for sd in data.get('students', []):
            existing = Student.query.filter_by(name=sd['name']).first()
            if existing:
                student_id_map[sd['id']] = existing
                results.append(f"⏭️  Aluno '{sd['name']}' já existe — pulando")
                continue

            ns = Student(
                name=sd['name'],
                plan=sd.get('plan', 'Mensal'),
                price=float(sd.get('price', 0)),
                start_date=sd.get('startDate', ''),
                end_date=sd.get('endDate', ''),
                payment_day=int(sd.get('paymentDay', 30)),
                next_payment=sd.get('nextPayment', ''),
                last_payment=sd.get('lastPayment', ''),
                classes_per_week=int(sd.get('classesPerWeek', 2)),
                credits=int(sd.get('credits', 0)),
                active=sd.get('active', True),
            )
            db.session.add(ns)
            db.session.flush()
            student_id_map[sd['id']] = ns

            # Replacements
            for r in sd.get('_replacements_raw', []):
                db.session.add(Replacement(
                    student_id=ns.id,
                    created_at=r['created_at'],
                    expires_at=r['expires_at']
                ))

            # History
            for h in sd.get('_history_raw', []):
                db.session.add(StudentHistory(
                    student_id=ns.id,
                    description=h['description'],
                    date_str=h['date_str'],
                    action_type=h.get('action_type', 'info'),
                    credit_delta=h.get('credit_delta', 0)
                ))

            # Parcelas (mensalidades) — se o backup for de versão anterior sem
            # elas, ficam vazias e o _backfill_mensalidades gera no próximo boot.
            for m in sd.get('_mensalidades_raw', []):
                db.session.add(Mensalidade(
                    student_id=ns.id,
                    ciclo=m.get('ciclo', 1), numero=m.get('numero', 1),
                    mes_ref=m.get('mes_ref', ''), vencimento=m.get('vencimento', ''),
                    valor=float(m.get('valor', 0) or 0), pago_em=m.get('pago_em')
                ))

            results.append(f"✅ Aluno '{sd['name']}' restaurado")

        # 3. Restore enrollments
        enroll_count = 0
        for e in data.get('enrollments', []):
            s_obj = student_id_map.get(e['student_id'])
            c_obj = class_id_map.get(e['class_id'])
            if s_obj and c_obj and c_obj not in s_obj.classes:
                s_obj.classes.append(c_obj)
                enroll_count += 1

        results.append(f"✅ {enroll_count} matrículas restauradas")
        db.session.commit()
        results.append("")
        results.append("🎉 Restore concluído com sucesso!")

    except Exception as e:
        db.session.rollback()
        results.append(f"❌ Erro durante restore: {e}")

    html = "<br>".join(results)
    return f"""
    <html><body style="font-family:monospace; padding:2rem; background:#f8fafc;">
    <h2 style="color:#1e293b;">📥 Resultado do Restore</h2>
    <div style="background:white; padding:1.5rem; border-radius:10px; border:1px solid #e2e8f0; line-height:2;">
    {html}
    </div>
    <br>
    <a href="/" style="background:#FF914D; color:white; padding:10px 20px; border-radius:8px; text-decoration:none; font-weight:bold;">
    ← Voltar ao Sistema
    </a>
    </body></html>
    """


# ── MÓDULO RANKING (Flask/SQLite, integrado em /ranking) ──────────────────────
try:
    import ranking_module
    ranking_module.register(app, db, User, _resource_path)
except Exception as _rk_err:
    print(f"[ranking] módulo não carregado: {_rk_err}")

# ── MÓDULO COMANDAS (Flask/SQLite, integrado em /comandas) ────────────────────
try:
    import comandas_module
    comandas_module.register(app, db, User, _resource_path)
except Exception as _cm_err:
    print(f"[comandas] módulo não carregado: {_cm_err}")


if __name__ == '__main__':
    with app.app_context():
        _auto_migrate()
        db.create_all()
        _backfill_mensalidades()
        _backfill_sync_fields()
    app.run(host='127.0.0.1', debug=False)