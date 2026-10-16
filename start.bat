@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Планировщик поездок — сервер
where python >nul 2>nul && (python server.py) || (py -3 server.py)
pause
