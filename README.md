# Arena AMP — App desktop (PG Systems)

Sistema **desktop** de gestão para arenas de beach tennis. É o programa que roda no
PC do dono (instalado via `ArenaAMP-Setup.exe`) e reúne os módulos do dia a dia:

- **Hub** — tela inicial que dá acesso aos módulos
- **Aulas** — alunos, turmas, frequência, mensalidades (parcelas por mês), reposições
- **Ranking**, **Comandas**, **Relatórios**

Backend em **Flask + SQLAlchemy** (SQLite local, no PC), empacotado com **Nuitka +
MinGW** e distribuído por um instalador **Inno Setup**. É **licenciado** (Ed25519) e
tem **atualização automática** (checa `min_version` no servidor de licença e baixa o
instalador novo sozinho).

> **Repositório do produto.** Este repo é o app que o cliente instala. Ele conversa
> com outros dois serviços, cada um no seu repositório:

| Serviço | Repositório | O que faz |
|---|---|---|
| **App desktop** (este) | `pg-systems-app` | O que roda no PC do cliente |
| **Sincronização na nuvem** | [`arena-sync`](https://github.com/feernandopg/ArenaAMP-app) | Espelha o módulo **Aulas** na nuvem (Render) pra a equipe acessar pelo celular com o PC desligado; sincroniza com o PC |
| **Servidor de licença** | `pg-systems-licencas` | Licença, pagamento, `min_version`/`download_url` do update. **NÃO** é tocado pelos outros |

## Como o acesso pelo celular funciona
A equipe acessa o **Aulas na nuvem** (serviço `arena-sync`). O PC **sincroniza**
com a nuvem (empurra/puxa) quando o dono liga *Configurações → Nuvem → Sincronizar
automaticamente*. As mudanças dos dois lados se juntam (mais-recente-vence). O motor
de sync é o `sync_core.py` (arquivo **compartilhado**, mantido idêntico nos dois
repos).

## Build / release
1. `build.bat` (ou rodar o Nuitka direto) gera `run_desktop.dist\Arena AMP.exe`
   (precisa Python 3.12 + MinGW; o `--file-version` e o `installer.iss` seguem o
   `version.py` — **fonte única da versão**).
2. Abrir `installer.iss` no **Inno Setup** (F9) → gera `Output\ArenaAMP-Setup.exe`.
3. Publicar como **Release** no GitHub (tag `vX.Y`) com o instalador anexado.
4. No painel de licença: setar `download_url` (dica: use a URL
   `releases/latest/download/ArenaAMP-Setup.exe`, que **nunca muda**) e subir o
   `min_version` — só então os clientes atualizam.

## Rodar em DEV
```bash
python run_dev.py        # servidor de desenvolvimento (single-thread)
```
Produção usa `run_desktop.py` (Flask `threaded=True` + janela do app). O banco fica
em `%APPDATA%\ArenaAMP\arena.db` (fora da pasta de instalação — reinstalar não apaga
os dados).

## Documentação
- `PLANO_ACESSO_REMOTO.md` — projeto completo do acesso pelo celular / sync.
- `MANUAL.md` — visão geral do app.
