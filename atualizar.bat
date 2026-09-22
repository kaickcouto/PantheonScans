@echo off
title PantheonScans - Atualizador
chcp 65001 >nul
cd /d "%~dp0"

echo ========================================================
echo          PANTHEON SCANS - ATUALIZANDO SISTEMA
echo ========================================================
echo.

where git >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo [AVISO] Git nao foi encontrado no PATH do Windows.
    echo Baixe a versao mais recente em:
    echo https://github.com/kaickcouto/PantheonScans/archive/refs/heads/main.zip
    echo.
    pause
    exit /b 1
)

echo [*] Puxando ultimas atualizacoes do GitHub...
git pull origin main

if %ERRORLEVEL% EQU 0 (
    echo.
    echo [*] Verificando dependencias...
    where python >nul 2>nul
    if %ERRORLEVEL% EQU 0 (
        python -m pip install -r requirements.txt --quiet
    )
    echo.
    echo ========================================================
    echo   ATUALIZACAO CONCLUIDA COM SUCESSO!
    echo ========================================================
) else (
    echo.
    echo [ERRO] Falha ao atualizar via git pull.
)

echo.
pause
