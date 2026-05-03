@echo off
echo Starting Kamil Council Servers...

start "Kamil Analyst [Qwen]" cmd /k "C:\llama.cpp\build\bin\llama-server.exe -m C:\kamil\models\qwen2.5-coder-14b-q4.gguf -c 8192 -ngl 99 --host 0.0.0.0 --port 8080"

start "Kamil Advocate [Mistral]" cmd /k "C:\llama.cpp\build\bin\llama-server.exe -m C:\kamil\models\mistral-7b-instruct-q4.gguf -c 8192 -ngl 99 --host 0.0.0.0 --port 8081"

start "Kamil Synthesizer [Llama3]" cmd /k "C:\llama.cpp\build\bin\llama-server.exe -m C:\kamil\models\llama-3.1-8b-instruct-q4.gguf -c 8192 -ngl 99 --host 0.0.0.0 --port 8082"

echo Waiting for models to load...
timeout /t 35 /nobreak > nul

echo Starting Kamil Web Interface...
start "Kamil Web" cmd /k "cd C:\kamil && venv313\Scripts\activate && python -m uvicorn interfaces.web.app:app --host 0.0.0.0 --port 3000"

echo Done. Open http://localhost:3000