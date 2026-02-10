FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
ENV PORT 4121
# Port pour le serveur de santé dédié
ENV HEALTH_PORT 4122

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl gcc libffi-dev musl-dev nodejs npm && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
COPY entrypoint.sh .
COPY ./collegue/health_server.py ./collegue/health_server.py

RUN chmod +x ./entrypoint.sh

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY ./collegue ./collegue
COPY ./skills ./skills

# Port de l'application principale
EXPOSE ${PORT}
# Port du serveur de santé
EXPOSE ${HEALTH_PORT}

CMD ["./entrypoint.sh"]
