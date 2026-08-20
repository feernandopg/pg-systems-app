"""
run_dev.py — Roda o ecossistema em modo DEV para testar no navegador.
Todos os módulos liberados (sem licença), login admin / admin123.
Uso:  .venv-build312\Scripts\python.exe run_dev.py   (ou o run_dev.bat)
"""
import os
import threading
import webbrowser

os.environ['AMP_DEV'] = '1'  # libera todos os módulos no Hub

from app import app, db, User, _auto_migrate, _backfill_mensalidades, _backfill_sync_fields

PORT = 5000


def _seed():
    with app.app_context():
        _auto_migrate()
        db.create_all()
        _backfill_mensalidades()
        _backfill_sync_fields()
        admin = User.query.filter_by(username='admin').first()
        if not admin:
            u = User(username='admin', role='adm', perms='aulas,ranking,comandas')
            u.set_password('admin123')
            db.session.add(u)
            db.session.commit()
        elif not admin.is_adm:
            admin.role = 'adm'
            db.session.commit()
    from app import auto_backup
    auto_backup()


if __name__ == '__main__':
    _seed()
    threading.Timer(1.5, lambda: webbrowser.open(f'http://127.0.0.1:{PORT}/')).start()
    print('\n  Arena AMP (DEV) em  http://127.0.0.1:%d   —  login: admin / admin123\n' % PORT)
    app.run(host='127.0.0.1', port=PORT, debug=False)
