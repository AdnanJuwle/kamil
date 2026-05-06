@echo off
echo Starting Kamil...

start "Kamil Primary [DeepSeek-R1-14B]" cmd /k "C:\llama.cpp\build\bin\llama-server.exe -m C:\kamil\models\deepseek-r1-distill-14b-q4.gguf -c 8192 -ngl 99 --host 0.0.0.0 --port 8080"

start "Kamil Voice [Gemma4-E4B]" cmd /k "C:\llama.cpp\build\bin\llama-server.exe -m C:\kamil\models\gemma4-e4b-q4.gguf -c 4096 -ngl 99 --host 0.0.0.0 --port 8084"

echo Waiting for models to load...
timeout /t 60 /nobreak > nul

echo Starting Kamil Web Interface...
start "Kamil Web" cmd /k "cd C:\kamil && venv313\Scripts\activate && python run.py"

echo Done. Open http://localhost:3000