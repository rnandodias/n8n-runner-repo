FROM mcr.microsoft.com/playwright/python:latest
COPY requirements.txt /tmp/req.txt
RUN python -m pip install --no-cache-dir --upgrade pip && python -m pip install --no-cache-dir -r /tmp/req.txt && python -m playwright install chromium
EXPOSE 8000
CMD ["uvicorn","app:app","--host","0.0.0.0","--port","8000","--timeout-keep-alive","1"]
