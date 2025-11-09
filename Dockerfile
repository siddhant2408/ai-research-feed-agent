FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ai_feed_agent.py .
ENV PYTHONUNBUFFERED=1
CMD ["python", "ai_feed_agent.py"]
