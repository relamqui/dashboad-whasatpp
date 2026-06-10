#!/bin/sh
echo "==========================================="
echo "   Iniciando Container WP CRM"
echo "==========================================="

echo "1. Rodando migracao de midias para o MinIO..."
python migrate_media_to_minio.py

echo "2. Iniciando servidor (Gunicorn)..."
exec gunicorn --worker-class eventlet -w 1 --bind 0.0.0.0:${PORT:-3008} app:app
