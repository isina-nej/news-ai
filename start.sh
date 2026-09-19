#!/bin/bash
# NewsAI - Start all services
cd "$(dirname "$0")"
source .venv/bin/activate
set -a && source .env && set +a
export PYTHONPATH=.

echo "Starting NewsAI services..."

# Kill old processes
pkill -f "celery.*config.celery" 2>/dev/null
pkill -f "manage.py runserver" 2>/dev/null
sleep 1

# Start Celery TG worker
nohup celery -A config.celery worker -l info -Q telegram -c 1 > /tmp/newsai-tg.log 2>&1 &
echo "TG worker: $!"

# Start Celery main worker
nohup celery -A config.celery worker -l info -Q celery,intelligence -c 2 > /tmp/newsai-main.log 2>&1 &
echo "Main worker: $!"

# Start Celery beat
nohup celery -A config.celery beat -l info --scheduler django_celery_beat.schedulers:DatabaseScheduler > /tmp/newsai-beat.log 2>&1 &
echo "Beat: $!"

# Start Django server
nohup python manage.py runserver 0.0.0.0:8000 > /tmp/newsai-server.log 2>&1 &
echo "Server: $!"

# Auto-run AI publish after 30 seconds
(sleep 30 && python scripts/publish_ai.py >> /tmp/newsai-publish.log 2>&1) &
echo "AI Publisher scheduled (30s delay)"

sleep 3
echo ""
echo "Services started! Check status:"
echo "  TG worker:    tail -f /tmp/newsai-tg.log"
echo "  Main worker:  tail -f /tmp/newsai-main.log"
echo "  Beat:         tail -f /tmp/newsai-beat.log"
echo "  Server:       tail -f /tmp/newsai-server.log"
echo ""
echo "Stop all: pkill -f 'celery.*config.celery' && pkill -f 'manage.py runserver'"
