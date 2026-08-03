# Render(및 시놀로지 x86 DS218+) 겸용 — python:3.12-slim (amd64)
FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

# Render는 $PORT 환경변수로 포트를 지정합니다. 없으면 8000(로컬/NAS)으로 실행.
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
