FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd -u 10001 -m agent
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY uai_agent_kit.py service.py ./
USER agent
ENV PORT=8080
EXPOSE 8080
CMD ["python", "service.py"]
