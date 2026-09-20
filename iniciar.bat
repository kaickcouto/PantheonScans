@echo off
title PantheonScans - Servidor
chcp 65001 >nul
cd /d "%~dp0"

echo ========================================================
echo        PANTHEON SCANS - INICIANDO SERVIDOR WEB
echo ========================================================
echo.

where python >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    where py >nul 2>nul
    if %ERRORLEVEL% EQU 0 (
        set PYTHON_CMD=py
    ) else (
        echo [ERRO] Python nao foi encontrado no PATH do Windows!
        echo Por favor instale o Python ou marque a opcao 'Add to PATH'.
        echo.
        pause
        exit /b 1
    )
) else (
    set PYTHON_CMD=python
)

echo [*] Iniciando app.py via %PYTHON_CMD%...
echo [*] O navegador abrira automaticamente em http://localhost:5000
echo.
%PYTHON_CMD% -u app.py

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [!] O servidor encerrou com codigo %ERRORLEVEL%.
    pause
)
