"""
version.py — Fonte ÚNICA da versão do app Arena AMP.

O app reporta APP_VERSION ao license-server em cada validação. Se o painel
de licenças exigir uma versão mínima maior que esta, o app trava na abertura
pedindo a atualização (ver run_desktop.py / license_client.py).

Ao lançar uma nova versão: suba este número, e por organização mantenha o
mesmo valor em installer.iss (#define AppVersion) e build.bat (--file-version)
— mas é ESTE arquivo que decide a lógica de atualização.
"""
APP_VERSION = "3.19"

# Qual produto é este app. A espinha dorsal (licença/pagamento/atualização/
# assinatura) é a mesma pra vários nichos; muda só os módulos e este id.
# Ex.: um futuro sistema de oficina usaria PRODUCT = "oficina".
# O app já reporta isto ao license-server desde agora — quando o servidor virar
# "ciente de produto", cada produto terá seu próprio min_version/download_url e
# a atualização de um NUNCA vaza pro outro, sem precisar reatualizar os clientes.
PRODUCT = "arena"
