
import os
import jwt
import uuid
import datetime
import pytz
from urllib.parse import urlparse

def get_now():
    return datetime.datetime.now(pytz.timezone('America/Sao_Paulo'))

def get_now_sp():
    """Retorna datetime naive no horário de São Paulo (sem info de timezone).
    Usado para salvar no banco (PostgreSQL/SQLite) mostrando o horário de SP."""
    return datetime.datetime.now(pytz.timezone('America/Sao_Paulo')).replace(tzinfo=None)

from functools import wraps
from flask import Flask, request, jsonify, send_from_directory
from flask_socketio import SocketIO, emit, join_room
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm.attributes import flag_modified
import requests
from dotenv import load_dotenv
import json
import io
from flask import Response

load_dotenv()

WAHA_API_URL = os.getenv('WAHA_API_URL', 'http://localhost:3000').rstrip('/')
WAHA_API_KEY = os.getenv('WAHA_API_KEY', '')

def get_waha_headers():
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
    if WAHA_API_KEY:
        headers['X-Api-Key'] = WAHA_API_KEY
    return headers

CORPAL_WEBHOOK_URL = 'https://n8n-n8n.ioms5g.easypanel.host/webhook/corpal-metrica'

import logging
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)
CORS(app)

# Auto-detectar modo async: eventlet em produção (Docker/Python 3.11),
# threading em desenvolvimento local (Python 3.12+ onde eventlet falha)
_async_mode = 'threading'
try:
    import eventlet
    eventlet.monkey_patch()
    _async_mode = 'eventlet'
except Exception:
    pass

socketio = SocketIO(app, cors_allowed_origins="*", async_mode=_async_mode)
print(f"[INIT] SocketIO async_mode={_async_mode}")

JWT_SECRET = os.getenv('JWT_SECRET', 'secret')
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
PUBLIC_DIR = os.path.join(ROOT_DIR, 'public')

# Identificador único de inicialização para forçar refresh nos clientes quando o backend for atualizado/reiniciado
SERVER_BOOT_ID = str(uuid.uuid4())

# Configuração do Local de Armazenamento
DATA_DIR = os.environ.get('DATA_DIR', os.path.join(ROOT_DIR, 'data'))
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

# ─── MinIO / S3 — Armazenamento Persistente de Mídia ────────────────────────
from minio import Minio
from minio.error import S3Error

MINIO_ENDPOINT = os.getenv('MINIO_ENDPOINT', 'teste-minio.ioms5g.easypanel.host')
MINIO_ACCESS_KEY = os.getenv('MINIO_ACCESS_KEY', '')
MINIO_SECRET_KEY = os.getenv('MINIO_SECRET_KEY', '')
MINIO_BUCKET = os.getenv('MINIO_BUCKET', 'corpal-wp')
MINIO_SECURE = os.getenv('MINIO_SECURE', 'true').lower() == 'true'
MINIO_PUBLIC_URL = f"{'https' if MINIO_SECURE else 'http'}://{MINIO_ENDPOINT}/{MINIO_BUCKET}"

minio_client = None
if MINIO_ACCESS_KEY and MINIO_SECRET_KEY:
    try:
        minio_client = Minio(
            MINIO_ENDPOINT,
            access_key=MINIO_ACCESS_KEY,
            secret_key=MINIO_SECRET_KEY,
            secure=MINIO_SECURE
        )
        # Garantir que o bucket existe
        if not minio_client.bucket_exists(MINIO_BUCKET):
            minio_client.make_bucket(MINIO_BUCKET)
        print(f"[MinIO] ✅ Conectado: {MINIO_ENDPOINT}/{MINIO_BUCKET}")
    except Exception as e:
        print(f"[MinIO] ❌ ERRO ao conectar: {e} — Fallback para armazenamento local")
        minio_client = None
else:
    print("[MinIO] ⚠️ Credenciais não configuradas — usando armazenamento local")


def upload_to_minio(filename, file_bytes, content_type='application/octet-stream'):
    """Faz upload de um arquivo para o MinIO. Retorna a URL pública ou None."""
    if not minio_client:
        return None
    try:
        minio_client.put_object(
            MINIO_BUCKET,
            filename,
            io.BytesIO(file_bytes),
            length=len(file_bytes),
            content_type=content_type
        )
        public_url = f"{MINIO_PUBLIC_URL}/{filename}"
        print(f"[MinIO] Upload OK: {filename} ({len(file_bytes)} bytes) → {public_url}")
        return public_url
    except Exception as e:
        print(f"[MinIO] Erro upload {filename}: {e}")
        return None


def delete_from_minio(filename):
    """Remove um arquivo do MinIO. Retorna True se removido."""
    if not minio_client:
        return False
    try:
        minio_client.remove_object(MINIO_BUCKET, filename)
        print(f"[MinIO] Deletado: {filename}")
        return True
    except Exception as e:
        print(f"[MinIO] Erro ao deletar {filename}: {e}")
        return False
# ─────────────────────────────────────────────────────────────────────────────

DB_PATH = os.environ.get('DB_PATH', os.path.join(DATA_DIR, 'db.json'))
DATABASE_URL = os.environ.get('DATABASE_URL', f"sqlite:///{os.path.join(DATA_DIR, 'wpcrm.db')}")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    pass

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db_sql = SQLAlchemy(app)

# ─── Modelos do Banco de Dados ──────────────────────────────────────────────

class Filial(db_sql.Model):
    id = db_sql.Column(db_sql.Integer, primary_key=True)
    name = db_sql.Column(db_sql.String(100), nullable=False)
    instance = db_sql.Column(db_sql.String(100), nullable=False)

class Setor(db_sql.Model):
    id = db_sql.Column(db_sql.Integer, primary_key=True)
    name = db_sql.Column(db_sql.String(100), nullable=False)
    filial_id = db_sql.Column(db_sql.Integer, db_sql.ForeignKey('filial.id'), nullable=False)
    filial_name = db_sql.Column(db_sql.String(100), nullable=True)

class User(db_sql.Model):
    id = db_sql.Column(db_sql.Integer, primary_key=True)
    name = db_sql.Column(db_sql.String(100), nullable=False)
    email = db_sql.Column(db_sql.String(120), unique=True, nullable=False)
    phone = db_sql.Column(db_sql.String(30), nullable=True)
    password = db_sql.Column(db_sql.String(200), nullable=False)
    role = db_sql.Column(db_sql.String(20), default='user')
    instances = db_sql.Column(db_sql.JSON, default=[]) # Nomes das instâncias vinculadas
    filial_id = db_sql.Column(db_sql.Integer, db_sql.ForeignKey('filial.id'), nullable=True)
    setor_id = db_sql.Column(db_sql.Integer, db_sql.ForeignKey('setor.id'), nullable=True)
    filial = db_sql.Column(db_sql.String(150), nullable=True)
    setor = db_sql.Column(db_sql.String(150), nullable=True)

class Contact(db_sql.Model):
    id = db_sql.Column(db_sql.String(150), primary_key=True) # c_phone_instance
    name = db_sql.Column(db_sql.String(100), nullable=False)
    phone = db_sql.Column(db_sql.String(30), nullable=False) # No longer unique
    avatar = db_sql.Column(db_sql.Text, nullable=True)
    instance = db_sql.Column(db_sql.String(100), nullable=True)
    tags = db_sql.Column(db_sql.JSON, default=['Novo Lead'])
    last_msg = db_sql.Column(db_sql.Text, nullable=True)
    last_msg_time = db_sql.Column(db_sql.String(20), nullable=True)
    unread = db_sql.Column(db_sql.Integer, default=0)
    assigned_to = db_sql.Column(db_sql.Integer, db_sql.ForeignKey('user.id'), nullable=True)
    assigned_name = db_sql.Column(db_sql.String(100), nullable=True)

class ContactRequest(db_sql.Model):
    id = db_sql.Column(db_sql.Integer, primary_key=True)
    phone = db_sql.Column(db_sql.String(30), nullable=False)
    attendant_name = db_sql.Column(db_sql.String(100), nullable=False)
    filial = db_sql.Column(db_sql.String(150), nullable=True)
    setor = db_sql.Column(db_sql.String(150), nullable=True)
    reason = db_sql.Column(db_sql.Text, nullable=False)
    status = db_sql.Column(db_sql.String(20), default='PENDING') # PENDING, ANSWERED
    created_at = db_sql.Column(db_sql.DateTime, default=datetime.datetime.utcnow)
    is_first_time = db_sql.Column(db_sql.Boolean, default=True)

class Message(db_sql.Model):
    id = db_sql.Column(db_sql.String(100), primary_key=True)
    contact_id = db_sql.Column(db_sql.String(150), db_sql.ForeignKey('contact.id'), nullable=False)
    text = db_sql.Column(db_sql.Text, nullable=False)
    type = db_sql.Column(db_sql.String(20), nullable=False) # 'in' or 'out'
    time = db_sql.Column(db_sql.String(20), nullable=False)
    timestamp = db_sql.Column(db_sql.BigInteger, nullable=False)
    instance = db_sql.Column(db_sql.String(100), nullable=True)
    ack = db_sql.Column(db_sql.Integer, default=0, nullable=True)
    sender_id = db_sql.Column(db_sql.Integer, db_sql.ForeignKey('user.id'), nullable=True)

class Setting(db_sql.Model):
    key = db_sql.Column(db_sql.String(50), primary_key=True)
    value = db_sql.Column(db_sql.Text, nullable=True)

class AtendimentoChat(db_sql.Model):
    __tablename__ = 'atendimentos_chat'
    id = db_sql.Column(db_sql.Integer, primary_key=True, autoincrement=True)
    numero = db_sql.Column(db_sql.Text, nullable=True, unique=True)
    status = db_sql.Column(db_sql.String(50), nullable=True)
    atendente = db_sql.Column(db_sql.String(100), nullable=True)
    registro_time_chat = db_sql.Column(db_sql.Text, nullable=True)
    ultimo_setor = db_sql.Column(db_sql.String(255), nullable=True)
    ultimo_atendente = db_sql.Column(db_sql.String(255), nullable=True)
    # Campos para controle de alertas de tempo de espera
    alerta_20min_enviado = db_sql.Column(db_sql.Boolean, default=False, nullable=False, server_default='false')
    alerta_40min_enviado = db_sql.Column(db_sql.Boolean, default=False, nullable=False, server_default='false')
    # Timestamp ISO de quando o status mudou para 'atendente' (início da espera)
    atendente_desde = db_sql.Column(db_sql.Text, nullable=True)
    # ── Campos de controle do fluxo NPS ──────────────────────────────────────
    # None | 'waiting_vote' | 'waiting_reason' | 'finished'
    nps_status     = db_sql.Column(db_sql.String(30), nullable=True)
    nps_poll_id    = db_sql.Column(db_sql.String(255), nullable=True)
    nps_started_at = db_sql.Column(db_sql.Text, nullable=True)
    nps_voto       = db_sql.Column(db_sql.String(50), nullable=True)

class SlaHistory(db_sql.Model):
    __tablename__ = 'sla_history'
    id = db_sql.Column(db_sql.Integer, primary_key=True, autoincrement=True)
    numero = db_sql.Column(db_sql.String(50), nullable=False)
    filial = db_sql.Column(db_sql.String(100), nullable=True)
    setor = db_sql.Column(db_sql.String(100), nullable=True)
    atendente = db_sql.Column(db_sql.String(100), nullable=True)
    tempo_na_fila_segundos = db_sql.Column(db_sql.Integer, nullable=True)
    tempo_primeira_resposta_segundos = db_sql.Column(db_sql.Integer, nullable=True)
    soma_tempo_resposta_segundos = db_sql.Column(db_sql.Integer, default=0)
    qtd_respostas_atendente = db_sql.Column(db_sql.Integer, default=0)
    ultimo_horario_mensagem_cliente = db_sql.Column(db_sql.Text, nullable=True)
    entrou_na_fila_em = db_sql.Column(db_sql.Text, nullable=True)
    assumido_em = db_sql.Column(db_sql.Text, nullable=True)
    finalizado_em = db_sql.Column(db_sql.Text, nullable=True)
    criado_em = db_sql.Column(db_sql.Text, nullable=False)

class TempoEspera(db_sql.Model):
    __tablename__ = 'tempo_espera'
    id = db_sql.Column(db_sql.Integer, primary_key=True, autoincrement=True)
    numero_cliente = db_sql.Column(db_sql.String(50), nullable=False)
    nome_atendente = db_sql.Column(db_sql.String(100), nullable=True)
    setor_filial = db_sql.Column(db_sql.String(150), nullable=True)
    inicio = db_sql.Column(db_sql.DateTime, nullable=False, default=get_now_sp)
    atendido = db_sql.Column(db_sql.DateTime, nullable=True)
    finalizado = db_sql.Column(db_sql.DateTime, nullable=True)

class MediaFile(db_sql.Model):
    __tablename__ = 'media_file'
    id = db_sql.Column(db_sql.Integer, primary_key=True, autoincrement=True)
    msg_id = db_sql.Column(db_sql.String(200), nullable=False, index=True)
    short_id = db_sql.Column(db_sql.String(200), nullable=True, index=True)
    contact_id = db_sql.Column(db_sql.String(150), nullable=True)
    instance = db_sql.Column(db_sql.String(100), nullable=True)
    media_type = db_sql.Column(db_sql.String(20), nullable=False)  # image, audio, video, document
    mimetype = db_sql.Column(db_sql.String(100), nullable=True)
    filename = db_sql.Column(db_sql.String(300), nullable=False)
    file_size = db_sql.Column(db_sql.Integer, nullable=True)
    original_filename = db_sql.Column(db_sql.String(300), nullable=True)
    storage_url = db_sql.Column(db_sql.Text, nullable=True)  # URL pública no MinIO
    created_at = db_sql.Column(db_sql.Text, nullable=False)

class NpsVoto(db_sql.Model):
    """Histórico de votos e motivos da pesquisa de satisfação NPS."""
    __tablename__ = 'nps_votos'
    id             = db_sql.Column(db_sql.Integer, primary_key=True, autoincrement=True)
    numero_cliente = db_sql.Column(db_sql.Text, nullable=True)
    atendente      = db_sql.Column(db_sql.String(100), nullable=True)
    filial         = db_sql.Column(db_sql.String(100), nullable=True)
    setor          = db_sql.Column(db_sql.String(100), nullable=True)
    voto           = db_sql.Column(db_sql.String(50), nullable=True)
    motivo         = db_sql.Column(db_sql.Text, nullable=True)
    data_voto      = db_sql.Column(db_sql.Text, nullable=True)

# ─── Utils ──────────────────────────────────────────────────────────────────
def normalize_phone(raw: str) -> str:
    """Remove sufixos do WhatsApp (@lid, @c.us, @s.whatsapp.net) e o 9 extra de celulares BR."""
    import re
    phone = str(raw or '').replace('@s.whatsapp.net', '').replace('@c.us', '').replace('@lid', '')
    phone = ''.join(filter(str.isdigit, phone))
    phone = re.sub(r'^(\d{4})9(\d{8})$', r'\1\2', phone)
    return phone

def normalize_br_phone(phone_str):
    if not phone_str: return ""
    p = str(phone_str)
    p = p.split('@')[0]
    p = "".join(filter(str.isdigit, p))
    if len(p) == 13 and p.startswith('55') and p[4] == '9':
        p = p[:4] + p[5:]
    return p

def extract_waha_msg_id(res_data, fallback):
    m_id = res_data.get('id')
    if isinstance(m_id, dict):
        m_id = m_id.get('id')
    return m_id or res_data.get('key', {}).get('id') or res_data.get('messageId') or fallback

def get_media_base64(instance, msg_data):
    """Busca mídia do MinIO, disco local ou WAHA. Retorna base64 se conseguir."""
    try:
        msg_id = msg_data.get('id')
        if not msg_id: return None
        import base64, glob
        short_id = msg_id.split('_')[-1] if '_' in msg_id else msg_id
        
        # 1. Verificar se existe no MinIO (via banco de dados)
        try:
            mf = MediaFile.query.filter(
                (MediaFile.msg_id == msg_id) | (MediaFile.short_id == short_id)
            ).first()
            if mf and mf.storage_url:
                dl = requests.get(mf.storage_url, timeout=15)
                if dl.status_code == 200:
                    return base64.b64encode(dl.content).decode('utf-8')
        except Exception:
            pass
        
        # 2. Verificar se existe localmente no disco
        media_dir = os.path.join(DATA_DIR, 'media')
        for check_id in [msg_id, short_id]:
            check_path = os.path.join(media_dir, check_id)
            matches = glob.glob(check_path + '.*')
            if os.path.exists(check_path):
                matches.insert(0, check_path)
            if matches:
                with open(matches[0], 'rb') as f:
                    return base64.b64encode(f.read()).decode('utf-8')
        
        # 3. Buscar do WAHA e salvar (MinIO + local)
        session_name = instance if instance else 'corpal'
        url = f"{WAHA_API_URL}/api/files"
        res = requests.get(url, headers=get_waha_headers(), params={'session': session_name, 'messageId': msg_id}, timeout=15)
        if res.status_code == 404 and short_id != msg_id:
            res = requests.get(url, headers=get_waha_headers(), params={'session': session_name, 'messageId': short_id}, timeout=15)
            
        if res.status_code == 200:
            file_bytes = res.content
            ctype = res.headers.get('Content-Type', '')
            
            if 'application/json' in ctype:
                import re
                json_data = res.json()
                real_mimetype = json_data.get('mimetype', ctype)
                if 'data' in json_data:
                    raw = json_data['data']
                    raw = re.sub(r'[^A-Za-z0-9+/]', '', raw)
                    raw += "=" * ((4 - len(raw) % 4) % 4)
                    file_bytes = base64.b64decode(raw)
                    ctype = real_mimetype
                elif 'url' in json_data:
                    real_url = json_data['url']
                    if real_url.startswith('http://localhost') or real_url.startswith('http://127.0.0.1'):
                        from urllib.parse import urlparse
                        real_url = f"{WAHA_API_URL}{urlparse(real_url).path}"
                    real_res = requests.get(real_url, headers=get_waha_headers(), timeout=15)
                    if real_res.status_code == 200:
                        file_bytes = real_res.content
                        ctype = real_res.headers.get('Content-Type', '') or real_mimetype
            
            m_type = 'document'
            if ctype.startswith('image/'): m_type = 'image'
            elif ctype.startswith('audio/'): m_type = 'audio'
            elif ctype.startswith('video/'): m_type = 'video'
            
            save_media_file(msg_id, file_bytes, m_type, instance=session_name, mimetype=ctype)
            return base64.b64encode(file_bytes).decode('utf-8')
    except Exception as e:
        print(f"Erro ao baixar midia base64 (WAHA): {e}")
    return None

def save_media_file(msg_id, file_bytes, media_type, instance=None, contact_id=None, mimetype=None, original_filename=None):
    """Salva arquivo de mídia no MinIO (prioridade) e disco local (fallback).
    Registra na tabela media_file com storage_url do MinIO.
    Retorna tupla (filename, minio_url) ou (None, None) se falhar."""
    if not msg_id or not file_bytes:
        return None, None
    try:
        media_dir = os.path.join(DATA_DIR, 'media')
        os.makedirs(media_dir, exist_ok=True)
        short_id = msg_id.split('_')[-1] if '_' in msg_id else msg_id

        # Determinar extensão baseada no tipo e mimetype
        ext = ''
        if media_type == 'image':
            if mimetype and 'png' in mimetype: ext = '.png'
            elif mimetype and 'webp' in mimetype: ext = '.webp'
            elif mimetype and 'gif' in mimetype: ext = '.gif'
            else: ext = '.jpeg'
        elif media_type in ('audio', 'voice', 'ptt'):
            if mimetype and 'ogg' in mimetype: ext = '.oga'
            elif mimetype and 'webm' in mimetype: ext = '.webm'
            elif mimetype and 'mpeg' in mimetype: ext = '.mp3'
            else: ext = '.oga'
        elif media_type == 'video':
            ext = '.mp4'
        elif media_type == 'document':
            if original_filename:
                _, doc_ext = os.path.splitext(original_filename)
                if doc_ext: ext = doc_ext
            if not ext and mimetype:
                import mimetypes as _mt_save
                guessed = _mt_save.guess_extension(mimetype.split(';')[0].strip())
                ext = guessed if guessed else '.bin'
            if not ext: ext = '.bin'
        else:
            ext = '.bin'

        filename = f"{short_id}{ext}"
        filepath = os.path.join(media_dir, filename)
        
        # Determinar content-type para upload
        upload_ct = mimetype.split(';')[0].strip() if mimetype else 'application/octet-stream'

        # 1. Upload para MinIO (prioridade — persistência entre deploys)
        minio_url = upload_to_minio(filename, file_bytes, content_type=upload_ct)

        # 2. Salvar arquivo no disco local (fallback + cache)
        try:
            with open(filepath, 'wb') as f:
                f.write(file_bytes)
            # Cópia com ID completo para o proxy encontrar por ambos
            if short_id != msg_id:
                try:
                    with open(os.path.join(media_dir, f"{msg_id}{ext}"), 'wb') as f:
                        f.write(file_bytes)
                except Exception:
                    pass
        except Exception as e_local:
            if not minio_url:
                print(f"[MediaFile] ERRO: Falha ao salvar local E no MinIO: {e_local}")
                return None, None
            print(f"[MediaFile] Aviso: Falha no disco local, mas salvo no MinIO: {e_local}")

        # 3. Registrar na tabela media_file (com storage_url do MinIO)
        try:
            existing = MediaFile.query.filter(
                (MediaFile.msg_id == msg_id) | (MediaFile.short_id == short_id)
            ).first()
            if existing:
                # Atualizar storage_url se não tinha antes
                if minio_url and not existing.storage_url:
                    existing.storage_url = minio_url
                    db_sql.session.commit()
            else:
                mf = MediaFile(
                    msg_id=msg_id,
                    short_id=short_id if short_id != msg_id else None,
                    contact_id=contact_id,
                    instance=instance,
                    media_type=media_type,
                    mimetype=mimetype,
                    filename=filename,
                    file_size=len(file_bytes),
                    original_filename=original_filename,
                    storage_url=minio_url,
                    created_at=get_now().isoformat()
                )
                db_sql.session.add(mf)
                db_sql.session.commit()
        except Exception as e_db:
            db_sql.session.rollback()
            print(f"[MediaFile] Erro ao registrar no banco: {e_db}")

        storage_label = 'MinIO' if minio_url else 'Local'
        print(f"[MediaFile] Salvo [{storage_label}]: {filename} ({media_type}, {len(file_bytes)} bytes)")
        return filename, minio_url
    except Exception as e:
        print(f"[MediaFile] Erro ao salvar: {e}")
        return None, None

def track_sla_event(numero, filial=None, setor=None, atendente=None, event_type='QUEUE_ENTER'):
    """
    event_type: 'QUEUE_ENTER', 'ASSIGNED', 'CLIENT_MSG', 'ATTENDANT_MSG', 'RELEASED'
    """
    try:
        now_iso = get_now().isoformat()
        
        # Busca SLA atual em aberto (sem finalizado_em)
        sla = SlaHistory.query.filter_by(numero=numero).filter(SlaHistory.finalizado_em == None).order_by(SlaHistory.id.desc()).first()
        
        if event_type == 'QUEUE_ENTER':
            # Se já houver um, vamos fechar para abrir um novo ciclo na fila
            if sla:
                sla.finalizado_em = now_iso
            # Cria novo
            sla = SlaHistory(
                numero=numero, filial=filial, setor=setor,
                entrou_na_fila_em=now_iso, criado_em=now_iso
            )
            db_sql.session.add(sla)
            
        elif sla:
            if event_type == 'ASSIGNED':
                sla.atendente = atendente
                sla.assumido_em = now_iso
                if sla.entrou_na_fila_em:
                    dt_fila = datetime.datetime.fromisoformat(sla.entrou_na_fila_em)
                    sla.tempo_na_fila_segundos = int((get_now() - dt_fila).total_seconds())
                    
            elif event_type == 'CLIENT_MSG':
                # Só registra se já foi assumido por alguém
                if sla.assumido_em:
                    sla.ultimo_horario_mensagem_cliente = now_iso
                    
            elif event_type == 'ATTENDANT_MSG':
                # Primeira resposta
                if not sla.tempo_primeira_resposta_segundos and sla.assumido_em:
                    dt_ass = datetime.datetime.fromisoformat(sla.assumido_em)
                    sla.tempo_primeira_resposta_segundos = int((get_now() - dt_ass).total_seconds())
                    
                # Respostas subsequentes
                if sla.ultimo_horario_mensagem_cliente:
                    dt_cliente = datetime.datetime.fromisoformat(sla.ultimo_horario_mensagem_cliente)
                    diff = int((get_now() - dt_cliente).total_seconds())
                    if diff < 0: diff = 0
                    sla.soma_tempo_resposta_segundos = (sla.soma_tempo_resposta_segundos or 0) + diff
                    sla.qtd_respostas_atendente = (sla.qtd_respostas_atendente or 0) + 1
                    # Limpa o último horário para não contar de novo a mesma msg
                    sla.ultimo_horario_mensagem_cliente = None
                    
            elif event_type == 'RELEASED':
                sla.finalizado_em = now_iso
                
        db_sql.session.commit()
    except Exception as e:
        db_sql.session.rollback()
        print(f"Erro em track_sla_event: {e}")

# ─── Database JSON Fallback / Migration ──────────────────────────────────────
def load_db():
    target_path = DB_PATH
    if not os.path.exists(target_path):
        legacy_path = os.path.join(ROOT_DIR, 'db.json')
        if os.path.exists(legacy_path):
            target_path = legacy_path
        else:
            return {"users": [], "instances": {}, "contacts": [], "messages": {}}
    try:
        with open(target_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {"users": [], "instances": {}, "contacts": [], "messages": {}}


def migrate_to_sql():
    with app.app_context():
        db_sql.create_all()
        
        # Add finalizado column to tempo_espera (TIMESTAMP funciona no SQLite e PostgreSQL)
        try:
            db_sql.session.execute(db_sql.text('ALTER TABLE tempo_espera ADD COLUMN finalizado TIMESTAMP'))
            db_sql.session.commit()
        except Exception:
            db_sql.session.rollback()
            
        # Add new columns if missing
        try:
            db_sql.session.execute(db_sql.text('ALTER TABLE "user" ADD COLUMN filial_id INTEGER REFERENCES filial(id)'))
            db_sql.session.execute(db_sql.text('ALTER TABLE "user" ADD COLUMN setor_id INTEGER REFERENCES setor(id)'))
            db_sql.session.commit()
        except Exception:
            db_sql.session.rollback()
            
        try:
            db_sql.session.execute(db_sql.text('ALTER TABLE "user" ADD COLUMN filial VARCHAR(150)'))
            db_sql.session.execute(db_sql.text('ALTER TABLE "user" ADD COLUMN setor VARCHAR(150)'))
            db_sql.session.commit()
        except Exception:
            db_sql.session.rollback()
            
        try:
            db_sql.session.execute(db_sql.text('ALTER TABLE "user" ADD COLUMN phone VARCHAR(30)'))
            db_sql.session.commit()
        except Exception:
            db_sql.session.rollback()
        
        # Add assignment columns to contact
        try:
            db_sql.session.execute(db_sql.text('ALTER TABLE contact ADD COLUMN assigned_to INTEGER REFERENCES "user"(id)'))
            db_sql.session.execute(db_sql.text('ALTER TABLE contact ADD COLUMN assigned_name VARCHAR(100)'))
            db_sql.session.commit()
        except Exception:
            db_sql.session.rollback()
        
        # Se Users estiver vazio, tenta migrar do JSON
        
        try:
            db_sql.session.execute(db_sql.text('ALTER TABLE contact ALTER COLUMN last_msg_time TYPE VARCHAR(20)'))
            db_sql.session.commit()
        except Exception:
            db_sql.session.rollback()
            
        try:
            db_sql.session.execute(db_sql.text('ALTER TABLE message ALTER COLUMN time TYPE VARCHAR(20)'))
            db_sql.session.commit()
        except Exception:
            db_sql.session.rollback()
            
        try:
            db_sql.session.execute(db_sql.text('ALTER TABLE message ADD COLUMN ack INTEGER DEFAULT 0'))
            db_sql.session.commit()
        except Exception:
            db_sql.session.rollback()
            
        try:
            db_sql.session.execute(db_sql.text('ALTER TABLE message ADD COLUMN sender_id INTEGER REFERENCES "user"(id)'))
            db_sql.session.commit()
        except Exception:
            db_sql.session.rollback()

        # -- Backfill de sender_id nas mensagens antigas --
        try:
            updated_count = db_sql.session.execute(db_sql.text('''
                UPDATE message 
                SET sender_id = (SELECT assigned_to FROM contact WHERE contact.id = message.contact_id) 
                WHERE type = 'out' AND sender_id IS NULL 
                AND contact_id IN (SELECT id FROM contact WHERE assigned_to IS NOT NULL)
            ''')).rowcount
            db_sql.session.commit()
            if updated_count and updated_count > 0:
                print(f"[Migration] Backfill concluído! {updated_count} mensagens antigas foram vinculadas aos atendentes.")
        except Exception as e_backfill:
            db_sql.session.rollback()
            print(f"[Migration] Erro no backfill de mensagens: {e_backfill}")

        # --- Tabela media_file ---
        try:
            db_sql.session.execute(db_sql.text('''
                CREATE TABLE IF NOT EXISTS media_file (
                    id SERIAL PRIMARY KEY,
                    msg_id VARCHAR(200) NOT NULL,
                    short_id VARCHAR(200),
                    contact_id VARCHAR(150),
                    instance VARCHAR(100),
                    media_type VARCHAR(20) NOT NULL,
                    mimetype VARCHAR(100),
                    filename VARCHAR(300) NOT NULL,
                    file_size INTEGER,
                    original_filename VARCHAR(300),
                    created_at TEXT NOT NULL
                )
            '''))
            db_sql.session.execute(db_sql.text('CREATE INDEX IF NOT EXISTS ix_media_file_msg_id ON media_file (msg_id)'))
            db_sql.session.execute(db_sql.text('CREATE INDEX IF NOT EXISTS ix_media_file_short_id ON media_file (short_id)'))
            db_sql.session.commit()
        except Exception:
            db_sql.session.rollback()

        # Migração: adicionar coluna storage_url para URLs do MinIO
        try:
            db_sql.session.execute(db_sql.text('ALTER TABLE media_file ADD COLUMN storage_url TEXT'))
            db_sql.session.commit()
            print("[MIGRATE] Coluna storage_url adicionada à tabela media_file")
        except Exception:
            db_sql.session.rollback()  # Coluna já existe

        # Popular tabela media_file com arquivos já existentes em data/media/
        try:
            media_dir = os.path.join(DATA_DIR, 'media')
            if os.path.exists(media_dir):
                existing_count = db_sql.session.execute(db_sql.text('SELECT COUNT(*) FROM media_file')).scalar()
                if existing_count == 0:
                    import mimetypes as _mt_scan
                    scanned = 0
                    for entry in os.scandir(media_dir):
                        if not entry.is_file(): continue
                        name = entry.name
                        base, ext_s = os.path.splitext(name)
                        ext_lower = ext_s.lower()
                        if ext_lower in ('.jpeg', '.jpg', '.png', '.webp', '.gif', '.bmp'): m_type = 'image'
                        elif ext_lower in ('.oga', '.ogg', '.webm', '.opus', '.mp3', '.wav'): m_type = 'audio'
                        elif ext_lower in ('.mp4', '.avi', '.mov', '.mkv', '.3gp'): m_type = 'video'
                        else: m_type = 'document'
                        guess_mime, _ = _mt_scan.guess_type(name)
                        db_sql.session.execute(db_sql.text(
                            'INSERT INTO media_file (msg_id, short_id, media_type, mimetype, filename, file_size, created_at) '
                            'VALUES (:msg_id, :short_id, :media_type, :mimetype, :filename, :file_size, :created_at)'
                        ), {'msg_id': base, 'short_id': base, 'media_type': m_type, 'mimetype': guess_mime,
                            'filename': name, 'file_size': entry.stat().st_size, 'created_at': get_now().isoformat()})
                        scanned += 1
                    db_sql.session.commit()
                    if scanned > 0:
                        print(f"[Migration] {scanned} arquivos de mídia registrados na tabela media_file")
        except Exception as e_media_scan:
            db_sql.session.rollback()
            print(f"[Migration] Erro ao popular media_file: {e_media_scan}")

        if User.query.first() is None:
            print("Migrando dados do JSON para o SQL...")
            old_db = load_db()
            
            # Migrar Usuários
            for u in old_db.get('users', []):
                new_u = User(id=u['id'], name=u['name'], email=u['email'], 
                             password=u.get('password', '123456'), role=u.get('role', 'user'),
                             instances=old_db.get('userInstances', {}).get(str(u['id']), []))
                db_sql.session.add(new_u)
            
            # Migrar Contatos
            for c in old_db.get('contacts', []):
                new_c = Contact(id=c['id'], name=c['name'], phone=c['phone'], 
                                avatar=c.get('avatar'), instance=c.get('instance'),
                                tags=c.get('tags', []), last_msg=c.get('lastMsg'),
                                last_msg_time=c.get('time'), unread=c.get('unread', 0))
                db_sql.session.add(new_c)
            
            # Migrar Mensagens
            for phone, msgs in old_db.get('messages', {}).items():
                for m in msgs:
                    # Tenta descobrir a instancia (nao vai ser perfeito pra msgs velhas sem instance)
                    inst = m.get('instance', 'default')
                    cid = f"c_{phone}_{inst}"
                    if not Message.query.get(m['id']):
                        new_m = Message(id=m['id'], contact_id=cid, text=m['text'],
                                       type=m['type'], time=m['time'], 
                                       timestamp=m.get('timestamp', 0), instance=inst)
                        db_sql.session.add(new_m)
            
            # Migrar Settings
            settings = old_db.get('settings', {})
            for k, v in settings.items():
                new_s = Setting(key=k, value=str(v))
                db_sql.session.add(new_s)
            
            db_sql.session.commit()
            print("Migração concluída.")

        # Migração dos campos de alerta de tempo de espera (compatível com SQLite e PostgreSQL)
        for col_def in [
            "ALTER TABLE atendimentos_chat ADD COLUMN IF NOT EXISTS alerta_20min_enviado BOOLEAN DEFAULT FALSE",
            "ALTER TABLE atendimentos_chat ADD COLUMN IF NOT EXISTS alerta_40min_enviado BOOLEAN DEFAULT FALSE",
            "ALTER TABLE atendimentos_chat ADD COLUMN IF NOT EXISTS atendente_desde TEXT",
        ]:
            try:
                db_sql.session.execute(db_sql.text(col_def))
                db_sql.session.commit()
                print(f"[MIGRATE] Coluna adicionada/verificada: {col_def.split('IF NOT EXISTS ')[1].split(' ')[0]}")
            except Exception as e_col:
                db_sql.session.rollback()
                # SQLite não suporta IF NOT EXISTS, tenta sem ele
                try:
                    col_def_sqlite = col_def.replace('IF NOT EXISTS ', '').replace('DEFAULT FALSE', 'DEFAULT 0')
                    db_sql.session.execute(db_sql.text(col_def_sqlite))
                    db_sql.session.commit()
                    print(f"[MIGRATE] Coluna adicionada (SQLite fallback): {col_def_sqlite.split('ADD COLUMN ')[1].split(' ')[0]}")
                except Exception:
                    db_sql.session.rollback()  # Coluna já existe ou outro erro ignorado
        
        # Garantir que existe pelo menos um ADMIN se o banco estiver vazio
        if User.query.filter_by(role='admin').first() is None:
            print("Criando usuário administrador padrão...")
            admin_email = os.getenv('ADMIN_EMAIL', 'admin@admin.com')
            admin_pass = os.getenv('ADMIN_PASSWORD', 'admin123')
            admin = User(
                name="Administrador",
                email=admin_email,
                password=admin_pass,
                role="admin",
                instances=[]
            )
            db_sql.session.add(admin)
            db_sql.session.commit()
            print(f"Usuário {admin_email} criado (senha: {admin_pass}).")

migrate_to_sql()

# ─── Middleware ─────────────────────────────────────────────────────────────

@app.before_request
def log_request_info():
    if not request.path.startswith('/static') and request.path != '/api/whatsapp/instances':
        print(f"Solicitação: {request.method} {request.path}")

def auth_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization')
        if not token:
            return jsonify({'error': 'Não autorizado - Sem token'}), 401
        try:
            token = token.split(" ")[1]
            data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
            request.user = data
        except Exception as e:
            return jsonify({'error': 'Token inválido ou expirado', 'details': str(e)}), 401
        
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.user.get('role') != 'admin':
            return jsonify({'error': 'Acesso negado. Apenas administradores.'}), 403
        return f(*args, **kwargs)
    return decorated

def admin_or_gestor_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        role = request.user.get('role')
        if role not in ('admin', 'gestor'):
            return jsonify({'error': 'Acesso negado. Apenas administradores ou gestores.'}), 403
        return f(*args, **kwargs)
    return decorated

@app.route('/api/health')
def health():
    return jsonify({
        'status': 'ok',
        'port': 3008,
        'waha_url': WAHA_API_URL
    })

@app.route('/api/bot/tags', methods=['POST'])
def add_bot_tag():
    """Rota para o N8N (ou outro bot) adicionar etiquetas via API"""
    data = request.json
    if not data:
        return jsonify({'error': 'Body vazio'}), 400
        
    phone_raw = data.get('phone')
    inst = data.get('instance')
    filial = data.get('filial')
    setor = data.get('setor')
    custom_tag = data.get('tag')
    
    if not phone_raw or not inst:
        return jsonify({'error': 'phone e instance são obrigatórios'}), 400
        
    if custom_tag and ':' in custom_tag and not (filial and setor):
        partes = custom_tag.split(':', 1)
        filial = partes[0].strip()
        setor = partes[1].strip()

    # ── Normalização e log do número recebido ──────────────────────────────────
    phone = normalize_br_phone(str(phone_raw).strip())
    inst = str(inst).strip()
    contact_id = f"c_{phone}_{inst}"
    print(f"[BOT/TAGS] phone recebido='{phone_raw}' → normalizado='{phone}', instance='{inst}'")
    print(f"[BOT/TAGS] Buscando contato: id={contact_id}")

    # ── Gerar variante do número (com/sem dígito 9 extra BR) ──────────────────
    # O webhook pode ter gravado o número com 13 dígitos (5511912345678)
    # enquanto o bot envia com 12 (551112345678) ou vice-versa.
    phone_variant = None
    digits_only = "".join(filter(str.isdigit, str(phone_raw).strip().split('@')[0]))
    if len(digits_only) == 13 and digits_only.startswith('55') and digits_only[4] == '9':
        # Número completo com 9 → gerar variante sem o 9
        phone_variant = digits_only[:4] + digits_only[5:]  # remove o 9
    elif len(digits_only) == 12 and digits_only.startswith('55'):
        # Número sem 9 → gerar variante com o 9
        phone_variant = digits_only[:4] + '9' + digits_only[4:]

    # ── Tentativa 1: busca pelo ID exato ──────────────────────────────────────
    contact = Contact.query.filter_by(id=contact_id).first()
    
    # ── Tentativa 2: busca pelo phone normalizado + instance ─────────────────
    if not contact:
        contact = Contact.query.filter_by(phone=phone, instance=inst).first()
        if contact:
            print(f"[BOT/TAGS] Encontrado por phone+instance: {contact.id}")
    
    # ── Tentativa 3: busca apenas pelo phone normalizado ─────────────────────
    if not contact:
        contact = Contact.query.filter_by(phone=phone).first()
        if contact:
            print(f"[BOT/TAGS] Encontrado apenas por phone: {contact.id} (instance no banco: {contact.instance})")

    # ── Tentativa 4: busca pela variante do número (9 extra BR) ──────────────
    if not contact and phone_variant:
        contact = Contact.query.filter_by(phone=phone_variant, instance=inst).first()
        if not contact:
            contact = Contact.query.filter_by(phone=phone_variant).first()
        if contact:
            print(f"[BOT/TAGS] Encontrado pela variante do número: {contact.id} (phone_variant={phone_variant})")

    # ── Tentativa 5: busca pelos últimos 8 dígitos (fallback tolerante) ───────
    if not contact and len(phone) >= 8:
        suffix = phone[-8:]
        all_contacts = Contact.query.filter(
            Contact.instance == inst
        ).all()
        for c in all_contacts:
            if c.phone and c.phone.endswith(suffix):
                contact = c
                print(f"[BOT/TAGS] Encontrado por sufixo '{suffix}': {contact.id} (phone no banco: {contact.phone})")
                break

    if not contact:
        print(f"[BOT/TAGS] Contato não encontrado, criando novo: {contact_id}")
        try:
            contact = Contact(
                id=contact_id, name=phone, phone=phone,
                avatar=phone[0] if phone else "?", instance=inst,
                tags=['Novo Lead'], last_msg='', last_msg_time='', unread=0
            )
            db_sql.session.add(contact)
            db_sql.session.flush()
        except Exception as e_create:
            db_sql.session.rollback()
            print(f"[BOT/TAGS] Erro ao criar contato: {e_create}")
            return jsonify({'error': f'Falha ao criar contato: {str(e_create)}'}), 500

    print(f"[BOT/TAGS] Contato resolvido: id={contact.id}, phone_banco={contact.phone}")
        
    current_tags = list(contact.tags or [])
    added = False
    
    if filial and setor:
        new_ftag = f"{filial}:{setor}"
        # Verifica se já existe alguma tag de filial:setor
        existing_filial_tags = [
            t for t in current_tags
            if isinstance(t, str) and ':' in t and not t.lower().startswith('atendente:')
        ]
        
        if new_ftag in current_tags:
            # Já tem a tag correta — não precisa alterar
            print(f"[BOT/TAGS] Tag '{new_ftag}' já existe, nenhuma alteração necessária.")
        else:
            # Remove qualquer tag Filial:Setor antiga e adiciona a nova
            for old_tag in existing_filial_tags:
                current_tags.remove(old_tag)
                print(f"[BOT/TAGS] Removendo tag antiga: {old_tag}")
            current_tags.append(new_ftag)
            added = True
            
            # Registra no SLA que o chat entrou na fila deste setor
            track_sla_event(phone, filial=filial, setor=setor, event_type='QUEUE_ENTER')
    elif filial:
        if filial not in current_tags:
            current_tags.append(filial)
            added = True
            
    if custom_tag and custom_tag not in current_tags:
        current_tags.append(custom_tag)
        added = True

    # ── Persistir tags se houve alteração ────────────────────────────────────
    try:
        if added:
            contact.tags = current_tags
            flag_modified(contact, 'tags')
            db_sql.session.commit()
            print(f"[BOT/TAGS] Tags salvas com sucesso para '{contact.id}': {contact.tags}")
        else:
            print(f"[BOT/TAGS] Nenhuma tag nova (filial={filial}, setor={setor}, tag={custom_tag}). Tags atuais: {current_tags}")

        # ── Emitir socket SEMPRE — garante resync mesmo em retentativas do bot ─
        _inst_room = contact.instance or 'unknown'
        _current_tags_list = list(contact.tags or [])
        socketio.emit('chat_tags_updated', {
            'id': contact.id,
            'tags': _current_tags_list
        }, room=f'instance_{_inst_room}')
        socketio.emit('chat_tags_updated', {
            'id': contact.id,
            'tags': _current_tags_list
        }, room='admin')
        print(f"[BOT/TAGS] Socket emitido para room=instance_{_inst_room} e room=admin")

    except Exception as e_tags:
        db_sql.session.rollback()
        print(f"[BOT/TAGS] ERRO ao salvar tags: {e_tags}")
        return jsonify({'error': f'Falha ao salvar tags: {str(e_tags)}'}), 500

    # ── Monitoramento de tempo de espera ─────────────────────────────────────
    try:
        espera_aberta = TempoEspera.query.filter_by(numero_cliente=phone, atendido=None).first()
        if not espera_aberta:
            nova_espera = TempoEspera(numero_cliente=phone, inicio=get_now_sp())
            db_sql.session.add(nova_espera)
            db_sql.session.commit()
            print(f"[TEMPO_ESPERA] Inicio registrado para {phone}")
    except Exception as e_te:
        db_sql.session.rollback()
        print(f"[TEMPO_ESPERA] Erro ao registrar inicio: {e_te}")
        
    return jsonify({'success': True, 'contact_id': contact.id, 'tags': list(contact.tags or []), 'phone_normalizado': phone}), 200

# ─── Webhooks WAHA API ────────────────────────────────────────────────────────────

# ─── Auth Routes ────────────────────────────────────────────────────────────

@app.route('/api/auth/login', methods=['POST'])
def login():
    data = request.json
    email = data.get('email')
    password = data.get('password')
    
    user = User.query.filter_by(email=email, password=password).first()
    if user:
        token = jwt.encode({
            'id': user.id,
            'email': user.email,
            'role': user.role,
            'filial_id': user.filial_id,
            'setor_id': user.setor_id,
            'exp': datetime.datetime.utcnow() + datetime.timedelta(days=1)
        }, JWT_SECRET, algorithm="HS256")
        
        # Resolve filial/setor names for the user response
        filial_name = user.filial
        setor_name = user.setor
        if not filial_name and user.filial_id:
            f_obj = Filial.query.get(user.filial_id)
            if f_obj: filial_name = f_obj.name
        if not setor_name and user.setor_id:
            s_obj = Setor.query.get(user.setor_id)
            if s_obj: setor_name = s_obj.name
        
        return jsonify({
            'token': token if isinstance(token, str) else token.decode('utf-8'),
            'user': {
                'id': user.id,
                'name': user.name,
                'email': user.email,
                'role': user.role,
                'filial_id': user.filial_id,
                'setor_id': user.setor_id,
                'filial': filial_name,
                'setor': setor_name,
                'instances': user.instances or []
            }
        })
    return jsonify({'error': 'Credenciais inválidas'}), 401

@app.route('/api/admin/users', methods=['GET'])
@auth_required
@admin_required
def list_users():
    users = User.query.filter(User.role != 'admin').all()
    users_list = []
    for u in users:
        users_list.append({
            'id': u.id,
            'name': u.name,
            'email': u.email,
            'phone': u.phone,
            'role': u.role,
            'instances': u.instances or [],
            'filial_id': u.filial_id,
            'setor_id': u.setor_id,
            'filial': u.filial,
            'setor': u.setor
        })
    return jsonify(users_list)

@app.route('/api/admin/users', methods=['POST'])
@auth_required
@admin_required
def create_user():
    data = request.json
    f_id = data.get('filial_id')
    s_id = data.get('setor_id')
    if not f_id or not s_id:
        return jsonify({'error': 'Filial e Setor são obrigatórios'}), 400

    if User.query.filter_by(email=data['email']).first():
        return jsonify({'error': 'E-mail já cadastrado'}), 400
        
    filial_name = None
    setor_name = None
    f_obj = Filial.query.get(f_id)
    if f_obj: filial_name = f_obj.name
    s_obj = Setor.query.get(s_id)
    if s_obj: setor_name = s_obj.name
    # Auto-preencher instances quando for gestor, baseado na filial
    auto_instances = []
    role = data.get('role', 'user') if data.get('role') in ('user', 'gestor') else 'user'
    if role == 'gestor' and f_obj and f_obj.instance:
        auto_instances = [f_obj.instance]
    
    new_user = User(
        name=data.get('name'),
        email=data.get('email'),
        phone=data.get('phone'),
        password=data.get('password'),
        role=role,
        instances=auto_instances,
        filial_id=f_id,
        setor_id=s_id,
        filial=data.get('filial') or filial_name,
        setor=data.get('setor') or setor_name
    )
    db_sql.session.add(new_user)
    db_sql.session.commit()
    
    return jsonify({
        'id': new_user.id,
        'name': new_user.name,
        'email': new_user.email,
        'role': new_user.role
    }), 201

@app.route('/api/admin/users/<int:user_id>', methods=['PUT', 'DELETE'])
@auth_required
@admin_required
def manage_user(user_id):
    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'Usuário não encontrado'}), 404
    
    if request.method == 'PUT':
        data = request.json
        if not data.get('filial_id') or not data.get('setor_id'):
            return jsonify({'error': 'Filial e Setor são obrigatórios'}), 400

        user.name = data.get('name', user.name)
        user.email = data.get('email', user.email)
        if 'phone' in data:
            user.phone = data.get('phone')
        if data.get('password'):
            user.password = data['password']
            
        f_id = data.get('filial_id')
        s_id = data.get('setor_id')
        if f_id:
            user.filial_id = f_id
            f_obj = Filial.query.get(f_id)
            if f_obj: user.filial = f_obj.name
        if s_id:
            user.setor_id = s_id
            s_obj = Setor.query.get(s_id)
            if s_obj: user.setor = s_obj.name
            
        if data.get('filial'):
            user.filial = data.get('filial')
        if data.get('setor'):
            user.setor = data.get('setor')
        
        # Permite atualizar role (apenas user ou gestor, nunca admin)
        new_role = data.get('role')
        if new_role in ('user', 'gestor'):
            user.role = new_role
            
        db_sql.session.commit()
        return jsonify({
            'id': user.id,
            'name': user.name,
            'email': user.email,
            'phone': user.phone,
            'role': user.role
        })
    
    if request.method == 'DELETE':
        if user.role == 'admin':
            return jsonify({'error': 'Não permitido excluir admin'}), 403
        db_sql.session.delete(user)
        db_sql.session.commit()
        return jsonify({'success': True})

@app.route('/api/admin/link-user-instance', methods=['POST'])
@auth_required
@admin_required
def link_instance():
    data = request.json
    user_id = data['userId']
    inst_name = data['instanceName']
    action = data['action']
    
    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'Usuário não encontrado'}), 404

    instances = list(user.instances or [])
    if action == 'add':
        if inst_name not in instances:
            instances.append(inst_name)
    else:
        if inst_name in instances:
            instances.remove(inst_name)
            
    user.instances = instances
    db_sql.session.commit()
    return jsonify({'success': True, 'instances': user.instances})

# ─── Gestor / Filial / Setor Routes ─────────────────────────────────────────

def get_gestor_allowed_instances(user):
    """Resolve as instâncias permitidas de um gestor.
    Prioridade: user.instances > derivado do filial_id.
    Retorna um set de nomes de instância."""
    # Se já tem instâncias explicitamente atribuídas, usar elas
    if user.instances and len(user.instances) > 0:
        return set(user.instances)
    # Senão, derivar da filial vinculada ao gestor
    if user.filial_id:
        filial = Filial.query.get(user.filial_id)
        if filial and filial.instance:
            return {filial.instance}
    return set()

@app.route('/api/admin/filiais', methods=['GET', 'POST'])
@auth_required
def manage_filiais():
    user = User.query.get(request.user['id'])
    if request.method == 'POST':
        if user.role not in ('admin', 'gestor'):
            return jsonify({'error': 'Acesso negado. Apenas administradores ou gestores.'}), 403
            
        data = request.json
        name = data.get('name')
        instance = data.get('instance')
        
        if not name or not instance:
            return jsonify({'error': 'Nome e Instância são obrigatórios'}), 400
        if user.role == 'gestor' and instance not in get_gestor_allowed_instances(user):
            return jsonify({'error': 'Você não tem permissão para gerenciar esta instância.'}), 403

        nova_filial = Filial(name=name, instance=instance)
        db_sql.session.add(nova_filial)
        db_sql.session.commit()
        return jsonify({'id': nova_filial.id, 'name': nova_filial.name, 'instance': nova_filial.instance}), 201

    if user.role == 'user':
        # Se for para transferência, permite ver todas as filiais
        if request.args.get('action') == 'transfer':
            filiais = Filial.query.all()
            return jsonify([{'id': f.id, 'name': f.name, 'instance': f.instance} for f in filiais])
            
        if user.filial_id:
            filial = Filial.query.get(user.filial_id)
            if filial:
                return jsonify([{'id': filial.id, 'name': filial.name, 'instance': filial.instance}])
        return jsonify([])
        
    elif user.role == 'gestor':
        # Se for para transferência, permite ver todas as filiais
        if request.args.get('action') == 'transfer':
            filiais = Filial.query.all()
            return jsonify([{'id': f.id, 'name': f.name, 'instance': f.instance} for f in filiais])
            
        allowed_instances = get_gestor_allowed_instances(user)
        print(f"[GESTOR FILIAIS] user={user.id} allowed_instances={allowed_instances}")
        
        from sqlalchemy import or_
        filters = []
        if allowed_instances:
            filters.append(Filial.instance.in_(list(allowed_instances)))
        if user.filial_id:
            filters.append(Filial.id == user.filial_id)
            
        if filters:
            filiais = Filial.query.filter(or_(*filters)).all()
        else:
            filiais = []
    else:
        filiais = Filial.query.all()
        
    return jsonify([{'id': f.id, 'name': f.name, 'instance': f.instance} for f in filiais])

@app.route('/api/admin/filiais/<int:filial_id>', methods=['PUT', 'DELETE'])
@auth_required
@admin_or_gestor_required
def manage_filial_single(filial_id):
    filial = Filial.query.get(filial_id)
    if not filial:
        return jsonify({'error': 'Filial não encontrada'}), 404

    user = User.query.get(request.user['id'])
    # Gestor só pode gerenciar filiais das suas instâncias
    if user.role == 'gestor' and filial.instance not in get_gestor_allowed_instances(user):
        return jsonify({'error': 'Sem permissão para esta filial.'}), 403

    if request.method == 'PUT':
        data = request.json
        name = data.get('name', filial.name)
        instance = data.get('instance', filial.instance)
        if user.role == 'gestor' and instance not in get_gestor_allowed_instances(user):
            return jsonify({'error': 'Sem permissão para esta instância.'}), 403
            
        if name != filial.name:
            User.query.filter_by(filial_id=filial_id).update({'filial': name})
            
        filial.name = name
        filial.instance = instance
        db_sql.session.commit()
        return jsonify({'id': filial.id, 'name': filial.name, 'instance': filial.instance})

    if request.method == 'DELETE':
        # Remover referências nos usuários
        User.query.filter_by(filial_id=filial_id).update({'filial_id': None, 'filial': None})
        # Remover setores vinculados e suas referências nos usuários
        setores = Setor.query.filter_by(filial_id=filial_id).all()
        for s in setores:
            User.query.filter_by(setor_id=s.id).update({'setor_id': None, 'setor': None})
            db_sql.session.delete(s)
            
        db_sql.session.delete(filial)
        db_sql.session.commit()
        return jsonify({'success': True})

@app.route('/api/admin/setores', methods=['GET', 'POST'])
@auth_required
def manage_setores():
    user = User.query.get(request.user['id'])
    if request.method == 'POST':
        if user.role not in ('admin', 'gestor'):
            return jsonify({'error': 'Acesso negado. Apenas administradores ou gestores.'}), 403
            
        data = request.json
        name = data.get('name')
        filial_id = data.get('filial_id')
        
        if not name or not filial_id:
            return jsonify({'error': 'Nome e Filial são obrigatórios'}), 400

        filial = Filial.query.get(filial_id)
        if not filial:
            return jsonify({'error': 'Filial não encontrada'}), 400
        if user.role == 'gestor' and filial.instance not in (user.instances or []):
            return jsonify({'error': 'Sem permissão para esta filial.'}), 403

        novo_setor = Setor(name=name, filial_id=filial_id, filial_name=filial.name)
        db_sql.session.add(novo_setor)
        db_sql.session.commit()
        return jsonify({'id': novo_setor.id, 'name': novo_setor.name, 'filial_id': novo_setor.filial_id, 'filial_name': novo_setor.filial_name}), 201

    if user.role == 'user':
        # Se for para transferência, permite ver todos os setores
        if request.args.get('action') == 'transfer':
            setores = Setor.query.all()
            return jsonify([{'id': s.id, 'name': s.name, 'filial_id': s.filial_id, 'filial_name': s.filial_name} for s in setores])
            
        if user.filial_id:
            setores = Setor.query.filter_by(filial_id=user.filial_id).all()
            return jsonify([{'id': s.id, 'name': s.name, 'filial_id': s.filial_id, 'filial_name': s.filial_name} for s in setores])
        return jsonify([])
        
    elif user.role == 'gestor':
        # Se for para transferência, permite ver todos os setores
        if request.args.get('action') == 'transfer':
            setores = Setor.query.all()
            return jsonify([{'id': s.id, 'name': s.name, 'filial_id': s.filial_id, 'filial_name': s.filial_name} for s in setores])
            
        allowed_instances = get_gestor_allowed_instances(user)
        print(f"[GESTOR SETORES] user={user.id} allowed_instances={allowed_instances}")
        
        allowed_f_ids = set()
        if user.filial_id:
            allowed_f_ids.add(user.filial_id)
            
        if allowed_instances:
            allowed_filiais = Filial.query.filter(Filial.instance.in_(list(allowed_instances))).all()
            for f in allowed_filiais:
                allowed_f_ids.add(f.id)
                
        if not allowed_f_ids:
            setores = []
        else:
            setores = Setor.query.filter(Setor.filial_id.in_(list(allowed_f_ids))).all()
    else:
        setores = Setor.query.all()

    return jsonify([{'id': s.id, 'name': s.name, 'filial_id': s.filial_id, 'filial_name': s.filial_name} for s in setores])

@app.route('/api/admin/setores/<int:setor_id>', methods=['PUT', 'DELETE'])
@auth_required
@admin_or_gestor_required
def manage_setor_single(setor_id):
    setor = Setor.query.get(setor_id)
    if not setor:
        return jsonify({'error': 'Setor não encontrado'}), 404

    user = User.query.get(request.user['id'])
    filial = Filial.query.get(setor.filial_id)
    # Gestor só pode gerenciar setores das suas instâncias
    if user.role == 'gestor' and filial and filial.instance not in get_gestor_allowed_instances(user):
        return jsonify({'error': 'Sem permissão para este setor.'}), 403

    if request.method == 'PUT':
        data = request.json
        new_name = data.get('name', setor.name)
        new_filial_id = data.get('filial_id', setor.filial_id)
        
        if new_filial_id:
            new_filial = Filial.query.get(new_filial_id)
            if user.role == 'gestor' and new_filial and new_filial.instance not in get_gestor_allowed_instances(user):
                return jsonify({'error': 'Sem permissão para esta filial.'}), 403
            setor.filial_id = new_filial_id
            setor.filial_name = new_filial.name if new_filial else setor.filial_name
            
        if new_name != setor.name:
            User.query.filter_by(setor_id=setor_id).update({'setor': new_name})
            setor.name = new_name
            
        db_sql.session.commit()
        return jsonify({'id': setor.id, 'name': setor.name, 'filial_id': setor.filial_id, 'filial_name': setor.filial_name})

    if request.method == 'DELETE':
        # Remover referências nos usuários
        User.query.filter_by(setor_id=setor_id).update({'setor_id': None, 'setor': None})
        db_sql.session.delete(setor)
        db_sql.session.commit()
        return jsonify({'success': True})

@app.route('/api/gestor/users', methods=['GET', 'POST'])
@auth_required
@admin_or_gestor_required
def gestor_manage_users():
    user_req = User.query.get(request.user['id'])
    allowed_instances = get_gestor_allowed_instances(user_req) if user_req.role == 'gestor' else None

    if request.method == 'POST':
        data = request.json
        f_id = data.get('filial_id')
        s_id = data.get('setor_id')
        if not f_id or not s_id:
            return jsonify({'error': 'Filial e Setor são obrigatórios'}), 400

        email = data.get('email')
        instances_to_assign = set(data.get('instances', []))

        if allowed_instances is not None:
            if not instances_to_assign:
                instances_to_assign = allowed_instances
            elif not instances_to_assign.issubset(allowed_instances):
                return jsonify({'error': 'Você só pode criar usuários para suas próprias instâncias. Deve selecionar pelo menos uma.'}), 403
        
        if User.query.filter_by(email=email).first():
            return jsonify({'error': 'E-mail já cadastrado'}), 400

        filial_name = None
        setor_name = None
        f_obj = Filial.query.get(f_id)
        if f_obj: filial_name = f_obj.name
        s_obj = Setor.query.get(s_id)
        if s_obj: setor_name = s_obj.name

        novo_usr = User(
            name=data.get('name'),
            email=email,
            phone=data.get('phone'),
            password=data.get('password', '123456'),
            role='user',
            instances=list(instances_to_assign),
            filial_id=f_id,
            setor_id=s_id,
            filial=data.get('filial') or filial_name,
            setor=data.get('setor') or setor_name
        )
        db_sql.session.add(novo_usr)
        db_sql.session.commit()

        return jsonify({'id': novo_usr.id, 'name': novo_usr.name, 'email': novo_usr.email}), 201

    # GET
    all_users = User.query.filter(User.role == 'user').all()
    if allowed_instances is not None:
        visible_users = []
        for u in all_users:
            has_instance = set(u.instances or []).intersection(allowed_instances)
            is_same_filial = (u.filial_id == user_req.filial_id) if user_req.filial_id else False
            if has_instance or is_same_filial:
                visible_users.append(u)
    else:
        visible_users = all_users

    users_list = []
    for u in visible_users:
        users_list.append({
            'id': u.id,
            'name': u.name,
            'email': u.email,
            'phone': u.phone,
            'instances': u.instances or [],
            'filial_id': u.filial_id,
            'setor_id': u.setor_id,
            'filial': u.filial,
            'setor': u.setor
        })
    return jsonify(users_list)

@app.route('/api/gestor/users/<int:user_id>', methods=['PUT', 'DELETE'])
@auth_required
@admin_or_gestor_required
def gestor_update_user(user_id):
    target_user = User.query.get(user_id)
    if not target_user or target_user.role != 'user':
        return jsonify({'error': 'Usuário não encontrado ou não permitido'}), 404

    user_req = User.query.get(request.user['id'])
    allowed_instances = get_gestor_allowed_instances(user_req) if user_req.role == 'gestor' else None
    
    if allowed_instances is not None:
        has_instance = bool(set(target_user.instances or []).intersection(allowed_instances))
        is_same_filial = (target_user.filial_id == user_req.filial_id) if user_req.filial_id else False
        if not (has_instance or is_same_filial):
             return jsonify({'error': 'Você não tem permissão sobre este usuário'}), 403

    if request.method == 'PUT':
        data = request.json
        if not data.get('filial_id') or not data.get('setor_id'):
            return jsonify({'error': 'Filial e Setor são obrigatórios'}), 400

        target_user.name = data.get('name', target_user.name)
        if 'phone' in data:
            target_user.phone = data.get('phone')
        if data.get('password'):
            target_user.password = data['password']
        if 'instances' in data and (allowed_instances is None or set(data['instances']).issubset(allowed_instances)):
            target_user.instances = list(data['instances'])
            
        f_id = data.get('filial_id')
        s_id = data.get('setor_id')
        if f_id:
            target_user.filial_id = f_id
            f_obj = Filial.query.get(f_id)
            if f_obj: target_user.filial = f_obj.name
        if s_id:
            target_user.setor_id = s_id
            s_obj = Setor.query.get(s_id)
            if s_obj: target_user.setor = s_obj.name
            
        if data.get('filial'):
            target_user.filial = data.get('filial')
        if data.get('setor'):
            target_user.setor = data.get('setor')
            
        db_sql.session.commit()
        return jsonify({'success': True})

    if request.method == 'DELETE':
        db_sql.session.delete(target_user)
        db_sql.session.commit()
        return jsonify({'success': True})

@app.route('/api/whatsapp/instances', methods=['GET'])
@auth_required
def get_instances():
    try:
        url = f"{WAHA_API_URL}/api/sessions/?all=true"
        response = requests.get(url, headers=get_waha_headers())
        all_inst = response.json()
        
        if request.user.get('role') != 'admin':
            user = User.query.get(request.user['id'])
            allowed = get_gestor_allowed_instances(user)
            all_inst = [i for i in all_inst if (i.get('name') or i.get('instanceName')) in allowed]
            
        return jsonify(all_inst)
    except Exception as e:
        print(f"Erro ao buscar instâncias: {str(e)}")
        return jsonify({'error': f"Erro na WAHA API: {str(e)}"}), 500

def auto_assign_chat_to_sender(contact, user_data):
    if not user_data: return
    user_email = user_data.get('email', '')
    contact.assigned_to = user_data.get('id')
    contact.assigned_name = user_email
    
    tags = list(contact.tags or [])
    tags = [t for t in tags if t != 'BOT']
    at_tag = f"Atendente: {user_email}"
    if at_tag not in tags:
        tags.append(at_tag)
        
    f_id, s_id = user_data.get('filial_id'), user_data.get('setor_id')
    if f_id and s_id:
        _f = Filial.query.get(f_id)
        _s = Setor.query.get(s_id)
        if _f and _s:
            fs_tag = f"{_f.name}:{_s.name}"
            if fs_tag not in tags:
                tags.append(fs_tag)
                
    contact.tags = tags
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(contact, 'tags')
    
    # Atualiza tabela atendimentos_chat
    agora_iso = get_now().isoformat()
    atend = AtendimentoChat.query.filter_by(numero=contact.phone).first()
    if atend:
        atend.atendente = user_email
        atend.status = 'atendente'
        atend.ultimo_atendente = user_email
        atend.registro_time_chat = agora_iso
        atend.atendente_desde = agora_iso
    else:
        atend = AtendimentoChat(numero=contact.phone, status='atendente', atendente=user_email, ultimo_atendente=user_email, registro_time_chat=agora_iso, atendente_desde=agora_iso)
        db_sql.session.add(atend)

    # Dispara evento socket para atualizar interface
    _inst_room = contact.instance or 'unknown'
    socketio.emit('chat_assignment', {
        'contact_id': contact.id,
        'assigned_to': user_data.get('id'),
        'assigned_name': user_email,
        'tags': tags
    }, room=f'instance_{_inst_room}')
    socketio.emit('chat_assignment', {
        'contact_id': contact.id,
        'assigned_to': user_data.get('id'),
        'assigned_name': user_email,
        'tags': tags
    }, room='admin')

@app.route('/api/whatsapp/send', methods=['POST'])
@auth_required
def send_message():
    data = request.json
    inst = data.get('instance')
    number = "".join(filter(str.isdigit, str(data.get('number', ''))))
    number = normalize_br_phone(number)
    text = data.get('text', '')
    
    if not inst or not number:
        return jsonify({'error': 'Instância e número são obrigatórios'}), 400

    # --- Chat locking check ---
    contact_id_check = f"c_{number}_{inst}"
    locked_contact = Contact.query.filter_by(id=contact_id_check).first()
    if locked_contact and locked_contact.assigned_to and locked_contact.assigned_to != request.user['id']:
        return jsonify({'error': f'Chat sendo atendido por {locked_contact.assigned_name or "outro atendente"}. Não é possível enviar mensagens.'}), 403

    try:
        now = get_now()
        time_str = now.strftime("%d/%m %H:%M")
        
        # Chamada para a API externa (WAHA)
        url = f"{WAHA_API_URL}/api/sendText"
        payload = {
            "chatId": f"{number}@c.us",
            "id": None,
            "reply_to": None,
            "text": text,
            "linkPreview": True,
            "linkPreviewHighQuality": False,
            "session": "corpal"
        }
        print(f"[SEND] URL: {url}")
        print(f"[SEND] Payload: {json.dumps(payload)}")
        res = requests.post(url, json=payload, headers=get_waha_headers(), timeout=30)
        print(f"[SEND] Response status: {res.status_code}")
        print(f"[SEND] Response body: {res.text[:300]}")
        try:
            res_data = res.json()
        except Exception:
            res_data = {'message': res.text}
        
        # Verificar se a API retornou erro
        if res.status_code != 200 and res.status_code != 201:
            error_msg = res_data.get('response', {}).get('message', res_data.get('message', str(res_data)))
            print(f"[SEND] ERRO WAHA API: {error_msg}")
            return jsonify({'error': f'WAHA API erro: {error_msg}'}), res.status_code
        
        msg_id = extract_waha_msg_id(res_data, f"out_{int(now.timestamp())}")
        contact_id = f"c_{number}_{inst}"
        
        # Atualizar ou Criar Contato
        contact = Contact.query.filter_by(id=contact_id).first()
        if contact:
            contact.last_msg = text
            contact.last_msg_time = time_str
            auto_assign_chat_to_sender(contact, request.user)
        else:
            contact = Contact(
                id=contact_id,
                name=number,
                phone=number,
                avatar=number[0] if number else "?",
                instance=inst,
                tags=['Novo Lead'],
                last_msg=text,
                last_msg_time=time_str,
                unread=0
            )
            auto_assign_chat_to_sender(contact, request.user)
            db_sql.session.add(contact)
        
        db_sql.session.flush()

        # Salvar mensagem no Banco SE NÃO EXISTIR
        if not Message.query.get(msg_id):
            new_msg = Message(
                id=msg_id,
                contact_id=contact_id,
                text=text,
                type='out',
                time=time_str,
                timestamp=int(now.timestamp()),
                instance=inst,
                sender_id=request.user['id']
            )
            db_sql.session.add(new_msg)
        
        # --- Forward to N8N (Attendant Message) ---
        webhook_key = f"n8n_webhook_{inst}"
        n8n_set = Setting.query.get(webhook_key)
        if n8n_set and n8n_set.value:
            try:
                n8n_payload = {
                    "event": "send.message",
                    "instance": inst,
                    "attendant": True,
                    "data": {
                        "key": {"remoteJid": f"{number}@s.whatsapp.net", "fromMe": True, "id": msg_id},
                        "message": {"conversation": text}
                    }
                }
                requests.post(n8n_set.value, json=n8n_payload, timeout=5)
            except Exception as w_e:
                print(f"Erro ao disparar webhook N8N para atendente: {w_e}")
                
        db_sql.session.commit()

        # --- Corpal Webhook (Attendant sent message) ---
        try:
            user_obj = User.query.get(request.user['id'])
            _contact_send = Contact.query.filter_by(id=contact_id).first()
            _filial = None
            _setor = None
            if user_obj:
                if user_obj.filial_id:
                    _f = Filial.query.get(user_obj.filial_id)
                    _filial = _f.name if _f else None
                if user_obj.setor_id:
                    _s = Setor.query.get(user_obj.setor_id)
                    _setor = _s.name if _s else None
            corpal_payload = {
                "evento": "mensagem",
                "atendimento_id": str(uuid.uuid4()),
                "numero_lead": number,
                "instancia": inst,
                "filial": _filial,
                "setor": _setor,
                "nome_atendente": user_obj.name if user_obj else "Desconhecido",
                "atendente_id": str(user_obj.id) if user_obj else None,
                "direcao": "atendente",
                "mensagem": text,
                "timestamp": now.isoformat()
            }
            requests.post(CORPAL_WEBHOOK_URL, json=corpal_payload, timeout=5)
        except Exception as corpal_e:
            print(f"Erro webhook corpal (send): {corpal_e}")
        return jsonify(res_data)
    except Exception as e:
        print(f"Erro ao enviar: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/whatsapp/send-audio', methods=['POST'])
@auth_required
def send_audio():
    """Envia audio gravado pelo atendente ao cliente via WAHA API."""
    data = request.json
    inst = data.get('instance')
    number = "".join(filter(str.isdigit, str(data.get('number', ''))))
    number = normalize_br_phone(number)
    audio_b64 = data.get('audio', '')

    if not inst or not number or not audio_b64:
        return jsonify({'error': 'instance, number e audio são obrigatórios'}), 400

    try:
        now = get_now()
        time_str = now.strftime("%d/%m %H:%M")

        # Enviar via WAHA API
        audio_raw = audio_b64
        mimetype = "audio/ogg; codecs=opus"
        if ';base64,' in audio_raw:
            mime_part, audio_raw = audio_raw.split(';base64,', 1)
            mimetype = mime_part.replace('data:', '')

        url = f"{WAHA_API_URL}/api/sendVoice"
        payload = {
            "session": "corpal",
            "chatId": f"{number}@c.us",
            "file": {
                "mimetype": mimetype,
                "data": audio_raw
            },
            "convert": True
        }
        print(f"[Send Audio] Enviando audio para {number} via {inst}")
        res = requests.post(url, json=payload, headers=get_waha_headers(), timeout=30)
        try:
            res_data = res.json()
        except Exception:
            res_data = {'message': res.text}
        print(f"[Send Audio] Resposta: status={res.status_code} body={json.dumps(res_data)[:300]}")

        msg_id = extract_waha_msg_id(res_data, f"audio_out_{int(now.timestamp())}")
        
        # Salvar o arquivo localmente via função centralizada
        try:
            import base64, re
            clean_b64 = re.sub(r'[^A-Za-z0-9+/]', '', audio_raw)
            pad_raw = clean_b64 + "=" * ((4 - len(clean_b64) % 4) % 4)
            contact_id = f"c_{number}_{inst}"
            save_media_file(msg_id, base64.b64decode(pad_raw), 'audio', instance=inst, contact_id=contact_id, mimetype=mimetype)
        except Exception as e:
            print(f"[Send Audio] Erro ao salvar arquivo local: {e}")

        text = f"[AUDIO_REF] {inst}|{msg_id}"

        contact_id = f"c_{number}_{inst}"

        contact = Contact.query.filter_by(id=contact_id).first()
        if not contact:
            contact = Contact(id=contact_id, phone=number, name=f"Novo {number}", instance=inst)
            db_sql.session.add(contact)
        auto_assign_chat_to_sender(contact, request.user)
        db_sql.session.flush()

        # Salvar mensagem
        if not Message.query.get(msg_id):
            new_msg = Message(
                id=msg_id, contact_id=contact_id, text=text,
                type='out', time=time_str, timestamp=int(now.timestamp()), instance=inst,
                sender_id=request.user['id']
            )
            db_sql.session.add(new_msg)

        contact.last_msg = '🎤 Áudio'
        contact.last_msg_time = time_str
        db_sql.session.commit()

        # NÃO emitir socket — o frontend já renderiza via optimistic update
        # Isso evita a duplicação de mensagem

        return jsonify({'ok': True, 'msg_id': msg_id, 'key': res_data.get('key', {})})
    except Exception as e:
        print(f"Erro send_audio: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/whatsapp/send-image', methods=['POST'])
@auth_required
def send_image():
    data = request.json
    inst = data.get('instance')
    number = "".join(filter(str.isdigit, str(data.get('number', ''))))
    number = normalize_br_phone(number)
    image_b64 = data.get('image', '')
    caption = data.get('caption', '')

    if not inst or not number or not image_b64:
        return jsonify({'error': 'instance, number e image são obrigatórios'}), 400

    try:
        now = get_now()
        time_str = now.strftime("%d/%m %H:%M")

        image_raw = image_b64
        mimetype = "image/jpeg"
        if ';base64,' in image_raw:
            mime_part, image_raw = image_raw.split(';base64,', 1)
            mimetype = mime_part.replace('data:', '')

        url = f"{WAHA_API_URL}/api/sendImage"
        payload = {
            "session": "corpal",
            "chatId": f"{number}@c.us",
            "caption": caption,
            "file": {
                "mimetype": mimetype,
                "data": image_raw
            }
        }
        res = requests.post(url, json=payload, headers=get_waha_headers(), timeout=30)
        try:
            res_data = res.json()
        except Exception:
            res_data = {'message': res.text}

        msg_id = extract_waha_msg_id(res_data, f"img_out_{int(now.timestamp())}")
        
        # Salvar o arquivo localmente via função centralizada
        try:
            import base64, re
            clean_b64 = re.sub(r'[^A-Za-z0-9+/]', '', image_raw)
            pad_raw = clean_b64 + "=" * ((4 - len(clean_b64) % 4) % 4)
            contact_id_img = f"c_{number}_{inst}"
            save_media_file(msg_id, base64.b64decode(pad_raw), 'image', instance=inst, contact_id=contact_id_img, mimetype=mimetype)
        except Exception as e:
            print(f"[Send Image] Erro ao salvar arquivo local: {e}")

        text = f"[IMAGE_REF] {inst}|{msg_id}"
        if caption:
            text += f"\n{caption}"

        contact_id = f"c_{number}_{inst}"

        contact = Contact.query.filter_by(id=contact_id).first()
        if not contact:
            contact = Contact(id=contact_id, phone=number, name=f"Novo {number}", instance=inst)
            db_sql.session.add(contact)
        auto_assign_chat_to_sender(contact, request.user)
        db_sql.session.flush()

        if not Message.query.get(msg_id):
            new_msg = Message(
                id=msg_id, contact_id=contact_id, text=text,
                type='out', time=time_str, timestamp=int(now.timestamp()), instance=inst,
                sender_id=request.user['id']
            )
            db_sql.session.add(new_msg)

        contact.last_msg = '🖼️ Imagem'
        contact.last_msg_time = time_str
        db_sql.session.commit()

        return jsonify({'ok': True, 'msg_id': msg_id, 'key': res_data.get('key', {})})
    except Exception as e:
        print(f"Erro send_image: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/whatsapp/delete-message', methods=['POST'])
@auth_required
def delete_message():
    data = request.json
    inst = data.get('instance')
    number = "".join(filter(str.isdigit, str(data.get('number', ''))))
    number = normalize_br_phone(number)
    msg_id = data.get('message_id')

    if not inst or not number or not msg_id:
        return jsonify({'error': 'Instância, número e message_id são obrigatórios'}), 400

    try:
        from urllib.parse import quote
        chat_id = f"{number}@c.us"

        # Monta o messageId completo se necessário
        waha_msg_id = msg_id
        if not waha_msg_id.startswith('true_') and not waha_msg_id.startswith('false_'):
            waha_msg_id = f"true_{chat_id}_{waha_msg_id}"

        safe_chat_id = quote(chat_id, safe='')
        safe_msg_id = quote(waha_msg_id, safe='')

        # Tentativa 1: DELETE /api/{session}/messages/{chatId}/{messageId}
        url_1 = f"{WAHA_API_URL}/api/{inst}/messages/{safe_chat_id}/{safe_msg_id}"
        print(f"[DELETE] Tentando URL 1: DELETE {url_1}")
        res = requests.delete(url_1, headers=get_waha_headers(), timeout=30)

        if res.status_code not in [200, 201, 204]:
            # Tentativa 2: DELETE /api/{session}/chats/{chatId}/messages/{messageId}
            url_2 = f"{WAHA_API_URL}/api/{inst}/chats/{safe_chat_id}/messages/{safe_msg_id}"
            print(f"[DELETE] Tentando URL 2: DELETE {url_2}")
            res = requests.delete(url_2, headers=get_waha_headers(), timeout=30)

        if res.status_code not in [200, 201, 204]:
            # Tentativa 3: POST /api/messages/delete (WAHA Core/Plus mais antigo)
            url_3 = f"{WAHA_API_URL}/api/messages/delete"
            payload = {"session": inst, "chatId": chat_id, "messageId": msg_id, "id": msg_id}
            print(f"[DELETE] Tentando URL 3: POST {url_3}")
            res = requests.post(url_3, json=payload, headers=get_waha_headers(), timeout=30)

        print(f"[DELETE] Response status final: {res.status_code}")

        if res.status_code not in [200, 201, 204]:
            return jsonify({'error': f'Falha ao apagar no WAHA: {res.status_code} - {res.text}'}), 500

        # Marca mensagem como apagada no banco local
        msg = Message.query.get(msg_id)
        if msg:
            msg.text = '[MENSAGEM_APAGADA]'
            db_sql.session.commit()

        return jsonify({'status': 'ok'})
    except Exception as e:
        print(f"Erro ao deletar: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/whatsapp/send-video', methods=['POST'])
@auth_required
def send_video():
    data = request.json
    inst = data.get('instance')
    number = "".join(filter(str.isdigit, str(data.get('number', ''))))
    number = normalize_br_phone(number)
    video_b64 = data.get('video', '')
    caption = data.get('caption', '')

    if not inst or not number or not video_b64:
        return jsonify({'error': 'instance, number e video são obrigatórios'}), 400

    try:
        now = get_now()
        time_str = now.strftime("%d/%m %H:%M")

        video_raw = video_b64
        mimetype = "video/mp4"
        if ';base64,' in video_raw:
            mime_part, video_raw = video_raw.split(';base64,', 1)
            mimetype = mime_part.replace('data:', '')

        url = f"{WAHA_API_URL}/api/sendVideo"
        payload = {
            "session": "corpal",
            "chatId": f"{number}@c.us",
            "caption": caption,
            "file": {
                "mimetype": mimetype,
                "data": video_raw
            }
        }
        res = requests.post(url, json=payload, headers=get_waha_headers(), timeout=60)
        try:
            res_data = res.json()
        except Exception:
            res_data = {'message': res.text}

        msg_id = extract_waha_msg_id(res_data, f"vid_out_{int(now.timestamp())}")
        
        # Salvar o arquivo localmente via função centralizada
        try:
            import base64, re
            clean_b64 = re.sub(r'[^A-Za-z0-9+/]', '', video_raw)
            pad_raw = clean_b64 + "=" * ((4 - len(clean_b64) % 4) % 4)
            contact_id_vid = f"c_{number}_{inst}"
            save_media_file(msg_id, base64.b64decode(pad_raw), 'video', instance=inst, contact_id=contact_id_vid, mimetype=mimetype)
        except Exception as e:
            print(f"[Send Video] Erro ao salvar arquivo local: {e}")

        text = f"[VIDEO_REF] {inst}|{msg_id}"
        if caption:
            text += f"\n{caption}"

        contact_id = f"c_{number}_{inst}"

        contact = Contact.query.filter_by(id=contact_id).first()
        if not contact:
            contact = Contact(id=contact_id, phone=number, name=f"Novo {number}", instance=inst)
            db_sql.session.add(contact)
        auto_assign_chat_to_sender(contact, request.user)
        db_sql.session.flush()

        if not Message.query.get(msg_id):
            new_msg = Message(
                id=msg_id, contact_id=contact_id, text=text,
                type='out', time=time_str, timestamp=int(now.timestamp()), instance=inst,
                sender_id=request.user['id']
            )
            db_sql.session.add(new_msg)

        contact.last_msg = '🎥 Vídeo'
        contact.last_msg_time = time_str
        db_sql.session.commit()

        return jsonify({'ok': True, 'msg_id': msg_id, 'key': res_data.get('key', {})})
    except Exception as e:
        print(f"Erro send_video: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/whatsapp/send-document', methods=['POST'])
@auth_required
def send_document():
    data = request.json
    inst = data.get('instance')
    number = "".join(filter(str.isdigit, str(data.get('number', ''))))
    number = normalize_br_phone(number)
    doc_b64 = data.get('document', '')
    doc_name = data.get('fileName', 'documento.pdf')
    caption = data.get('caption', '')

    if not inst or not number or not doc_b64:
        return jsonify({'error': 'instance, number e document são obrigatórios'}), 400

    try:
        now = get_now()
        time_str = now.strftime("%d/%m %H:%M")

        doc_raw = doc_b64
        mimetype = "application/pdf"
        if ';base64,' in doc_raw:
            mime_part, doc_raw = doc_raw.split(';base64,', 1)
            mimetype = mime_part.replace('data:', '')

        # --- Salvar PRIMEIRO com um ID temporário ---
        import uuid, os, base64, re, jwt, shutil
        temp_id = f"temp_{uuid.uuid4().hex}"
        media_dir = os.path.join(DATA_DIR, 'media')
        os.makedirs(media_dir, exist_ok=True)
        
        _, file_ext = os.path.splitext(doc_name)
        if not file_ext:
            if 'application/pdf' in mimetype: file_ext = '.pdf'
            elif 'application/zip' in mimetype: file_ext = '.zip'
            elif 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' in mimetype: file_ext = '.docx'
            elif 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' in mimetype: file_ext = '.xlsx'
            elif 'text/plain' in mimetype: file_ext = '.txt'
            else: file_ext = '.bin'
            
        clean_b64 = re.sub(r'[^A-Za-z0-9+/]', '', doc_raw)
        pad_raw = clean_b64 + "=" * ((4 - len(clean_b64) % 4) % 4)
        
        temp_filename = temp_id + file_ext
        temp_path = os.path.join(media_dir, temp_filename)
        try:
            doc_bytes_decoded = base64.b64decode(pad_raw)
            with open(temp_path, 'wb') as f:
                f.write(doc_bytes_decoded)
        except Exception as e:
            print(f"[Send Document] Erro b64decode/save: {e}")
            return jsonify({'error': 'Erro ao processar arquivo base64'}), 400

        # Gerar URL pública — preferir MinIO (acessível externamente), fallback para proxy local
        upload_ct = mimetype.split(';')[0].strip() if mimetype else 'application/octet-stream'
        minio_temp_url = upload_to_minio(temp_filename, doc_bytes_decoded, content_type=upload_ct)
        
        if minio_temp_url:
            doc_url = minio_temp_url
            print(f"[Send Document] Usando URL MinIO: {doc_url}")
        else:
            temp_token = jwt.encode({"id": 1, "role": "admin"}, JWT_SECRET, algorithm="HS256")
            host = request.headers.get('X-Forwarded-Host', request.headers.get('Host', 'localhost'))
            scheme = request.headers.get('X-Forwarded-Proto', 'https')
            base_url = f"{scheme}://{host}"
            doc_url = f"{base_url}/api/media/document?instance={inst}&msg_id={temp_id}&token={temp_token}"
            print(f"[Send Document] Usando URL proxy local: {doc_url}")

        url = f"{WAHA_API_URL}/api/sendFile"
        payload = {
            "session": "corpal",
            "chatId": f"{number}@c.us",
            "caption": caption,
            "file": {
                "mimetype": mimetype,
                "filename": doc_name,
                "url": doc_url
            }
        }
        print(f"[Send Document] Enviando para WAHA via URL: {doc_url}")
        res = requests.post(url, json=payload, headers=get_waha_headers(), timeout=60)
        try:
            res_data = res.json()
        except Exception:
            res_data = {'message': res.text}

        msg_id = extract_waha_msg_id(res_data, f"doc_out_{int(now.timestamp())}")
        
        # --- Atualizar Cache com ID Real (MinIO + Local) ---
        try:
            with open(temp_path, 'rb') as f:
                doc_bytes = f.read()
            contact_id_doc = f"c_{number}_{inst}"
            save_media_file(msg_id, doc_bytes, 'document', instance=inst, contact_id=contact_id_doc, mimetype=mimetype, original_filename=doc_name)
            # Limpar temporário local
            os.remove(temp_path)
            # Limpar temporário do MinIO
            if minio_temp_url:
                delete_from_minio(temp_filename)
        except Exception as e:
            print(f"[Send Document] Erro ao salvar arquivo real: {e}")
        # -------------------------
        
        # --- NEW CACHING LOGIC ---


        text = f"[DOC_REF] {inst}|{msg_id}|{doc_name}"

        contact_id = f"c_{number}_{inst}"

        contact = Contact.query.filter_by(id=contact_id).first()
        if not contact:
            contact = Contact(id=contact_id, phone=number, name=f"Novo {number}", instance=inst)
            db_sql.session.add(contact)
        auto_assign_chat_to_sender(contact, request.user)
        db_sql.session.flush()

        if not Message.query.get(msg_id):
            new_msg = Message(
                id=msg_id, contact_id=contact_id, text=text,
                type='out', time=time_str, timestamp=int(now.timestamp()), instance=inst,
                sender_id=request.user['id']
            )
            db_sql.session.add(new_msg)

        contact.last_msg = '📎 Arquivo'
        contact.last_msg_time = time_str
        db_sql.session.commit()

        return jsonify({'ok': True, 'msg_id': msg_id, 'key': res_data.get('key', {})})
    except Exception as e:
        print(f"Erro send_document: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/whatsapp/send-location', methods=['POST'])
@auth_required
def send_location():
    data = request.json
    inst = data.get('instance')
    number = "".join(filter(str.isdigit, str(data.get('number', ''))))
    number = normalize_br_phone(number)
    name = data.get('name', 'Localização')
    address = data.get('address', '')
    latitude = data.get('latitude')
    longitude = data.get('longitude')

    if not inst or not number or latitude is None or longitude is None:
        return jsonify({'error': 'instance, number, latitude e longitude são obrigatórios'}), 400

    try:
        now = get_now()
        time_str = now.strftime("%d/%m %H:%M")

        url = f"{WAHA_API_URL}/api/sendLocation"
        payload = {
            "session": "corpal",
            "chatId": f"{number}@c.us",
            "latitude": float(latitude),
            "longitude": float(longitude),
            "title": name,
            "description": address
        }
        res = requests.post(url, json=payload, headers=get_waha_headers(), timeout=60)
        res.raise_for_status()
        try:
            res_data = res.json()
        except Exception:
            res_data = {'message': res.text}

        msg_id = extract_waha_msg_id(res_data, f"loc_out_{int(now.timestamp())}")
        
        text = f"[LOCATION_REF] {latitude}|{longitude}|{name}|{address}"

        contact_id = f"c_{number}_{inst}"

        contact = Contact.query.filter_by(id=contact_id).first()
        if not contact:
            contact = Contact(id=contact_id, phone=number, name=f"Novo {number}", instance=inst)
            db_sql.session.add(contact)
        auto_assign_chat_to_sender(contact, request.user)
        db_sql.session.flush()

        if not Message.query.get(msg_id):
            new_msg = Message(
                id=msg_id, contact_id=contact_id, text=text,
                type='out', time=time_str, timestamp=int(now.timestamp()), instance=inst,
                sender_id=request.user['id']
            )
            db_sql.session.add(new_msg)

        contact.last_msg = '📍 Localização'
        contact.last_msg_time = time_str
        db_sql.session.commit()

        fake_event = {
            'event': 'send.message',
            'instance': inst,
            'data': {
                'key': {'remoteJid': f"{number}@s.whatsapp.net", 'fromMe': True, 'id': msg_id},
                'message': {'locationMessage': {}}
            },
            '_processed_text': text
        }
        socketio.emit('whatsapp_event', fake_event, room=f'instance_{inst}')
        socketio.emit('whatsapp_event', fake_event, room='admin')

        return jsonify({'ok': True, 'msg_id': msg_id, 'key': res_data.get('key', {})})
    except Exception as e:
        print(f"Erro send_location: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/whatsapp/send-contact', methods=['POST'])
@auth_required
def send_contact():
    """Envia um cartão de contato (vCard) para o cliente via WAHA API."""
    data = request.json
    inst = data.get('instance')
    number = "".join(filter(str.isdigit, str(data.get('number', ''))))
    number = normalize_br_phone(number)
    contact_name = data.get('contact_name', 'Contato').strip()
    contact_phone = "".join(filter(str.isdigit, str(data.get('contact_phone', ''))))
    contact_phone = normalize_br_phone(contact_phone)

    if not inst or not number or not contact_phone:
        return jsonify({'error': 'instance, number e contact_phone são obrigatórios'}), 400

    try:
        now = get_now()
        time_str = now.strftime("%d/%m %H:%M")

        # Montar vCard padrão
        vcard = (
            "BEGIN:VCARD\r\n"
            "VERSION:3.0\r\n"
            f"FN:{contact_name}\r\n"
            f"TEL;type=CELL;type=VOICE;waid={contact_phone}:+{contact_phone}\r\n"
            "END:VCARD"
        )

        url = f"{WAHA_API_URL}/api/sendContactVcard"
        payload = {
            "session": "corpal",
            "chatId": f"{number}@c.us",
            "contacts": [
                {
                    "fullName": contact_name,
                    "phoneNumber": contact_phone
                }
            ]
        }
        print(f"[Send Contact] Enviando contato '{contact_name}' ({contact_phone}) para {number} via {inst}")
        res = requests.post(url, json=payload, headers=get_waha_headers(), timeout=30)
        try:
            res_data = res.json()
        except Exception:
            res_data = {'message': res.text}
        print(f"[Send Contact] Resposta: status={res.status_code} body={json.dumps(res_data)[:300]}")

        msg_id = extract_waha_msg_id(res_data, f"contact_out_{int(now.timestamp())}")
        text = f"[CONTACT_REF] {contact_name}|+{contact_phone}|{vcard}"

        contact_id = f"c_{number}_{inst}"
        contact = Contact.query.filter_by(id=contact_id).first()
        if not contact:
            contact = Contact(id=contact_id, phone=number, name=f"Novo {number}", instance=inst)
            db_sql.session.add(contact)
        auto_assign_chat_to_sender(contact, request.user)
        db_sql.session.flush()

        if not Message.query.get(msg_id):
            new_msg = Message(
                id=msg_id, contact_id=contact_id, text=text,
                type='out', time=time_str, timestamp=int(now.timestamp()), instance=inst,
                sender_id=request.user['id']
            )
            db_sql.session.add(new_msg)

        contact.last_msg = f'👤 {contact_name}'
        contact.last_msg_time = time_str
        db_sql.session.commit()

        # Emitir socket para o frontend renderizar imediatamente
        fake_event = {
            'event': 'send.message',
            'instance': inst,
            'data': {
                'key': {'remoteJid': f"{number}@s.whatsapp.net", 'fromMe': True, 'id': msg_id},
                'message': {'contactMessage': {'displayName': contact_name, 'vcard': vcard}}
            },
            '_processed_text': text
        }
        socketio.emit('whatsapp_event', fake_event, room=f'instance_{inst}')
        socketio.emit('whatsapp_event', fake_event, room='admin')

        return jsonify({'ok': True, 'msg_id': msg_id, 'key': res_data.get('key', {})})
    except Exception as e:
        print(f"Erro send_contact: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/bot-message', methods=['POST'])
def bot_message_webhook():
    try:
        data = request.json
        if not data: return jsonify({'error': 'Body vazio'}), 400
        
        # Suporte para o array bruto do N8N/WAHA
        if isinstance(data, list) and len(data) > 0:
            data = data[0]

        if 'data' in data and 'key' in data.get('data', {}):
            # Formato do Sistema Interno (mantido para retrocompatibilidade com N8N)
            d = data['data']
            inst = d.get('instanceId') or d.get('instance')
            phone = str(d.get('key', {}).get('remoteJid', '')).split('@')[0].split(':')[0]
            phone = normalize_br_phone(phone)
            
            raw_id = d.get('key', {}).get('id', '')
            msg_id = f"bot_{raw_id}" if not raw_id.startswith('bot_') else raw_id

            # Parse message content including audio
            m_data = d.get('message', {})
            if 'audioMessage' in m_data:
                audio_base64 = data.get('base64') or d.get('base64') or m_data.get('base64') or m_data.get('audioMessage', {}).get('url')
                
                if not audio_base64 or str(audio_base64).startswith('http'):
                    fetched_b64 = get_media_base64(inst, d)
                    if fetched_b64:
                        audio_base64 = fetched_b64

                if audio_base64:
                    if str(audio_base64).startswith('data:') or str(audio_base64).startswith('http'):
                        text = f"[AUDIO] {audio_base64}"
                    else:
                        text = f"[AUDIO] data:audio/ogg;base64,{audio_base64}"
                else:
                    text = "[Áudio do Bot]"
            else:
                text = m_data.get('conversation') or m_data.get('extendedTextMessage', {}).get('text') or "[Mensagem do Bot]"
        else:
            # Formato Customizado Opcional
            inst = data.get('instanceId') or data.get('instance')
            phone = str(data.get('phone'))
            phone = normalize_br_phone(phone)
            text = data.get('text')
            msg_id = f"bot_{int(get_now().timestamp())}_{str(phone)[-4:]}"
        
        if not inst or not phone or not text:
            return jsonify({'error': 'Faltam campos obrigatorios: instance/instanceId, phone (ou remoteJid), e text'}), 400
            
        now = get_now()
        time_str = now.strftime("%d/%m %H:%M")
        contact_id = f"c_{phone}_{inst}"
        
        # Update Contact
        # Update Contact
        contact = Contact.query.filter_by(id=contact_id).first()
        if contact:
            contact.last_msg = text
            contact.last_msg_time = time_str
            tags = list(contact.tags or [])
            if 'BOT' not in tags:
                tags.append('BOT')
                contact.tags = tags
                flag_modified(contact, 'tags')
        else:
            new_contact = Contact(
                id=contact_id, name=phone, phone=phone,
                avatar=phone[0] if phone else "?", instance=inst,
                tags=['Novo Lead', 'BOT'], last_msg=text, last_msg_time=time_str, unread=0
            )
            db_sql.session.add(new_contact)
            
        db_sql.session.flush()

        # Save Message (com verificação de duplicata)
        if not Message.query.get(msg_id):
            new_msg = Message(
                id=msg_id,
                contact_id=contact_id,
                text=text,
                type='out',
                time=time_str,
                timestamp=int(now.timestamp()),
                instance=inst,
                sender_id=None
            )
            db_sql.session.add(new_msg)
        
        db_sql.session.commit()
        
        # Emit to frontend
        fake_event = {
            "event": "send.message",
            "instance": inst,
            "data": {
                "key": {"remoteJid": f"{phone}@s.whatsapp.net", "fromMe": True, "id": msg_id},
                "message": {"conversation": text}
            }
        }
        socketio.emit('whatsapp_event', fake_event, room=f'instance_{inst}')
        socketio.emit('whatsapp_event', fake_event, room='admin')
        
        return jsonify({"success": True, "message_id": msg_id}), 200
    except Exception as e:
        print(f"Erro bot-message: {e}")
        return jsonify({'error': str(e)}), 500

import threading
import time

# ─── Monitor de Tempo de Espera ─────────────────────────────────────────────

WEBHOOK_CHAMAR_URL = 'https://n8n-n8n.ioms5g.easypanel.host/webhook/alerta-tempo'
WEBHOOK_CHAMAR_GERENTE_URL = 'https://n8n-n8n.ioms5g.easypanel.host/webhook/alerta-tempo'

def wait_time_monitor_loop():
    """Thread em background que monitora clientes aguardando atendimento.
    Dispara alertas em 20 min (atendentes) e 40 min (gerentes)."""
    print("[MONITOR] Thread de monitoramento de tempo de espera iniciada.")
    while True:
        try:
            with app.app_context():
                agora = get_now()
                pendentes = AtendimentoChat.query.filter_by(status='atendente').all()
                for reg in pendentes:
                    if not reg.atendente_desde:
                        continue
                    try:
                        inicio = datetime.datetime.fromisoformat(reg.atendente_desde)
                        if inicio.tzinfo is None:
                            inicio = pytz.timezone('America/Sao_Paulo').localize(inicio)
                        decorrido_min = (agora - inicio).total_seconds() / 60

                        # Busca dados extras do contato para enriquecer o payload
                        contact_obj = Contact.query.filter_by(phone=reg.numero).order_by(Contact.id).first()
                        nome_cliente = contact_obj.name if contact_obj else reg.numero
                        instance_obj = contact_obj.instance if contact_obj else None

                        # Busca filial e setor: tenta pelo atendente primeiro, depois pelo ultimo_setor
                        filial_nome = None
                        setor_nome = reg.ultimo_setor  # fallback: setor de destino da transferência
                        if contact_obj and contact_obj.assigned_to:
                            atend_user = User.query.get(contact_obj.assigned_to)
                            if atend_user:
                                if atend_user.filial_id:
                                    _f = Filial.query.get(atend_user.filial_id)
                                    filial_nome = _f.name if _f else atend_user.filial
                                if atend_user.setor_id:
                                    _s = Setor.query.get(atend_user.setor_id)
                                    setor_nome = _s.name if _s else atend_user.setor
                                if not filial_nome:
                                    filial_nome = atend_user.filial
                                if not setor_nome:
                                    setor_nome = atend_user.setor
                        elif reg.atendente:
                            atend_user = User.query.filter_by(name=reg.atendente).first()
                            if atend_user:
                                if atend_user.filial_id:
                                    _f = Filial.query.get(atend_user.filial_id)
                                    filial_nome = _f.name if _f else atend_user.filial
                                if atend_user.setor_id:
                                    _s = Setor.query.get(atend_user.setor_id)
                                    setor_nome = _s.name if _s else atend_user.setor
                                if not filial_nome:
                                    filial_nome = atend_user.filial
                                if not setor_nome:
                                    setor_nome = atend_user.setor

                        payload_base = {
                            "numero": reg.numero,
                            "nome_cliente": nome_cliente,
                            "atendente": reg.atendente or "Aguardando atendente",
                            "filial": filial_nome,
                            "setor": setor_nome,
                            "instancia": instance_obj,
                            "minutos_esperando": round(decorrido_min, 1),
                            "atendente_desde": reg.atendente_desde,
                        }

                        # ── Alerta 20 minutos — notifica atendentes ──
                        if decorrido_min >= 20 and not reg.alerta_20min_enviado:
                            try:
                                payload_20 = dict(payload_base)
                                payload_20["nivel_alerta"] = "atendente"
                                payload_20["mensagem"] = f"Cliente {nome_cliente} aguardando há {round(decorrido_min, 1)} minutos sem atendimento."
                                requests.post(WEBHOOK_CHAMAR_URL, json=payload_20, timeout=10)
                                reg.alerta_20min_enviado = True
                                db_sql.session.commit()
                                print(f"[MONITOR] Alerta 20min enviado para {reg.numero} ({round(decorrido_min, 1)} min)")
                            except Exception as e20:
                                db_sql.session.rollback()
                                print(f"[MONITOR] Erro ao enviar alerta 20min para {reg.numero}: {e20}")

                        # ── Alerta 40 minutos — notifica gerentes ──
                        if decorrido_min >= 40 and not reg.alerta_40min_enviado:
                            try:
                                payload_40 = dict(payload_base)
                                payload_40["nivel_alerta"] = "gerente"
                                payload_40["mensagem"] = f"URGENTE: Cliente {nome_cliente} aguardando há {round(decorrido_min, 1)} minutos sem atendimento."
                                requests.post(WEBHOOK_CHAMAR_GERENTE_URL, json=payload_40, timeout=10)
                                reg.alerta_40min_enviado = True
                                db_sql.session.commit()
                                print(f"[MONITOR] Alerta 40min (gerente) enviado para {reg.numero} ({round(decorrido_min, 1)} min)")
                            except Exception as e40:
                                db_sql.session.rollback()
                                print(f"[MONITOR] Erro ao enviar alerta 40min para {reg.numero}: {e40}")

                    except Exception as e_reg:
                        print(f"[MONITOR] Erro ao processar registro {reg.numero}: {e_reg}")

                # ── NPS: timeout de 5 minutos ─────────────────────────────────────
                try:
                    agora_nps = get_now()
                    nps_pendentes = AtendimentoChat.query.filter(
                        AtendimentoChat.nps_status.in_(['waiting_vote', 'waiting_reason'])
                    ).all()
                    for _n in nps_pendentes:
                        if not _n.nps_started_at:
                            continue
                        try:
                            _inicio_nps = datetime.datetime.fromisoformat(_n.nps_started_at)
                            if _inicio_nps.tzinfo is None:
                                _inicio_nps = pytz.timezone('America/Sao_Paulo').localize(_inicio_nps)
                            _elapsed = (agora_nps - _inicio_nps).total_seconds()
                            if _elapsed >= 300:  # 5 minutos
                                # Salva no histórico caso já tenha voto mas não motivo
                                if _n.nps_voto:
                                    _timeout_voto = NpsVoto(
                                        numero_cliente = _n.numero,
                                        atendente      = _n.ultimo_atendente,
                                        filial         = None,
                                        setor          = _n.ultimo_setor,
                                        voto           = _n.nps_voto,
                                        motivo         = None,
                                        data_voto      = agora_nps.isoformat()
                                    )
                                    db_sql.session.add(_timeout_voto)
                                _n.nps_status = 'finished'
                                db_sql.session.commit()
                                print(f"[NPS] Timeout para {_n.numero} após {int(_elapsed)}s — NPS encerrado.")
                                # Avisa o cliente
                                try:
                                    _contact_nps = Contact.query.filter(
                                        db_sql.or_(
                                            Contact.phone == _n.numero,
                                            Contact.phone.like(f"%{_n.numero}%")
                                        )
                                    ).first()
                                    _inst_nps = (_contact_nps.instance if _contact_nps else None) or "corpal"
                                    _clean_num = normalize_phone(_n.numero)
                                    requests.post(
                                        f"{WAHA_API_URL}/api/sendText",
                                        headers=get_waha_headers(),
                                        json={
                                            "chatId": f"{_clean_num}@c.us",
                                            "text": "Sua pesquisa de satisfação foi encerrada por inatividade. Obrigado pela sua participação! 😊",
                                            "session": _inst_nps
                                        },
                                        timeout=5
                                    )
                                except Exception:
                                    pass
                        except Exception as _nps_te:
                            db_sql.session.rollback()
                            print(f"[NPS] Erro ao processar timeout para {_n.numero}: {_nps_te}")
                except Exception as _nps_loop_e:
                    print(f"[NPS] Erro no loop de timeout: {_nps_loop_e}")
                # ─────────────────────────────────────────────────────────────────

        except Exception as e_loop:
            print(f"[MONITOR] Erro no loop de monitoramento: {e_loop}")
        time.sleep(60)  # Verifica a cada 60 segundos


def start_wait_time_monitor():
    """Inicia a thread de monitoramento de tempo de espera em background."""
    t = threading.Thread(target=wait_time_monitor_loop, daemon=True, name="WaitTimeMonitor")
    t.start()
    print("[MONITOR] Thread WaitTimeMonitor iniciada com sucesso.")

start_wait_time_monitor()


def fetch_and_update_avatar_async(contact_id, phone, instance):
    def _fetch():
        try:
            url = f"{WAHA_API_URL}/api/contacts/profilePicture?session=corpal&phone={phone}@c.us"
            res = requests.get(url, headers=get_waha_headers(), timeout=10)
            if res.status_code == 200:
                data = res.json()
                picture_url = data.get('profilePictureUrl')
                if picture_url:
                    with app.app_context():
                        contact = Contact.query.get(contact_id)
                        if contact:
                            contact.avatar = picture_url
                            db_sql.session.commit()
                            
                            # Emitir socket para atualizar o frontend
                            socketio.emit('chat_avatar_updated', {
                                'id': contact_id,
                                'avatar': picture_url
                            }, room=f'instance_{instance}')
                            socketio.emit('chat_avatar_updated', {
                                'id': contact_id,
                                'avatar': picture_url
                            }, room='admin')
        except Exception as e:
            print(f"[Avatar] Erro ao buscar foto para {phone}: {e}")

    threading.Thread(target=_fetch).start()

@app.route('/api/webhooks/waha', methods=['POST'])
def webhook():
    try:
        data = request.json
        if not data: return 'OK', 200

        # DEBUG TEMPORARIO: loga TODOS os eventos recebidos (remover depois)
        _evt_debug = data.get('event', 'SEM_EVENTO')
        print(f"[WEBHOOK IN] event={_evt_debug!r} session={data.get('session')!r} keys={list(data.keys())}")

        # ── NPS: intercepta evento de voto na enquete (poll.vote) ─────────────
        if data.get('event') == 'poll.vote':
            try:
                _vote_payload = data.get('payload', {})
                _poll_info    = _vote_payload.get('poll', {})
                _poll_id_full = _poll_info.get('id', '')
                _msg_hash     = _poll_id_full.split('_')[-1] if '_' in _poll_id_full else _poll_id_full

                _vote_info    = _vote_payload.get('vote', {})
                _vote_from    = _vote_info.get('from', '')
                _vote_session = data.get('session', 'corpal')
                
                # Converte LID para numero principal se necessario
                if '@lid' in _vote_from:
                    try:
                        resp_lid = requests.get(
                            f"{WAHA_API_URL}/api/{_vote_session}/lids/{_vote_from}",
                            headers=get_waha_headers(),
                            timeout=5
                        )
                        if resp_lid.status_code == 200:
                            lid_data = resp_lid.json()
                            print(f"[NPS] API LID converteu {_vote_from} para: {lid_data}")
                            _pn = lid_data.get('pn', '')
                            if _pn:
                                _vote_from = _pn
                    except Exception as _e_lid:
                        print(f"[NPS] Erro na api/lids: {_e_lid}")

                _vote_phone   = normalize_phone(_vote_from)
                _selected     = (_vote_info.get('selectedOptions') or [''])[0]
                
                print(f"[NPS] poll.vote recebido — opção: {_selected!r} hash: {_msg_hash!r}")
                if _selected:  # ignora votos vazios (desvoto no WhatsApp)
                    atend_nps = None
                    if _msg_hash:
                        atend_nps = AtendimentoChat.query.filter_by(nps_poll_id=_msg_hash, nps_status='waiting_vote').first()
                    
                    # Fallback por numero caso a hash falhe
                    if not atend_nps:
                        atend_nps = AtendimentoChat.query.filter(
                            db_sql.or_(
                                AtendimentoChat.numero == _vote_phone,
                                AtendimentoChat.numero.like(f"%{_vote_phone}%")
                            ),
                            AtendimentoChat.nps_status == 'waiting_vote'
                        ).first()

                    if atend_nps:
                        _real_phone = atend_nps.numero
                        atend_nps.nps_voto   = _selected
                        atend_nps.nps_status = 'waiting_reason'
                        db_sql.session.commit()
                        print(f"[NPS] Voto '{_selected}' registrado para {_vote_phone} — aguardando motivo")
                        
                        # Define a mensagem de pedido de motivo com base na nota
                        try:
                            nota = int(_selected)
                        except ValueError:
                            nota = 0

                        if nota >= 9:
                            msg_texto = (
                                "Muito obrigado pela sua avaliação! Ficamos felizes em saber que sua experiência com a Corpal foi positiva. 🌱\n\n"
                                "Caso queira deixar algum comentário sobre o atendimento recebido, é só enviar por aqui. Sua mensagem será registrada com muito carinho."
                            )
                        elif nota >= 7:
                            msg_texto = (
                                "Obrigado pela sua avaliação! 🌱\n\n"
                                "Queremos melhorar cada vez mais. Caso tenha alguma sugestão sobre como poderíamos tornar seu atendimento ainda melhor, envie por aqui. Sua opinião será registrada e analisada pela nossa equipe."
                            )
                        else:
                            msg_texto = (
                                "Sentimos muito por sua experiência não ter sido como esperávamos.\n\n"
                                "A Corpal valoriza muito a sua opinião e queremos melhorar. Por favor, envie um comentário contando o que aconteceu ou como podemos melhorar nos próximos atendimentos."
                            )

                        # Pergunta o motivo
                        requests.post(
                            f"{WAHA_API_URL}/api/sendText",
                            headers=get_waha_headers(),
                            json={
                                "chatId": f"{_vote_phone}@c.us",
                                "text": msg_texto,
                                "session": _vote_session
                            },
                            timeout=10
                        )
                    else:
                        _debug_msg = f"DEBUG NPS: Nenhuma sessão 'waiting_vote' encontrada para o número {_vote_phone} (Voto: {_selected}). Tente verificar se o número no banco bate."
                        print(f"[NPS] {_debug_msg}")
                        requests.post(
                            f"{WAHA_API_URL}/api/sendText",
                            headers=get_waha_headers(),
                            json={
                                "chatId": f"{_vote_phone}@c.us",
                                "text": _debug_msg,
                                "session": _vote_session
                            },
                            timeout=5
                        )
            except Exception as _nps_vote_err:
                db_sql.session.rollback()
                _debug_msg = f"DEBUG NPS: Erro interno ao processar poll.vote: {str(_nps_vote_err)}"
                print(f"[NPS] {_debug_msg}")
                try:
                    # Tenta enviar erro pro número que veio no poll, se existir
                    _err_phone = data.get('payload', {}).get('vote', {}).get('from', '').split('@')[0]
                    if _err_phone:
                        requests.post(
                            f"{WAHA_API_URL}/api/sendText",
                            headers=get_waha_headers(),
                            json={
                                "chatId": f"{_err_phone}@c.us",
                                "text": _debug_msg,
                                "session": data.get('session', 'corpal')
                            },
                            timeout=5
                        )
                except: pass
            return 'OK', 200
        # ─────────────────────────────────────────────────────────────────────

        # ---- WAHA TO INTERNAL CONVERTER ----
        if data.get('event') in ('message', 'message.any', 'message.ack') and 'payload' in data:
            waha_event = data.get('event')
            session = data.get('session')
            payload = data.get('payload', {})

            
            if waha_event == 'message.ack':
                ack_val = payload.get('ack', 0)
                ack_name = payload.get('ackName', '')
                waha_id = payload.get('id', '')
                
                # WAHA costuma enviar "false_numero@c.us_HASH" no message.ack
                db_msg_id = waha_id
                if waha_id and '_' in waha_id:
                    db_msg_id = waha_id.split('_')[-1]
                
                # Tenta atualizar no BD (case-insensitive para lidar com hashes do WAHA/Baileys)
                msg_obj = Message.query.filter(Message.id.ilike(db_msg_id)).first()
                if not msg_obj:
                    # Fallback pro id completo
                    msg_obj = Message.query.filter(Message.id.ilike(waha_id)).first()
                    if msg_obj:
                        db_msg_id = msg_obj.id
                elif msg_obj:
                    # Garantir que o db_msg_id enviado ao front seja exatamente como está no BD
                    db_msg_id = msg_obj.id
                
                print(f"[ACK] Recebido ack={ack_val} ({ack_name}) para msg={waha_id}. Encontrou BD? {'Sim' if msg_obj else 'Nao'}")
                        
                if msg_obj:
                    msg_obj.ack = ack_val
                    db_sql.session.commit()
                
                # Emitir socket para a interface com o ID real
                ack_data = {
                    'event': 'message.ack',
                    'instance': session,
                    'messageId': db_msg_id,
                    'ack': ack_val,
                    'ackName': ack_name
                }
                socketio.emit('whatsapp_ack', ack_data, room=f'instance_{session}')
                socketio.emit('whatsapp_ack', ack_data, room='admin')
                return 'OK', 200
            waha_id = payload.get('id', '')
            waha_from = payload.get('from', '')
            waha_to = payload.get('to', '')
            
            # NOWEB support for @lid fallback
            remote_jid_alt = payload.get('_data', {}).get('key', {}).get('remoteJidAlt')
            if remote_jid_alt:
                if waha_from and waha_from.endswith('@lid'):
                    waha_from = remote_jid_alt
                if waha_to and waha_to.endswith('@lid'):
                    waha_to = remote_jid_alt
                    
            fromMe = payload.get('fromMe', False)
            raw_jid = waha_to if fromMe else waha_from
            body = payload.get('body', '')
            msg_type = payload.get('type', 'chat')

            # ── NPS: intercepta motivo do cliente (mensagem de texto após voto) ──
            if not fromMe and msg_type == 'chat' and body:
                _nps_phone = normalize_phone(raw_jid)
                
                # Bloqueia a linha no banco (FOR UPDATE) para evitar race condition de webhooks duplicados
                _atend_nps_motivo = AtendimentoChat.query.filter(
                    db_sql.or_(
                        AtendimentoChat.numero == _nps_phone,
                        AtendimentoChat.numero.like(f"%{_nps_phone}%")
                    ),
                    AtendimentoChat.nps_status == 'waiting_reason'
                ).with_for_update().first()
                
                if _atend_nps_motivo:
                    try:
                        # Salva voto + motivo no histórico
                        _nps_voto_obj = NpsVoto(
                            numero_cliente = _nps_phone,
                            atendente      = _atend_nps_motivo.ultimo_atendente,
                            filial         = None,
                            setor          = _atend_nps_motivo.ultimo_setor,
                            voto           = _atend_nps_motivo.nps_voto,
                            motivo         = body,
                            data_voto      = get_now().isoformat()
                        )
                        db_sql.session.add(_nps_voto_obj)
                        _atend_nps_motivo.nps_status = 'finished'
                        db_sql.session.commit()
                        print(f"[NPS] Motivo registrado para {_nps_phone}: '{body[:80]}'")
                        # Mensagem de agradecimento final
                        requests.post(
                            f"{WAHA_API_URL}/api/sendText",
                            headers=get_waha_headers(),
                            json={
                                "chatId": f"{_nps_phone}@c.us",
                                "text": "Agradecemos a sua resposta! 🙏 Seu feedback é muito importante para nós.",
                                "session": session or "corpal"
                            },
                            timeout=10
                        )
                    except Exception as _nps_motivo_err:
                        db_sql.session.rollback()
                        print(f"[NPS] Erro ao salvar motivo: {_nps_motivo_err}")
                    return 'OK', 200  # Não exibe no chat do atendente
            # ─────────────────────────────────────────────────────────────────


            if payload.get('hasMedia') and msg_type in ('chat', None, ''):
                mimetype = payload.get('media', {}).get('mimetype', '')
                if mimetype.startswith('audio/'):
                    msg_type = 'audio'
                elif mimetype.startswith('image/'):
                    msg_type = 'image'
                elif mimetype.startswith('video/'):
                    msg_type = 'video'
                else:
                    msg_type = 'document'
            elif msg_type in ('chat', None, ''):
                if payload.get('location'):
                    msg_type = 'location'
                elif payload.get('vCards'):
                    msg_type = 'contact'

            # --- DOWNLOAD AUTOMÁTICO DE MEDIA DA PAYLOAD DO WAHA ---
            # Sempre forçar download se for mídia (mesmo que a payload.media não venha completa)
            if payload.get('hasMedia') or msg_type in ('image', 'video', 'document', 'audio', 'ptt', 'voice'):
                media_dir = os.path.join(DATA_DIR, 'media')
                os.makedirs(media_dir, exist_ok=True)
                short_id = waha_id.split('_')[-1] if '_' in waha_id else waha_id
                
                # --- Determinar extensão CORRETA baseada no mimetype real ---
                media_info = payload.get('media', {})
                media_mimetype = media_info.get('mimetype', '')
                media_url = media_info.get('url')
                media_b64 = media_info.get('data')
                
                ext = ''
                if msg_type == 'image':
                    if 'png' in media_mimetype: ext = '.png'
                    elif 'webp' in media_mimetype: ext = '.webp'
                    elif 'gif' in media_mimetype: ext = '.gif'
                    else: ext = '.jpeg'
                elif msg_type in ('audio', 'voice', 'ptt'):
                    ext = '.oga'
                elif msg_type == 'video':
                    ext = '.mp4'
                elif msg_type == 'document':
                    # Tentar pela extensão do fileName primeiro
                    doc_filename = payload.get('body', '') or payload.get('_data', {}).get('message', {}).get('documentMessage', {}).get('fileName', '')
                    if doc_filename:
                        _, doc_ext = os.path.splitext(doc_filename)
                        if doc_ext: ext = doc_ext
                    # Fallback pelo mimetype
                    if not ext:
                        import mimetypes as _mt
                        guessed = _mt.guess_extension(media_mimetype.split(';')[0].strip())
                        ext = guessed if guessed else '.bin'
                
                filepath = os.path.join(media_dir, f"{short_id}{ext}")
                # Também salvar com o ID completo para que o proxy encontre por ambos
                filepath_full = os.path.join(media_dir, f"{waha_id}{ext}") if waha_id != short_id else None
                
                import glob as _glob
                # Verificar se JÁ existe com qualquer extensão (evitar re-download)
                already_exists = False
                for check_id in set([short_id, waha_id]):
                    check_path = os.path.join(media_dir, check_id)
                    if _glob.glob(check_path + '.*') or os.path.exists(check_path):
                        already_exists = True
                        break

                if not already_exists:
                    saved_locally = False
                    file_bytes_saved = None
                    try:
                        if media_b64:
                            import base64
                            file_bytes_saved = base64.b64decode(media_b64)
                            _fn, _murl = save_media_file(waha_id, file_bytes_saved, msg_type, instance=session, contact_id=f"c_{raw_jid}_{session}", mimetype=media_mimetype)
                            saved_locally = True
                            print(f"[Media] Arquivo salvo via Base64 do webhook (MinIO={'SIM' if _murl else 'NAO'})")
                        elif media_url:
                            # Corrige URL caso venha localhost
                            if media_url.startswith('http://localhost') or media_url.startswith('http://127.0.0.1'):
                                from urllib.parse import urlparse
                                parsed = urlparse(media_url)
                                media_url = f"{WAHA_API_URL}{parsed.path}?{parsed.query}" if parsed.query else f"{WAHA_API_URL}{parsed.path}"
                                
                            dl_res = requests.get(media_url, headers=get_waha_headers(), timeout=15)
                            if dl_res.status_code == 200:
                                file_bytes_saved = dl_res.content
                                _fn, _murl = save_media_file(waha_id, file_bytes_saved, msg_type, instance=session, contact_id=f"c_{raw_jid}_{session}", mimetype=media_mimetype)
                                saved_locally = True
                                print(f"[Media] Arquivo salvo via URL do webhook (MinIO={'SIM' if _murl else 'NAO'})")
                            else:
                                print(f"[Media] Falha ao baixar da URL {media_url}: HTTP {dl_res.status_code}")
                    except Exception as e:
                        print(f"[Media] Erro ao salvar media via payload: {e}")
                        
                    # Fallback Agressivo: Se falhou ao salvar pela payload, forçar busca via GET /api/files
                    if not saved_locally:
                        try:
                            waha_url_1 = f"{WAHA_API_URL}/api/files"
                            dl_res = requests.get(waha_url_1, headers=get_waha_headers(), params={'session': session, 'messageId': waha_id}, timeout=15)
                            
                            # Tentar fallback com id curto
                            if dl_res.status_code == 404 and short_id != waha_id:
                                dl_res = requests.get(waha_url_1, headers=get_waha_headers(), params={'session': session, 'messageId': short_id}, timeout=15)
                                
                            if dl_res.status_code == 200:
                                file_bytes_saved = dl_res.content
                                ctype = dl_res.headers.get('Content-Type', '')
                                if 'application/json' in ctype:
                                    import base64, re
                                    json_data = dl_res.json()
                                    if 'data' in json_data:
                                        raw = json_data['data']
                                        raw = re.sub(r'[^A-Za-z0-9+/]', '', raw)
                                        raw += "=" * ((4 - len(raw) % 4) % 4)
                                        file_bytes_saved = base64.b64decode(raw)
                                    elif 'url' in json_data:
                                        real_url = json_data['url']
                                        if real_url.startswith('http://localhost') or real_url.startswith('http://127.0.0.1'):
                                            from urllib.parse import urlparse
                                            real_url = f"{WAHA_API_URL}{urlparse(real_url).path}"
                                        real_res = requests.get(real_url, headers=get_waha_headers(), timeout=15)
                                        if real_res.status_code == 200:
                                            file_bytes_saved = real_res.content
                                if file_bytes_saved:
                                    _fn, _murl = save_media_file(waha_id, file_bytes_saved, msg_type, instance=session, contact_id=f"c_{raw_jid}_{session}", mimetype=ctype)
                                    saved_locally = True
                                    print(f"[Media] Arquivo salvo via FALLBACK Agressivo (MinIO={'SIM' if _murl else 'NAO'})")
                            else:
                                print(f"[Media] FALLBACK Agressivo falhou: HTTP {dl_res.status_code}")
                        except Exception as e:
                            print(f"[Media] Erro no FALLBACK Agressivo: {e}")
                    
                    if not saved_locally:
                        print(f"[Media] ⚠️ AVISO: Mídia NÃO foi salva para msg_id={waha_id} tipo={msg_type} — será buscada sob demanda")
            # -------------------------------------------------------

            # --- Normalizar JID para 12 dígitos ---
            raw_jid = waha_to if fromMe else waha_from
            norm_phone = normalize_br_phone(raw_jid)
            final_jid = f"{norm_phone}@s.whatsapp.net" if norm_phone else raw_jid
            # ----------------------------------------
            
            evo_data = {
                "event": "messages.upsert",
                "instance": session,
                "data": {
                    "key": {
                        "remoteJid": final_jid,
                        "fromMe": fromMe,
                        "id": waha_id
                    },
                    "message": {}
                }
            }
            
            media_b64 = ''
            waha_media_url = ''
            public_media_url = ''
            if payload.get('hasMedia') and 'media' in payload:
                media_info = payload.get('media', {})
                media_b64 = media_info.get('data', '')
                waha_media_url = media_info.get('url', '')
                if waha_media_url.startswith('http://localhost'):
                    from urllib.parse import urlparse
                    parsed = urlparse(waha_media_url)
                    waha_media_url = f"{WAHA_API_URL}{parsed.path}?{parsed.query}" if parsed.query else f"{WAHA_API_URL}{parsed.path}"
                try:
                    media_token = jwt.encode({'media_access': True, 'exp': datetime.datetime.utcnow() + datetime.timedelta(hours=24)}, JWT_SECRET, algorithm="HS256")
                    public_media_url = f"{request.host_url.rstrip('/')}/api/media/audio?instance={session}&msg_id={waha_id}&token={media_token}"
                except Exception:
                    pass

            if payload.get('hasMedia') or msg_type in ('image', 'video', 'document', 'audio', 'ptt', 'voice'):
                base_media_data = {"base64": media_b64, "wahaUrl": waha_media_url}
                if msg_type == 'image':
                    evo_data['data']['message']['imageMessage'] = {"caption": body, "url": public_media_url.replace('/audio?', '/image?'), **base_media_data}
                elif msg_type == 'video':
                    evo_data['data']['message']['videoMessage'] = {"caption": body, "url": public_media_url.replace('/audio?', '/video?'), **base_media_data}
                elif msg_type in ('audio', 'ptt', 'voice'):
                    evo_data['data']['message']['audioMessage'] = {"url": public_media_url, **base_media_data}
                elif msg_type == 'document':
                    evo_data['data']['message']['documentMessage'] = {"fileName": body or 'Arquivo', "url": public_media_url.replace('/audio?', '/document?'), **base_media_data}
                else:
                    # fallback
                    evo_data['data']['message']['documentMessage'] = {"fileName": 'Arquivo', "url": public_media_url.replace('/audio?', '/document?'), **base_media_data}
            elif msg_type == 'location':
                loc_name = payload.get('location', {}).get('description') or payload.get('body') or ''
                evo_data['data']['message']['locationMessage'] = {
                    "degreesLatitude": payload.get('location', {}).get('latitude', ''),
                    "degreesLongitude": payload.get('location', {}).get('longitude', ''),
                    "name": loc_name,
                }
            elif msg_type in ('vcard', 'contact'):
                vcards = payload.get('vCards') or []
                vcard_str = vcards[0] if isinstance(vcards, list) and len(vcards) > 0 else (body or '')
                
                display_name = "Contato"
                if vcard_str and isinstance(vcard_str, str):
                    for line in vcard_str.split('\n'):
                        if line.startswith('FN:'):
                            display_name = line.split('FN:')[1].strip()
                            break
                            
                evo_data['data']['message']['contactMessage'] = {
                    "vcard": vcard_str,
                    "displayName": display_name
                }
            else:
                evo_data['data']['message']['conversation'] = body

            data = evo_data
        # ---- FIM CONVERTER ----
        
        event = data.get('event')
        instance = data.get('instance')
        
        # n8n Forwarding per instance
        webhook_key = f"n8n_webhook_{instance}"
        n8n_set = Setting.query.get(webhook_key)
        if n8n_set and n8n_set.value:
            try: requests.post(n8n_set.value, json=data, timeout=5)
            except: pass

        if event in ('messages.upsert', 'send.message'):
            msg_data = data.get('data', {})
            key = msg_data.get('key', {})
            remoteJid = key.get('remoteJid', '')
            if not remoteJid or remoteJid == 'status@broadcast': return 'OK', 200

            phone_original = remoteJid.split('@')[0].split(':')[0]
            phone = normalize_br_phone(phone_original)
            
            if phone != phone_original:
                key['remoteJid'] = f"{phone}@s.whatsapp.net"

            fromMe = key.get('fromMe', False)
            
            m = msg_data.get('message', {})
            msg_id_key = key.get('id', '')
            if 'audioMessage' in m:
                text = f"[AUDIO_REF] {instance}|{msg_id_key}"
                print(f"[Audio] Guardando ref de audio: instance={instance} msg_id={msg_id_key}")
            elif 'imageMessage' in m:
                caption = m.get('imageMessage', {}).get('caption', '')
                text = f"[IMAGE_REF] {instance}|{msg_id_key}"
                if caption:
                    text += f"\n{caption}"
                print(f"[Image] Guardando ref de imagem: instance={instance} msg_id={msg_id_key}")
            elif 'videoMessage' in m:
                caption = m.get('videoMessage', {}).get('caption', '')
                text = f"[VIDEO_REF] {instance}|{msg_id_key}"
                if caption:
                    text += f"\n{caption}"
                print(f"[Video] Guardando ref de video: instance={instance} msg_id={msg_id_key}")
            elif 'documentMessage' in m:
                doc_name = m.get('documentMessage', {}).get('fileName', 'Arquivo')
                text = f"[DOC_REF] {instance}|{msg_id_key}|{doc_name}"
                print(f"[Doc] Guardando ref de documento: instance={instance} msg_id={msg_id_key}")
            elif 'locationMessage' in m:
                lat = m.get('locationMessage', {}).get('degreesLatitude', '')
                lng = m.get('locationMessage', {}).get('degreesLongitude', '')
                name = m.get('locationMessage', {}).get('name', '')
                address = m.get('locationMessage', {}).get('address', '')
                text = f"[LOCATION_REF] {lat}|{lng}|{name}|{address}"
                print(f"[Location] Guardando ref de localizacao: instance={instance} msg_id={msg_id_key}")
            elif 'contactMessage' in m:
                contact_data = m.get('contactMessage', {})
                display_name = contact_data.get('displayName', 'Contato')
                vcard = contact_data.get('vcard', '')
                
                contact_phone = ''
                for line in vcard.split('\n'):
                    if 'waid=' in line:
                        contact_phone = line.split('waid=')[1].split(':')[0]
                        break
                    elif line.strip().upper().startswith('TEL'):
                        contact_phone = line.split(':')[-1].strip().replace('\r', '')
                
                text = f"[CONTACT_REF] {display_name}|{contact_phone}|{vcard}"
                print(f"[Contact] Guardando ref de contato: name={display_name} phone={contact_phone}")
                
            elif 'contactsArrayMessage' in m:
                contacts_list = m.get('contactsArrayMessage', {}).get('contacts', [])
                names = ', '.join([c.get('displayName', '?') for c in contacts_list])
                
                contact_phone = ''
                if contacts_list:
                    vcard = contacts_list[0].get('vcard', '')
                    for line in vcard.split('\n'):
                        if 'waid=' in line:
                            contact_phone = line.split('waid=')[1].split(':')[0]
                            break
                        elif line.strip().upper().startswith('TEL'):
                            contact_phone = line.split(':')[-1].strip().replace('\r', '')
                            
                text = f"[CONTACT_REF] {names}|{contact_phone}|{str(contacts_list)}"
                print(f"[Contact] Guardando array de contatos: {names}")
            else:
                text = m.get('conversation') or \
                       m.get('extendedTextMessage', {}).get('text') or \
                       m.get('buttonsResponseMessage', {}).get('selectedDisplayText') or \
                       m.get('listResponseMessage', {}).get('title') or \
                       "[Mensagem N8N/Mídia]"

            now = get_now()
            time_str = now.strftime("%d/%m %H:%M")
            contact_id = f"c_{phone}_{instance}"

            # Update/Create Contact
            contact = Contact.query.filter_by(id=contact_id).first()
            if not contact:
                contact = Contact(
                    id=contact_id, name=phone, phone=phone,
                    avatar=phone[0] if phone else "?",
                    instance=instance,
                    tags=['Novo Lead'], last_msg=text, last_msg_time=time_str,
                    unread=0 if fromMe else 1
                )
                db_sql.session.add(contact)
                db_sql.session.flush()
                # Chama a busca de foto
                fetch_and_update_avatar_async(contact_id, phone, instance)
            else:
                contact.last_msg = text
                contact.last_msg_time = time_str
                if not fromMe:
                    contact.unread = (contact.unread or 0) + 1
                    
                # Se não tem foto real (é só uma letra ou '?'), tenta buscar
                if not contact.avatar or len(contact.avatar) <= 2 or not contact.avatar.startswith('http'):
                    fetch_and_update_avatar_async(contact_id, phone, instance)
            
            db_sql.session.flush()

            # Save Message
            msg_id = key.get('id')
            if not Message.query.get(msg_id):
                new_msg = Message(
                    id=msg_id,
                    contact_id=contact_id,
                    text=text,
                    type='out' if fromMe else 'in',
                    time=time_str,
                    timestamp=int(now.timestamp()),
                    instance=instance,
                    sender_id=contact.assigned_to if fromMe else None
                )
                db_sql.session.add(new_msg)
                
                # Registra evento SLA de mensagem
                track_sla_event(phone, event_type='ATTENDANT_MSG' if fromMe else 'CLIENT_MSG')
                
                if not fromMe:
                    # Verifica se este cliente estava na fila aguardando resposta
                    pending_reqs = ContactRequest.query.filter_by(status='PENDING').all()
                    req = None
                    for pr in pending_reqs:
                        # Compara os últimos 8 dígitos (ignora DDD e o 9 extra do BR) para evitar falhas
                        if pr.phone[-8:] == phone[-8:]:
                            req = pr
                            break
                            
                    if req:
                        req.status = 'ANSWERED'
                        
                        # Atribuir o chat automaticamente para o atendente
                        # Primeiro tenta buscar por email (novo padrão), se não achar tenta por nome (requests antigas)
                        att_user = User.query.filter_by(email=req.attendant_name).first()
                        if not att_user:
                            att_user = User.query.filter_by(name=req.attendant_name).first()
                            
                        # Notifica o N8N que o cliente respondeu
                        try:
                            n8n_payload = {
                                "phone": req.phone,
                                "attendant": att_user.name if att_user else req.attendant_name,
                                "attendant_email": req.attendant_name,
                                "filial": req.filial,
                                "setor": req.setor,
                                "reason": req.reason,
                                "is_first_time": False,
                                "request_id": req.id
                            }
                            requests.post("https://n8n-n8n.ioms5g.easypanel.host/webhook/chamar-atendente-solicitado", json=n8n_payload, timeout=5)
                        except Exception as e:
                            print(f"Erro N8N atendido: {e}")
                            
                        if att_user:
                            contact.assigned_to = att_user.id
                            contact.assigned_name = att_user.name
                            tags = list(contact.tags or [])
                            tags = [t for t in tags if str(t).upper() != 'BOT']
                            at_tag = f"Atendente: {att_user.email}"
                            if at_tag not in tags: tags.append(at_tag)
                            if att_user.filial and att_user.setor:
                                fst = f"{att_user.filial}:{att_user.setor}"
                                if fst not in tags: tags.append(fst)
                            contact.tags = tags
                            flag_modified(contact, 'tags')
                            
                            # Atualiza AtendimentoChat
                            agora_iso = get_now().isoformat()
                            atend = AtendimentoChat.query.filter_by(numero=phone).first()
                            if atend:
                                atend.atendente = att_user.name
                                atend.status = 'atendente'
                                atend.ultimo_atendente = att_user.name
                                atend.registro_time_chat = agora_iso
                                atend.atendente_desde = agora_iso
                            else:
                                atend = AtendimentoChat(numero=phone, status='atendente', atendente=att_user.name, ultimo_atendente=att_user.name, registro_time_chat=agora_iso, atendente_desde=agora_iso)
                                db_sql.session.add(atend)

            db_sql.session.commit()

            # --- Corpal Webhook (Lead or outgoing message) ---
            try:
                _contact_for_webhook = Contact.query.filter_by(id=contact_id).first()
                _att_user = User.query.get(_contact_for_webhook.assigned_to) if _contact_for_webhook and _contact_for_webhook.assigned_to else None
                _filial_wh = None
                _setor_wh = None
                if _att_user:
                    if _att_user.filial_id:
                        _f = Filial.query.get(_att_user.filial_id)
                        _filial_wh = _f.name if _f else None
                    if _att_user.setor_id:
                        _s = Setor.query.get(_att_user.setor_id)
                        _setor_wh = _s.name if _s else None
                corpal_payload = {
                    "evento": "mensagem",
                    "atendimento_id": str(uuid.uuid4()),
                    "numero_lead": phone,
                    "instancia": instance,
                    "filial": _filial_wh,
                    "setor": _setor_wh,
                    "nome_atendente": _contact_for_webhook.assigned_name if _contact_for_webhook and _contact_for_webhook.assigned_name else "",
                    "atendente_id": str(_att_user.id) if _att_user else None,
                    "direcao": "lead" if not fromMe else "atendente",
                    "mensagem": text,
                    "timestamp": now.isoformat()
                }
                requests.post(CORPAL_WEBHOOK_URL, json=corpal_payload, timeout=5)
            except Exception as corpal_e:
                print(f"Erro webhook corpal (webhook): {corpal_e}")

            # Emitir evento com texto processado para o frontend
            emit_data = dict(data)
            emit_data['_processed_text'] = text
            emit_data['_instance'] = instance
            socketio.emit('whatsapp_event', emit_data, room=f'instance_{instance}')
            socketio.emit('whatsapp_event', emit_data, room='admin')
        return 'OK', 200
    except Exception as e:
        print(f"Erro webhook: {e}")
        return 'ERR', 500

@app.route('/api/agenda', methods=['GET'])
@auth_required
def get_agenda():
    """Retorna todos os contatos com nome válido para a agenda do modal de novo chat."""
    contacts = Contact.query.filter(
        Contact.name != None,
        Contact.name != ''
    ).all()

    seen_phones = set()
    agenda_list = []

    for c in contacts:
        # Considera válido se o nome for diferente do telefone
        if c.name != c.phone and c.phone not in seen_phones:
            seen_phones.add(c.phone)
            agenda_list.append({'name': c.name, 'phone': c.phone})

    # Ordenar alfabeticamente pelo nome
    agenda_list.sort(key=lambda x: (x['name'] or '').lower())
    return jsonify(agenda_list)

@app.route('/api/contacts', methods=['GET'])
@auth_required
def get_contacts():
    user = User.query.get(request.user['id'])
    allowed_instances = get_gestor_allowed_instances(user)
    
    if request.user.get('role') == 'admin':
        # Admin vê todos os chats
        contacts = Contact.query.all()
    else:
        # Buscar contatos das instâncias permitidas
        if allowed_instances:
            contacts = Contact.query.filter(Contact.instance.in_(allowed_instances)).all()
        else:
            contacts = []
        
        # Filtrar por tags de filial:setor conforme cargo
        if user.role == 'gestor':
            # Gestor vê chats de TODOS os setores da sua filial
            filial_name = user.filial
            if not filial_name and user.filial_id:
                f_obj = Filial.query.get(user.filial_id)
                if f_obj: filial_name = f_obj.name
            
            if filial_name:
                # Buscar nomes de todos os setores da filial do gestor
                setores_da_filial = Setor.query.filter_by(filial_id=user.filial_id).all()
                allowed_tags = set()
                for s in setores_da_filial:
                    allowed_tags.add(f"{filial_name}:{s.name}")
                # Também permitir tag só da filial (sem setor)
                allowed_tags.add(filial_name)
                
                # Coletar TODOS os nomes de filiais para detectar tags de outras filiais
                all_filial_names = set(f.name for f in Filial.query.all())
                
                print(f"[GESTOR CONTACTS] user={user.id} filial={filial_name} allowed_tags={allowed_tags}")
                
                filtered = []
                # Buscar IDs de atendentes da mesma filial para ver chats em atendimento
                filial_user_ids = set(u.id for u in User.query.filter_by(filial_id=user.filial_id).all())
                for c in contacts:
                    contact_tags = c.tags or []
                    
                    # Verifica se o contato tem tag filial:setor da minha filial e/ou de outra
                    has_other_filial_tag = False
                    has_my_filial_tag = False
                    has_any_filial_tag = False
                    for t in contact_tags:
                        if ':' in t and not t.lower().startswith('atendente:'):
                            tag_filial = t.split(':')[0]
                            if tag_filial in all_filial_names:
                                has_any_filial_tag = True
                                if t in allowed_tags or tag_filial == filial_name:
                                    has_my_filial_tag = True
                                else:
                                    has_other_filial_tag = True
                        elif t in all_filial_names:
                            has_any_filial_tag = True
                            if t == filial_name:
                                has_my_filial_tag = True
                            else:
                                has_other_filial_tag = True
                    
                    # Excluir apenas se tem tag de outra filial e NENHUMA da minha
                    # (transferências criam tags de múltiplas filiais intencionalmente)
                    if has_other_filial_tag and not has_my_filial_tag:
                        continue
                    
                    # Verifica se alguma tag do contato bate com as tags permitidas
                    has_allowed_tag = any(t in allowed_tags for t in contact_tags)
                    # Também mostra chats atribuídos ao gestor ou a qualquer user da filial
                    is_assigned_to_filial = (c.assigned_to in filial_user_ids) if c.assigned_to else False
                    
                    # Contato precisa ter tag da filial OU estar atribuído a alguém da filial
                    if has_allowed_tag or is_assigned_to_filial:
                        filtered.append(c)
                contacts = filtered
            # Se não tem filial, não filtra por tag (fica vazio pois sem instância)
        else:
            # Usuário comum: vê apenas chats com tag exata da sua filial:setor
            filial_name = user.filial
            setor_name = user.setor
            if not filial_name and user.filial_id:
                f_obj = Filial.query.get(user.filial_id)
                if f_obj: filial_name = f_obj.name
            if not setor_name and user.setor_id:
                s_obj = Setor.query.get(user.setor_id)
                if s_obj: setor_name = s_obj.name
            
            if filial_name and setor_name:
                required_tag = f"{filial_name}:{setor_name}"
                print(f"[USER CONTACTS] user={user.id} required_tag={required_tag}")
                
                filtered = []
                for c in contacts:
                    contact_tags = c.tags or []
                    has_tag = required_tag in contact_tags
                    # Também mostra chats atribuídos ao próprio usuário
                    is_assigned_to_me = (c.assigned_to == user.id)
                    if has_tag or is_assigned_to_me:
                        filtered.append(c)
                contacts = filtered
            # Se não tem filial/setor configurado, não mostra nada
            elif filial_name or setor_name:
                contacts = [c for c in contacts if c.assigned_to == user.id]
        
    contacts_list = []
    for c in contacts:
        contacts_list.append({
            'id': c.id,
            'name': c.name,
            'phone': c.phone,
            'avatar': c.avatar,
            'instance': c.instance,
            'tags': c.tags or [],
            'lastMsg': c.last_msg,
            'time': c.last_msg_time,
            'unread': c.unread,
            'assigned_to': c.assigned_to,
            'assigned_name': c.assigned_name
        })
    return jsonify(contacts_list)

@app.route('/api/contacts/<id>', methods=['PUT'])
@auth_required
def update_contact(id):
    data = request.json
    contact = Contact.query.filter_by(id=id).first()
    
    if not contact:
        return jsonify({'error': 'Contato não encontrado'}), 404
        
    if 'name' in data:
        new_name = data.get('name')
        if not new_name:
            return jsonify({'error': 'Nome é obrigatório'}), 400
        contact.name = new_name
        if contact.avatar and len(contact.avatar) <= 1:
            contact.avatar = new_name[0].upper()
            
    if 'tags' in data:
        contact.tags = data.get('tags')
        flag_modified(contact, 'tags')
        
    db_sql.session.commit()
    
    if 'tags' in data:
        _inst_room = contact.instance or 'unknown'
        socketio.emit('chat_tags_updated', {
            'id': contact.id,
            'tags': list(contact.tags or [])
        }, room=f'instance_{_inst_room}')
        socketio.emit('chat_tags_updated', {
            'id': contact.id,
            'tags': list(contact.tags or [])
        }, room='admin')
        
    return jsonify({
        'id': contact.id,
        'name': contact.name,
        'phone': contact.phone,
        'avatar': contact.avatar,
        'tags': contact.tags
    })


@app.route('/api/contacts', methods=['POST'])
@auth_required
def create_contact():
    data = request.json
    phone = data.get('phone')
    instance = data.get('instance')
    
    if not phone or not instance:
        return jsonify({'error': 'Telefone e Instância são obrigatórios'}), 400
        
    # Normaliza ANTES de validar o tamanho para evitar rejeitar números válidos
    phone = normalize_br_phone(str(phone).strip())
    if len(phone) < 12 or len(phone) > 13:
        return jsonify({'error': 'Formato inválido! Insira DDI + DDD + Número (Ex: 5535999888777)'}), 400
        
    contact_id = f"c_{phone}_{instance}"
    
    contact = Contact.query.filter_by(id=contact_id).first()
    if not contact:
        contact = Contact(
            id=contact_id,
            name=phone,
            phone=phone,
            avatar=phone[0] if phone else "?",
            instance=instance,
            tags=['Novo Lead'],
            last_msg='Iniciando conversa...',
            last_msg_time=get_now().strftime('%H:%M'),
            unread=0
        )
        db_sql.session.add(contact)
    else:
        # Contato já existe — remove tag BOT para o atendente assumir o controle
        current_tags = list(contact.tags or [])
        current_tags = [t for t in current_tags if isinstance(t, str) and t.strip().upper() != 'BOT']
        contact.tags = current_tags
        flag_modified(contact, 'tags')
    
    user = User.query.get(request.user['id'])
    contact.assigned_to = user.id
    contact.assigned_name = user.name
    
    atendente_tag = f"Atendente: {user.name}"
    tags = list(contact.tags or [])
    if atendente_tag not in tags:
        tags.append(atendente_tag)
    
    # Adicionar tag Filial:Setor do atendente para roteamento correto
    if user.filial and user.setor:
        filial_setor_tag = f"{user.filial}:{user.setor}"
        if filial_setor_tag not in tags:
            tags.append(filial_setor_tag)
    elif user.filial_id and user.setor_id:
        _f = Filial.query.get(user.filial_id)
        _s = Setor.query.get(user.setor_id)
        if _f and _s:
            filial_setor_tag = f"{_f.name}:{_s.name}"
            if filial_setor_tag not in tags:
                tags.append(filial_setor_tag)
    
    contact.tags = tags
    flag_modified(contact, 'tags')
    
    db_sql.session.commit()
    
    now = get_now()
    _filial_a = None
    _setor_a = None
    if user.filial_id:
        _f = Filial.query.get(user.filial_id)
        _filial_a = _f.name if _f else None
    if user.setor_id:
        _s = Setor.query.get(user.setor_id)
        _setor_a = _s.name if _s else None
        
    try:
        corpal_payload = {
            "evento": "atender",
            "atendimento_id": str(uuid.uuid4()),
            "numero_lead": contact.phone,
            "instancia": contact.instance,
            "filial": _filial_a,
            "setor": _setor_a,
            "nome_atendente": user.name,
            "atendente_id": str(user.id),
            "direcao": None,
            "mensagem": None,
            "timestamp": now.isoformat()
        }
        requests.post(CORPAL_WEBHOOK_URL, json=corpal_payload, timeout=5)
    except Exception as e:
        print(f"Erro webhook corpal (assign novo chat): {e}")

    # Atualiza tabela atendimentos_chat diretamente para bloquear o bot
    try:
        agora_iso = get_now().isoformat()
        atend_chat = AtendimentoChat.query.filter_by(numero=contact.phone).first()
        if atend_chat:
            status_anterior = atend_chat.status
            atend_chat.atendente = user.name
            atend_chat.status = 'atendente'
            atend_chat.ultimo_atendente = user.name
            atend_chat.registro_time_chat = agora_iso
            if status_anterior != 'atendente':
                atend_chat.atendente_desde = agora_iso
                atend_chat.alerta_20min_enviado = False
                atend_chat.alerta_40min_enviado = False
        else:
            atend_chat = AtendimentoChat(
                numero=contact.phone,
                status='atendente',
                atendente=user.name,
                ultimo_atendente=user.name,
                registro_time_chat=agora_iso,
                atendente_desde=agora_iso,
                alerta_20min_enviado=False,
                alerta_40min_enviado=False
            )
            db_sql.session.add(atend_chat)
        db_sql.session.commit()
        print(f"[NOVO CHAT] atendimentos_chat atualizado: numero={contact.phone}, atendente={user.name}")
    except Exception as e_ac:
        db_sql.session.rollback()
        print(f"Erro ao atualizar atendimentos_chat (novo chat): {e_ac}")

    try:
        if os.getenv('WEBHOOK_ATENDIMENTO_URL'):
            webhook_payload = {
                "evento": "atendimento_iniciado",
                "contato": {
                    "id": contact.id,
                    "phone": contact.phone,
                    "name": contact.name,
                    "instance": contact.instance
                },
                "atendente": {
                    "id": user.id,
                    "name": user.name,
                    "email": user.email
                },
                "timestamp": now.isoformat()
            }
            requests.post(os.getenv('WEBHOOK_ATENDIMENTO_URL'), json=webhook_payload, timeout=5)
    except Exception as e:
        print(f"Erro webhook atendimento (assign novo chat): {e}")

    socketio.emit('chat_assignment', {
        'contact_id': contact.id,
        'assigned_to': user.id,
        'assigned_name': user.name,
        'tags': contact.tags
    }, room=f"instance_{contact.instance or 'unknown'}")
    socketio.emit('chat_assignment', {
        'contact_id': contact.id,
        'assigned_to': user.id,
        'assigned_name': user.name,
        'tags': contact.tags
    }, room='admin')

    return jsonify({
        'id': contact.id,
        'name': contact.name,
        'phone': contact.phone,
        'avatar': contact.avatar,
        'instance': contact.instance,
        'tags': contact.tags,
        'assigned_to': contact.assigned_to,
        'assigned_name': contact.assigned_name
    }), 201

@app.route('/api/contact-requests', methods=['GET'])
@auth_required
def list_contact_requests():
    user = User.query.get(request.user['id'])
    # Retorna apenas as solicitações feitas pelo atendente logado (agora rastreado por email)
    requests_db = ContactRequest.query.filter_by(attendant_name=user.email).order_by(ContactRequest.created_at.desc()).limit(50).all()
    result = []
    for r in requests_db:
        result.append({
            'id': r.id,
            'phone': r.phone,
            'attendant_name': r.attendant_name,
            'reason': r.reason,
            'status': r.status,
            'created_at': r.created_at.isoformat() + 'Z'
        })
    return jsonify(result), 200

@app.route('/api/contact-requests', methods=['POST'])
@auth_required
def create_contact_request():
    data = request.json
    phone = data.get('phone')
    reason = data.get('reason')
    
    if not phone or not reason:
        return jsonify({'error': 'Telefone e Motivo são obrigatórios'}), 400
        
    phone = normalize_br_phone(str(phone).strip())
    if len(phone) < 12 or len(phone) > 13:
        return jsonify({'error': 'Formato inválido! Insira DDI + DDD + Número (Ex: 5535999888777)'}), 400
        
    user = User.query.get(request.user['id'])
    
    # 1. Verificar se o número já está sendo atendido por outra pessoa
    atend_chat = AtendimentoChat.query.filter_by(numero=phone).first()
    if atend_chat and atend_chat.status == 'atendente' and atend_chat.atendente != user.name:
        return jsonify({'error': f'Este número já está em atendimento por {atend_chat.atendente}.'}), 403
        
    # Verificar se já existe uma solicitação pendente para este número
    existing_req = ContactRequest.query.filter_by(phone=phone, status='PENDING').first()
    if existing_req:
        if existing_req.attendant_name != user.email:
            # existing_req.attendant_name stores the email now
            return jsonify({'error': f'Este número já possui uma solicitação pendente por outro atendente ({existing_req.attendant_name}).'}), 403
        else:
            return jsonify({'error': 'Você já possui uma solicitação pendente para este número.'}), 403
        
    # 2. Criar a solicitação
    _f = Filial.query.get(user.filial_id) if user.filial_id else None
    _s = Setor.query.get(user.setor_id) if user.setor_id else None
    
    filial_name = user.filial or (_f.name if _f else '')
    setor_name = user.setor or (_s.name if _s else '')
    
    new_req = ContactRequest(
        phone=phone,
        attendant_name=user.email,  # Mudança crucial: gravamos o EMAIL para diferenciar homônimos
        filial=filial_name,
        setor=setor_name,
        reason=reason,
        status='PENDING',
        is_first_time=True
    )
    db_sql.session.add(new_req)
    db_sql.session.commit()
    
    # 3. Disparar webhook para o N8N (bot de disparo inicial)
    try:
        n8n_payload = {
            "phone": phone,
            "attendant": user.name,
            "attendant_email": user.email, # N8N agora tem acesso ao e-mail exato
            "filial": filial_name,
            "setor": setor_name,
            "reason": reason,
            "is_first_time": True,
            "request_id": new_req.id
        }
        requests.post("https://n8n-n8n.ioms5g.easypanel.host/webhook/atendido", json=n8n_payload, timeout=5)
    except Exception as e:
        print(f"Erro ao notificar N8N da solicitacao: {e}")
        
    return jsonify({'success': True, 'message': 'Solicitação enviada com sucesso!'}), 201


@app.route('/api/contacts/<id>/read', methods=['POST'])
@auth_required
def read_contact(id):
    contact = Contact.query.filter_by(id=id).first()
    if not contact:
        return jsonify({'error': 'Contato não encontrado'}), 404
        
    contact.unread = 0
    db_sql.session.commit()
    return jsonify({'success': True})

@app.route('/api/contacts/<id>/messages', methods=['GET'])
@auth_required
def get_messages(id):
    # Expect id to be the full c_phone_instance string
    msgs = Message.query.filter(Message.contact_id == id).order_by(Message.timestamp).all()
    
    msgs_list = []
    media_msg_ids = []
    
    for m in msgs:
        text = m.text or ''
        msg_id = None
        if text.startswith('[IMAGE_REF] ') or text.startswith('[VIDEO_REF] '):
            ref = text.split('\n')[0].split(' ')[1]
            if '|' in ref:
                msg_id = ref.split('|')[1]
        elif text.startswith('[AUDIO_REF] ') or text.startswith('[DOC_REF] '):
            ref = text.split(' ')[1]
            if '|' in ref:
                msg_id = ref.split('|')[1]
        elif text.startswith('[IMAGE_SENT] ') or text.startswith('[VIDEO_SENT] '):
            ref = text.split(' ')[1]
            if '|' in ref:
                msg_id = ref.split('|')[1]
        
        m_dict = {
            'id': m.id,
            'text': m.text,
            'type': m.type,
            'time': m.time,
            'timestamp': m.timestamp,
            'ack': m.ack if m.ack is not None else 2,
            'media_msg_id': msg_id
        }
        if msg_id:
            media_msg_ids.append(msg_id)
        msgs_list.append(m_dict)
        
    if media_msg_ids:
        try:
            # Busca todas as URLs do MinIO de uma vez
            mfs = MediaFile.query.filter(MediaFile.msg_id.in_(media_msg_ids)).all()
            url_map = {mf.msg_id: mf.storage_url for mf in mfs if mf.storage_url}
            
            # Tentar com short_ids também
            short_ids = [mid.split('_')[-1] for mid in media_msg_ids if '_' in mid]
            if short_ids:
                mfs_short = MediaFile.query.filter(MediaFile.short_id.in_(short_ids)).all()
                for mf in mfs_short:
                    if mf.storage_url and mf.msg_id not in url_map:
                        for mid in media_msg_ids:
                            if mid.endswith(mf.short_id):
                                url_map[mid] = mf.storage_url
                                break
                                
            for m in msgs_list:
                if m.get('media_msg_id') and m['media_msg_id'] in url_map:
                    m['minio_url'] = url_map[m['media_msg_id']]
        except Exception as e:
            print(f"Erro ao buscar MediaFile em get_messages: {e}")

    return jsonify(msgs_list)

# ─── Atendimento (Assign / Release) ─────────────────────────────────────────

@app.route('/api/contacts/<id>/assign', methods=['POST'])
@auth_required
def assign_chat(id):
    """Atender: atribui o chat ao usuário logado."""
    contact = Contact.query.filter_by(id=id).first()
    if not contact:
        return jsonify({'error': 'Contato não encontrado'}), 404
    
    if contact.assigned_to and contact.assigned_to != request.user['id']:
        return jsonify({'error': f'Chat já está sendo atendido por {contact.assigned_name}'}), 409
    
    user = User.query.get(request.user['id'])
    contact.assigned_to = user.id
    contact.assigned_name = user.name
    
    atendente_tag = f"Atendente: {user.name}"
    # Preserva tags de Filial:Setor, remove BOT e tag de atendente anterior
    current_tags = list(contact.tags or [])
    new_tags = [
        t for t in current_tags
        if isinstance(t, str)
        and not t.strip().lower().startswith('atendente:')
        and t.strip().upper() != 'BOT'
    ]
    if atendente_tag not in new_tags:
        new_tags.append(atendente_tag)
    contact.tags = new_tags
    flag_modified(contact, 'tags')
    
    # Atualiza o monitoramento de tempo de espera
    try:
        espera_aberta = TempoEspera.query.filter_by(numero_cliente=contact.phone, atendido=None).order_by(TempoEspera.id.desc()).first()
        if espera_aberta:
            espera_aberta.nome_atendente = user.name
            
            _f = Filial.query.get(user.filial_id) if user.filial_id else None
            _s = Setor.query.get(user.setor_id) if user.setor_id else None
            _f_name = _f.name if _f else ""
            _s_name = _s.name if _s else ""
            
            if _s_name and _f_name:
                espera_aberta.setor_filial = f"{_s_name}:{_f_name}"
            elif _s_name or _f_name:
                espera_aberta.setor_filial = _s_name or _f_name
                
            espera_aberta.atendido = get_now_sp()
            db_sql.session.commit()
            print(f"[TEMPO_ESPERA] Atendido registrado para {contact.phone}")
    except Exception as e_te:
        db_sql.session.rollback()
        print(f"[TEMPO_ESPERA] Erro ao registrar atendido: {e_te}")
    
    db_sql.session.commit()
    
    track_sla_event(contact.phone, atendente=user.name, event_type='ASSIGNED')
    
    # Corpal Webhook — evento atender
    now = get_now()
    _filial_a = None
    _setor_a = None
    if user.filial_id:
        _f = Filial.query.get(user.filial_id)
        _filial_a = _f.name if _f else None
    if user.setor_id:
        _s = Setor.query.get(user.setor_id)
        _setor_a = _s.name if _s else None
        
    try:
        corpal_payload = {
            "evento": "atender",
            "atendimento_id": str(uuid.uuid4()),
            "numero_lead": contact.phone,
            "instancia": contact.instance,
            "filial": _filial_a,
            "setor": _setor_a,
            "nome_atendente": user.name,
            "atendente_id": str(user.id),
            "direcao": None,
            "mensagem": None,
            "timestamp": now.isoformat()
        }
        requests.post(CORPAL_WEBHOOK_URL, json=corpal_payload, timeout=5)
    except Exception as e:
        print(f"Erro webhook corpal (assign): {e}")

    # Atualiza tabela atendimentos_chat diretamente para bloquear o bot
    try:
        agora_iso = get_now().isoformat()
        atend_chat = AtendimentoChat.query.filter_by(numero=contact.phone).first()
        if atend_chat:
            status_anterior = atend_chat.status
            atend_chat.atendente = user.name
            atend_chat.status = 'atendente'
            atend_chat.ultimo_atendente = user.name
            atend_chat.registro_time_chat = agora_iso
            # Só reseta o timer de espera se estava em outro status (ex: bot)
            if status_anterior != 'atendente':
                atend_chat.atendente_desde = agora_iso
                atend_chat.alerta_20min_enviado = False
                atend_chat.alerta_40min_enviado = False
        else:
            atend_chat = AtendimentoChat(
                numero=contact.phone,
                status='atendente',
                atendente=user.name,
                ultimo_atendente=user.name,
                registro_time_chat=agora_iso,
                atendente_desde=agora_iso,
                alerta_20min_enviado=False,
                alerta_40min_enviado=False
            )
            db_sql.session.add(atend_chat)
        db_sql.session.commit()
        print(f"[ASSIGN] atendimentos_chat atualizado: numero={contact.phone}, atendente={user.name}")
    except Exception as e_ac:
        db_sql.session.rollback()
        print(f"Erro ao atualizar atendimentos_chat (assign): {e_ac}")
    
    _inst_room = contact.instance or 'unknown'
    socketio.emit('chat_assignment', {
        'contact_id': id,
        'assigned_to': user.id,
        'assigned_name': user.name,
        'tags': contact.tags,
        'action': 'assign'
    }, room=f'instance_{_inst_room}')
    socketio.emit('chat_assignment', {
        'contact_id': id,
        'assigned_to': user.id,
        'assigned_name': user.name,
        'tags': contact.tags,
        'action': 'assign'
    }, room='admin')
    
    return jsonify({
        'success': True,
        'assigned_to': user.id,
        'assigned_name': user.name,
        'tags': contact.tags
    })

@app.route('/api/contacts/<id>/release', methods=['POST'])
@auth_required
def release_chat(id):
    """Finalizar atendimento: libera o chat."""
    contact = Contact.query.filter_by(id=id).first()
    if not contact:
        return jsonify({'error': 'Contato não encontrado'}), 404
    
    # Apenas o atendente atual ou admin podem finalizar
    if contact.assigned_to and contact.assigned_to != request.user['id'] and request.user.get('role') != 'admin':
        return jsonify({'error': 'Apenas o atendente atual pode finalizar o atendimento'}), 403
    
    user = User.query.get(request.user['id'])
    old_name = contact.assigned_name or user.name
    contact.assigned_to = None
    contact.assigned_name = None
    
    _filial_r = None
    _setor_r = None
    if user:
        if user.filial_id:
            _f = Filial.query.get(user.filial_id)
            _filial_r = _f.name if _f else None
        if user.setor_id:
            _s = Setor.query.get(user.setor_id)
            _setor_r = _s.name if _s else None
            
    # Ao finalizar: remove tag do atendente, mantém Filial:Setor, adiciona BOT
    current_tags = list(contact.tags or [])
    
    # Remove tags de atendente (ex: "Atendente: Fulano") — case-insensitive com strip
    preserved_tags = [
        t for t in current_tags
        if not (isinstance(t, str) and t.strip().lower().startswith('atendente:'))
    ]
    
    # Remove BOT caso já exista (para não duplicar) e adiciona no início
    preserved_tags = [t for t in preserved_tags if t.strip().upper() != 'BOT']
    preserved_tags.insert(0, 'BOT')
    
    contact.tags = preserved_tags
    flag_modified(contact, 'tags')
    
    db_sql.session.commit()
    
    # Atualiza o monitoramento de tempo de espera com o timestamp de finalizacao
    try:
        espera_ativa = TempoEspera.query.filter_by(numero_cliente=contact.phone, finalizado=None).order_by(TempoEspera.id.desc()).first()
        if espera_ativa:
            espera_ativa.finalizado = get_now_sp()
            db_sql.session.commit()
            print(f"[TEMPO_ESPERA] Finalizado registrado para {contact.phone}")
    except Exception as e_te:
        db_sql.session.rollback()
        print(f"[TEMPO_ESPERA] Erro ao registrar finalizado: {e_te}")
    
    # Registra no SLA que o atendimento foi finalizado
    track_sla_event(contact.phone, event_type='RELEASED')
    
    # ── Dispara NPS direto via WAHA e registra estado na tabela atendimentos_chat ──
    try:
        _nps_session = contact.instance or "corpal"
        _nps_options = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "10"]
        poll_msg = (
            "Obrigado por entrar em contato com a Corpal! 🌱\n\n"
            "Para continuarmos melhorando nosso atendimento, avalie sua experiência de *1 a 10*, sendo:\n\n"
            "*1 = Muito insatisfeito*\n"
            "*10 = Muito satisfeito*\n\n"
            "Sua opinião é muito importante para nós."
        )
        poll_resp = requests.post(
            f"{WAHA_API_URL}/api/sendPoll",
            headers=get_waha_headers(),
            json={
                "chatId": f"{contact.phone}@c.us",
                "poll": {
                    "name": poll_msg,
                    "options": _nps_options,
                    "multipleAnswers": False
                },
                "session": _nps_session
            },
            timeout=10
        )
        # Registra o estado NPS na tabela atendimentos_chat
        atend_chat_rel = AtendimentoChat.query.filter_by(numero=contact.phone).first()
        if atend_chat_rel:
            atend_chat_rel.status = 'bot'
            atend_chat_rel.atendente = ''
            atend_chat_rel.atendente_desde = None
            atend_chat_rel.alerta_20min_enviado = False
            atend_chat_rel.alerta_40min_enviado = False
            atend_chat_rel.nps_status = 'waiting_vote'
            atend_chat_rel.nps_started_at = get_now().isoformat()
            atend_chat_rel.nps_voto = None
            # Captura o ID da enquete se o WAHA retornar
            try:
                _poll_id = poll_resp.json().get('id') if poll_resp.ok else None
                atend_chat_rel.nps_poll_id = _poll_id
            except Exception:
                pass
            db_sql.session.commit()
            print(f"[NPS] Poll enviado e estado 'waiting_vote' registrado para {contact.phone}")
        else:
            print(f"[NPS] Registro atendimentos_chat não encontrado para {contact.phone} — NPS não registrado")
    except Exception as nps_e:
        db_sql.session.rollback()
        print(f"[NPS] Erro ao disparar NPS: {nps_e}")

    
    # Corpal Webhook — evento finalizar
    try:
        now = get_now()
        corpal_payload = {
            "evento": "finalizar",
            "atendimento_id": str(uuid.uuid4()),
            "numero_lead": contact.phone,
            "instancia": contact.instance,
            "filial": _filial_r,
            "setor": _setor_r,
            "nome_atendente": old_name,
            "atendente_id": str(request.user['id']),
            "direcao": None,
            "mensagem": None,
            "timestamp": now.isoformat()
        }
        requests.post(CORPAL_WEBHOOK_URL, json=corpal_payload, timeout=5)
        
        # Novo Webhook específico para finalizar atendimento
        try:
            n8n_final_payload = {
                "numero_lead": contact.phone,
                "nome_atendente": old_name,
                "setor": _setor_r,
                "filial": _filial_r
            }
            requests.post("https://n8n-n8n.ioms5g.easypanel.host/webhook/corpal-final-atendimento", json=n8n_final_payload, timeout=5)
        except Exception as e_n8n:
            print(f"Erro no webhook n8n-final-atendimento: {e_n8n}")
    except Exception as e:
        print(f"Erro webhook corpal (release): {e}")
    
    # Emitir socket para todos os clientes atualizarem
    _inst_room = contact.instance or 'unknown'
    socketio.emit('chat_assignment', {
        'contact_id': id,
        'assigned_to': None,
        'assigned_name': None,
        'tags': contact.tags,
        'action': 'release'
    }, room=f'instance_{_inst_room}')
    socketio.emit('chat_assignment', {
        'contact_id': id,
        'assigned_to': None,
        'assigned_name': None,
        'tags': contact.tags,
        'action': 'release'
    }, room='admin')
    
    return jsonify({
        'success': True,
        'tags': contact.tags
    })

@app.route('/api/admin/settings', methods=['GET', 'POST'])
@auth_required
@admin_required
def manage_settings():
    if request.method == 'POST':
        data = request.json
        for k, v in data.items():
            setting = Setting.query.get(k)
            if setting:
                setting.value = str(v)
            else:
                db_sql.session.add(Setting(key=k, value=str(v)))
        db_sql.session.commit()
        
    all_s = Setting.query.all()
    return jsonify({s.key: s.value for s in all_s})

@app.route('/api/admin/deduplicate', methods=['POST'])
@auth_required
@admin_required
def api_deduplicate():
    try:
        from limpar_duplicados import run_deduplication
        stats = run_deduplication()
        return jsonify(stats)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/migrate-filial-names', methods=['POST'])
@auth_required
@admin_required
def api_migrate_filial_names():
    try:
        # Criar a coluna no banco se não existir
        try:
            db_sql.session.execute(db_sql.text("ALTER TABLE setor ADD COLUMN filial_name VARCHAR(100)"))
            db_sql.session.commit()
        except Exception:
            db_sql.session.rollback()  # Coluna já existe, segue normal

        setores = Setor.query.all()
        count = 0
        for s in setores:
            filial = Filial.query.get(s.filial_id)
            if filial:
                s.filial_name = filial.name
                count += 1
        db_sql.session.commit()
        return jsonify({'updated': count})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/fix-13-digits', methods=['POST'])
@auth_required
@admin_required
def api_fix_13_digits():
    try:
        updated_contacts = 0
        contacts = Contact.query.all()
        for c in contacts:
            number = c.phone
            if number and len(number) == 13 and number.startswith('55') and number[4] == '9':
                new_number = number[:4] + number[5:]
                old_id = c.id
                new_id = f"c_{new_number}_{c.instance}"
                
                # Verificar se o contato novo já existe, senao cria
                new_contact = Contact.query.get(new_id)
                if not new_contact:
                    new_contact = Contact(
                        id=new_id,
                        name=new_number if c.name == number else c.name,
                        phone=new_number,
                        avatar=c.avatar,
                        instance=c.instance,
                        tags=c.tags,
                        last_msg=c.last_msg,
                        last_msg_time=c.last_msg_time,
                        unread=c.unread,
                        assigned_to=c.assigned_to,
                        assigned_name=c.assigned_name
                    )
                    db_sql.session.add(new_contact)
                    db_sql.session.flush() # Salva no banco para que a foreign key seja satisfeita
                
                # Atualizar as mensagens para apontar para o novo contato
                Message.query.filter_by(contact_id=old_id).update({"contact_id": new_id})
                
                # Deletar o contato antigo
                db_sql.session.delete(c)
                updated_contacts += 1
        
        db_sql.session.commit()
        return jsonify({'success': True, 'updated': updated_contacts})
    except Exception as e:
        db_sql.session.rollback()
        return jsonify({'error': str(e), 'success': False}), 500

@app.route('/api/admin/migrate-to-corpal', methods=['POST'])
@auth_required
@admin_required
def api_migrate_to_corpal():
    try:
        updated_contacts = 0
        contacts = Contact.query.all()
        for c in contacts:
            if c.instance != 'corpal':
                old_id = c.id
                new_id = f"c_{c.phone}_corpal"
                
                new_contact = Contact.query.get(new_id)
                if not new_contact:
                    new_contact = Contact(
                        id=new_id,
                        name=c.name,
                        phone=c.phone,
                        avatar=c.avatar,
                        instance='corpal',
                        tags=c.tags,
                        last_msg=c.last_msg,
                        last_msg_time=c.last_msg_time,
                        unread=c.unread,
                        assigned_to=c.assigned_to,
                        assigned_name=c.assigned_name
                    )
                    db_sql.session.add(new_contact)
                    db_sql.session.flush()
                
                # Update messages
                Message.query.filter_by(contact_id=old_id).update({
                    "contact_id": new_id,
                    "instance": "corpal"
                })
                
                db_sql.session.delete(c)
                updated_contacts += 1
                
        db_sql.session.commit()
        return jsonify({'success': True, 'updated': updated_contacts})
    except Exception as e:
        db_sql.session.rollback()
        return jsonify({'error': str(e), 'success': False}), 500

@app.route('/api/chat/<path:contact_id>', methods=['DELETE'])
@auth_required
def api_delete_chat(contact_id):
    # Opcional: verificar se o usuário é admin. Por enquanto vou deixar o gestor e admin apagarem.
    try:
        contact = Contact.query.filter_by(id=contact_id).first()
        if not contact:
            return jsonify({'error': 'Contato não encontrado'}), 404
        
        # Deletar todas as mensagens vinculadas a esse contato
        Message.query.filter_by(contact_id=contact_id).delete()
        
        # Deletar o contato
        db_sql.session.delete(contact)
        db_sql.session.commit()
        
        return jsonify({'success': True}), 200
    except Exception as e:
        db_sql.session.rollback()
        return jsonify({'error': str(e), 'success': False}), 500

@app.route('/api/chat/transfer', methods=['POST'])
@auth_required
def chat_transfer():
    data = request.json
    contact_id = data.get('contact_id')
    filial = data.get('filial')
    setor = data.get('setor')
    
    if not contact_id or not filial or not setor:
        return jsonify({'error': 'Parâmetros inválidos (contact_id, filial, setor são obrigatórios)'}), 400
        
    contact = Contact.query.get(contact_id)
    if not contact:
        return jsonify({'error': 'Contato não encontrado'}), 404
        
    user = User.query.get(request.user['id'])
    
    # Bloquear transferência se chat está sendo atendido por outra pessoa
    if contact.assigned_to and contact.assigned_to != user.id:
        return jsonify({'error': 'Este chat já está sendo atendido por outra pessoa'}), 403
        
    # Regra removida: Todos os usuários podem transferir para qualquer filial e setor
    # if user.role == 'user':
    #     if not user.filial or user.filial != filial:
    #         return jsonify({'error': 'Você só pode transferir para a sua própria filial'}), 403
        
    # Atualiza as tags para refletir o novo setor de destino
    # MANTÉM a tag do setor de origem (do usuário que fez a transferência)
    # para que o setor de origem ainda possa visualizar/acompanhar o chat
    tag_destino = f"{filial}:{setor}"
    tag_origem = None
    if user.filial and user.setor:
        tag_origem = f"{user.filial}:{user.setor}"
    
    current_tags = list(contact.tags or [])
    # Remove apenas tags de atendente e BOT; mantém outras tags filial:setor existentes
    new_tags = [
        t for t in current_tags
        if isinstance(t, str)
        and not t.strip().lower().startswith('atendente:')
        and t.strip().upper() != 'BOT'
    ]
    # Adiciona a tag de destino se ainda não existir
    if tag_destino not in new_tags:
        new_tags.append(tag_destino)
    # Garante que a tag de origem do usuário que transferiu está presente (para leitura)
    if tag_origem and tag_origem not in new_tags:
        new_tags.append(tag_origem)
    
    contact.tags = new_tags
    flag_modified(contact, 'tags')
        
    # Liberar o atendimento (remover assigned_to)
    if contact.assigned_to:
        contact.assigned_to = None
        contact.assigned_name = None
    
    db_sql.session.commit()
    
    # Registra no SLA que o chat entrou na fila de transferência
    track_sla_event(contact.phone, filial=filial, setor=setor, event_type='QUEUE_ENTER')

    # Ao transferir: reinicia o timer de espera (contagem começa do zero a partir da transferência)
    try:
        agora_tr_iso = get_now().isoformat()
        atend_chat_tr = AtendimentoChat.query.filter_by(numero=contact.phone).first()
        if atend_chat_tr:
            atend_chat_tr.status = 'atendente'      # mantém 'atendente' para o monitor rastrear
            atend_chat_tr.atendente = None           # sem atendente fixo (aguardando novo)
            atend_chat_tr.atendente_desde = agora_tr_iso  # timer reinicia agora
            atend_chat_tr.alerta_20min_enviado = False
            atend_chat_tr.alerta_40min_enviado = False
            atend_chat_tr.ultimo_setor = setor       # registra setor de destino
            db_sql.session.commit()
            print(f"[TRANSFER] Timer de espera reiniciado para {contact.phone} → {filial}/{setor}")
    except Exception as e_tr:
        db_sql.session.rollback()
        print(f"Erro ao resetar alertas na transferência: {e_tr}")

    n8n_webhook_url = "https://n8n-n8n.ioms5g.easypanel.host/webhook/chamar"
    
    payload = {
        "numero": contact.phone,
        "filial": filial,
        "setor": setor
    }
    
    # Dispara o webhook em background ou após o commit, para que o N8N leia o banco já atualizado
    try:
        res = requests.post(n8n_webhook_url, json=payload, timeout=10)
        res.raise_for_status()
    except Exception as e:
        print(f"Erro ao disparar webhook de transferência n8n: {e}")
        return jsonify({'error': 'Erro ao comunicar com n8n (mas chat foi transferido)'}), 500
    
    # Emite evento para os clientes atualizarem
    socketio.emit('chat_tags_updated', {
        'id': contact.id,
        'tags': list(contact.tags or [])
    }, room=f"instance_{contact.instance or 'unknown'}")
    
    socketio.emit('chat_tags_updated', {
        'id': contact.id,
        'tags': list(contact.tags or [])
    }, room='admin')
    
    socketio.emit('chat_assignment', {
        'contact_id': contact.id,
        'assigned_to': None,
        'assigned_name': None,
        'tags': list(contact.tags or [])
    }, room=f"instance_{contact.instance or 'unknown'}")
    
    socketio.emit('chat_assignment', {
        'contact_id': contact.id,
        'assigned_to': None,
        'assigned_name': None,
        'tags': list(contact.tags or [])
    }, room='admin')
    
    return jsonify({'success': True})

@app.route('/api/media/<media_type>')
def stream_media(media_type):
    """Proxy de midia: busca o arquivo da WAHA e retorna como stream.
    Aceita token via query param porque a tag media pode nao enviar headers customizados."""
    token = request.args.get('token') or (request.headers.get('Authorization', '').replace('Bearer ', ''))
    if not token:
        return jsonify({'error': 'Token obrigatorio'}), 401
    try:
        jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except Exception:
        return jsonify({'error': 'Token invalido'}), 401

    instance = request.args.get('instance')
    msg_id = request.args.get('msg_id')
    if not instance or not msg_id:
        return jsonify({'error': 'instance e msg_id sao obrigatorios'}), 400
    try:
        # Default mimetypes based on requested media_type
        content_type = 'application/octet-stream'
        if media_type == 'audio': content_type = 'audio/ogg'
        elif media_type == 'image': content_type = 'image/jpeg'
        elif media_type == 'video': content_type = 'video/mp4'

        # Verificar se o arquivo existe localmente
        media_dir = os.path.join(DATA_DIR, 'media')
        local_path = os.path.join(media_dir, msg_id)
        short_id = msg_id.split('_')[-1] if '_' in msg_id else msg_id
        local_path_short = os.path.join(media_dir, short_id)
        
        import glob
        cache_path = None
        
        # 1. Buscar no banco de dados — se tem storage_url (MinIO), redirecionar
        try:
            short_id_db = msg_id.split('_')[-1] if '_' in msg_id else msg_id
            mf = MediaFile.query.filter((MediaFile.msg_id == msg_id) | (MediaFile.short_id == short_id_db)).first()
            if mf:
                # Se tem URL do MinIO, redirecionar diretamente (mais rápido)
                if mf.storage_url:
                    from flask import redirect
                    print(f"[{media_type.capitalize()} Proxy] Redirecionando para MinIO: {mf.storage_url}")
                    return redirect(mf.storage_url)
                # Se não tem MinIO mas tem no disco local
                if mf.filename:
                    db_path = os.path.join(media_dir, mf.filename)
                    if os.path.exists(db_path):
                        cache_path = db_path
                        if mf.mimetype:
                            content_type = mf.mimetype
        except Exception as e_db_proxy:
            print(f"[{media_type.capitalize()} Proxy] Erro ao consultar banco: {e_db_proxy}")
            
        # 2. Busca com glob (fallback se não estiver no banco, mas existir no disco legado)
        if not cache_path:
            matches = glob.glob(glob.escape(local_path) + '.*')
            if os.path.exists(local_path): matches.insert(0, local_path)
            matches_short = glob.glob(glob.escape(local_path_short) + '.*')
            if os.path.exists(local_path_short): matches_short.insert(0, local_path_short)
            for p in matches + matches_short:
                cache_path = p
                break
            
        if cache_path:
            print(f"[{media_type.capitalize()} Proxy] Cache local encontrado: {cache_path}")
            if media_type == 'audio':
                try:
                    with open(cache_path, 'rb') as f: header = f.read(4)
                    if header.startswith(b'OggS'): content_type = 'audio/ogg'
                    elif header.startswith(b'\x1aE\xdf\xa3'): content_type = 'audio/webm'
                    else: content_type = 'audio/webm'
                except: content_type = 'audio/webm'
            elif media_type == 'document':
                import mimetypes
                guess, _ = mimetypes.guess_type(cache_path)
                if guess: content_type = guess
                else: content_type = 'application/octet-stream'
            
            with open(cache_path, 'rb') as f: file_bytes = f.read()
            
            # Fazer upload para o MinIO de forma síncrona para que a resposta já seja o redirecionamento
            try:
                print(f"[{media_type.capitalize()} Proxy] Enviando arquivo de cache local para o MinIO: {cache_path}")
                _, minio_url = save_media_file(msg_id, file_bytes, media_type, instance=instance, mimetype=content_type)
                if minio_url:
                    from flask import redirect
                    print(f"[{media_type.capitalize()} Proxy] Upload OK! Redirecionando para MinIO: {minio_url}")
                    return redirect(minio_url)
            except Exception as e_cache_upload:
                print(f"[{media_type.capitalize()} Proxy] Erro ao enviar cache local para o MinIO: {e_cache_upload}")

            cd = 'inline' if media_type in ('audio', 'image', 'video', 'document') else 'attachment'
            return Response(file_bytes, mimetype=content_type, headers={'Content-Disposition': cd, 'Accept-Ranges': 'bytes', 'Cache-Control': 'public, max-age=3600'})

        # 3. Fallback final para WAHA: Baixa, serve como byte stream e salva no MinIO em background (NUNCA expõe a URL)
        print(f"[{media_type.capitalize()} Proxy] Arquivo não encontrado no MinIO/Disco. Buscando do WAHA em tempo real: {msg_id}")
        waha_url_1 = f"{WAHA_API_URL}/api/files"
        import requests
        res = requests.get(waha_url_1, headers=get_waha_headers(), params={'session': instance, 'messageId': msg_id}, timeout=15)
        if res.status_code == 404 and short_id != msg_id:
            res = requests.get(waha_url_1, headers=get_waha_headers(), params={'session': instance, 'messageId': short_id}, timeout=15)
        
        if res.status_code == 200:
            ctype = res.headers.get('Content-Type', '')
            file_bytes = None
            if 'application/json' in ctype:
                import base64, re
                json_data = res.json()
                if 'data' in json_data:
                    raw = json_data['data']
                    raw = re.sub(r'[^A-Za-z0-9+/]', '', raw)
                    raw += "=" * ((4 - len(raw) % 4) % 4)
                    file_bytes = base64.b64decode(raw)
                elif 'url' in json_data:
                    real_url = json_data['url']
                    if real_url.startswith('http://localhost') or real_url.startswith('http://127.0.0.1'):
                        from urllib.parse import urlparse
                        real_url = f"{WAHA_API_URL}{urlparse(real_url).path}"
                    
                    real_res = requests.get(real_url, headers=get_waha_headers(), timeout=15)
                    if real_res.status_code == 200:
                        file_bytes = real_res.content
                        ctype = real_res.headers.get('Content-Type', '') or content_type
            else:
                file_bytes = res.content
                
            if file_bytes:
                # Salvar no banco/MinIO de forma assíncrona para não bloquear a resposta!
                import threading
                def _bg_save(w_id, w_bytes, w_type, w_inst, w_ctype):
                    with app.app_context():
                        save_media_file(w_id, w_bytes, w_type, instance=w_inst, mimetype=w_ctype)
                threading.Thread(target=_bg_save, args=(msg_id, file_bytes, media_type, instance, ctype)).start()

                cd = 'inline' if media_type in ('audio', 'image', 'video', 'document') else 'attachment'
                return Response(file_bytes, mimetype=ctype, headers={'Content-Disposition': cd, 'Accept-Ranges': 'bytes', 'Cache-Control': 'public, max-age=3600'})

        print(f"[{media_type.capitalize()} Proxy] FALHA DEFINITIVA. Não está no MinIO, Disco ou WAHA: {msg_id}")
        return jsonify({'error': 'Arquivo não encontrado na api ou disco.'}), 404
    except Exception as e:
        print(f"Erro stream_{media_type}: {e}")
        return jsonify({'error': str(e)}), 500

# ─── Admin: Media Browser ────────────────────────────────────────────────────

@app.route('/api/media/prefetch', methods=['POST'])
@auth_required
def prefetch_media():
    """Pré-baixa mídias do WAHA e salva no MinIO em background.
    Aceita uma lista de {instance, msg_id} para pré-carregar."""
    data = request.json
    if not data:
        return jsonify({'error': 'Body vazio'}), 400
    
    items = data.get('items', [])
    if not items:
        return jsonify({'error': 'items é obrigatório (lista de {instance, msg_id})'}), 400
    
    # Limitar a 20 itens por requisição
    items = items[:20]
    queued = 0
    
    import threading as _thr
    for item in items:
        inst = item.get('instance')
        mid = item.get('msg_id')
        if inst and mid:
            t = _thr.Thread(target=_prefetch_media_worker, args=(inst, mid), daemon=True)
            t.start()
            queued += 1
    
    return jsonify({'status': 'queued', 'count': queued})


def _prefetch_media_worker(instance, msg_id):
    """Worker thread: baixa mídia do WAHA e salva no MinIO."""
    try:
        with app.app_context():
            short_id = msg_id.split('_')[-1] if '_' in msg_id else msg_id
            
            # 1. Verificar se já existe no MinIO (via banco)
            mf = MediaFile.query.filter(
                (MediaFile.msg_id == msg_id) | (MediaFile.short_id == short_id)
            ).first()
            if mf and mf.storage_url:
                return  # Já está no MinIO
            
            # 2. Verificar se existe localmente mas sem MinIO — fazer upload
            if mf and mf.filename:
                media_dir = os.path.join(DATA_DIR, 'media')
                local_path = os.path.join(media_dir, mf.filename)
                if os.path.exists(local_path):
                    with open(local_path, 'rb') as f:
                        file_bytes = f.read()
                    upload_ct = mf.mimetype or 'application/octet-stream'
                    minio_url = upload_to_minio(mf.filename, file_bytes, content_type=upload_ct)
                    if minio_url:
                        mf.storage_url = minio_url
                        db_sql.session.commit()
                        print(f"[Prefetch] Disco local -> MinIO OK: {mf.filename}")
                    return
            
            # 3. Buscar do WAHA via /api/files
            waha_url = f"{WAHA_API_URL}/api/files"
            res = requests.get(waha_url, headers=get_waha_headers(), params={'session': instance, 'messageId': msg_id}, timeout=15)
            
            if res.status_code == 404 and short_id != msg_id:
                res = requests.get(waha_url, headers=get_waha_headers(), params={'session': instance, 'messageId': short_id}, timeout=15)
            
            if res.status_code == 200:
                file_bytes = res.content
                ctype = res.headers.get('Content-Type', '')
                
                # Se WAHA retornou JSON
                if 'application/json' in ctype:
                    import base64, re
                    json_data = res.json()
                    real_mimetype = json_data.get('mimetype', ctype)
                    if 'data' in json_data:
                        raw = json_data['data']
                        raw = re.sub(r'[^A-Za-z0-9+/]', '', raw)
                        raw += "=" * ((4 - len(raw) % 4) % 4)
                        file_bytes = base64.b64decode(raw)
                        ctype = real_mimetype
                    elif 'url' in json_data:
                        real_url = json_data['url']
                        if real_url.startswith('http://localhost') or real_url.startswith('http://127.0.0.1'):
                            parsed = urlparse(real_url)
                            real_url = f"{WAHA_API_URL}{parsed.path}"
                        real_res = requests.get(real_url, headers=get_waha_headers(), timeout=15)
                        if real_res.status_code == 200:
                            file_bytes = real_res.content
                            ctype = real_res.headers.get('Content-Type', '') or real_mimetype
                        else:
                            print(f"[Prefetch] Falha ao baixar URL {real_url}: HTTP {real_res.status_code}")
                            return
                
                # Determinar tipo de mídia
                m_type = 'document'
                if ctype.startswith('image/'): m_type = 'image'
                elif ctype.startswith('audio/'): m_type = 'audio'
                elif ctype.startswith('video/'): m_type = 'video'
                
                saved_fn, saved_url = save_media_file(msg_id, file_bytes, m_type, instance=instance, mimetype=ctype)
                if saved_url:
                    print(f"[Prefetch] WAHA -> MinIO OK: {saved_fn}")
                elif saved_fn:
                    print(f"[Prefetch] WAHA -> Local OK (MinIO falhou): {saved_fn}")
                else:
                    print(f"[Prefetch] FALHA ao salvar: msg_id={msg_id}")
            else:
                print(f"[Prefetch] WAHA retornou {res.status_code} para msg_id={msg_id}")
    except Exception as e:
        print(f"[Prefetch] Erro: {e}")


@app.route('/api/admin/media', methods=['GET'])
@auth_required
def admin_list_media():
    """Lista arquivos de mídia armazenados no banco de dados com paginação e filtros. Apenas admin."""
    if request.user.get('role') != 'admin':
        return jsonify({'error': 'Acesso negado'}), 403

    filter_type = request.args.get('type', 'all')  # all, audio, image, video, document
    page = max(1, int(request.args.get('page', 1)))
    per_page = min(100, max(10, int(request.args.get('per_page', 50))))
    search = request.args.get('search', '').strip().lower()

    query = MediaFile.query

    if filter_type != 'all':
        query = query.filter(MediaFile.media_type == filter_type)
    
    if search:
        query = query.filter(
            (MediaFile.filename.ilike(f"%{search}%")) |
            (MediaFile.original_filename.ilike(f"%{search}%")) |
            (MediaFile.msg_id.ilike(f"%{search}%"))
        )

    # Ordernar por mais recentes
    query = query.order_by(MediaFile.id.desc())

    # Paginação
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    
    # Contagem total e por tipo
    total = pagination.total
    total_size = db_sql.session.query(db_sql.func.sum(MediaFile.file_size)).scalar() or 0

    page_files = []
    for mf in pagination.items:
        _, ext = os.path.splitext(mf.filename) if mf.filename else ('', '')
        page_files.append({
            'name': mf.filename or f"{mf.msg_id}{ext}",
            'size': mf.file_size or 0,
            'modified': mf.created_at, # Usando data de criação como timestamp
            'type': mf.media_type or 'unknown',
            'ext': ext.lower() or '(sem ext)',
            'storage_url': mf.storage_url
        })

    # Stats para UI
    type_counts_db = db_sql.session.query(MediaFile.media_type, db_sql.func.count(MediaFile.id)).group_by(MediaFile.media_type).all()
    type_counts = {t: c for t, c in type_counts_db}

    return jsonify({
        'files': page_files,
        'total': total,
        'total_size': total_size,
        'page': page,
        'per_page': per_page,
        'total_pages': pagination.pages,
        'type_counts': type_counts
    })

@app.route('/api/admin/media/serve/<path:filename>')
def admin_serve_media(filename):
    """Serve um arquivo de mídia diretamente. Apenas admin.
    Aceita token via query param porque tags <audio>/<img>/<video> não enviam headers customizados."""
    token = request.args.get('token') or (request.headers.get('Authorization', '').replace('Bearer ', ''))
    if not token:
        return jsonify({'error': 'Token obrigatório'}), 401
    try:
        user_data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except Exception:
        return jsonify({'error': 'Token inválido'}), 401
    if user_data.get('role') != 'admin':
        return jsonify({'error': 'Acesso negado'}), 403

    import mimetypes as _mt
    media_dir = os.path.join(DATA_DIR, 'media')

    # Verificar se existe no MinIO (via banco de dados)
    try:
        base_name = os.path.splitext(filename)[0]
        mf = MediaFile.query.filter(
            (MediaFile.filename == filename) | (MediaFile.msg_id == base_name) | (MediaFile.short_id == base_name)
        ).first()
        if mf and mf.storage_url:
            from flask import redirect
            return redirect(mf.storage_url)
    except Exception:
        pass

    filepath = os.path.join(media_dir, filename)

    # Segurança: impedir path traversal
    if not os.path.abspath(filepath).startswith(os.path.abspath(media_dir)):
        return jsonify({'error': 'Caminho inválido'}), 400

    if not os.path.exists(filepath):
        return jsonify({'error': 'Arquivo não encontrado'}), 404

    with open(filepath, 'rb') as f:
        file_data = f.read()

    # Detectar content_type por magic bytes primeiro, fallback para extensão
    _, ext = os.path.splitext(filename)
    ext_lower = ext.lower()

    content_type = 'application/octet-stream'
    if file_data[:4] == b'OggS':
        content_type = 'audio/ogg'
    elif file_data[:4] == b'\x1aE\xdf\xa3':  # WebM/Matroska
        content_type = 'audio/webm'
    elif file_data[:3] == b'ID3' or file_data[:2] == b'\xff\xfb':
        content_type = 'audio/mpeg'
    elif file_data[:8] == b'\x89PNG\r\n\x1a\n':
        content_type = 'image/png'
    elif file_data[:2] == b'\xff\xd8':
        content_type = 'image/jpeg'
    elif file_data[:4] == b'RIFF' and file_data[8:12] == b'WEBP':
        content_type = 'image/webp'
    elif file_data[:3] == b'GIF':
        content_type = 'image/gif'
    elif file_data[4:8] == b'ftyp':  # MP4/M4A
        # Distinguir vídeo de áudio M4A
        ftyp_brand = file_data[8:12]
        if ftyp_brand in (b'M4A ', b'M4B '):
            content_type = 'audio/mp4'
        else:
            content_type = 'video/mp4'
    elif file_data[:4] == b'%PDF':
        content_type = 'application/pdf'
    else:
        # Fallback para extensão
        if ext_lower in ('.oga', '.ogg', '.opus'):
            content_type = 'audio/ogg'
        elif ext_lower == '.webm':
            content_type = 'audio/webm'
        elif ext_lower in ('.mp3',):
            content_type = 'audio/mpeg'
        elif ext_lower in ('.jpeg', '.jpg'):
            content_type = 'image/jpeg'
        elif ext_lower == '.png':
            content_type = 'image/png'
        elif ext_lower == '.webp':
            content_type = 'image/webp'
        elif ext_lower == '.mp4':
            content_type = 'video/mp4'
        elif ext_lower == '.pdf':
            content_type = 'application/pdf'
        else:
            guess, _ = _mt.guess_type(filepath)
            if guess:
                content_type = guess

    return Response(file_data, mimetype=content_type, headers={
        'Content-Disposition': 'inline',
        'Accept-Ranges': 'bytes',
        'Cache-Control': 'public, max-age=3600'
    })

@app.route('/api/admin/media/delete', methods=['POST'])
@auth_required
def admin_delete_media():
    """Deleta arquivos de mídia. Apenas admin."""
    if request.user.get('role') != 'admin':
        return jsonify({'error': 'Acesso negado'}), 403

    data = request.json
    filenames = data.get('filenames', [])
    if not filenames:
        return jsonify({'error': 'Nenhum arquivo especificado'}), 400

    media_dir = os.path.join(DATA_DIR, 'media')
    deleted = 0
    for fn in filenames:
        # Deletar do MinIO
        try:
            base_name = os.path.splitext(fn)[0]
            mf = MediaFile.query.filter(
                (MediaFile.filename == fn) | (MediaFile.msg_id == base_name) | (MediaFile.short_id == base_name)
            ).first()
            if mf:
                if mf.storage_url:
                    delete_from_minio(fn)
                db_sql.session.delete(mf)
                db_sql.session.commit()
        except Exception as e_del_minio:
            db_sql.session.rollback()
            print(f"[Admin Delete] Erro ao remover do MinIO/DB: {e_del_minio}")
        
        # Deletar do disco local
        fp = os.path.join(media_dir, fn)
        if os.path.abspath(fp).startswith(os.path.abspath(media_dir)) and os.path.exists(fp):
            try:
                os.remove(fp)
                deleted += 1
            except Exception:
                pass

    return jsonify({'deleted': deleted})

@app.route('/api/admin/media/stats', methods=['GET'])
@auth_required
def admin_media_stats():
    """Retorna estatísticas da mídia armazenada (baseado no banco de dados). Apenas admin."""
    if request.user.get('role') != 'admin':
        return jsonify({'error': 'Acesso negado'}), 403

    total_files = MediaFile.query.count()
    total_size = db_sql.session.query(db_sql.func.sum(MediaFile.file_size)).scalar() or 0

    types_db = db_sql.session.query(MediaFile.media_type, db_sql.func.count(MediaFile.id)).group_by(MediaFile.media_type).all()
    types = {t: c for t, c in types_db if t}

    return jsonify({
        'total_files': total_files,
        'total_size': total_size,
        'types': types
    })

@app.route('/api/admin/force-reload', methods=['POST'])
@auth_required
def admin_force_reload():
    """Força todos os usuários logados a deslogar e recarregar a página. Apenas admin."""
    if request.user.get('role') != 'admin':
        return jsonify({'error': 'Acesso negado'}), 403

    socketio.emit('force_logout_reload') # Broadcast para todos os clientes conectados
    return jsonify({'success': True})

@app.route('/api/debug/test-alerta-espera', methods=['POST'])
def test_alerta_espera():
    """Rota de teste: simula um cliente esperando ha X minutos.
    Body JSON: { "numero": "5535999...", "minutos": 25, "atendente": "Teste" }
    Isso forca o monitor a disparar na proxima varredura (ate 60s).
    """
    data = request.json or {}
    numero = data.get('numero', '5500000000000')
    minutos = int(data.get('minutos', 21))
    atendente_nome = data.get('atendente', 'Atendente Teste')

    # Calcula o timestamp simulado (agora - X minutos)
    desde = get_now() - datetime.timedelta(minutes=minutos)
    desde_iso = desde.isoformat()

    try:
        reg = AtendimentoChat.query.filter_by(numero=numero).first()
        if reg:
            reg.status = 'atendente'
            reg.atendente = atendente_nome
            reg.atendente_desde = desde_iso
            reg.alerta_20min_enviado = False
            reg.alerta_40min_enviado = False
            reg.registro_time_chat = desde_iso
        else:
            reg = AtendimentoChat(
                numero=numero,
                status='atendente',
                atendente=atendente_nome,
                atendente_desde=desde_iso,
                registro_time_chat=desde_iso,
                alerta_20min_enviado=False,
                alerta_40min_enviado=False
            )
            db_sql.session.add(reg)
        db_sql.session.commit()
        return jsonify({
            'ok': True,
            'numero': numero,
            'atendente': atendente_nome,
            'minutos_simulados': minutos,
            'atendente_desde': desde_iso,
            'aviso': 'O monitor verifica a cada 60s. Aguarde ate 1 minuto e verifique os webhooks no N8N.'
        }), 200
    except Exception as e:
        db_sql.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/api/debug/last-webhook', methods=['GET', 'POST'])
def debug_webhook():
    """Dev-only: POST salva payload, GET retorna o ultimo."""
    debug_file = os.path.join(DATA_DIR, 'last_webhook.json')
    if request.method == 'POST':
        with open(debug_file, 'w', encoding='utf-8') as f:
            json.dump(request.json, f, indent=2, ensure_ascii=False)
        return 'OK', 200
    if os.path.exists(debug_file):
        with open(debug_file, 'r', encoding='utf-8') as f:
            return Response(f.read(), mimetype='application/json')
    return jsonify({})

@app.route('/api/reports/sla', methods=['GET'])
@auth_required
@admin_or_gestor_required
def report_sla():
    """
    Retorna os dados de SLA (Tempo de Fila, 1ª Resposta, Tempo de Resposta Contínuo).
    Pode filtrar por data (start_date, end_date).
    """
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    
    query = SlaHistory.query
    
    if start_date:
        query = query.filter(SlaHistory.criado_em >= start_date)
    if end_date:
        query = query.filter(SlaHistory.criado_em <= end_date + 'T23:59:59')
        
    records = query.order_by(SlaHistory.id.desc()).all()
    
    data = []
    for r in records:
        media_continua = 0
        if r.qtd_respostas_atendente and r.qtd_respostas_atendente > 0:
            media_continua = r.soma_tempo_resposta_segundos / r.qtd_respostas_atendente
            
        data.append({
            'id': r.id,
            'numero': r.numero,
            'filial': r.filial,
            'setor': r.setor,
            'atendente': r.atendente,
            'entrou_na_fila_em': r.entrou_na_fila_em,
            'assumido_em': r.assumido_em,
            'finalizado_em': r.finalizado_em,
            'tempo_na_fila_segundos': r.tempo_na_fila_segundos,
            'tempo_primeira_resposta_segundos': r.tempo_primeira_resposta_segundos,
            'qtd_respostas_atendente': r.qtd_respostas_atendente,
            'soma_tempo_resposta_segundos': r.soma_tempo_resposta_segundos,
            'media_tempo_resposta_segundos': media_continua,
            'criado_em': r.criado_em
        })
        
    return jsonify({'success': True, 'data': data}), 200


@app.route('/api/reports/ranking', methods=['GET'])
@auth_required
@admin_required
def report_ranking():
    try:
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        
        query = Message.query
        if start_date:
            try:
                start_ts = int(datetime.datetime.strptime(start_date, '%Y-%m-%d').timestamp())
                query = query.filter(Message.timestamp >= start_ts)
            except Exception:
                pass
        if end_date:
            try:
                end_ts = int(datetime.datetime.strptime(end_date + ' 23:59:59', '%Y-%m-%d %H:%M:%S').timestamp())
                query = query.filter(Message.timestamp <= end_ts)
            except Exception:
                pass
            
        messages = query.order_by(Message.contact_id, Message.timestamp).all()
        
        attendant_stats = {} 
        last_in_time = None
        current_contact = None
        
        for msg in messages:
            if current_contact != msg.contact_id:
                current_contact = msg.contact_id
                last_in_time = None
                
            if msg.type == 'in':
                last_in_time = msg.timestamp
            elif msg.type == 'out':
                if msg.sender_id:
                    if msg.sender_id not in attendant_stats:
                        attendant_stats[msg.sender_id] = {'total_msgs': 0, 'conversations': {}}
                    
                    attendant_stats[msg.sender_id]['total_msgs'] += 1
                    
                    if last_in_time is not None:
                        resp_time = msg.timestamp - last_in_time
                        if resp_time < 0: resp_time = 0
                        
                        if msg.contact_id not in attendant_stats[msg.sender_id]['conversations']:
                            attendant_stats[msg.sender_id]['conversations'][msg.contact_id] = []
                        
                        attendant_stats[msg.sender_id]['conversations'][msg.contact_id].append(resp_time)
                    
                last_in_time = None 
                
        ranking = []
        users = {u.id: u for u in User.query.all()}
        for uid, stats in attendant_stats.items():
            user = users.get(uid)
            if user and stats['total_msgs'] > 0:
                conv_averages = []
                for contact_id, times in stats['conversations'].items():
                    if len(times) > 0:
                        conv_avg = sum(times) / len(times)
                        conv_averages.append(conv_avg)
                
                if len(conv_averages) > 0:
                    final_avg_time = sum(conv_averages) / len(conv_averages)
                else:
                    final_avg_time = 0
                
                ranking.append({
                    'id': user.id,
                    'name': user.name,
                    'email': user.email,
                    'avg_time': final_avg_time,
                    'count': stats['total_msgs']
                })
                
        ranking.sort(key=lambda x: (-x['count'], x['avg_time']))
        return jsonify({'success': True, 'data': ranking}), 200
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'error': str(e), 'trace': traceback.format_exc()}), 500

@app.route('/api/reports/nps-filiais', methods=['GET'])
@auth_required
@admin_or_gestor_required
def report_nps_filiais():
    """Retorna ranking NPS por Filial e Setor."""
    try:
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')

        filters = ""
        params = {}
        if start_date:
            filters += " AND data_voto >= :start_date"
            params['start_date'] = start_date
        if end_date:
            filters += " AND data_voto <= :end_date"
            params['end_date'] = end_date + ' 23:59:59'

        sql = db_sql.text(f"""
            SELECT filial, setor, atendente,
                   COUNT(*) as total_votos,
                   AVG(CAST(SPLIT_PART(voto, ' ', 1) AS INTEGER)) as media_nota,
                   SUM(CASE WHEN CAST(SPLIT_PART(voto, ' ', 1) AS INTEGER) >= 9 THEN 1 ELSE 0 END) as promotores,
                   SUM(CASE WHEN CAST(SPLIT_PART(voto, ' ', 1) AS INTEGER) IN (7, 8) THEN 1 ELSE 0 END) as neutros,
                   SUM(CASE WHEN CAST(SPLIT_PART(voto, ' ', 1) AS INTEGER) <= 6 THEN 1 ELSE 0 END) as detratores
            FROM nps_votos
            WHERE 1=1 {filters}
            GROUP BY filial, setor, atendente
            ORDER BY filial, setor, media_nota DESC
        """)
        rows = db_sql.session.execute(sql, params).fetchall()

        # Organiza por filial > setor
        filiais = {}
        for row in rows:
            filial = row[0] or 'Sem Filial'
            setor = row[1] or 'Sem Setor'
            total = row[3] or 0
            promotores = row[5] or 0
            detratores = row[7] or 0
            nps = round(((promotores - detratores) / total) * 100) if total > 0 else 0

            if filial not in filiais:
                filiais[filial] = {}
            if setor not in filiais[filial]:
                filiais[filial][setor] = {
                    'total_votos': 0, 'media_nota': 0,
                    'promotores': 0, 'neutros': 0, 'detratores': 0,
                    'notas_sum': 0, 'nps': 0
                }

            s = filiais[filial][setor]
            s['total_votos'] += total
            s['notas_sum'] += (row[4] or 0) * total
            s['promotores'] += promotores
            s['neutros'] += (row[6] or 0)
            s['detratores'] += detratores

        # Calcula médias finais por setor
        result = []
        for filial, setores in filiais.items():
            setores_list = []
            for setor, s in setores.items():
                total = s['total_votos']
                media = round(s['notas_sum'] / total, 1) if total > 0 else 0
                nps_score = round(((s['promotores'] - s['detratores']) / total) * 100) if total > 0 else 0
                setores_list.append({
                    'setor': setor,
                    'total_votos': total,
                    'media_nota': media,
                    'promotores': s['promotores'],
                    'neutros': s['neutros'],
                    'detratores': s['detratores'],
                    'nps_score': nps_score
                })
            setores_list.sort(key=lambda x: -x['nps_score'])
            result.append({'filial': filial, 'setores': setores_list})
        result.sort(key=lambda x: x['filial'])
        return jsonify({'success': True, 'data': result}), 200
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/reports/nps-atendentes', methods=['GET'])
@auth_required
@admin_or_gestor_required
def report_nps_atendentes():
    """Retorna ranking NPS por Atendente."""
    try:
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')

        filters = ""
        params = {}
        if start_date:
            filters += " AND data_voto >= :start_date"
            params['start_date'] = start_date
        if end_date:
            filters += " AND data_voto <= :end_date"
            params['end_date'] = end_date + ' 23:59:59'

        sql = db_sql.text(f"""
            SELECT atendente, filial, setor,
                   COUNT(*) as total_votos,
                   AVG(CAST(SPLIT_PART(voto, ' ', 1) AS INTEGER)) as media_nota,
                   SUM(CASE WHEN CAST(SPLIT_PART(voto, ' ', 1) AS INTEGER) >= 9 THEN 1 ELSE 0 END) as promotores,
                   SUM(CASE WHEN CAST(SPLIT_PART(voto, ' ', 1) AS INTEGER) IN (7, 8) THEN 1 ELSE 0 END) as neutros,
                   SUM(CASE WHEN CAST(SPLIT_PART(voto, ' ', 1) AS INTEGER) <= 6 THEN 1 ELSE 0 END) as detratores
            FROM nps_votos
            WHERE atendente IS NOT NULL AND atendente != '' {filters}
            GROUP BY atendente, filial, setor
            ORDER BY media_nota DESC
        """)
        rows = db_sql.session.execute(sql, params).fetchall()

        import math
        result = []
        for row in rows:
            total = row[3] or 0
            promotores = row[5] or 0
            detratores = row[7] or 0
            nps_score = round(((promotores - detratores) / total) * 100) if total > 0 else 0
            # Pontuação combinada: equilibra NPS com volume de votos
            combined_score = nps_score * math.log(total + 1)
            result.append({
                'atendente': row[0] or '-',
                'filial': row[1] or '-',
                'setor': row[2] or '-',
                'total_votos': total,
                'media_nota': round(row[4] or 0, 1),
                'promotores': promotores,
                'neutros': row[6] or 0,
                'detratores': detratores,
                'nps_score': nps_score,
                'combined_score': round(combined_score, 1)
            })
        # Ordena por pontuação combinada (NPS × log(votos+1))
        result.sort(key=lambda x: -x['combined_score'])
        return jsonify({'success': True, 'data': result}), 200
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/reports/nps-respostas', methods=['GET'])
@auth_required
@admin_or_gestor_required
def report_nps_respostas():
    """Retorna a lista de votos individuais do NPS."""
    try:
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')

        filters = ""
        params = {}
        if start_date:
            filters += " AND data_voto >= :start_date"
            params['start_date'] = start_date
        if end_date:
            filters += " AND data_voto <= :end_date"
            params['end_date'] = end_date + ' 23:59:59'

        sql = db_sql.text(f"""
            SELECT numero_cliente as cliente, 
                   voto, COALESCE(motivo, comentario) as comentario, atendente, filial, setor, data_voto
            FROM nps_votos
            WHERE 1=1 {filters}
            ORDER BY data_voto DESC
        """)
        rows = db_sql.session.execute(sql, params).fetchall()

        result = []
        for row in rows:
            result.append({
                'cliente': row[0] or '-',
                'voto': row[1] or '-',
                'comentario': row[2] or '',
                'atendente': row[3] or '-',
                'filial': row[4] or '-',
                'setor': row[5] or '-',
                'data_voto': str(row[6]) if row[6] else '-'
            })
        
        return jsonify({'success': True, 'data': result}), 200
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'error': str(e), 'trace': traceback.format_exc()}), 500


def _segundos_espera_sql():
    return "EXTRACT(EPOCH FROM (atendido - inicio))"

def _segundos_chat_sql():
    return "EXTRACT(EPOCH FROM (finalizado - atendido))"


@app.route('/api/reports/tempo-espera-atendentes', methods=['GET'])
@auth_required
@admin_or_gestor_required
def report_tempo_espera_atendentes():
    """Ranking de atendentes por eficiência de tempo de espera."""
    try:
        import math
        start_date = request.args.get('start_date')
        end_date   = request.args.get('end_date')
        filters = "AND atendido IS NOT NULL AND nome_atendente IS NOT NULL AND nome_atendente != ''"
        params  = {}
        if start_date:
            filters += " AND inicio >= :start_date"
            params['start_date'] = start_date
        if end_date:
            filters += " AND inicio <= :end_date"
            params['end_date'] = end_date + ' 23:59:59'
        sql = db_sql.text(f"""
            SELECT nome_atendente, setor_filial, COUNT(*) as total_atendidos,
                   AVG({_segundos_espera_sql()}) as avg_espera_seg,
                   AVG(CASE WHEN finalizado IS NOT NULL THEN {_segundos_chat_sql()} END) as avg_chat_seg,
                   MIN({_segundos_espera_sql()}) as min_espera_seg,
                   MAX({_segundos_espera_sql()}) as max_espera_seg
            FROM tempo_espera WHERE 1=1 {filters}
            GROUP BY nome_atendente, setor_filial
        """)
        rows = db_sql.session.execute(sql, params).fetchall()
        result = []
        for row in rows:
            nome       = row[0] or '-'
            sf         = row[1] or '-'
            total      = int(row[2] or 0)
            avg_espera = float(row[3] or 0)
            avg_chat   = float(row[4] or 0)
            total_med  = avg_espera + avg_chat
            score      = math.log(total + 1) * 10000 / (total_med + 1) if total > 0 else 0
            partes     = sf.split(':', 1) if ':' in sf else [sf, '-']
            setor      = partes[0].strip()
            filial     = partes[1].strip() if len(partes) > 1 else '-'
            result.append({
                'atendente': nome, 'setor_filial': sf, 'setor': setor, 'filial': filial,
                'total_atendidos': total,
                'avg_espera_seg': round(avg_espera, 0),
                'avg_chat_seg':   round(avg_chat, 0),
                'avg_total_seg':  round(total_med, 0),
                'min_espera_seg': round(float(row[5] or 0), 0),
                'max_espera_seg': round(float(row[6] or 0), 0),
                'score': round(score, 1)
            })
        result.sort(key=lambda x: -x['score'])
        return jsonify({'success': True, 'data': result}), 200
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/reports/tempo-espera-extrato', methods=['GET'])
@auth_required
@admin_or_gestor_required
def report_tempo_espera_extrato():
    """Extrato detalhado de atendimentos agrupados por atendente."""
    try:
        start_date = request.args.get('start_date')
        end_date   = request.args.get('end_date')
        filters = "AND atendido IS NOT NULL AND nome_atendente IS NOT NULL AND nome_atendente != ''"
        params  = {}
        if start_date:
            filters += " AND inicio >= :start_date"
            params['start_date'] = start_date
        if end_date:
            filters += " AND inicio <= :end_date"
            params['end_date'] = end_date + ' 23:59:59'

        # Busca todos os atendimentos
        sql = db_sql.text(f"""
            SELECT nome_atendente, numero_cliente, inicio,
                   {_segundos_espera_sql()} as espera_seg,
                   (CASE WHEN finalizado IS NOT NULL THEN {_segundos_chat_sql()} ELSE NULL END) as chat_seg,
                   setor_filial
            FROM tempo_espera WHERE 1=1 {filters}
            ORDER BY nome_atendente, inicio DESC
        """)
        rows = db_sql.session.execute(sql, params).fetchall()
        
        # Agrupa no backend para facilitar pro frontend
        atendentes_map = {}
        for row in rows:
            nome = row[0] or 'Desconhecido'
            if nome not in atendentes_map:
                atendentes_map[nome] = []
                
            atendentes_map[nome].append({
                'cliente': row[1] or '-',
                'data': str(row[2]) if row[2] else '-',
                'espera_seg': float(row[3] or 0),
                'chat_seg': float(row[4] or 0) if row[4] is not None else None,
                'setor_filial': row[5] or '-'
            })
            
        # Converte para lista
        result = [{'atendente': nome, 'atendimentos': atendimentos} for nome, atendimentos in atendentes_map.items()]
        # Ordena pela quantidade de atendimentos
        result.sort(key=lambda x: len(x['atendimentos']), reverse=True)

        return jsonify({'success': True, 'data': result}), 200
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/reports/tempo-espera-filiais', methods=['GET'])
@auth_required
@admin_or_gestor_required
def report_tempo_espera_filiais():
    """Ranking de filiais/setores por eficiência de tempo de espera."""
    try:
        import math
        start_date = request.args.get('start_date')
        end_date   = request.args.get('end_date')
        filters = "AND atendido IS NOT NULL AND setor_filial IS NOT NULL AND setor_filial != ''"
        params  = {}
        if start_date:
            filters += " AND inicio >= :start_date"
            params['start_date'] = start_date
        if end_date:
            filters += " AND inicio <= :end_date"
            params['end_date'] = end_date + ' 23:59:59'
        sql = db_sql.text(f"""
            SELECT setor_filial, COUNT(*) as total_atendidos,
                   AVG({_segundos_espera_sql()}) as avg_espera_seg,
                   AVG(CASE WHEN finalizado IS NOT NULL THEN {_segundos_chat_sql()} END) as avg_chat_seg
            FROM tempo_espera WHERE 1=1 {filters}
            GROUP BY setor_filial
        """)
        rows = db_sql.session.execute(sql, params).fetchall()
        filiais = {}
        for row in rows:
            sf         = row[0] or '-'
            total      = int(row[1] or 0)
            avg_espera = float(row[2] or 0)
            avg_chat   = float(row[3] or 0)
            total_med  = avg_espera + avg_chat
            score      = math.log(total + 1) * 10000 / (total_med + 1) if total > 0 else 0
            partes     = sf.split(':', 1) if ':' in sf else [sf, '-']
            setor      = partes[0].strip()
            filial     = partes[1].strip() if len(partes) > 1 else '-'
            if filial not in filiais:
                filiais[filial] = []
            filiais[filial].append({
                'setor': setor, 'total_atendidos': total,
                'avg_espera_seg': round(avg_espera, 0),
                'avg_chat_seg':   round(avg_chat, 0),
                'avg_total_seg':  round(total_med, 0),
                'score': round(score, 1)
            })
        result = []
        for filial, setores in filiais.items():
            setores.sort(key=lambda x: -x['score'])
            total_f    = sum(s['total_atendidos'] for s in setores)
            avg_esp_f  = sum(s['avg_espera_seg'] * s['total_atendidos'] for s in setores) / total_f if total_f else 0
            avg_chat_f = sum((s['avg_chat_seg'] or 0) * s['total_atendidos'] for s in setores) / total_f if total_f else 0
            score_f    = math.log(total_f + 1) * 10000 / (avg_esp_f + avg_chat_f + 1) if total_f > 0 else 0
            result.append({
                'filial': filial, 'total_atendidos': total_f,
                'avg_espera_seg': round(avg_esp_f, 0),
                'avg_chat_seg':   round(avg_chat_f, 0),
                'score': round(score_f, 1),
                'setores': setores
            })
        result.sort(key=lambda x: -x['score'])
        return jsonify({'success': True, 'data': result}), 200
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/reports/volume-chats-filiais', methods=['GET'])
@auth_required
@admin_or_gestor_required
def report_volume_chats_filiais():
    """Volume de chats criados, fechados e abertos por filial/setor (categorizados por tags)."""
    try:
        filiais_objs = Filial.query.all()
        valid_filiais = {f.name.lower().strip(): f.name.strip() for f in filiais_objs}
        
        def resolve_sf(sf_str):
            if not sf_str or ':' not in sf_str:
                return None, None
            partes = sf_str.split(':', 1)
            p0 = partes[0].strip()
            p1 = partes[1].strip()
            if not p0 or not p1 or p0 == '-' or p1 == '-' or p0.lower() == 'null' or p1.lower() == 'null':
                return None, None
            
            if p0.lower() in valid_filiais:
                return valid_filiais[p0.lower()], p1 # p0 = filial, p1 = setor
            elif p1.lower() in valid_filiais:
                return valid_filiais[p1.lower()], p0 # p1 = filial, p0 = setor
            else:
                return p0, p1 # Default assume p0 = filial

        start_date = request.args.get('start_date')
        end_date   = request.args.get('end_date')
        
        params = {}
        date_filters = ""
        if start_date and end_date:
            params['start_date'] = start_date
            params['end_date'] = end_date + ' 23:59:59'
            date_filters = """
                WHERE (inicio >= :start_date AND inicio <= :end_date)
                   OR (finalizado >= :start_date AND finalizado <= :end_date)
                   OR (finalizado IS NULL)
            """
        else:
            date_filters = "WHERE 1=1"

        # Pega criados e fechados no periodo
        sql = db_sql.text(f"""
            SELECT setor_filial,
                   SUM(CASE WHEN inicio >= :start_date AND inicio <= :end_date THEN 1 ELSE 0 END) as criados,
                   SUM(CASE WHEN finalizado >= :start_date AND finalizado <= :end_date THEN 1 ELSE 0 END) as fechados
            FROM tempo_espera
            {date_filters}
            GROUP BY setor_filial
        """)
        rows = db_sql.session.execute(sql, params).fetchall()

        filiais = {}
        for row in rows:
            sf       = row[0] or '-'
            if not sf or sf == '-': continue
            criados  = int(row[1] or 0)
            fechados = int(row[2] or 0)
            
            filial, setor = resolve_sf(sf)
            if not filial or not setor:
                continue
            
            if filial not in filiais:
                filiais[filial] = {}
            if setor not in filiais[filial]:
                filiais[filial][setor] = {'criados': 0, 'fechados': 0, 'triagem': 0, 'espera': 0, 'atendimento': 0}
            
            filiais[filial][setor]['criados'] += criados
            filiais[filial][setor]['fechados'] += fechados

        # Agora pega a fila de ESPERA (tempo_espera sem atendente)
        sql_espera = db_sql.text("""
            SELECT setor_filial, COUNT(*) as qtd
            FROM tempo_espera
            WHERE finalizado IS NULL AND atendido IS NULL
            GROUP BY setor_filial
        """)
        espera_rows = db_sql.session.execute(sql_espera).fetchall()
        for row in espera_rows:
            sf = row[0] or '-'
            if not sf or sf == '-': continue
            qtd = int(row[1] or 0)
            
            filial, setor = resolve_sf(sf)
            if not filial or not setor:
                continue
            
            if filial not in filiais: filiais[filial] = {}
            if setor not in filiais[filial]: filiais[filial][setor] = {'criados': 0, 'fechados': 0, 'triagem': 0, 'espera': 0, 'atendimento': 0}
            filiais[filial][setor]['espera'] += qtd

        # Pega a fila de ATENDIMENTO (atendimentos_chat com status='atendente')
        sql_atend = db_sql.text("""
            SELECT ultimo_setor, COUNT(*) as qtd
            FROM atendimentos_chat
            WHERE status = 'atendente'
            GROUP BY ultimo_setor
        """)
        atend_rows = db_sql.session.execute(sql_atend).fetchall()
        for row in atend_rows:
            sf = row[0] or '-'
            if not sf or sf == '-': continue
            qtd = int(row[1] or 0)
            
            filial, setor = resolve_sf(sf)
            if not filial or not setor:
                continue
            
            if filial not in filiais: filiais[filial] = {}
            if setor not in filiais[filial]: filiais[filial][setor] = {'criados': 0, 'fechados': 0, 'triagem': 0, 'espera': 0, 'atendimento': 0}
            filiais[filial][setor]['atendimento'] += qtd
            
        # Para TRIAGEM (BOT), usamos a tag BOT nos contatos que NAO estao em atendimento nem em espera.
        # Devido ao volume, podemos buscar contatos com tag BOT que tiveram mensagem hoje (ativo).
        hoje = get_now_sp().strftime('%d/%m/%Y')
        # Uma aproximação para triagem (já que a tabela não tem state exclusivo pra isso sem estar fechado)
        sql_triagem = db_sql.text("""
            SELECT tags 
            FROM contacts 
            WHERE unread > 0 OR last_msg_time LIKE :hoje
        """)
        try:
            triagem_rows = db_sql.session.execute(sql_triagem, {'hoje': f"{hoje}%"}).fetchall()
            for r in triagem_rows:
                tags = r[0]
                if tags:
                    if type(tags) == str:
                        import json
                        try: tags = json.loads(tags)
                        except: tags = []
                    
                    has_bot = any(str(t).strip().upper() == 'BOT' for t in tags)
                    has_att = any(str(t).strip().lower().startswith('atendente:') for t in tags)
                    
                    if has_bot and not has_att:
                        # Achou um em triagem. O setor muitas vezes ainda nao existe (está no menu).
                        # Vamos colocar em Geral/Triagem
                        sf = 'Triagem:Geral'
                        partes = sf.split(':', 1)
                        setor = partes[0].strip()
                        filial = partes[1].strip()
                        if filial not in filiais: filiais[filial] = {}
                        if setor not in filiais[filial]: filiais[filial][setor] = {'criados': 0, 'fechados': 0, 'triagem': 0, 'espera': 0, 'atendimento': 0}
                        filiais[filial][setor]['triagem'] += 1
        except:
            pass

        result = []
        for filial, setores_dict in filiais.items():
            setores_list = []
            for setor, stats in setores_dict.items():
                setores_list.append({
                    'setor': setor,
                    'criados': stats['criados'],
                    'fechados': stats['fechados'],
                    'triagem': stats['triagem'],
                    'espera': stats['espera'],
                    'atendimento': stats['atendimento']
                })
            setores_list.sort(key=lambda x: -x['criados'])
            result.append({
                'filial': filial,
                'criados': sum(s['criados'] for s in setores_list),
                'fechados': sum(s['fechados'] for s in setores_list),
                'triagem': sum(s['triagem'] for s in setores_list),
                'espera': sum(s['espera'] for s in setores_list),
                'atendimento': sum(s['atendimento'] for s in setores_list),
                'setores': setores_list
            })
        result.sort(key=lambda x: -x['criados'])
        return jsonify({'success': True, 'data': result}), 200
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/reports/volume-chats-atendentes', methods=['GET'])
@auth_required
@admin_or_gestor_required
def report_volume_chats_atendentes():
    """Volume de chats criados, fechados e abertos por atendente."""
    try:
        start_date = request.args.get('start_date')
        end_date   = request.args.get('end_date')
        
        params = {}
        date_filters = ""
        if start_date and end_date:
            params['start_date'] = start_date
            params['end_date'] = end_date + ' 23:59:59'
            date_filters = """
                AND ((inicio >= :start_date AND inicio <= :end_date)
                   OR (finalizado >= :start_date AND finalizado <= :end_date)
                   OR (finalizado IS NULL))
            """
        
        sql = db_sql.text(f"""
            SELECT nome_atendente, setor_filial,
                   SUM(CASE WHEN inicio >= :start_date AND inicio <= :end_date THEN 1 ELSE 0 END) as criados,
                   SUM(CASE WHEN finalizado >= :start_date AND finalizado <= :end_date THEN 1 ELSE 0 END) as fechados
            FROM tempo_espera
            WHERE nome_atendente IS NOT NULL AND nome_atendente != '' {date_filters}
            GROUP BY nome_atendente, setor_filial
        """)
        rows = db_sql.session.execute(sql, params).fetchall()

        # Obter todos os usuarios para mapear email -> nome e pegar setor/filial real do usuario
        users = User.query.all()
        email_to_name = {u.email.lower().strip(): u.name.strip() for u in users if u.email and u.name}
        name_to_user = {u.name.lower().strip(): u for u in users if u.name}

        def normalize_atendente_nome(n):
            n_str = str(n).strip()
            if '@' in n_str:
                n_lower = n_str.lower()
                if n_lower in email_to_name:
                    return email_to_name[n_lower]
            return n_str

        atendentes_map = {}
        for row in rows:
            nome     = normalize_atendente_nome(row[0] or '-')
            criados  = int(row[2] or 0)
            # Fetching fechados from atendimentos_chat now instead of tempo_espera
            
            key = nome.lower()
            if key not in atendentes_map:
                atendentes_map[key] = {'nome': nome, 'criados': 0, 'fechados': 0, 'abertos': 0}
            
            atendentes_map[key]['criados'] += criados

        # Agora pega a fila de ATENDIMENTO REAL usando a tabela atendimentos_chat
        sql_abertos = db_sql.text("""
            SELECT atendente, 
                   SUM(CASE WHEN LOWER(status) = 'atendente' THEN 1 ELSE 0 END) as abertos,
                   SUM(CASE WHEN LOWER(status) = 'bot' THEN 1 ELSE 0 END) as fechados
            FROM atendimentos_chat
            WHERE atendente IS NOT NULL AND atendente != ''
            GROUP BY atendente
        """)
        abertos_rows = db_sql.session.execute(sql_abertos).fetchall()
        for row in abertos_rows:
            nome = normalize_atendente_nome(row[0] or '-')
            qtd_abertos = int(row[1] or 0)
            qtd_fechados = int(row[2] or 0)
            
            key = nome.lower()
            if key not in atendentes_map:
                atendentes_map[key] = {'nome': nome, 'criados': 0, 'fechados': 0, 'abertos': 0}
                
            atendentes_map[key]['abertos'] = qtd_abertos
            atendentes_map[key]['fechados'] = qtd_fechados

        result = []
        for key, data in atendentes_map.items():
            user_obj = name_to_user.get(key)
            filial = user_obj.filial if user_obj and user_obj.filial else '-'
            setor = user_obj.setor if user_obj and user_obj.setor else '-'
            
            result.append({
                'atendente': data['nome'],
                'filial': filial,
                'setor': setor,
                'criados': data['criados'],
                'fechados': data['fechados'],
                'abertos': data['abertos']
            })
            
        result.sort(key=lambda x: (-x['abertos'], -x['criados'], -x['fechados']))
        return jsonify({'success': True, 'data': result}), 200
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/')

def index_page():
    return send_from_directory(PUBLIC_DIR, 'index.html')

@app.route('/<path:path>')
def serve_frontend(path):
    if path in ('index.html', 'dashboard.html', 'admin.html', 'reports.html', 'ranking.html', 'relatorio.html'):
        return send_from_directory(PUBLIC_DIR, path)
    if path.startswith('css/') or path.startswith('js/'):
        return send_from_directory(PUBLIC_DIR, path)
    if path.lower().endswith(('.png', '.jpg', '.jpeg', '.svg', '.ico', '.webp')):
        # Logo is in root or public? I should allow serving from ROOT_DIR for images that were not moved.
        # But wait, I only moved html, js, css. Images weren't moved!
        # Let's check if there are images in root.
        if os.path.exists(os.path.join(PUBLIC_DIR, path)):
            return send_from_directory(PUBLIC_DIR, path)
        return send_from_directory(ROOT_DIR, path)
    return jsonify({'error': 'Not found'}), 404

@socketio.on('connect')
def test_connect():
    print('>>> Cliente conectado ao SocketIO')
    emit('server_boot', {'boot_id': SERVER_BOOT_ID})

@socketio.on('join_company')
def on_join(company_id):
    join_room(company_id)
    print(f'Client joined room: {company_id}')

@socketio.on('join_instances')
def on_join_instances(data):
    """Usuário entra nas rooms das instâncias que tem acesso."""
    instances = data.get('instances', [])
    role = data.get('role', 'user')
    
    for inst_name in instances:
        room_name = f'instance_{inst_name}'
        join_room(room_name)
        print(f'Client joined instance room: {room_name}')
    
    if role == 'admin':
        join_room('admin')
        print('Client joined admin room')

if __name__ == '__main__':
    port = int(os.getenv('PORT', 3008))
    print(f"Servidor Python rodando na porta {port}...")
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)
