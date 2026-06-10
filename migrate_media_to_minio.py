#!/usr/bin/env python3
"""
Script de Migração: data/media/ → MinIO (S3-Compatible)

Migra todos os arquivos de mídia existentes na pasta data/media/ para o MinIO,
atualiza os registros no banco de dados com a storage_url, e garante que
todos os arquivos fiquem persistentes no armazenamento de objetos.

Uso:
    python migrate_media_to_minio.py

Variáveis de ambiente necessárias (ou definidas no .env):
    MINIO_ENDPOINT, MINIO_ACCESS_KEY, MINIO_SECRET_KEY, MINIO_BUCKET
"""

import os
import sys
import time
import io
import mimetypes

# Carregar .env se existir
from dotenv import load_dotenv
load_dotenv()

from minio import Minio
from minio.error import S3Error

# ─── Configuração ────────────────────────────────────────────────────────────
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('DATA_DIR', os.path.join(ROOT_DIR, 'data'))
MEDIA_DIR = os.path.join(DATA_DIR, 'media')

MINIO_ENDPOINT = os.getenv('MINIO_ENDPOINT', 'teste-minio.ioms5g.easypanel.host')
MINIO_ACCESS_KEY = os.getenv('MINIO_ACCESS_KEY', '')
MINIO_SECRET_KEY = os.getenv('MINIO_SECRET_KEY', '')
MINIO_BUCKET = os.getenv('MINIO_BUCKET', 'corpal-wp')
MINIO_SECURE = os.getenv('MINIO_SECURE', 'true').lower() == 'true'
MINIO_PUBLIC_URL = f"{'https' if MINIO_SECURE else 'http'}://{MINIO_ENDPOINT}/{MINIO_BUCKET}"

# Conectar ao banco de dados
DATABASE_URL = os.environ.get('DATABASE_URL', f"sqlite:///{os.path.join(DATA_DIR, 'wpcrm.db')}")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)


def main():
    print("=" * 60)
    print("   MIGRACAO DE MIDIA: Disco Local -> MinIO")
    print("=" * 60)
    print()

    # Verificar credenciais
    if not MINIO_ACCESS_KEY or not MINIO_SECRET_KEY:
        print("[ERRO] Credenciais MinIO nao configuradas!")
        print("   Configure MINIO_ACCESS_KEY e MINIO_SECRET_KEY no .env")
        sys.exit(1)

    # Verificar pasta de mídia
    if not os.path.exists(MEDIA_DIR):
        print(f"[AVISO] Pasta de midia nao encontrada: {MEDIA_DIR}")
        print("   Nada para migrar.")
        sys.exit(0)

    # Contar arquivos
    files = [f for f in os.listdir(MEDIA_DIR) if os.path.isfile(os.path.join(MEDIA_DIR, f))]
    if not files:
        print("[AVISO] Nenhum arquivo encontrado na pasta de midia.")
        sys.exit(0)

    total_size = sum(os.path.getsize(os.path.join(MEDIA_DIR, f)) for f in files)
    print(f"[INFO] Encontrados {len(files)} arquivos ({total_size / 1024 / 1024:.1f} MB)")
    print(f"[INFO] Destino: {MINIO_ENDPOINT}/{MINIO_BUCKET}")
    print()

    # Conectar ao MinIO
    print("Conectando ao MinIO...")
    try:
        client = Minio(
            MINIO_ENDPOINT,
            access_key=MINIO_ACCESS_KEY,
            secret_key=MINIO_SECRET_KEY,
            secure=MINIO_SECURE
        )
        if not client.bucket_exists(MINIO_BUCKET):
            client.make_bucket(MINIO_BUCKET)
            print(f"   Bucket '{MINIO_BUCKET}' criado.")
        print(f"   [OK] Conectado!")
    except Exception as e:
        print(f"   [ERRO] Falha ao conectar: {e}")
        sys.exit(1)

    # Conectar ao banco de dados (opcional — atualiza storage_url)
    db_available = False
    try:
        from flask import Flask
        from flask_sqlalchemy import SQLAlchemy
        
        app = Flask(__name__)
        app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
        app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        db = SQLAlchemy(app)
        
        with app.app_context():
            # Verificar se tabela existe
            try:
                result = db.session.execute(db.text("SELECT COUNT(*) FROM media_file")).scalar()
                db_available = True
                print(f"   [OK] Banco de dados conectado ({result} registros em media_file)")
            except Exception:
                print("   [AVISO] Tabela media_file nao encontrada - so fara upload sem atualizar DB")
    except Exception as e:
        print(f"   [AVISO] Banco de dados indisponivel: {e}")

    print()
    print("Iniciando migracao...")
    print("-" * 60)

    uploaded = 0
    skipped = 0
    failed = 0
    start_time = time.time()

    for i, filename in enumerate(files, 1):
        filepath = os.path.join(MEDIA_DIR, filename)
        file_size = os.path.getsize(filepath)

        # Verificar se já existe no MinIO
        try:
            client.stat_object(MINIO_BUCKET, filename)
            # Já existe — atualizar DB se necessário
            if db_available:
                try:
                    with app.app_context():
                        base_name = os.path.splitext(filename)[0]
                        storage_url = f"{MINIO_PUBLIC_URL}/{filename}"
                        db.session.execute(
                            db.text(
                                "UPDATE media_file SET storage_url = :url "
                                "WHERE (filename = :fn OR msg_id = :base OR short_id = :base) "
                                "AND (storage_url IS NULL OR storage_url = '')"
                            ),
                            {'url': storage_url, 'fn': filename, 'base': base_name}
                        )
                        db.session.commit()
                except Exception:
                    pass
            skipped += 1
            progress = f"[{i}/{len(files)}]"
            print(f"  {progress} [PULADO] {filename} (ja existe no MinIO)")
            continue
        except S3Error as e:
            if e.code != 'NoSuchKey':
                print(f"  [ERRO] ao verificar {filename}: {e}")
                failed += 1
                continue

        # Fazer upload
        try:
            # Detectar content-type
            guess_type, _ = mimetypes.guess_type(filename)
            content_type = guess_type or 'application/octet-stream'

            # Detectar por magic bytes para ser mais preciso
            with open(filepath, 'rb') as f:
                header = f.read(12)
            if header[:4] == b'OggS':
                content_type = 'audio/ogg'
            elif header[:4] == b'\x1aE\xdf\xa3':
                content_type = 'audio/webm'
            elif header[:8] == b'\x89PNG\r\n\x1a\n':
                content_type = 'image/png'
            elif header[:2] == b'\xff\xd8':
                content_type = 'image/jpeg'
            elif header[:4] == b'RIFF' and header[8:12] == b'WEBP':
                content_type = 'image/webp'
            elif header[4:8] == b'ftyp':
                content_type = 'video/mp4'
            elif header[:4] == b'%PDF':
                content_type = 'application/pdf'

            with open(filepath, 'rb') as f:
                file_data = f.read()

            client.put_object(
                MINIO_BUCKET,
                filename,
                io.BytesIO(file_data),
                length=len(file_data),
                content_type=content_type
            )

            storage_url = f"{MINIO_PUBLIC_URL}/{filename}"

            # Atualizar banco de dados
            if db_available:
                try:
                    with app.app_context():
                        base_name = os.path.splitext(filename)[0]
                        # Tentar atualizar registro existente
                        rows = db.session.execute(
                            db.text(
                                "UPDATE media_file SET storage_url = :url "
                                "WHERE filename = :fn OR msg_id = :base OR short_id = :base"
                            ),
                            {'url': storage_url, 'fn': filename, 'base': base_name}
                        ).rowcount
                        
                        # Se não existia registro, criar um novo
                        if rows == 0:
                            ext_lower = os.path.splitext(filename)[1].lower()
                            if ext_lower in ('.jpeg', '.jpg', '.png', '.webp', '.gif'):
                                m_type = 'image'
                            elif ext_lower in ('.oga', '.ogg', '.webm', '.opus', '.mp3', '.wav'):
                                m_type = 'audio'
                            elif ext_lower in ('.mp4', '.avi', '.mov', '.mkv', '.3gp'):
                                m_type = 'video'
                            else:
                                m_type = 'document'
                            
                            import datetime, pytz
                            now_iso = datetime.datetime.now(pytz.timezone('America/Sao_Paulo')).isoformat()
                            db.session.execute(
                                db.text(
                                    "INSERT INTO media_file (msg_id, short_id, media_type, mimetype, filename, file_size, storage_url, created_at) "
                                    "VALUES (:msg_id, :short_id, :media_type, :mimetype, :filename, :file_size, :storage_url, :created_at)"
                                ),
                                {
                                    'msg_id': base_name, 'short_id': base_name,
                                    'media_type': m_type, 'mimetype': content_type,
                                    'filename': filename, 'file_size': len(file_data),
                                    'storage_url': storage_url, 'created_at': now_iso
                                }
                            )
                        db.session.commit()
                except Exception as e_db:
                    pass

            uploaded += 1
            size_mb = file_size / 1024 / 1024
            progress = f"[{i}/{len(files)}]"
            print(f"  {progress} [OK] {filename} ({size_mb:.2f} MB, {content_type})")

        except Exception as e:
            failed += 1
            progress = f"[{i}/{len(files)}]"
            print(f"  {progress} [ERRO] {filename}: {e}")

    elapsed = time.time() - start_time
    print()
    print("-" * 60)
    print(f"Migracao concluida em {elapsed:.1f}s")
    print(f"   Uploads: {uploaded}")
    print(f"   Pulados: {skipped}")
    print(f"   Falhas: {failed}")
    print(f"   Total: {uploaded + skipped}/{len(files)}")
    print()
    
    if failed > 0:
        print("[AVISO] Alguns arquivos falharam. Rode o script novamente para tentar de novo.")
    else:
        print("[SUCESSO] Todos os arquivos foram migrados com sucesso!")
    

if __name__ == '__main__':
    main()
