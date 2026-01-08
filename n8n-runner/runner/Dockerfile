FROM mcr.microsoft.com/playwright/python:latest

# Instala FFmpeg + LibreOffice com suporte UNO para Python
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libreoffice \
    libreoffice-writer \
    python3-uno \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Configura variáveis de ambiente para o UNO funcionar
ENV PYTHONPATH="/usr/lib/python3/dist-packages:${PYTHONPATH}"
ENV URE_BOOTSTRAP="file:///usr/lib/libreoffice/program/fundamentalrc"

COPY requirements.txt /tmp/req.txt
RUN python -m pip install --no-cache-dir --upgrade pip && \
    python -m pip install --no-cache-dir -r /tmp/req.txt && \
    python -m playwright install chromium

# Script de inicialização que starta o LibreOffice em modo headless
COPY start.sh /start.sh
RUN chmod +x /start.sh

EXPOSE 8000
CMD ["/start.sh"]