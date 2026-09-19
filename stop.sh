#!/bin/bash
# NewsAI - Stop all services
echo "Stopping NewsAI services..."
pkill -f "celery.*config.celery" 2>/dev/null && echo "Celery stopped" || echo "No celery running"
pkill -f "manage.py runserver" 2>/dev/null && echo "Server stopped" || echo "No server running"
echo "Done."
