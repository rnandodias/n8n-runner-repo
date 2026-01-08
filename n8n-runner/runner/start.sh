#!/bin/bash

# Inicia LibreOffice em modo headless (servidor UNO) em background
echo "🚀 Iniciando LibreOffice headless na porta 2002..."
soffice --headless --accept="socket,host=localhost,port=2002;urp;StarOffice.ServiceManager" &

# Aguarda o LibreOffice iniciar (5 segundos é suficiente)
sleep 5

# Verifica se o LibreOffice está rodando
if pgrep -x "soffice.bin" > /dev/null; then
    echo "✅ LibreOffice iniciado com sucesso!"
else
    echo "⚠️ LibreOffice pode não ter iniciado corretamente, mas continuando..."
fi

# Inicia o FastAPI
echo "🚀 Iniciando FastAPI..."
exec uvicorn app:app --host 0.0.0.0 --port 8000 --timeout-keep-alive 1
