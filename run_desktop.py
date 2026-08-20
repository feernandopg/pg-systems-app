r"""
run_desktop.py — Ponto de entrada do app instalável Arena AMP.

Abre o sistema numa JANELA NATIVA própria (pywebview) — sem a barra do Edge,
título com o nome da arena e ícone da marca. Se o pywebview não estiver
disponível/empacotado (ou falhar em runtime), cai automaticamente pro Microsoft
Edge em modo --app, que é o caminho antigo e robusto. No pior caso, abre igual
antes — o cliente nunca fica sem janela.

Sequência:
  1. Sobe o Flask local (127.0.0.1) numa porta livre.
  2. Abre a janela na "portaria" de licença (/_gate), que valida ANTES de liberar.
       - sem chave  → tela de ativação
       - cortada    → tela de bloqueio
       - ok         → entra no sistema
Os dados ficam no PC, em %APPDATA%\ArenaAMP\arena.db.
"""
import os
import socket
import threading
import time
import subprocess
import tempfile
import urllib.request
import webbrowser

# CA certificates: no .exe empacotado (Nuitka) o Python não acha os certificados
# do Windows, então HTTPS (ex.: baixar a atualização do GitHub) falha com
# 'certificate verify failed'. Aponta o SSL pro bundle do certifi, que vai junto
# no pacote. Se não achar, segue sem — o download tem fallback próprio.
try:
    import certifi as _certifi
    _cafile = _certifi.where()
    if os.path.exists(_cafile):
        os.environ.setdefault('SSL_CERT_FILE', _cafile)
        os.environ.setdefault('REQUESTS_CA_BUNDLE', _cafile)
except Exception:
    pass

from flask import jsonify, request
from flask_login import logout_user

import license_client
from app import app as flask_app, db, User, _local_data_dir
try:
    from version import APP_VERSION
except Exception:
    APP_VERSION = '999'

# Nome do mutex — precisa BATER com o AppMutex do installer.iss, pra o
# instalador conseguir fechar o app antes de sobrescrever os arquivos.
APP_MUTEX = 'ArenaAMP_Running_Mutex'

# Contato exibido na tela de bloqueio.
SUPPORT_CONTACT = "Fernando · WhatsApp (11) 97244-7927 · fehgodinho98@gmail.com"

_flask_started = False
_base_url = None
_last_beat = time.time()
_edge_proc = None                       # processo da janela do Edge (pra fechar no update)
_update = {'phase': 'idle', 'pct': 0}   # progresso da atualização (download/instalação)
_update_lock = threading.Lock()        # garante UMA atualização por vez (sem downloads concorrentes)


def _log(msg):
    """Registra passos da inicialização em %APPDATA%\\ArenaAMP\\launcher.log."""
    try:
        with open(os.path.join(_local_data_dir(), 'launcher.log'), 'a', encoding='utf-8') as f:
            f.write(time.strftime('%Y-%m-%d %H:%M:%S') + '  ' + msg + '\n')
    except Exception:
        pass


_mutex_handle = None
def _create_mutex():
    """Cria o mutex nomeado (Windows) que o instalador (AppMutex no installer.iss)
    usa pra detectar o app aberto e fechá-lo antes de atualizar. Mantido vivo
    enquanto o processo existir."""
    global _mutex_handle
    if os.name != 'nt':
        return
    try:
        import ctypes
        _mutex_handle = ctypes.windll.kernel32.CreateMutexW(None, False, APP_MUTEX)
    except Exception as e:
        _log('não foi possível criar o mutex: ' + repr(e))


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _pick_port(remote):
    """Com acesso remoto LIGADO, usa uma porta FIXA (5000..5010) pro link do
    celular ficar estável. Desligado, mantém a porta dinâmica de sempre."""
    if not remote:
        return _free_port()
    for p in range(5000, 5011):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(('0.0.0.0', p))
            s.close()
            return p
        except OSError:
            s.close()
            continue
    return _free_port()


def _remote_enabled():
    """Acesso pela rede local / Tailscale foi REMOVIDO — o acesso pelo celular
    agora é 100% pela nuvem (arena-sync). O servidor local sempre fica só no
    127.0.0.1 (mais seguro: nada exposto na rede)."""
    return False


def _ensure_admin():
    with flask_app.app_context():
        # 1º migra colunas que faltam (updates com campo novo), 2º cria tabelas
        # novas, e só ENTÃO consulta — senão crasharia com 'no such column'.
        try:
            from app import _auto_migrate
            _auto_migrate()
        except Exception:
            pass
        db.create_all()
        # Gera as parcelas (mensalidades) dos alunos que ainda não têm — roda 1x
        # na 1ª abertura após a atualização, migrando toda a base do cliente.
        try:
            from app import (_backfill_mensalidades, _backfill_sync_fields,
                             _backfill_fix_parcela_alignment)
            _backfill_mensalidades()
            _backfill_fix_parcela_alignment()   # corrige 1ª parcela p/ o mês de início
            _backfill_sync_fields()
        except Exception:
            pass
        admin = User.query.filter_by(username='admin').first()
        if not admin:
            admin = User(username='admin', role='adm', perms='aulas,ranking,comandas')
            admin.set_password('admin123')
            db.session.add(admin)
            db.session.commit()
        elif not getattr(admin, 'is_adm', False):
            admin.role = 'adm'
            db.session.commit()
    # backup automático semanal do banco inteiro (%APPDATA%\ArenaAMP\backups)
    try:
        from app import auto_backup
        auto_backup()
    except Exception:
        pass


def _start_flask_once():
    global _flask_started, _base_url
    if _flask_started:
        return _base_url
    _ensure_admin()
    # Acesso remoto (opt-in): liga o bind na rede + porta fixa; senão, local só.
    remote = _remote_enabled()
    host = '0.0.0.0' if remote else '127.0.0.1'
    port = _pick_port(remote)
    _base_url = f'http://127.0.0.1:{port}/'   # a janela do dono é sempre local
    _log(f'flask host={host} port={port} remoto={"on" if remote else "off"}')

    def run():
        flask_app.run(host=host, port=port,
                      threaded=True, use_reloader=False, debug=False)

    threading.Thread(target=run, daemon=True).start()
    for _ in range(60):
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    _flask_started = True
    _start_auto_sync()
    return _base_url


def _start_auto_sync():
    """Auto-sync com a nuvem (opt-in `cloud_sync`): puxa ao abrir + a cada 5 min,
    e empurra ao fechar. Tudo best-effort e em background — o `sync_auto` já é
    no-op se não estiver configurado, então em cliente sem nuvem não faz nada."""
    def _run(kind):
        try:
            with flask_app.app_context():
                from app import sync_auto
                sync_auto(kind)
        except Exception as e:
            _log('auto-sync ' + kind + ' falhou: ' + repr(e))

    # puxa ao abrir (depois de 8s, em background — não trava a janela; se o Render
    # estiver dormindo, o cold start de ~40s acontece aqui sem incomodar)
    threading.Timer(8, lambda: _run('pull')).start()

    def _loop():
        while True:
            time.sleep(300)     # a cada 5 min: empurra + puxa
            _run('both')
    threading.Thread(target=_loop, daemon=True).start()

    import atexit
    atexit.register(lambda: _run('push'))   # empurra ao fechar (best-effort)


# ── Portaria de licença servida pelo Flask ────────────────────────────────────
@flask_app.route('/_gate')
def _gate_page():
    # Por padrão força logout a cada abertura (PC compartilhado não pode abrir
    # já logado como quem usou por último). MAS se o usuário marcou "Manter
    # conectado neste computador", respeita: não desloga, e ele entra direto.
    try:
        from app import get_setting
        keep = (get_setting('remember_login', '') == '1')
    except Exception:
        keep = False
    if not keep:
        logout_user()
    return GATE_HTML.replace('__TITLEBAR__', '').replace('__SUPPORT__', SUPPORT_CONTACT)


@flask_app.route('/_gate/state')
def _gate_state():
    return jsonify(license_client.check_license())


@flask_app.route('/_gate/activate', methods=['POST'])
def _gate_activate():
    key = (request.get_json(silent=True) or {}).get('key', '')
    return jsonify(license_client.activate(key))


def _fechar_janela_edge():
    """Fecha a janela antiga do app (o Edge --app que abrimos) pra não ficar
    uma sobrando atrás da nova depois da atualização. Best-effort."""
    global _edge_proc
    try:
        if _edge_proc and _edge_proc.poll() is None:
            # /T mata a árvore (o processo pai do Edge cria filhos).
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(_edge_proc.pid)],
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                           timeout=5)
    except Exception as e:
        _log('fechar janela (pid) falhou: ' + repr(e))
    # Rede de segurança: o Edge às vezes RE-LANÇA o processo, então o pid acima
    # não pega a janela e ela ficava sobrando ("Instalando…") atrás da nova. Mata
    # qualquer msedge que esteja mostrando a NOSSA janela (--app=http://127.0.0.1),
    # sem tocar no Edge normal do usuário.
    try:
        ps = ("Get-CimInstance Win32_Process | Where-Object { "
              "$_.Name -eq 'msedge.exe' -and $_.CommandLine -like '*app=http://127.0.0.1*' } | "
              "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
        subprocess.run(['powershell', '-NoProfile', '-Command', ps],
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=8)
    except Exception as e:
        _log('fechar janela (por --app) falhou: ' + repr(e))


def _rodar_atualizacao(url):
    """Roda em thread: baixa o instalador (com barra de progresso via _update),
    instala em modo silencioso (sem o assistente do Inno aparecer), fecha a
    janela antiga e encerra o processo. A URL vem ASSINADA (Ed25519) — não dá
    pra um MITM redirecionar o download."""
    global _update
    # Um download por vez: sem isto, dois disparos rodavam em paralelo e a barra
    # oscilava (um escrevia 80, outro 12, no mesmo contador).
    if not _update_lock.acquire(blocking=False):
        _log('update: já em andamento — ignorando disparo duplicado')
        return
    dest = os.path.join(tempfile.gettempdir(), 'ArenaAMP-Setup.exe')
    _log('update: iniciando — url=' + (url or '(vazia)'))

    def _baixar(context):
        req = urllib.request.Request(url, headers={'User-Agent': 'ArenaAMP-Updater'})
        with urllib.request.urlopen(req, timeout=license_client.NETWORK_TIMEOUT, context=context) as r, \
                open(dest, 'wb') as f:
            total = 0
            try:
                total = int(r.headers.get('Content-Length') or 0)
            except Exception:
                total = 0
            baixado = 0
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                baixado += len(chunk)
                if total > 0:
                    novo = min(99, int(baixado * 100 / total))
                else:
                    # Sem Content-Length: mostra um progresso "andando".
                    novo = min(95, _update.get('pct', 0) + 1)
                # NUNCA deixa a barra voltar (evita a oscilação vista em retry/redirect).
                if novo > _update.get('pct', 0):
                    _update['pct'] = novo

    try:
        _update = {'phase': 'downloading', 'pct': 0}
        import ssl
        # 1ª tentativa: verificando o certificado com o bundle do certifi.
        try:
            import certifi
            ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            ctx = ssl.create_default_context()
        try:
            _baixar(ctx)
        except Exception as e1:
            # Se o .exe não tiver os certificados (verify failed), baixa SEM
            # verificar. É seguro: a URL vem ASSINADA (Ed25519) no payload da
            # licença — ninguém consegue redirecionar pra outro lugar.
            _log('download verificado falhou (' + repr(e1) + ') — tentando sem verificação (URL assinada)')
            _baixar(ssl._create_unverified_context())
        _update['pct'] = 100
        _log('atualização baixada em ' + dest + ' — instalando em silêncio')
    except Exception as e:
        _log('falha ao baixar atualização: ' + repr(e))
        _update = {'phase': 'error', 'pct': 0, 'reason': 'download_falhou'}
        _update_lock.release()   # libera pra permitir "Tentar novamente"
        return

    # Instala via um HELPER (.cmd) DESACOPLADO do app. O helper espera ~3s (o app
    # morre em ~1.2s e libera o "Arena AMP.exe"), mata qualquer sobra do processo,
    # e SÓ ENTÃO roda o instalador com /LOG. Assim: (a) some a corrida "instalar
    # com o exe em uso" e (b) SEMPRE fica um log do instalador em
    # %APPDATA%\ArenaAMP\inno_update.log, mesmo o app já tendo encerrado.
    try:
        _update = {'phase': 'installing', 'pct': 100}
        tam = os.path.getsize(dest) if os.path.exists(dest) else -1
        _log('download OK (%d bytes) em %s' % (tam, dest))
        innolog = os.path.join(_local_data_dir(), 'inno_update.log')
        # Lança o INSTALADOR direto (sem cmd/ping) — assim NÃO pisca janela preta
        # na cara do cliente. O instalador (installer.iss [Code]) já encerra o app,
        # fecha a janela antiga e REABRE a nova sozinho. /LOG grava o que ele fez
        # em %APPDATA%\ArenaAMP\inno_update.log. Sem console: DETACHED_PROCESS
        # (Inno em /VERYSILENT não mostra GUI, então a atualização é invisível).
        flags = (getattr(subprocess, 'DETACHED_PROCESS', 0x8)
                 | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x200)
                 | getattr(subprocess, 'CREATE_BREAKAWAY_FROM_JOB', 0x1000000))
        args = [dest, '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/LOG=' + innolog]
        _log('lançando instalador (silencioso, sem janela)')
        try:
            subprocess.Popen(args, creationflags=flags, close_fds=True)
        except OSError as e:
            _log('breakaway falhou (%r) — tentando só desacoplado' % e)
            subprocess.Popen(args, creationflags=getattr(subprocess, 'DETACHED_PROCESS', 0x8),
                             close_fds=True)
        _log('instalador lançado; fechando janela e encerrando o app')
    except Exception as e:
        _log('falha ao lançar instalador: ' + repr(e))
        _update = {'phase': 'error', 'pct': 100, 'reason': 'exec_falhou'}
        _update_lock.release()   # libera pra permitir "Tentar novamente"
        return

    # Fecha a janela antiga e encerra este processo pra liberar os arquivos
    # (o helper já está desacoplado e sobrevive ao fim do app).
    _fechar_janela_edge()
    _log('encerrando o app em 1.2s (os._exit) — helper assume a instalação')
    threading.Timer(1.2, lambda: os._exit(0)).start()


@flask_app.route('/_gate/update', methods=['POST'])
def _gate_update():
    """Dispara a atualização em segundo plano (download + instalação silenciosa)
    e devolve na hora. A tela acompanha o progresso via /_gate/update/progress.
    Só roda quando a checagem de licença retornou status 'update_required'."""
    global _update
    if _update.get('phase') in ('downloading', 'installing'):
        return jsonify({'ok': True, 'ja_rodando': True})
    st = license_client.check_license()
    if st.get('status') != 'update_required':
        return jsonify({'ok': False, 'reason': 'sem_atualizacao'})
    url = (st.get('download_url') or '').strip()
    if not url:
        return jsonify({'ok': False, 'reason': 'sem_url'})
    _update = {'phase': 'downloading', 'pct': 0}
    threading.Thread(target=_rodar_atualizacao, args=(url,), daemon=True).start()
    return jsonify({'ok': True})


@flask_app.route('/_gate/update/progress')
def _gate_update_progress():
    """Estado atual da atualização, pra barra de progresso na tela do gate."""
    return jsonify(_update)


@flask_app.route('/_gate/reset', methods=['POST'])
def _gate_reset():
    """Esquece a licença guardada neste PC (apaga o license.json) pra o app
    voltar a pedir uma chave. Não toca no arena.db (dados do cliente)."""
    try:
        license_client.clear_key()
    except Exception as e:
        _log('falha ao limpar licença: ' + repr(e))
    return jsonify({'ok': True})


# ── "Batimento" janela↔servidor: mantém o backend vivo só enquanto a janela existe ──
@flask_app.route('/_beat')
def _beat():
    global _last_beat
    _last_beat = time.time()
    return ''


@flask_app.after_request
def _inject_beat(resp):
    try:
        if resp.content_type and resp.content_type.startswith('text/html'):
            html = resp.get_data(as_text=True)
            if '</body>' in html:
                tag = "<script>setInterval(function(){fetch('/_beat').catch(function(){})},3000);</script>"
                resp.set_data(html.replace('</body>', tag + '</body>'))
    except Exception:
        pass
    return resp


def _watchdog():
    # Vigia a JANELA (o processo do Edge). Se ela fecha, encerra o backend pra
    # não deixar um processo zumbi. NÃO dependemos mais só do "batimento": o
    # Chromium ESTRANGULA o setInterval quando a janela está minimizada/em
    # segundo plano, e o watchdog matava o backend por engano (o famoso
    # "127.0.0.1 recusou"). Agora o sinal principal é o processo do Edge estar
    # vivo; o batimento é só um fallback com MUITA folga.
    time.sleep(40)  # deixa a janela carregar (inclui o cold-start da licença)
    while True:
        time.sleep(5)
        proc = _edge_proc
        if proc is not None:
            if proc.poll() is not None:
                _log('watchdog: janela do app fechada — encerrando')
                os._exit(0)
            continue  # Edge vivo → mantém o backend, mesmo minimizado/sem batimento
        # Sem processo de janela conhecido (fallback): usa o batimento com folga
        # grande, porque o Chromium em segundo plano pode espaçar até ~60s.
        if time.time() - _last_beat > 180:
            _log('watchdog: sem janela e sem batimento há muito tempo — encerrando')
            os._exit(0)


# ── Janela do app ─────────────────────────────────────────────────────────────
# Preferência: janela NATIVA própria (pywebview) — sem a barra do Edge, título
# com o nome da arena, ícone da marca. Se o pywebview não estiver disponível ou
# falhar (ex.: empacotamento/WebView2), cai automaticamente pro Edge --app, que
# é o caminho antigo e robusto. Assim, no pior caso, o app abre igual antes.
def _window_title():
    try:
        from app import get_setting
        with flask_app.app_context():
            nome = (get_setting('arena_name', '') or '').strip()
        return nome or 'Arena AMP'
    except Exception:
        return 'Arena AMP'


def _find_edge():
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        os.path.join(os.environ.get('LOCALAPPDATA', ''), r"Microsoft\Edge\Application\msedge.exe"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def _open_window_edge(url):
    """Abre via Edge --app e retorna imediatamente (fire-and-forget). O watchdog
    cuida do fim (batimento /_beat). É o fallback robusto."""
    # O watchdog só faz sentido no caminho Edge (fire-and-forget). Na janela
    # nativa NÃO usamos ele — senão, ao minimizar, os beats poderiam parar e o
    # app se encerraria sozinho. Lá o webview.start() já cuida do fechar.
    threading.Thread(target=_watchdog, daemon=True).start()
    edge = _find_edge()
    if edge:
        profile = os.path.join(_local_data_dir(), 'edge-profile')
        _log('abrindo janela via Edge: ' + edge)
        try:
            global _edge_proc
            _edge_proc = subprocess.Popen([
                edge, f'--app={url}',
                f'--user-data-dir={profile}',
                '--no-first-run', '--no-default-browser-check',
                '--window-size=1180,800',
                # Tira "cara de navegador": sem tradução, sem mini-menu de seleção,
                # sem sugestões/serviços web do Edge.
                '--disable-features=Translate,TranslateUI,msEdgeTranslate,msTranslateBubble,'
                'msEdgeMiniMenu,msMiniMenu,MSAcrobat,msEdgeSidebar,msWebOOBE,msEdgeShoppingAssist',
                '--disable-translate',
                '--disable-sync',
                '--no-service-autorun',
                '--disable-component-update',
            ])
            return
        except Exception as e:
            _log('falha ao abrir Edge (' + repr(e) + '), usando navegador padrão')
    else:
        _log('Edge não encontrado, usando navegador padrão')
    try:
        webbrowser.open(url)
    except Exception as e:
        _log('falha ao abrir navegador: ' + repr(e))


def _open_window(url):
    """Abre a janela do app via Edge --app (janela dedicada, sem abas/barra de
    endereço). É o caminho único e robusto — o pywebview foi descartado porque
    depende do pythonnet, que não empacota de forma confiável com o Nuitka."""
    _open_window_edge(url)




GATE_HTML = """
<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#0A1420">
<link rel="icon" type="image/x-icon" href="/static/logo.ico">
<link rel="shortcut icon" type="image/x-icon" href="/static/logo.ico">
<title>Arena AMP</title>
<style>
  *{box-sizing:border-box;} body{margin:0;font-family:system-ui,Segoe UI,sans-serif;
    background:linear-gradient(160deg,#0f172a,#1e293b);color:#e2e8f0;height:100vh;
    display:grid;place-items:center;}
  .box{width:min(420px,90vw);background:#1e293b;border:1px solid #334155;border-radius:16px;
    padding:2.2rem;text-align:center;box-shadow:0 20px 60px rgba(0,0,0,.4);}
  h1{font-size:1.5rem;margin:.2rem 0 .1rem;} .sub{color:#94a3b8;font-size:.9rem;margin-bottom:1.5rem;transition:opacity .3s;}
  .logo{font-size:2.4rem;}
  input{width:100%;padding:13px;border-radius:10px;border:1px solid #475569;background:#0f172a;
    color:#fff;font-size:1.1rem;text-align:center;letter-spacing:2px;text-transform:uppercase;margin-bottom:12px;}
  button{width:100%;padding:13px;border:none;border-radius:10px;background:#f97316;color:#fff;
    font-weight:700;font-size:1rem;cursor:pointer;} button:disabled{opacity:.5;cursor:default;}
  .msg{margin-top:12px;font-size:.9rem;min-height:20px;}
  .err{color:#f87171;} .contact{color:#fbbf24;font-weight:600;margin-top:8px;}
  .spin{display:inline-block;width:16px;height:16px;border:2px solid #fff;border-top-color:transparent;
    border-radius:50%;animation:s .7s linear infinite;vertical-align:middle;} @keyframes s{to{transform:rotate(360deg)}}
  .spin.big{width:44px;height:44px;border-width:4px;border-color:#f97316;border-top-color:transparent;}
  .bar{width:100%;height:12px;border-radius:8px;background:#0f172a;border:1px solid #475569;
    overflow:hidden;margin:6px 0 4px;}
  .bar>i{display:block;height:100%;width:0;background:linear-gradient(90deg,#f97316,#fb923c);
    border-radius:8px;transition:width .3s ease;}
  .pct{font-size:.85rem;color:#cbd5e1;font-weight:600;}
</style></head><body>
__TITLEBAR__
<div class="box" id="box">
  <div class="logo" id="logo"><span class="spin big"></span></div>
  <h1 id="title">Verificando licença…</h1>
  <div class="sub" id="subtitle">Conectando ao servidor…</div>
  <div id="form" style="display:none;">
    <input id="key" placeholder="AMP-XXXX-XXXX-XXXX" maxlength="19" autocomplete="off">
    <button id="btn" onclick="doActivate()">Ativar sistema</button>
  </div>
  <div id="retry" style="display:none;"><button onclick="init()">Tentar novamente</button></div>
  <div id="update" style="display:none;"><button id="upbtn" onclick="doUpdate()">Baixar e instalar atualização</button></div>
  <div id="reset" style="display:none;margin-top:10px"><button onclick="doReset()" style="background:transparent;border:1px solid #475569;color:#cbd5e1">Usar outra chave de licença</button></div>
  <div id="upprog" style="display:none;">
    <div class="bar"><i id="upbar"></i></div>
    <div class="pct" id="uppct">0%</div>
  </div>
  <div class="msg" id="msg"></div>
</div>
<script>
  const $ = id => document.getElementById(id);
  const SUPPORT = "__SUPPORT__";
  let _timers = [];
  function clearTimers(){ _timers.forEach(clearTimeout); _timers = []; }
  function setSub(t){ const el=$('subtitle'); el.style.opacity=0; setTimeout(()=>{el.textContent=t; el.style.opacity=1;},150); }
  function startWaitHints(){
    clearTimers();
    _timers.push(setTimeout(()=>setSub('O servidor está iniciando — a 1ª conexão pode levar até 30 segundos…'),4000));
    _timers.push(setTimeout(()=>setSub('Quase lá, aguarde só mais um instante…'),18000));
  }
  function show(el,on){ $(el).style.display = on?'block':'none'; }

  async function init(){
    $('box').classList.remove('blocked');
    $('logo').innerHTML='<span class="spin big"></span>';
    $('title').textContent='Verificando licença…';
    $('subtitle').textContent='Conectando ao servidor…'; $('msg').textContent='';
    show('form',false); show('retry',false); show('update',false); show('upprog',false); show('reset',false);
    startWaitHints();
    try {
      const st = await (await fetch('/_gate/state')).json();
      clearTimers(); render(st);
    } catch(e){
      clearTimers();
      $('logo').textContent='⚠️'; $('title').textContent='Não foi possível verificar';
      $('subtitle').textContent='Houve um problema ao validar a licença. Tente novamente.';
      show('retry',true);
    }
  }

  function render(st){
    if(st.status==='ok'){
      $('logo').textContent='✅'; $('title').textContent='Acesso liberado';
      $('subtitle').textContent='Abrindo o sistema…';
      location.href='/'; return;
    }
    if(st.status==='need_key'){
      $('logo').textContent='🔑'; $('title').textContent='Ativar Arena AMP';
      $('subtitle').textContent='Digite a chave de licença que você recebeu.';
      show('form',true); $('key').focus(); return;
    }
    if(st.status==='update_required'){
      $('logo').textContent='⬆️'; $('title').textContent='Atualização obrigatória';
      $('subtitle').innerHTML='Há uma nova versão do sistema.<br>Atualize para continuar usando.';
      $('upbtn').disabled=false; $('upbtn').textContent='Baixar e instalar atualização';
      show('upprog',false); show('update',true); return;
    }
    $('logo').textContent = st.status==='offline_blocked' ? '📡' : '🔒';
    if(st.status==='offline_blocked'){
      $('title').textContent='Sem conexão';
      $('subtitle').textContent='Conecte-se à internet para validar a licença e continuar usando.';
    } else {
      $('title').textContent='Acesso suspenso';
      $('subtitle').innerHTML='Este sistema está temporariamente desativado.<br>Entre em contato para regularizar:';
      $('msg').innerHTML='<div class="contact">'+SUPPORT+'</div>';
      show('reset',true);   // permite trocar de chave (ex.: licença movida de PC)
    }
    show('retry',true);
  }
  function doReset(){
    if(!confirm('Isto desconecta esta licença deste computador pra você entrar com OUTRA chave.\\n\\nOs dados do sistema (alunos, comandas etc.) NÃO são apagados. Continuar?')) return;
    fetch('/_gate/reset',{method:'POST'}).then(function(){ init(); }).catch(function(){ init(); });
  }

  async function doActivate(){
    const key = $('key').value.trim();
    if(!key){ $('msg').innerHTML='<span class="err">Digite a chave.</span>'; return; }
    $('btn').disabled=true; $('btn').innerHTML='<span class="spin"></span> Validando…'; $('msg').textContent='';
    setSub('Validando sua licença…'); startWaitHints();
    let st;
    try {
      st = await (await fetch('/_gate/activate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key})})).json();
    } catch(e){
      clearTimers(); $('btn').disabled=false; $('btn').textContent='Ativar sistema';
      $('msg').innerHTML='<span class="err">Erro ao validar. Tente novamente.</span>'; return;
    }
    clearTimers(); $('btn').disabled=false; $('btn').textContent='Ativar sistema';
    if(st.status==='ok'){ render(st); return; }
    if(st.status==='update_required'){ render(st); return; }
    if(st.status==='need_key'){ $('msg').innerHTML='<span class="err">Chave inválida.</span>'; return; }
    if(st.status==='offline_blocked'){ $('msg').innerHTML='<span class="err">Sem internet para validar. Conecte-se e tente de novo.</span>'; return; }
    $('msg').innerHTML='<span class="err">Licença não reconhecida ou suspensa.</span>';
  }

  function setBar(p){ $('upbar').style.width=p+'%'; $('uppct').textContent=p+'%'; }

  async function doUpdate(){
    // Dispara a atualização e some com o botão — daqui pra frente é tudo
    // automático (baixa, instala em silêncio e reabre sozinho).
    let r;
    try {
      r = await (await fetch('/_gate/update',{method:'POST'})).json();
    } catch(e){
      $('msg').innerHTML='<span class="err">Não foi possível iniciar. Verifique a internet e tente de novo.</span>';
      return;
    }
    if(!(r && r.ok)){
      $('msg').innerHTML='<span class="err">Não foi possível iniciar a atualização. Verifique a internet.</span>';
      return;
    }
    show('update',false); show('upprog',true);
    $('logo').innerHTML='<span class="spin big"></span>';
    $('title').textContent='Baixando atualização…';
    $('subtitle').textContent='Não feche o sistema. Ele vai reabrir sozinho quando terminar.';
    $('msg').textContent=''; setBar(0);
    pollUpdate();
  }

  async function pollUpdate(){
    let st;
    try {
      st = await (await fetch('/_gate/update/progress')).json();
    } catch(e){
      // Se a resposta sumir, é porque o app já encerrou pra instalar (esperado).
      return;
    }
    if(st.phase==='downloading'){
      $('title').textContent='Baixando atualização…'; setBar(st.pct||0);
      setTimeout(pollUpdate,400); return;
    }
    if(st.phase==='installing'){
      $('title').textContent='Instalando…';
      $('subtitle').textContent='Quase lá — o sistema vai reabrir sozinho em instantes.';
      setBar(100); setTimeout(pollUpdate,600); return;
    }
    if(st.phase==='error'){
      show('upprog',false); show('update',true);
      $('logo').textContent='⚠️'; $('title').textContent='Atualização obrigatória';
      $('subtitle').innerHTML='Há uma nova versão do sistema.<br>Atualize para continuar usando.';
      $('upbtn').disabled=false; $('upbtn').textContent='Tentar novamente';
      $('msg').innerHTML='<span class="err">Não foi possível baixar a atualização. Verifique a internet.</span>';
      return;
    }
    // idle/desconhecido: continua checando um pouco
    setTimeout(pollUpdate,600);
  }

  document.addEventListener('input', e=>{
    if(e.target.id!=='key') return;
    let v = e.target.value.toUpperCase().replace(/[^A-Z0-9]/g,'');
    if(v.startsWith('AMP')) v=v.slice(3);
    let out='AMP',i=0; while(i<v.length){ out+='-'+v.slice(i,i+4); i+=4; }
    e.target.value=out;
  });
  init();
</script>
</body></html>
"""


def main():
    _log('=== iniciando Arena AMP ===')
    _create_mutex()
    base = _start_flask_once()
    _log('flask no ar em ' + base)
    # OBS: o watchdog é iniciado dentro de _open_window_edge (só no fallback Edge).
    # Na janela nativa (pywebview) ele não roda — o webview.start() controla o fim.
    _open_window(base + '_gate')
    _log('janela solicitada; mantendo servidor vivo')
    # Mantém o processo vivo enquanto a janela estiver aberta.
    # O watchdog encerra sozinho quando os "batimentos" param (janela fechada).
    while True:
        time.sleep(1)


if __name__ == '__main__':
    main()
