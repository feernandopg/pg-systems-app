@echo off
REM ============================================================
REM  Gera o "Arena AMP.exe" (ecossistema: Hub + Aulas + Ranking)
REM  COMPILADO com Nuitka + MinGW = codigo protegido E PORTAVEL
REM  (roda em qualquer processador x64).
REM
REM  Requisitos: Python 3.12 instalado (o MinGW do Nuitka nao
REM  funciona no 3.13+). Instale com:  winget install Python.Python.3.12
REM ============================================================
setlocal
set VENV=.venv-build312

echo.
echo [1/4] Preparando ambiente (Python 3.12)...
if not exist "%VENV%\" ( py -3.12 -m venv %VENV% )
call %VENV%\Scripts\activate.bat

echo.
echo [2/4] Instalando dependencias...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements-desktop.txt

echo.
echo [3/4] Limpando builds anteriores...
if exist "run_desktop.build\" rmdir /s /q run_desktop.build
if exist "run_desktop.dist\"  rmdir /s /q run_desktop.dist

echo.
echo [4/4] Compilando (Nuitka + MinGW, alguns minutos)...
REM mingwfix contem um header que falta no MinGW (structuredquerycondition.h)
set CPATH=%CD%\mingwfix
python -m nuitka --standalone --mingw64 ^
  --windows-console-mode=disable ^
  --include-data-dir=templates=templates ^
  --include-data-dir=static=static ^
  --include-data-dir=ranking_static=ranking_static ^
  --include-data-dir=comandas_static=comandas_static ^
  --include-data-dir=relatorios_static=relatorios_static ^
  --include-package=certifi ^
  --include-package-data=certifi ^
  --windows-icon-from-ico=static\logo.ico ^
  --output-filename="Arena AMP.exe" ^
  --company-name="Fernando Prestes Godinho" ^
  --product-name="Arena AMP" ^
  --file-version=3.19 ^
  --assume-yes-for-downloads ^
  --remove-output ^
  run_desktop.py

echo.
if exist "run_desktop.dist\Arena AMP.exe" (
  echo ============================================================
  echo  PRONTO! App em:  run_desktop.dist\Arena AMP.exe
  echo  Instalador: abra o installer.iss no Inno Setup ^(F9^).
  echo ============================================================
) else (
  echo ============================================================
  echo  FALHOU! O .exe NAO foi gerado. Veja o erro FATAL acima.
  echo  Nada foi criado em run_desktop.dist. Me mande o erro.
  echo ============================================================
)
pause
