# 📘 PLANO TÉCNICO — Acesso remoto da funcionária (Fases 3–5)

> Documento de desenho. Nada aqui foi codado ainda. Serve pra alinhar antes de
> escrever código, principalmente porque a Fase 3 toca infra nova e os dados
> vivos do 3.18. Fases 1 (mobile) e 2 (acesso pela rede/Tailscale) já estão
> prontas no app (ver [[project-acesso-remoto-funcionaria]]).

## 0. Decisões já travadas
- **Modelo híbrido "passa-bastão":** PC ligado → funcionária acessa via **Tailscale → PC** (tempo real). PC desligado → **fallback hospedado no Render**. Os dois nunca escrevem ao mesmo tempo (PC off = PC não edita).
- **Serviço Render SEPARADO** (`arena-sync`) — o servidor de licença (`pg-systems-licencas`) fica **100% intocado**. Risco isolado.
- **Isolamento por arena** via a chave de licença (dados de uma arena nunca cruzam com outra).
- **Sync sem migração de PK:** usa uma coluna `sync_uid` (UUID) aditiva; os ids autoincrement atuais NÃO mudam (sem risco nos dados do 3.18 em campo).
- **Cadência:** empurra ao fechar + a cada poucos minutos; o servidor **dorme quando ninguém usa** (gasto mínimo de Render).

---

## 1. Arquitetura

```
                         (horário de funcionamento — PC ligado)
   Celular da funcionária ───── Tailscale (WireGuard) ─────► PC da arena
        │  navegador                                          Flask local + SQLite
        │                                                     (fonte da verdade)
        │  (fora do horário — PC desligado)                        │  sync
        │                                                          ▼
        └────────────────────────────────────────────►  arena-sync (Render)
                            navegador                     Flask web + Postgres
                                                          (espelho por arena)
```

- **Link único inteligente** (Fase 5): a funcionária abre UM endereço; ele tenta o PC (Tailscale) e, se off, cai no `arena-sync`.

---

## 2. Serviço `arena-sync` (novo, no Render)
- **Código:** deriva do `pg-systems-app` — reaproveita models e telas do **módulo Aulas** rodando em modo web (o app já roda como web e já fala `DATABASE_URL` Postgres). NÃO inclui Comandas/Ranking.
- **Deploy:** serviço Render próprio (free tier, dorme em 15 min — ótimo pro uso ocasional), Postgres (schema por arena no mesmo Supabase, ou banco próprio). `Procfile` + gunicorn.
- **Multi-inquilino sem reescrever queries:** **um schema Postgres por arena** (`search_path` por conexão). Cada request resolve o schema pela arena do token → o código do Aulas roda **sem alteração de query** (continua "monoarena", só que apontado pro schema certo). Schema criado no 1º provisionamento.

## 3. Provisionamento + Autenticação
- **Provisionar arena:** quando o dono liga "Acesso remoto na nuvem", o PC chama `arena-sync /provision` mandando a **chave de licença**. O `arena-sync` valida a chave **server-to-server** contra o `pg-systems-licencas` (não confia no cliente), cria o **schema** da arena e devolve um **`arena_token`** opaco (pra URL) + um **segredo de sync**.
- **Login da funcionária na nuvem:** os **usuários** da arena (owner/funcionária, com senha) sincronizam pro schema dela. A URL carrega o `arena_token` (`/a/<arena_token>/…`) → o login acontece dentro do conjunto de usuários daquela arena. Permissão só 'aulas' pra ela.
- **Auth de sync (PC ↔ arena-sync):** o PC assina/authentica com o segredo de sync da arena. Sem segredo, não empurra/puxa.

## 4. Dados que sincronizam (só Aulas)
Tabelas: `student`, `class_session`, `enrollments`, `replacement`, `student_history`, `mensalidade`, e `settings` (identidade). **Não** sincroniza Comandas/Ranking/activity_log pesado.

Colunas **aditivas** (via `_auto_migrate`, sem quebrar nada) em cada tabela sincronizável:
- `sync_uid` VARCHAR(36) — UUID estável entre dispositivos (backfill nos registros existentes no 1º boot da 3.19).
- `updated_at` VARCHAR(19) — 'YYYY-MM-DD HH:MM:SS', tocado a cada escrita.
- `deleted_at` VARCHAR(19) NULL — soft-delete (nunca apaga fisicamente o que sincroniza).

> PK autoincrement continua igual. FK entre tabelas é resolvida na importação pelo `sync_uid` do pai → PK local. **Zero migração de PK.**

## 5. Protocolo de sync (passa-bastão + merge por registro)
Cada lado guarda `last_sync_at`.
- **PUSH (PC → arena-sync):** manda registros com `updated_at > last_sync` (incluindo `deleted_at`). Servidor faz **upsert por `sync_uid`**; **mais recente vence**.
- **PULL (arena-sync → PC):** servidor devolve o que mudou desde o `last_sync` do PC. PC faz upsert por `sync_uid`, resolvendo FKs pelo `sync_uid`.
- **Conflito:** `updated_at` mais novo por registro vence; soft-delete respeitado. Como PC-on e PC-off não escrevem juntos, conflito real é raríssimo — o merge é a rede de segurança.
- **Botão "Fulana fez X mudanças desde <data>":** o PULL conta os registros que chegaram do servidor; o dono aplica (ou aplica automático). 

## 6. Link único inteligente (Fase 5)
- Página leve no `arena-sync` (sempre no ar): ao abrir, o **navegador da funcionária tenta alcançar o PC** (fetch no IP Tailscale com timeout curto).
  - PC responde → **redireciona pro PC** (tempo real, sem tocar no Render).
  - PC não responde → **serve o Aulas hospedado** (fallback).
- Ela salva esse 1 link na tela inicial. Nunca precisa escolher.

## 7. Migração dos dados vivos (3.18 → 3.19)
- Só colunas **aditivas** (`sync_uid`, `updated_at`, `deleted_at`) — o `_auto_migrate` já faz isso com segurança.
- Backfill: gerar `sync_uid` e `updated_at` nos registros existentes no 1º boot.
- **Nenhuma mudança de PK.** Risco nos dados em campo: baixo.

## 8. Segurança
- HTTPS do Render, login por arena, isolamento por schema, `arena_token` opaco, segredo de sync por arena, e o caminho direto protegido pela tailnet.
- O `arena-sync` NÃO tem a chave privada Ed25519 (não é licenciamento) — só valida a licença consultando o servidor de licença.

## 9. Riscos & mitigação
- **Bug de sync corromper dados** → backup automático nos dois lados antes de aplicar; arena-piloto primeiro; merge sempre por registro (nunca substitui o banco).
- **Tocar produção** → o `arena-sync` é serviço novo e isolado; o servidor de licença não é alterado.
- **Render dormindo** → aceitável (cold start ~40s no fallback ocasional).

## 10. Ordem de execução (menor risco primeiro)
1. ✅ **Groundwork no app desktop** (FEITO) — `SyncMixin` (sync_uid/updated_at/deleted_at) nos 5 modelos sincronizáveis; eventos carimbam sync_uid+updated_at em toda escrita; `_backfill_sync_fields()` no startup (3 lançadores). Aditivo, testado (colunas criadas, backfill 100%, insert/update carimbam). PK intocado.
2. ✅ **`arena-sync` esqueleto** (FEITO) — projeto próprio em `Documentos/Arena AMP/arena-sync/` (repo separado). Tenancy por arena (SQLite/arena no dev, Postgres schema/arena no prod), `/provision` (valida licença + cria arena + devolve token/segredo), auth por arena (`/a/<token>/login`), stubs de `sync/push|pull` (gate por `X-Sync-Secret`). Testado ponta a ponta. Aulas hospedado ainda é PLACEHOLDER.
3. ✅ **Sync ponta a ponta** — COMPLETO.
   - ✅ **Motor de sync** (`arena-sync/sync_core.py`, arquivo compartilhado) — full-state merge por `sync_uid`, **mais-recente-vence**, soft-delete, FK resolvida por uid (id local de cada lado é independente). Testado: convergência PC↔servidor, conflito, soft-delete, idempotência. Timestamps em **microssegundos** (`updated_at` String(26)) pra não empatar edições no mesmo segundo. `applying()` desliga o carimbo na importação.
   - ✅ **Cliente de sync no desktop** (FEITO) — `sync_core.py` copiado pro app; `User` do desktop ganhou colunas de sync + entrou no `_SYNC_MODELS`/SPECS (usuários sincronizam pro login na nuvem); `applying()` local desliga o carimbo na importação; funções `sync_provision/sync_pull/sync_push/sync_now` (urllib, sem dependência nova; chave via `license_client.current_key()`); endpoint `/api/sync/now` + botão "Sincronizar agora" em Configurações. Testado PONTA A PONTA por HTTP (arena-sync em processo separado): push 17 alunos → funcionária edita na nuvem → pull traz edição + aluno novo → idempotência ✓.
   - ✅ **Aulas portado pro arena-sync** (FEITO) — reaproveita a UI real (index.html/script.js/style.css copiados) e reimplementa o backend em `arena-sync/aulas.py` (helpers de parcela/pagamento/renovação portados do desktop, `student_dict` no MESMO formato, rotas usando `g.db` da arena). Roteamento por sessão: `/a/<token>/` → login → `/aulas` (arena vem da sessão, então o script.js chama `/api/...` sem prefixo). `ActivityLog` local (não sincroniza) pro feed. Soft-delete no delete de aluno. Testado por HTTP: login → criar aluno (6 parcelas/vencido) → pagar parcela → renovar → alerts/activity ✓. Identidade (nome/cor) ainda usa padrão (Settings não sincroniza — melhoria futura).
   - ✅ **Auto-sync** (FEITO) — opt-in `cloud_sync` (padrão OFF; toggle "Sincronizar automaticamente" no card da nuvem). `sync_auto(kind)` best-effort (no-op se desligado/sem URL, NUNCA levanta). `run_desktop._start_auto_sync`: puxa ao abrir (8s, background — cold start do Render sem travar a janela) + a cada 5 min (both) + empurra ao fechar (atexit). Endpoint `/api/sync/auto` (GET/POST; ao ligar já provisiona + 1ª sync). Testado: no-op quando off, empurra quando on, best-effort não quebra com URL ruim ✓.
   - ✅ **Enrollments (M2M)** aluno↔turma (FEITO) — o `TableSpec` ganhou `m2m` genérico; o Student exporta `class_uids` (sync_uids das turmas) e no merge as turmas são reaplicadas por uid. A matrícula "pega carona" no `updated_at` do aluno (bumpado ao matricular/desmatricular nos dois lados). Testado: matrícula com ids de turma diferentes, add/remove, idempotência ✓.
   - ⚠️ **`sync_core.py` está DUPLICADO** (app + arena-sync) — ao mexer, atualizar os DOIS. `arena_sync_url` ainda sem default (setado no deploy, Etapa 5).
4. ✅ **Decisão do link** (Opção A) — a funcionária usa SEMPRE o link do Render; o PC auto-sincroniza (~5 min). Sem "link inteligente" (o probe HTTPS→HTTP do PC é bloqueado por mixed-content). **Tailscale fica pra depois** (o dono decidiu — os horários da funcionária não batem com o PC ligado, então quase não usaria). O "Fulana fez X" já está pronto (contagem em `api_sync_now`). Nada a codar aqui.
5. 🔨 **Deploy** — arena-sync JÁ NO AR e validado em produção (`https://arena-sync-rh5a.onrender.com`, Supabase sa-east-1 via **Session pooler** :5432 — a conexão direta é IPv6 e o Render não alcança). Provisionamento (licença assinada) + push (17 alunos/1 user/75 parcelas) + pull idempotente testados em produção. `apply_state` otimizado (1 query/tabela) pra o push grande não estourar timeout; `_sync_post` timeout 90s. `ARENA_SYNC_URL_DEFAULT` setado no desktop. FALTA: buildar/lançar a 3.19 + piloto (criar usuário da funcionária, ligar o toggle, passar o link). O prep abaixo continua valendo de referência. arena-sync ficou **production-ready**: tenancy em **Postgres schema-por-arena** (branch por `DATABASE_URL`; SQLite só no dev) + **validação de licença por assinatura** (o PC manda o payload Ed25519 já assinado do cache via `license_client.cached_license()`; o arena-sync verifica com a chave pública embutida — sem depender do machine binding; testado com o license.json real: válido passa, adulterado falha). Guia completo em `arena-sync/DEPLOY.md` (Supabase + Render). FALTA (mãos do dono + eu guio): criar Supabase, repo GitHub do arena-sync, web service no Render com envs (SECRET_KEY, DATABASE_URL), pegar a URL → setar `arena_sync_url` default no desktop → buildar 3.19 → piloto.

> Tudo fica atrás do opt-in que já existe (`remote_access` OFF por padrão) — clientes que não usam não são afetados.

## 10.6 Polimento de UX/UI no celular (FEITO — nuvem no ar em 19/08/2026)
Rodada de melhorias focada no acesso pelo celular da funcionária (e alguns fixes de desktop):
- **Login da nuvem redesenhado** (`arena-sync/app.py` `_LOGIN_HTML`) com a identidade da Arena (logo, navy/laranja, Barlow) + **"Manter conectado"** (`session.permanent`, `PERMANENT_SESSION_LIFETIME=30 dias`).
- **`/manifest.json`** (rota nova nos DOIS: desktop `app.py` e `arena-sync/app.py`) + `apple-touch-icon`/meta PWA no `index.html` → **atalho na tela inicial com a logo**. Ícone via `static/logo.png` (público, sem login — o celular busca o ícone sem sessão).
- **Tela de carregamento** (`#app-loading` overlay + spinner, CSS `.app-loading` no `style.css`, `hideAppLoading()` no `finally` do `loadAll()` em `script.js`).
- **X de fechar fixo** no celular (`.modal-header { position:sticky; top:0 }`) — verificado: header é 1º filho do container que rola (`.modal-content overflow:auto`).
- **Gesto "voltar" não desloga** (`trapBackGesture()`: `history.pushState`+`popstate` → fecha modal aberto ou re-prende; nunca sai da página).
- **"Criar usuário não fazia nada" (desktop)** — o handler de `#form-user` foi endurecido: valida usuário/senha com toast, trata resposta não-JSON, mostra erro visível, trava clique duplo. **Nunca mais falha em silêncio.** (Raiz provável: erro lançado no handler morria como promise rejeitada.)
- **Desempenho**: `loadAll()` roda as cargas independentes em paralelo (`Promise.all([fetchAlerts, fetchActivity])`).
- Compartilhados (`index.html`/`style.css`/`script.js`) **recopiados** pro `arena-sync`; `sync_core.py` continua idêntico.
- **Publicado**: `arena-sync` commitado e `git push` → Render redeployou; login novo + manifest validados **ao vivo em produção**. **Desktop 3.19 continua SÓ neste PC** (nunca foi pro git/Release) — quando for testar/publicar o desktop, rebuildar (mantém 3.19 até publicar de verdade; aí sobe o número).

## 10.7 Correções de regra + fuso + atividades + perf (FEITO — 20/08/2026)
- **Parcela no mês de início (bug do Larissa):** `_competencias` NÃO pula mais o 1º
  mês quando o dia de início passou do vencimento. A 1ª parcela é sempre o mês em
  que o aluno começou; o vencimento da 1ª nunca cai antes da data de início.
  Corrigido nos dois lados (`app.py` + `arena-sync/aulas.py`). Migração idempotente
  `_backfill_fix_parcela_alignment()` (flag `parcelas_realinhadas` em Settings)
  realinha as parcelas já existentes preservando quais foram pagas — **rodada no
  banco real** (backup `arena.pre_realign_*.db`; 36 parcelas; Larissa fev, Amanda
  abr, Rodrigo mai, Jhara jun, Lucas Silveira jul). Regra de "quitado" já estava
  certa (só quando todas pagas). Renovação não afetada (começa no dia 1 do mês
  seguinte ao último vencimento).
- **Fuso de Brasília na nuvem:** o Render roda em UTC → horários das ações saíam
  ~3h adiantados no celular. `app.py` do arena-sync seta `TZ=America/Sao_Paulo` +
  `time.tzset()` no boot (guardado p/ Windows dev). Sem DST desde 2019 = UTC-3 fixo.
  Alinha display E os timestamps de sync (o PC já usa hora local BR).
- **Atividades sincronizam (PC↔nuvem):** `ActivityLog` virou modelo sincronizável
  (SyncMixin/SyncCols nos dois; entrou em `_SYNC_MODELS`/`SYNC_MODELS` + `SPECS`).
  Agora o dono vê no "Atividades" o que a funcionária fez pelo celular (com o nome
  dela; a nuvem grava `user` a partir da sessão). Migração de coluna no provision
  (`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` no PG; try/except no SQLite). Testado
  ponta a ponta (nuvem exporta → PC importa; idempotente).
- **Acesso pela rede local / Tailscale REMOVIDO** do PC: sumiu o card "Ativar acesso
  pelo celular" (aba renomeada p/ **Nuvem**, só o card de sincronização); `run_desktop`
  sempre liga em `127.0.0.1` (nada exposto na rede). O acesso pelo celular é 100%
  pela nuvem agora.
- **Performance:** busca com debounce (140ms), rolagem com inércia nos modais
  (`-webkit-overflow-scrolling`), e `todayLocalISO()` no lugar de `toISOString()`
  (que virava "amanhã" depois das 21h). Cargas independentes já em paralelo.

## 11. Onde mora cada coisa (organização das pastas)
- **`Documentos/Arena AMP/Arena AMP - App/`** — app desktop (`pg-systems-app`). Groundwork de sync + opt-in de rede + este plano.
- **`Documentos/Arena AMP/arena-sync/`** — serviço da nuvem (repo NOVO, separado). Ver o `README.md` de lá.
- **`Documentos/Painel Admin/Arena AMP - Servidor (admin)/`** — servidor de licença (`pg-systems-licencas`). **NÃO é tocado** por esta obra; o arena-sync só o CONSULTA pra validar a chave.
