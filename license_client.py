"""
license_client.py — Trava de licença do app Arena AMP (lado do cliente)

Roda ANTES do sistema abrir. Fluxo:
  1. Descobre um ID estável do PC.
  2. Consulta o servidor de licença (você controla) e verifica a ASSINATURA
     da resposta com a chave pública embutida — impede falsificação.
  3. Sem internet: usa a última validação por até GRACE_DAYS dias.
  4. Se você "cortar o acesso" no painel, a próxima validação bloqueia.

Os dados do sistema (SQLite) NÃO passam por aqui — só o status da licença.
"""
import os
import json
import base64
import secrets
import platform
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

from cryptography.hazmat.primitives.serialization import load_pem_public_key
from cryptography.exceptions import InvalidSignature

try:
    from version import APP_VERSION, PRODUCT
except Exception:
    APP_VERSION = '999'  # fallback alto: se não der pra ler a versão, NÃO trava
                         # o cliente por engano (fail-open, nunca update_required)
    PRODUCT = 'arena'

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURAÇÃO — ajuste após publicar o servidor de licença no Render
# ══════════════════════════════════════════════════════════════════════════════
LICENSE_SERVER_URL = os.environ.get(
    'LICENSE_SERVER_URL',
    'https://arena-amp-licencas-nl7l.onrender.com'  # servidor de licença no Render
)

GRACE_DAYS = 5          # dias que o sistema funciona sem internet
# Espera longa o suficiente para o "cold start" do Render Free (~30s ao acordar).
# Se fosse curto, a 1ª validação falharia antes do servidor responder.
NETWORK_TIMEOUT = 40    # segundos de espera pela resposta do servidor

# Chave PÚBLICA de verificação (a privada fica SÓ no servidor).
PUBLIC_KEY_PEM = b"""-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAZzLlJ5ejnZrDSqLR6tHtQYsTGrZmnD8djm34N8AG5o4=
-----END PUBLIC KEY-----"""

_PUBLIC_KEY = load_pem_public_key(PUBLIC_KEY_PEM)


# ── Armazenamento local (%APPDATA%\ArenaAMP) ──────────────────────────────────
def storage_dir():
    base = os.environ.get('APPDATA') or os.path.expanduser('~')
    d = os.path.join(base, 'ArenaAMP')
    os.makedirs(d, exist_ok=True)
    return d


def _license_file():
    return os.path.join(storage_dir(), 'license.json')


def _load_local():
    try:
        with open(_license_file(), 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _save_local(data):
    with open(_license_file(), 'w', encoding='utf-8') as f:
        json.dump(data, f)


# ── Identidade estável do PC ──────────────────────────────────────────────────
def get_machine_id():
    """ID que muda pouco: MachineGuid do Windows + nome da máquina."""
    guid = ''
    if platform.system() == 'Windows':
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                               r'SOFTWARE\Microsoft\Cryptography')
            guid, _ = winreg.QueryValueEx(k, 'MachineGuid')
            winreg.CloseKey(k)
        except Exception:
            guid = ''
    raw = f"{guid}|{platform.node()}|{platform.system()}"
    import hashlib
    return hashlib.sha256(raw.encode()).hexdigest()[:40]


# ── Verificação de assinatura ─────────────────────────────────────────────────
def _verify(payload: dict, signature_b64: str) -> bool:
    try:
        raw = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
        _PUBLIC_KEY.verify(base64.b64decode(signature_b64), raw)
        return True
    except (InvalidSignature, Exception):
        return False


def _now():
    return datetime.now(timezone.utc)


# ── Atualização obrigatória ───────────────────────────────────────────────────
def _ver_tuple(v):
    """'3.2' / '3.2.1' → (3,2) / (3,2,1). Ignora pedaços não-numéricos."""
    return tuple(int(x) for x in str(v or '0').split('.') if x.isdigit())


def _update_required(payload):
    """Se o payload ASSINADO exigir uma versão maior que a nossa, devolve o dict
    de update travado; senão None. A URL de download também vem assinada."""
    mv = (payload or {}).get('min_version') or ''
    if mv and _ver_tuple(mv) > _ver_tuple(APP_VERSION):
        return {'status': 'update_required', 'reason': 'versao_antiga',
                'min_version': mv, 'download_url': (payload or {}).get('download_url', '')}
    return None


# ── Consulta ao servidor ──────────────────────────────────────────────────────
def _validate_online(license_key, machine_id):
    nonce = secrets.token_hex(12)
    body = json.dumps({
        'license_key': license_key,
        'machine_id': machine_id,
        'nonce': nonce,
        'client_version': APP_VERSION,
        'product': PRODUCT,   # gancho multi-produto: o servidor ignora por ora
    }).encode()
    req = urllib.request.Request(
        LICENSE_SERVER_URL.rstrip('/') + '/api/validate',
        data=body, headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=NETWORK_TIMEOUT) as resp:
        result = json.loads(resp.read().decode())

    payload = result.get('payload', {})
    signature = result.get('signature', '')

    # A resposta precisa ser autêntica E corresponder ao que pedimos.
    if not _verify(payload, signature):
        return {'ok': False, 'trusted': False, 'reason': 'assinatura_invalida'}
    if payload.get('nonce') != nonce or \
       payload.get('machine_id') != machine_id or \
       payload.get('license_key') != license_key:
        return {'ok': False, 'trusted': False, 'reason': 'resposta_incoerente'}

    # Resposta assinada e coerente → devolve o PAYLOAD INTEIRO para ser guardado
    # e re-verificado offline (não confiamos em nenhum flag local editável).
    return {'ok': bool(payload.get('active')), 'trusted': True,
            'reason': payload.get('reason', ''), 'payload': payload, 'signature': signature}


# ── Relógio à prova de "voltar a data" (guarda o maior horário já visto) ───────
def _effective_now(local):
    now = _now()
    seen = local.get('max_seen')
    try:
        seen_dt = datetime.fromisoformat(seen) if seen else None
    except Exception:
        seen_dt = None
    eff = now if (seen_dt is None or now >= seen_dt) else seen_dt
    local['max_seen'] = eff.isoformat()
    return eff


# ── Re-verificação OFFLINE do último payload assinado ─────────────────────────
def _check_cached(local, machine_id):
    """Valida a licença SEM internet usando o último payload ASSINADO guardado.
    Editar o arquivo quebra a assinatura → bloqueia. Retorna dict ou None."""
    payload = local.get('payload')
    signature = local.get('signature')
    if not payload or not signature:
        return None
    if not _verify(payload, signature):
        return {'status': 'blocked', 'reason': 'licenca_adulterada'}
    if payload.get('machine_id') != machine_id:
        return {'status': 'blocked', 'reason': 'outro_pc'}
    if not payload.get('active'):
        return {'status': 'blocked', 'reason': payload.get('reason', 'suspensa')}

    eff = _effective_now(local)

    # Corte rígido (licença demo): a data está assinada, não dá pra esticar.
    exp = payload.get('expires_at')
    if exp:
        try:
            if eff > datetime.fromisoformat(exp):
                return {'status': 'blocked', 'reason': 'demo_expirada'}
        except Exception:
            pass

    # Tolerância offline = issued_at (relógio do servidor, assinado) + offline_days.
    try:
        issued = datetime.fromisoformat(payload.get('issued_at'))
        limite = issued + timedelta(days=int(payload.get('offline_days', 0) or 0))
        if eff > limite:
            return {'status': 'offline_blocked', 'reason': 'validacao_expirada'}
    except Exception:
        return {'status': 'offline_blocked', 'reason': 'sem_internet'}

    # Mesmo no cache offline: se a última resposta assinada já exigia uma versão
    # nova, trava — assim ninguém escapa do update ficando sem internet.
    upd = _update_required(payload)
    if upd:
        return upd

    return {'status': 'ok', 'reason': 'offline_grace',
            'modules': payload.get('modules', '')}


# ── API pública do módulo ─────────────────────────────────────────────────────
def check_license():
    """
    Retorna dict:
      {'status': 'ok'|'blocked'|'need_key'|'offline_blocked',
       'reason': str}
    """
    local = _load_local()
    key = local.get('license_key')
    if not key:
        return {'status': 'need_key', 'reason': 'sem_licenca'}

    machine_id = get_machine_id()

    # 1) Tenta validar online. QUALQUER falha aqui (rede, timeout, resposta
    #    fora do padrão/JSON inválido no cold start) vira "sem conexão" — nunca
    #    deixa uma exceção estourar e travar a tela.
    res = None
    try:
        res = _validate_online(key, machine_id)
    except Exception:
        res = None

    # 2) Servidor respondeu com um payload ASSINADO e coerente → guarda o payload
    #    inteiro (fonte da verdade, à prova de edição) e decide por ele.
    if res is not None and res.get('trusted'):
        local['license_key'] = key
        local['payload'] = res['payload']
        local['signature'] = res['signature']
        _effective_now(local)
        _save_local(local)
        if res['ok']:
            # Licença ok, mas a versão pode estar defasada → trava pedindo update.
            upd = _update_required(res['payload'])
            if upd:
                return upd
            return {'status': 'ok', 'reason': res['reason'],
                    'modules': res['payload'].get('modules', '')}
        # cortada/suspensa/demo expirada/outro_pc → bloqueia
        return {'status': 'blocked', 'reason': res['reason']}

    # (resposta não confiável — assinatura inválida/incoerente — NÃO sobrescreve
    #  o cache bom; cai para a verificação offline do último payload assinado.)

    # 3) Sem resposta confiável → re-verifica o último payload assinado guardado.
    off = _check_cached(local, machine_id)
    _save_local(local)  # persiste o max_seen atualizado
    if off is not None:
        return off
    return {'status': 'offline_blocked', 'reason': 'sem_internet'}


def activate(license_key):
    """Grava a chave digitada pelo cliente e valida na hora."""
    license_key = (license_key or '').strip().upper()
    if not license_key:
        return {'status': 'need_key', 'reason': 'vazia'}
    local = _load_local()
    local['license_key'] = license_key
    _save_local(local)
    return check_license()


def current_key():
    return _load_local().get('license_key', '')


def cached_license():
    """Devolve a licença assinada guardada localmente (payload + assinatura), pra
    provar a outros serviços (ex.: arena-sync) que este PC tem licença ativa —
    sem eles precisarem ser a máquina vinculada. Só repassa o que o servidor
    de licença já assinou."""
    l = _load_local()
    return {'license_key': l.get('license_key', ''),
            'payload': l.get('payload'),
            'signature': l.get('signature')}


def clear_key():
    """Esquece a licença guardada neste PC (apaga o license.json) pra o app
    voltar a pedir uma chave. Usado no 'Usar outra chave' — resolve o caso de
    cortar/desvincular pra reusar a mesma key em outro computador. NÃO mexe no
    arena.db (dados operacionais do cliente)."""
    try:
        f = _license_file()
        if os.path.exists(f):
            os.remove(f)
        return True
    except Exception:
        # fallback: zera o conteúdo se não conseguir apagar
        try:
            _save_local({})
            return True
        except Exception:
            return False


# ── Checkout (pagamento) ───────────────────────────────────────────────────────
def _post_json(path, body):
    """POST JSON pro license-server. Devolve o dict de resposta (200 ou erro
    JSON já com 'error') ou {'error': ...} genérico se nem isso for possível."""
    try:
        req = urllib.request.Request(
            LICENSE_SERVER_URL.rstrip('/') + path,
            data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'},
            method='POST')
        with urllib.request.urlopen(req, timeout=NETWORK_TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {'error': 'Falha ao conectar ao servidor de licenças.'}
    except Exception:
        return {'error': 'Falha ao conectar ao servidor de licenças.'}


def create_checkout(plan, site=False):
    """Pede ao license-server pra criar um checkout de pagamento avulso e
    renovar/ativar a licença deste PC. `site` inclui o add-on de site (+R$40).
    Retorna {'checkout_url':...} ou {'error':...}."""
    key = current_key()
    if not key:
        return {'error': 'Nenhuma licença ativada neste PC.'}
    body = {'license_key': key, 'machine_id': get_machine_id(), 'plan': plan, 'site': bool(site)}
    return _post_json('/api/checkout/create', body)


def create_subscription(plan, site, email):
    """Cria uma ASSINATURA recorrente (cartão, renova sozinha) no MP. Retorna
    {'init_point':...} (link pra autorizar) ou {'error':...}."""
    key = current_key()
    if not key:
        return {'error': 'Nenhuma licença ativada neste PC.'}
    body = {'license_key': key, 'machine_id': get_machine_id(),
            'plan': plan, 'site': bool(site), 'email': (email or '').strip()}
    return _post_json('/api/subscribe', body)


def cancel_subscription():
    """Cancela a assinatura recorrente. Retorna {'ok':True} ou {'error':...}."""
    key = current_key()
    if not key:
        return {'error': 'Nenhuma licença ativada neste PC.'}
    body = {'license_key': key, 'machine_id': get_machine_id()}
    return _post_json('/api/subscription/cancel', body)


def subscription_info():
    """Dados da assinatura desta licença (plano, vencimento, ciclo, histórico
    de pagamentos) pra montar a página de Assinatura. Retorna dict ou {'error'}."""
    key = current_key()
    if not key:
        return {'error': 'Nenhuma licença ativada neste PC.'}
    body = {'license_key': key, 'machine_id': get_machine_id()}
    return _post_json('/api/subscription', body)


def get_promo():
    """Banners promocionais GLOBAIS do license-server — valem pra todos os
    produtos. Público, sem assinatura. Falha silenciosa se o servidor estiver
    fora do ar. Retorna {'banners':[...], 'image_url':..., 'wa_text':...}."""
    try:
        req = urllib.request.Request(LICENSE_SERVER_URL.rstrip('/') + '/api/promo')
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return {'banners': [], 'image_url': '', 'wa_text': ''}


def get_prices():
    """Preços vigentes DESTE PRODUTO (sistema/site, em reais) definidos no /admin.
    Público. Falha silenciosa → {} (o app usa os preços padrão embutidos)."""
    try:
        req = urllib.request.Request(LICENSE_SERVER_URL.rstrip('/') + '/api/prices?product=' + PRODUCT)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return {}


def get_terms():
    """Termos de Uso vigentes DESTE PRODUTO (versão + HTML), definidos no /admin.
    Público. Retorna {'version':..., 'html':...} ou {} se o servidor não responder."""
    try:
        req = urllib.request.Request(LICENSE_SERVER_URL.rstrip('/') + '/api/terms?product=' + PRODUCT)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return {}


def get_brand():
    """Identidade central do estúdio (PG System) + nome/logo deste produto.
    Público, fail-silent. Usado pra assinar 'por <marca>' e mostrar o contato de
    suporte de forma VARIÁVEL (muda no /admin e reflete em todos os apps)."""
    try:
        req = urllib.request.Request(LICENSE_SERVER_URL.rstrip('/') + '/api/brand?product=' + PRODUCT)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return {}


def report_terms_accept(version):
    """Avisa o license-server que este cliente aceitou os termos (pra aparecer
    no /admin quem aceitou e quando). Falha silenciosa."""
    try:
        body = json.dumps({'license_key': current_key(), 'machine_id': get_machine_id(),
                           'version': version}).encode()
        req = urllib.request.Request(LICENSE_SERVER_URL.rstrip('/') + '/api/terms/accept',
                                     data=body, headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return {}


def get_modules():
    """Módulos liberados — lidos do payload ASSINADO e re-verificados.
    Licença inválida/adulterada/expirada → lista vazia (tudo bloqueado)."""
    local = _load_local()
    off = _check_cached(local, get_machine_id())
    _save_local(local)
    if off and off.get('status') == 'ok':
        raw = off.get('modules') or ''   # vazio = NENHUM módulo (à prova de falhas)
        return [m.strip() for m in raw.split(',') if m.strip()]
    return []


def _verified_payload():
    """Devolve o payload assinado SE ainda for autêntico e do PC certo; senão {}."""
    local = _load_local()
    payload = local.get('payload') or {}
    sig = local.get('signature')
    if payload and sig and _verify(payload, sig) and payload.get('machine_id') == get_machine_id():
        return payload
    return {}


def get_plan():
    """Estado da assinatura: plan + dias de demo restantes.
    Só reporta o plano real se a licença estiver de fato válida AGORA (mesma
    checagem de get_modules(), via _check_cached) — senão um cliente bloqueado/
    com assinatura vencida continuaria vendo 'Premium ativo' em vez da tela
    de renovação, mesmo já trancado dos módulos."""
    local = _load_local()
    off = _check_cached(local, get_machine_id())
    _save_local(local)
    if not (off and off.get('status') == 'ok'):
        return {'plan': 'free', 'demo_days_left': None, 'has_site': False, 'site_url': ''}

    payload = local.get('payload') or {}
    plan = payload.get('plan', 'free')
    dias = None
    if plan == 'demo' and payload.get('expires_at'):
        try:
            eff = _effective_now(local)
            _save_local(local)
            exp = datetime.fromisoformat(payload['expires_at'])
            dias = max(0, (exp - eff).days)
        except Exception:
            pass
    return {'plan': plan, 'demo_days_left': dias, 'has_site': bool(payload.get('has_site')),
            'site_url': payload.get('site_url', '') or ''}


def get_banner():
    """Aviso remoto (assinado) para exibir no Hub. Você muda no painel de licenças."""
    return _verified_payload().get('banner', '') or ''
