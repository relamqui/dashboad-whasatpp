@echo off
title WP CRM Server

:: Navegar para a pasta do script (funciona com duplo-clique de qualquer lugar)
cd /d "%~dp0"

echo ============================================
echo        WP CRM Server - Iniciando
echo ============================================
echo.

:: Verificar se o Python esta instalado
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERRO] Python nao foi encontrado no PATH.
    echo Instale o Python em https://www.python.org/downloads/
    echo Marque a opcao "Add Python to PATH" durante a instalacao.
    pause
    exit /b 1
)

:: Instalar todas as dependencias do requirements.txt
echo [1/2] Instalando dependencias...
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [AVISO] Houve um problema ao instalar algumas dependencias.
    echo Verifique o requirements.txt e tente novamente.
    pause
    exit /b 1
)

echo.
echo [2/2] Iniciando servidor WP CRM...
echo Acesse: http://localhost:3008
echo Pressione Ctrl+C para encerrar.
echo.
python app.py
pause
