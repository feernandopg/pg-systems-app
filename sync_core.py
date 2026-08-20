"""
sync_core.py — Motor de sincronização (MESMO arquivo nos dois lados: desktop e
arena-sync). Cada lado monta a lista de TableSpec com SEUS modelos e chama
`export_state` / `apply_state`.

Estratégia (proposital pra o volume de uma arena): **full-state merge**.
- Cada lado exporta TODOS os registros sincronizáveis (com sync_uid/updated_at/
  deleted_at). FK vira o `sync_uid` do pai (student_id -> student_uid).
- O outro lado funde por `sync_uid`: **o `updated_at` mais recente vence**;
  `deleted_at` é respeitado (soft-delete). Sem cursor, sem depender de relógios
  sincronizados — o merge por registro é a rede de segurança.
- É idempotente: sincronizar duas vezes não muda nada.

Otimização pra delta (enviar só o que mudou) fica pra depois; full-state é
barato pra dezenas/centenas de linhas e muito menos sujeito a bug.
"""


class TableSpec:
    def __init__(self, name, model, scalars, fks=None, m2m=None, natural_key=None):
        self.name = name              # nome lógico da tabela (bate nos dois lados)
        self.model = model            # a classe ORM DESTE lado
        self.scalars = scalars        # colunas simples que sincronizam
        self.fks = fks or {}          # {coluna_fk_local: nome_tabela_pai}
        # M2M (ex.: matrícula aluno↔turma): {chave_export: (attr_relacao, tabela_alvo)}
        # exporta a lista de sync_uid dos relacionados; a matrícula "pega carona" no
        # updated_at do dono (o aluno), então segue o mesmo mais-recente-vence.
        self.m2m = m2m or {}
        # Coluna com UNIQUE (ex.: 'username'). Se o sync_uid não bater mas a chave
        # natural sim, unifica (adota o uid do peer) em vez de duplicar/violar o
        # unique — cobre o caso do mesmo registro ter uid diferente nos dois lados.
        self.natural_key = natural_key


def _fk_key(fkcol):
    # 'student_id' -> 'student_uid'
    base = fkcol[:-3] if fkcol.endswith('_id') else fkcol
    return base + '_uid'


def export_state(session, specs):
    """Serializa TODO o estado sincronizável. FK vira sync_uid do pai."""
    # mapa local_id -> sync_uid por tabela (pra traduzir FKs)
    id2uid = {}
    for spec in specs:
        m = {}
        for row in session.query(spec.model).all():
            m[getattr(row, 'id', None)] = row.sync_uid
        id2uid[spec.name] = m

    tables = {}
    for spec in specs:
        rows = []
        for row in session.query(spec.model).all():
            d = {
                'sync_uid': row.sync_uid,
                'updated_at': row.updated_at or '',
                'deleted_at': row.deleted_at,
            }
            for c in spec.scalars:
                d[c] = getattr(row, c)
            for fkcol, parent in spec.fks.items():
                pid = getattr(row, fkcol)
                d[_fk_key(fkcol)] = id2uid.get(parent, {}).get(pid)
            for key, (rel_attr, _tbl) in spec.m2m.items():
                d[key] = [getattr(x, 'sync_uid') for x in getattr(row, rel_attr)]
            rows.append(d)
        tables[spec.name] = rows
    return {'tables': tables}


def apply_state(session, specs, payload, applying_ctx):
    """Funde o estado recebido no banco local. `applying_ctx` é o contextmanager
    que desliga o carimbo automático (pra preservar o updated_at que veio do peer).
    Devolve um resumo {tabela: {novos, atualizados, apagados, ignorados}}."""
    tables = (payload or {}).get('tables', {})
    spec_by_name = {s.name: s for s in specs}
    resumo = {}

    obj_maps = {}   # {tabela: {sync_uid: obj}} — resolve FK e M2M sem query por linha
    with applying_ctx():
        for spec in specs:                       # pais antes dos filhos (ordem da lista)
            incoming = tables.get(spec.name, [])
            novos = atualizados = apagados = ignorados = 0
            with session.no_autoflush:
                # 1 query só: carrega TODAS as linhas existentes desta tabela por
                # sync_uid (em vez de um SELECT por registro — crucial no Postgres).
                existing = {r.sync_uid: r for r in session.query(spec.model).all() if r.sync_uid}
                by_nk = ({getattr(r, spec.natural_key): r for r in existing.values()
                          if getattr(r, spec.natural_key, None) is not None}
                         if spec.natural_key else {})
                for d in incoming:
                    uid = d.get('sync_uid')
                    if not uid:
                        continue
                    inc_ts = d.get('updated_at') or ''
                    ex = existing.get(uid)
                    # sync_uid não bate, mas a chave natural sim → unifica (adota o
                    # uid do peer), em vez de inserir duplicata / violar o unique.
                    if ex is None and spec.natural_key:
                        cand = by_nk.get(d.get(spec.natural_key))
                        if cand is not None:
                            ex = cand
                            ex.sync_uid = uid
                            existing[uid] = ex
                    if ex is not None and (ex.updated_at or '') >= inc_ts:
                        ignorados += 1            # o nosso é igual/mais novo
                        continue
                    target = ex
                    if target is None:
                        target = spec.model()
                        target.sync_uid = uid
                        session.add(target)
                        existing[uid] = target
                        novos += 1
                    else:
                        atualizados += 1
                    for c in spec.scalars:
                        setattr(target, c, d.get(c))
                    for fkcol, parent in spec.fks.items():
                        pobj = obj_maps.get(parent, {}).get(d.get(_fk_key(fkcol)))
                        setattr(target, fkcol, getattr(pobj, 'id', None) if pobj else None)
                    # M2M: reaplica os relacionados (resolvidos do mapa dos pais)
                    for key, (rel_attr, tbl) in spec.m2m.items():
                        tmap = obj_maps.get(tbl, {})
                        setattr(target, rel_attr, [tmap[u] for u in (d.get(key) or []) if u in tmap])
                    target.updated_at = inc_ts
                    target.deleted_at = d.get('deleted_at')
                    if d.get('deleted_at'):
                        apagados += 1
            session.flush()                       # grava este nível (dá id) antes dos filhos
            obj_maps[spec.name] = existing        # já com os ids preenchidos
            resumo[spec.name] = {'novos': novos, 'atualizados': atualizados,
                                 'apagados': apagados, 'ignorados': ignorados}
        session.commit()
    return resumo


def count_incoming_changes(session, specs, payload):
    """Quantos registros do payload são NOVOS ou mais recentes que o local
    (pro botão 'Fulana fez X mudanças'). Não escreve nada."""
    tables = (payload or {}).get('tables', {})
    total = 0
    for spec in specs:
        rows = tables.get(spec.name, [])
        if not rows:
            continue
        existing = {r.sync_uid: (r.updated_at or '') for r in session.query(spec.model).all() if r.sync_uid}
        for d in rows:
            uid = d.get('sync_uid')
            if not uid:
                continue
            if uid not in existing or existing[uid] < (d.get('updated_at') or ''):
                total += 1
    return total
