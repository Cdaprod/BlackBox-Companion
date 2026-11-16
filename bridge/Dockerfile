FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt && mkdir -p /var/spool/blackbox
COPY server.py /app/
EXPOSE 8795
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8795", "--proxy-headers"]
