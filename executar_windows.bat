@echo off
cd /d %~dp0
if not exist .venv (
  py -m venv .venv
)
call .venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
start http://127.0.0.1:8000
uvicorn backend.app.main:app --host 0.0.0.0 --port 8000
pause
