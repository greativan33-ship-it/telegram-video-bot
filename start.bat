@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist .env (
    copy .env.example .env >nul
    echo Создан .env. Вставьте токен из @BotFather в строку BOT_TOKEN= и запустите файл снова.
    notepad .env
    pause
    exit /b 1
)
if not exist .venv\Scripts\python.exe (
    py -3.12 -m venv .venv
    if errorlevel 1 (
        echo Установите Python 3.12. Подробности в README.md.
        pause
        exit /b 1
    )
)
if not exist .venv\dependencies-ready (
    .venv\Scripts\python.exe -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Не удалось установить зависимости. Проверьте интернет и выполните команды из README.md.
        pause
        exit /b 1
    )
    type nul >.venv\dependencies-ready
)
.venv\Scripts\python.exe -m videobot --check
if errorlevel 1 (
    pause
    exit /b 1
)
.venv\Scripts\python.exe -m videobot
pause
