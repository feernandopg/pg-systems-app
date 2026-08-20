; ============================================================
;  Instalador do Arena AMP (Inno Setup)
;  Gera um "Arena AMP Setup.exe" que o cliente instala em qualquer PC.
;
;  Como usar:
;   1. Instale o Inno Setup: https://jrsoftware.org/isdl.php
;   2. Rode o build.bat primeiro (gera dist\Arena AMP\)
;   3. Abra este arquivo no Inno Setup e clique em "Compile" (F9)
;   4. O instalador sai na pasta "Output\"
; ============================================================

#define AppName "Arena AMP"
#define AppVersion "3.19"
#define AppPublisher "Fernando Prestes Godinho"
#define AppExe "Arena AMP.exe"
; Precisa BATER com APP_MUTEX em run_desktop.py — é assim que o instalador
; detecta o app aberto e o fecha antes de sobrescrever os arquivos.
#define AppMutexName "ArenaAMP_Running_Mutex"

[Setup]
AppId={{A7E3C9B1-4F2D-4A6E-9C11-ARENAAMP2024}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
OutputBaseFilename=ArenaAMP-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
; Instala sem exigir admin (fica na pasta do usuario)
PrivilegesRequired=lowest
SetupIconFile=static\logo.ico
; NÃO usar AppMutex/CloseApplications: o app é SEM JANELA (Nuitka), então o Inno
; não consegue fechá-lo e, em modo silencioso, ABORTAVA a instalação logo no
; começo (era a causa raiz do "atualiza mas continua na versão antiga"). Quem
; encerra o app é o [Code] via taskkill, logo antes de copiar os arquivos.
CloseApplications=no
RestartApplications=no

[Languages]
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "desktopicon"; Description: "Criar atalho na Area de Trabalho"; GroupDescription: "Atalhos:"

[Files]
Source: "run_desktop.dist\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
; nowait: não trava o instalador esperando o app. SEM skipifsilent: numa
; atualização silenciosa (/VERYSILENT) o app REABRE sozinho ao terminar.
Filename: "{app}\{#AppExe}"; Description: "Abrir o {#AppName} agora"; Flags: nowait

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
var
  rc: Integer;
begin
  if CurStep = ssInstall then
  begin
    { O app é um processo SEM JANELA (Nuitka, console desabilitado), então o
      CloseApplications do Inno não o alcança e o "Arena AMP.exe" fica travado
      em uso -> a atualização automática não conseguia sobrescrever os arquivos
      e ficava em loop. Aqui a gente encerra o processo à força ANTES de copiar,
      liberando os arquivos. Roda também pra clientes já instalados, porque quem
      faz o kill é ESTE instalador (o novo), não o app antigo. }
    { SEM /T: o instalador é processo-filho do app; /T mataria o próprio
      instalador. /IM sozinho encerra só o "Arena AMP.exe" (não os filhos). }
    Exec(ExpandConstant('{cmd}'), '/C taskkill /F /IM "Arena AMP.exe"',
         '', SW_HIDE, ewWaitUntilTerminated, rc);
    { Fecha a janela ANTIGA do Edge --app (a nossa, --app=http://127.0.0.1) pra
      não ficar uma "Instalando…" sobrando atrás da nova depois de atualizar.
      Filtra pela linha de comando, então NÃO mexe no Edge normal do usuário.
      Roda aqui (no instalador) pra funcionar com qualquer versão do app. }
    Exec('powershell.exe',
         '-NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq ''msedge.exe'' -and $_.CommandLine -like ''*app=http://127.0.0.1*'' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"',
         '', SW_HIDE, ewWaitUntilTerminated, rc);
    Sleep(1500);
  end;
end;
