FROM python:3.12-alpine
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY support_console_app.py .
CMD ["python3", "support_console_app.py"]
