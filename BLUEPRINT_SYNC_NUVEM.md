# Blueprint — Acesso remoto pela nuvem + sincronização (padrão reutilizável)

> **Para que serve este arquivo:** é a **receita genérica** do que fizemos na Arena
> AMP, escrita pra ser reaproveitada em **qualquer outro produto** (oficina, clínica,
> etc.). O diário específico da Arena está em `PLANO_ACESSO_REMOTO.md`; aqui é só o
> padrão + as pegadinhas que custaram caro. Peça a mim ("me dá o manual da integração
> na nuvem") e eu sigo este blueprint.

---

## 1. Quando usar este padrão
App **desktop local-first** (dados no PC do cliente, funciona offline) que **também**
precisa ser acessado por outra pessoa **de fora / pelo celular**, às vezes com o PC
principal **desligado**. Não vira SaaS puro (o local continua sendo a fonte); a nuvem
é um **espelho + fallback** de UM módulo. Modelo "passa-bastão": um lado escreve por
vez, os dois se juntam depois.

## 2. As três peças (separação de responsabilidades)
| Peça | Papel | Regra de ouro |
|---|---|---|
| **App desktop** | Roda no PC, fonte principal, empacotado/instalado | Não muda a postura de quem não usa a nuvem (feature **opt-in**, OFF por padrão) |
| **Serviço de nuvem** (repo/serviço SEPARADO) | Espelha 1 módulo + endpoints de sync | Deploy independente; **não** mistura com o app nem com a licença |
| **Servidor de licença** | Licença/pagamento/versão mínima | Só é **consultado**; nunca alterado por esta obra |

Manter em **repositórios separados** (o de nuvem redeploya a cada push; o desktop
não deve disparar isso). Se a duplicação incomodar, monorepo com subpastas +
"Root Directory" no host — mas isso é refactor à parte.

## 3. Motor de sync (o coração) — `sync_core.py`, IDÊNTICO nos dois lados
**Full-state merge, registro-a-registro** (barato e pouco sujeito a bug pro volume de
um cliente; otimização por delta fica pra depois):

- Cada modelo sincronizável ganha 3 colunas:
  - `sync_uid` (UUID, String(36)) — **identidade estável** entre os lados (o `id`
    autoincrement é local de cada lado e não serve).
  - `updated_at` (String(**26**), `'%Y-%m-%d %H:%M:%S.%f'` — **microssegundos**, senão
    edições no mesmo segundo empatam e uma se perde).
  - `deleted_at` (String(26), nullable) — **soft-delete** (nunca apagar de verdade em
    tabela sincronizável, senão o registro "ressuscita" no próximo pull).
- **Merge:** por `sync_uid`, **o `updated_at` mais recente vence**. Idempotente.
- **FK** vira o `sync_uid` do pai (`student_id` → `student_uid`); resolve no destino
  pelo mapa de pais (por isso a ordem do SPECS é **pais antes de filhos**).
- **M2M** (ex. matrícula): exporta a lista de `sync_uid` dos relacionados; a relação
  "pega carona" no `updated_at` do dono (bumpar o dono explicitamente ao mexer só na
  M2M).
- **`natural_key`** (ex. `username`): se o `sync_uid` não bate mas a chave única sim,
  **unifica** (adota o uid do peer) em vez de duplicar/violar o UNIQUE — cobre o caso
  do mesmo registro ter nascido com uid diferente nos dois lados.
- **Carimbo automático** (evento `before_insert`/`before_update`) seta `sync_uid` +
  `updated_at`. Um contexto **`applying()`** DESLIGA o carimbo durante a importação,
  pra preservar o `updated_at` que veio do peer (senão nunca converge).
- **Performance:** no apply, carregar todas as linhas da tabela em **1 query** por
  `sync_uid` (não um SELECT por registro) + `no_autoflush` — crucial no Postgres pra
  não estourar timeout no push grande.

## 4. Multi-tenant (isolamento por cliente)
- **Token** opaco e estável derivado da licença: `sha256(chave + '|<sufixo>|tok')[:16]`
  — o PC calcula o **mesmo** token, sem precisar trocar ids.
- **PROD:** um **schema Postgres por cliente** (`SET search_path TO "<schema>"` por
  sessão; `CREATE SCHEMA IF NOT EXISTS` no provision). **DEV:** um arquivo SQLite por
  cliente. **Os mesmos modelos** rodam nos dois (declarative Base agnóstico de schema).
- Toda rota de dados é por `/a/<token>/…`; dados de clientes diferentes nunca se cruzam.

## 5. Provisionamento + segurança (sem revalidar máquina)
1. O PC manda pra nuvem o **payload de licença JÁ ASSINADO** (assinatura Ed25519 do
   cache local) — não a máquina.
2. A nuvem **verifica a assinatura** com a **chave pública embutida** (a mesma do
   cliente) + confere `active` + a chave bate. Não depende de machine-binding.
3. A nuvem cria o espaço do cliente e devolve um **`sync_secret`** por cliente; as
   chamadas de sync usam header `X-Sync-Secret`.
> A chave **privada** NUNCA sai do servidor de licença. A nuvem só **verifica**.

## 6. Auto-sync no desktop (não travar a UI)
- Opt-in (`cloud_sync` OFF por padrão). Funções `sync_provision/pull/push/now`
  **best-effort** (no-op se desligado/sem URL; **nunca** levantam — não podem quebrar
  abrir/fechar o app).
- Puxar ao abrir (com atraso, em **background** — aguenta cold start do host grátis),
  a cada ~5 min, e empurrar ao fechar (`atexit`). Status por polling pra UI não travar.

## 7. Pegadinhas de deploy que custaram caro (Render + Supabase)
- **Supabase IPv6:** a conexão **direta** (`db.<ref>.supabase.co:5432`) é só IPv6 → o
  Render não alcança. **Use o Session pooler** (`aws-0-…pooler.supabase.com:5432`,
  user `postgres.<ref>`). Senha com `@` vira **`%40`** na URL.
- **Fuso:** host roda em **UTC**. Fixe o fuso no boot (`os.environ['TZ']=...; time.tzset()`,
  guardado p/ Windows) — senão horários de log/ações saem adiantados. (Brasil = UTC-3
  fixo desde 2019.)
- **`create_all` NÃO altera tabela existente:** ao adicionar coluna nova (ex. tornar
  uma tabela sincronizável depois), rode um **auto-migrate** (`ALTER TABLE … ADD COLUMN
  IF NOT EXISTS` no PG; try/except no SQLite) no provision/startup.
- **Timeout no push grande:** bulk-load (item 3) + timeout de cliente generoso (~90s).
- **Mixed-content:** navegador HTTPS não consegue "sondar" um PC em HTTP na rede — por
  isso o acesso remoto virou 100% nuvem (link do host), sem "link inteligente".

## 8. Integração com o auto-update (se o app tiver)
- `min_version` + `download_url` vêm **assinados** do servidor de licença. Publicar a
  release **antes** de subir o `min_version` (senão o cliente tenta atualizar e não
  acha o arquivo).
- Use a URL **estável** `…/releases/latest/download/<Setup>.exe` no `download_url` —
  nunca muda a cada release; aí só se mexe no `min_version`.

## 9. Checklist pra replicar num projeto novo
1. [ ] Adicionar `sync_uid`/`updated_at`/`deleted_at` + carimbo + `applying()` nos
       modelos sincronizáveis; backfill idempotente no startup.
2. [ ] Copiar `sync_core.py` (manter idêntico nos dois lados) e montar os `SPECS`
       (ordem pai→filho; `fks`, `m2m`, `natural_key` conforme o caso).
3. [ ] Criar o serviço de nuvem separado: tenancy por token (schema PG / arquivo
       SQLite), `models.py` espelho, endpoints `/provision` e `/a/<token>/sync/pull|push`.
4. [ ] Provision com verificação de assinatura de licença + `sync_secret`.
5. [ ] Cliente de sync no desktop (opt-in, best-effort, background) + auto-sync.
6. [ ] Deploy (pooler + `%40` + TZ + auto-migrate) e testar push/pull/idempotência.
7. [ ] (se houver) update: `download_url` = `releases/latest/…`, subir `min_version`.
