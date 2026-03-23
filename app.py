
import os
import jwt
import datetime
from functools import wraps
from flask import Flask, request, jsonify, send_from_directory
from flask_socketio import SocketIO, emit, join_room
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
import requests
from dotenv import load_dotenv
import json
import io
from flask import Response

load_dotenv()

app = Flask(__name__)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

JWT_SECRET = os.getenv('JWT_SECRET', 'secret')
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))

# Configuração do Local de Armazenamento
DATA_DIR = os.path.join(os.getcwd(), 'data')
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

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

class User(db_sql.Model):
    id = db_sql.Column(db_sql.Integer, primary_key=True)
    name = db_sql.Column(db_sql.String(100), nullable=False)
    email = db_sql.Column(db_sql.String(120), unique=True, nullable=False)
    password = db_sql.Column(db_sql.String(200), nullable=False)
    role = db_sql.Column(db_sql.String(20), default='user')
    instances = db_sql.Column(db_sql.JSON, default=[]) # Nomes das instâncias vinculadas

class Contact(db_sql.Model):
    id = db_sql.Column(db_sql.String(150), primary_key=True) # c_phone_instance
    name = db_sql.Column(db_sql.String(100), nullable=False)
    phone = db_sql.Column(db_sql.String(30), nullable=False) # No longer unique
    avatar = db_sql.Column(db_sql.String(10), nullable=True)
    instance = db_sql.Column(db_sql.String(100), nullable=True)
    tags = db_sql.Column(db_sql.JSON, default=['Novo Lead'])
    last_msg = db_sql.Column(db_sql.Text, nullable=True)
    last_msg_time = db_sql.Column(db_sql.String(10), nullable=True)
    unread = db_sql.Column(db_sql.Integer, default=0)

class Message(db_sql.Model):
    id = db_sql.Column(db_sql.String(100), primary_key=True)
    contact_id = db_sql.Column(db_sql.String(150), db_sql.ForeignKey('contact.id'), nullable=False)
    text = db_sql.Column(db_sql.Text, nullable=False)
    type = db_sql.Column(db_sql.String(10), nullable=False) # 'in' or 'out'
    time = db_sql.Column(db_sql.String(10), nullable=False)
    timestamp = db_sql.Column(db_sql.BigInteger, nullable=False)
    instance = db_sql.Column(db_sql.String(100), nullable=True)

class Setting(db_sql.Model):
    key = db_sql.Column(db_sql.String(50), primary_key=True)
    value = db_sql.Column(db_sql.Text, nullable=True)

# ─── Utils ──────────────────────────────────────────────────────────────────
def normalize_br_phone(phone_str):
    if not phone_str: return ""
    p = str(phone_str)
    if p.startswith('55') and len(p) == 12:
        return f"{p[:4]}9{p[4:]}"
    return p

def get_media_base64(instance, msg_data):
    try:
        url = f"{os.getenv('EVOLUTION_API_URL')}/chat/getBase64FromMediaMessage/{instance}"
        headers = {'apikey': os.getenv('EVOLUTION_API_KEY'), 'Content-Type': 'application/json'}
        payload = {"message": msg_data}
        res = requests.post(url, json=payload, headers=headers, timeout=10)
        if res.status_code == 200:
            return res.json().get('base64')
    except Exception as e:
        print(f"Erro ao baixar midia base64: {e}")
    return None

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
        
        # Se Users estiver vazio, tenta migrar do JSON
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
    if not request.path.startswith('/static'):
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
            return f(*args, **kwargs)
        except Exception:
            return jsonify({'error': 'Token inválido ou expirado'}), 401
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.user.get('role') != 'admin':
            return jsonify({'error': 'Acesso negado. Apenas administradores.'}), 403
        return f(*args, **kwargs)
    return decorated

@app.route('/api/health')
def health():
    return jsonify({
        'status': 'ok',
        'port': 3008,
        'evolution_url': os.getenv('EVOLUTION_API_URL')
    })

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
            'exp': datetime.datetime.utcnow() + datetime.timedelta(days=1)
        }, JWT_SECRET, algorithm="HS256")
        
        return jsonify({
            'token': token if isinstance(token, str) else token.decode('utf-8'),
            'user': {
                'id': user.id,
                'name': user.name,
                'email': user.email,
                'role': user.role,
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
            'role': u.role,
            'instances': u.instances or []
        })
    return jsonify(users_list)

@app.route('/api/admin/users', methods=['POST'])
@auth_required
@admin_required
def create_user():
    data = request.json
    if User.query.filter_by(email=data['email']).first():
        return jsonify({'error': 'E-mail já cadastrado'}), 400
    
    new_user = User(
        name=data.get('name'),
        email=data.get('email'),
        password=data.get('password'),
        role='user',
        instances=[]
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
        user.name = data.get('name', user.name)
        user.email = data.get('email', user.email)
        if data.get('password'):
            user.password = data['password']
        db_sql.session.commit()
        return jsonify({
            'id': user.id,
            'name': user.name,
            'email': user.email,
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

@app.route('/api/whatsapp/instances', methods=['GET'])
@auth_required
def get_instances():
    try:
        url = f"{os.getenv('EVOLUTION_API_URL')}/instance/fetchInstances"
        headers = {'apikey': os.getenv('EVOLUTION_API_KEY')}
        response = requests.get(url, headers=headers)
        all_inst = response.json()
        
        if request.user.get('role') != 'admin':
            user = User.query.get(request.user['id'])
            allowed = user.instances or []
            all_inst = [i for i in all_inst if (i.get('instanceName') or i.get('name')) in allowed]
            
        return jsonify(all_inst)
    except Exception as e:
        print(f"Erro ao buscar instâncias: {str(e)}")
        return jsonify({'error': f"Erro na Evolution API: {str(e)}"}), 500

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

    try:
        now = datetime.datetime.now()
        time_str = now.strftime("%H:%M")
        
        # Chamada para a API externa (Evolution)
        url = f"{os.getenv('EVOLUTION_API_URL')}/message/sendText/{inst}"
        headers = {'apikey': os.getenv('EVOLUTION_API_KEY')}
        payload = {"number": number, "text": text}
        res = requests.post(url, json=payload, headers=headers)
        res_data = res.json()
        
        msg_id = res_data.get('key', {}).get('id') or res_data.get('messageId') or f"out_{int(now.timestamp())}"
        contact_id = f"c_{number}_{inst}"
        
        # Salvar mensagem no Banco SE NÃO EXISTIR
        if not Message.query.get(msg_id):
            new_msg = Message(
                id=msg_id,
                contact_id=contact_id,
                text=text,
                type='out',
                time=time_str,
                timestamp=int(now.timestamp()),
                instance=inst
            )
            db_sql.session.add(new_msg)
        
        # Atualizar ou Criar Contato
        contact = Contact.query.filter_by(id=contact_id).first()
        if contact:
            contact.last_msg = text
            contact.last_msg_time = time_str
        else:
            new_contact = Contact(
                id=contact_id,
                name=f"+{number}",
                phone=number,
                avatar=number[0] if number else "?",
                instance=inst,
                tags=['Novo Lead'],
                last_msg=text,
                last_msg_time=time_str,
                unread=0
            )
            db_sql.session.add(new_contact)
        
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
        return jsonify(res_data)
    except Exception as e:
        print(f"Erro ao enviar: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/whatsapp/send-audio', methods=['POST'])
@auth_required
def send_audio():
    """Envia audio gravado pelo atendente ao cliente via Evolution API."""
    data = request.json
    inst = data.get('instance')
    number = "".join(filter(str.isdigit, str(data.get('number', ''))))
    number = normalize_br_phone(number)
    audio_b64 = data.get('audio', '')

    if not inst or not number or not audio_b64:
        return jsonify({'error': 'instance, number e audio são obrigatórios'}), 400

    try:
        now = datetime.datetime.now()
        time_str = now.strftime("%H:%M")

        # Enviar via Evolution API
        # Evolution exige base64 puro (sem prefixo data:audio/xxx;base64,)
        audio_raw = audio_b64
        if ';base64,' in audio_raw:
            audio_raw = audio_raw.split(';base64,', 1)[1]

        url = f"{os.getenv('EVOLUTION_API_URL')}/message/sendWhatsAppAudio/{inst}"
        headers = {'apikey': os.getenv('EVOLUTION_API_KEY'), 'Content-Type': 'application/json'}
        payload = {"number": number, "audio": audio_raw}
        print(f"[Send Audio] Enviando audio para {number} via {inst}")
        res = requests.post(url, json=payload, headers=headers, timeout=30)
        res_data = res.json()
        print(f"[Send Audio] Resposta: status={res.status_code} body={json.dumps(res_data)[:300]}")

        msg_id = res_data.get('key', {}).get('id') or res_data.get('messageId') or f"audio_out_{int(now.timestamp())}"
        text = f"[AUDIO_REF] {inst}|{msg_id}"

        contact_id = f"c_{number}_{inst}"

        contact = Contact.query.filter_by(id=contact_id).first()
        if not contact:
            contact = Contact(id=contact_id, phone=number, name=f"Novo {number}", instance=inst)
            db_sql.session.add(contact)
            db_sql.session.flush()

        # Salvar mensagem
        if not Message.query.get(msg_id):
            new_msg = Message(
                id=msg_id, contact_id=contact_id, text=text,
                type='out', time=time_str, timestamp=int(now.timestamp()), instance=inst
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
        now = datetime.datetime.now()
        time_str = now.strftime("%H:%M")

        image_raw = image_b64
        mimetype = "image/jpeg"
        if ';base64,' in image_raw:
            mime_part, image_raw = image_raw.split(';base64,', 1)
            mimetype = mime_part.replace('data:', '')

        url = f"{os.getenv('EVOLUTION_API_URL')}/message/sendMedia/{inst}"
        headers = {'apikey': os.getenv('EVOLUTION_API_KEY'), 'Content-Type': 'application/json'}
        payload = {
            "number": number,
            "mediatype": "image",
            "mimetype": mimetype,
            "caption": caption,
            "media": image_raw
        }
        res = requests.post(url, json=payload, headers=headers, timeout=30)
        res_data = res.json()

        msg_id = res_data.get('key', {}).get('id') or res_data.get('messageId') or f"img_out_{int(now.timestamp())}"
        text = f"[IMAGE_REF] {caption}"

        contact_id = f"c_{number}_{inst}"

        contact = Contact.query.filter_by(id=contact_id).first()
        if not contact:
            contact = Contact(id=contact_id, phone=number, name=f"Novo {number}", instance=inst)
            db_sql.session.add(contact)
            db_sql.session.flush()

        if not Message.query.get(msg_id):
            new_msg = Message(
                id=msg_id, contact_id=contact_id, text=text,
                type='out', time=time_str, timestamp=int(now.timestamp()), instance=inst
            )
            db_sql.session.add(new_msg)

        contact.last_msg = '🖼️ Imagem'
        contact.last_msg_time = time_str
        db_sql.session.commit()

        return jsonify({'ok': True, 'msg_id': msg_id, 'key': res_data.get('key', {})})
    except Exception as e:
        print(f"Erro send_image: {e}")
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
        now = datetime.datetime.now()
        time_str = now.strftime("%H:%M")

        video_raw = video_b64
        mimetype = "video/mp4"
        if ';base64,' in video_raw:
            mime_part, video_raw = video_raw.split(';base64,', 1)
            mimetype = mime_part.replace('data:', '')

        url = f"{os.getenv('EVOLUTION_API_URL')}/message/sendMedia/{inst}"
        headers = {'apikey': os.getenv('EVOLUTION_API_KEY'), 'Content-Type': 'application/json'}
        payload = {
            "number": number,
            "mediatype": "video",
            "mimetype": mimetype,
            "caption": caption,
            "media": video_raw
        }
        res = requests.post(url, json=payload, headers=headers, timeout=60)
        res_data = res.json()

        msg_id = res_data.get('key', {}).get('id') or res_data.get('messageId') or f"vid_out_{int(now.timestamp())}"
        text = f"[VIDEO_REF] {caption}"

        contact_id = f"c_{number}_{inst}"

        contact = Contact.query.filter_by(id=contact_id).first()
        if not contact:
            contact = Contact(id=contact_id, phone=number, name=f"Novo {number}", instance=inst)
            db_sql.session.add(contact)
            db_sql.session.flush()

        if not Message.query.get(msg_id):
            new_msg = Message(
                id=msg_id, contact_id=contact_id, text=text,
                type='out', time=time_str, timestamp=int(now.timestamp()), instance=inst
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
        now = datetime.datetime.now()
        time_str = now.strftime("%H:%M")

        doc_raw = doc_b64
        mimetype = "application/pdf"
        if ';base64,' in doc_raw:
            mime_part, doc_raw = doc_raw.split(';base64,', 1)
            mimetype = mime_part.replace('data:', '')

        url = f"{os.getenv('EVOLUTION_API_URL')}/message/sendMedia/{inst}"
        headers = {'apikey': os.getenv('EVOLUTION_API_KEY'), 'Content-Type': 'application/json'}
        payload = {
            "number": number,
            "mediatype": "document",
            "mimetype": mimetype,
            "fileName": doc_name,
            "caption": caption,
            "media": doc_raw
        }
        res = requests.post(url, json=payload, headers=headers, timeout=60)
        res_data = res.json()

        msg_id = res_data.get('key', {}).get('id') or res_data.get('messageId') or f"doc_out_{int(now.timestamp())}"
        text = f"[DOCUMENT_REF] {doc_name}"

        contact_id = f"c_{number}_{inst}"

        contact = Contact.query.filter_by(id=contact_id).first()
        if not contact:
            contact = Contact(id=contact_id, phone=number, name=f"Novo {number}", instance=inst)
            db_sql.session.add(contact)
            db_sql.session.flush()

        if not Message.query.get(msg_id):
            new_msg = Message(
                id=msg_id, contact_id=contact_id, text=text,
                type='out', time=time_str, timestamp=int(now.timestamp()), instance=inst
            )
            db_sql.session.add(new_msg)

        contact.last_msg = '📎 Arquivo'
        contact.last_msg_time = time_str
        db_sql.session.commit()

        return jsonify({'ok': True, 'msg_id': msg_id, 'key': res_data.get('key', {})})
    except Exception as e:
        print(f"Erro send_document: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/bot-message', methods=['POST'])
def bot_message_webhook():
    try:
        data = request.json
        if not data: return jsonify({'error': 'Body vazio'}), 400
        
        # Suporte para o array bruto do N8N/Evolution
        if isinstance(data, list) and len(data) > 0:
            data = data[0]

        if 'data' in data and 'key' in data.get('data', {}):
            # Formato Evolution Originário
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
            msg_id = f"bot_{int(datetime.datetime.now().timestamp())}_{str(phone)[-4:]}"
        
        if not inst or not phone or not text:
            return jsonify({'error': 'Faltam campos obrigatorios: instance/instanceId, phone (ou remoteJid), e text'}), 400
            
        now = datetime.datetime.now()
        time_str = now.strftime("%H:%M")
        contact_id = f"c_{phone}_{inst}"
        
        # Save Message
        new_msg = Message(
            id=msg_id,
            contact_id=contact_id,
            text=text,
            type='out',
            time=time_str,
            timestamp=int(now.timestamp()),
            instance=inst
        )
        db_sql.session.add(new_msg)
        
        # Update Contact
        contact = Contact.query.filter_by(id=contact_id).first()
        if contact:
            contact.last_msg = text
            contact.last_msg_time = time_str
        else:
            new_contact = Contact(
                id=contact_id, name=f"+{phone}", phone=phone,
                avatar=phone[0] if phone else "?", instance=inst,
                tags=['Novo Lead'], last_msg=text, last_msg_time=time_str, unread=0
            )
            db_sql.session.add(new_contact)
            
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
        socketio.emit('whatsapp_event', fake_event)
        
        return jsonify({"success": True, "message_id": msg_id}), 200
    except Exception as e:
        print(f"Erro bot-message: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/webhooks/evolution', methods=['POST'])
def webhook():
    try:
        data = request.json
        if not data: return 'OK', 200
        
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
            if 'audioMessage' in m:
                audio_info = m.get('audioMessage', {})
                msg_id = key.get('id', '')
                # Store [AUDIO_REF] with instance and message id so frontend can stream it
                text = f"[AUDIO_REF] {instance}|{msg_id}"
                print(f"[Audio] Guardando ref de audio: instance={instance} msg_id={msg_id}")
            else:
                text = m.get('conversation') or \
                       m.get('extendedTextMessage', {}).get('text') or \
                       m.get('buttonsResponseMessage', {}).get('selectedDisplayText') or \
                       m.get('listResponseMessage', {}).get('title') or \
                       m.get('imageMessage', {}).get('caption') or \
                       m.get('videoMessage', {}).get('caption') or \
                       m.get('documentMessage', {}).get('caption') or \
                       "[Mensagem N8N/Mídia]"

            now = datetime.datetime.now()
            time_str = now.strftime("%H:%M")
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
            else:
                contact.last_msg = text
                contact.last_msg_time = time_str
                if not fromMe:
                    contact.unread = (contact.unread or 0) + 1
            
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
                    instance=instance
                )
                db_sql.session.add(new_msg)
            db_sql.session.commit()

            # Emitir evento com texto processado para o frontend
            emit_data = dict(data)
            emit_data['_processed_text'] = text
            emit_data['_instance'] = instance
            socketio.emit('whatsapp_event', emit_data)
        return 'OK', 200
    except Exception as e:
        print(f"Erro webhook: {e}")
        return 'ERR', 500

@app.route('/api/contacts', methods=['GET'])
@auth_required
def get_contacts():
    user = User.query.get(request.user['id'])
    allowed_instances = user.instances or []
    
    if request.user.get('role') == 'admin':
        contacts = Contact.query.all()
    else:
        # Filter contacts by instances the user has access to
        contacts = Contact.query.filter(Contact.instance.in_(allowed_instances)).all()
        
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
            'unread': c.unread
        })
    return jsonify(contacts_list)

@app.route('/api/contacts/<id>', methods=['PUT'])
@auth_required
def update_contact(id):
    data = request.json
    new_name = data.get('name')
    
    if not new_name:
        return jsonify({'error': 'Nome é obrigatório'}), 400
        
    contact = Contact.query.filter_by(id=id).first()
    if contact:
        contact.name = new_name
        # Update avatar initial if it was autogenerated (was just phone first digit)
        if contact.avatar and len(contact.avatar) <= 1:
            contact.avatar = new_name[0].upper()
        
        db_sql.session.commit()
        return jsonify({
            'id': contact.id,
            'name': contact.name,
            'phone': contact.phone,
            'avatar': contact.avatar
        })
    
    return jsonify({'error': 'Contato não encontrado'}), 404

@app.route('/api/contacts/<id>/messages', methods=['GET'])
@auth_required
def get_messages(id):
    # Expect id to be the full c_phone_instance string
    msgs = Message.query.filter(Message.contact_id == id).order_by(Message.timestamp).all()
    
    msgs_list = []
    for m in msgs:
        msgs_list.append({
            'id': m.id,
            'text': m.text,
            'type': m.type,
            'time': m.time,
            'timestamp': m.timestamp
        })
    return jsonify(msgs_list)

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

@app.route('/api/media/<media_type>')
def stream_media(media_type):
    """Proxy de midia: busca o base64 da Evolution e retorna como stream.
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
        evo_url = f"{os.getenv('EVOLUTION_API_URL')}/chat/getBase64FromMediaMessage/{instance}"
        headers = {'apikey': os.getenv('EVOLUTION_API_KEY'), 'Content-Type': 'application/json'}
        payload = {"message": {"key": {"id": msg_id}}}
        print(f"[{media_type.capitalize()} Proxy] Buscando {media_type}: instance={instance} msg_id={msg_id}")
        res = requests.post(evo_url, json=payload, headers=headers, timeout=15)
        print(f"[{media_type.capitalize()} Proxy] status={res.status_code} resp_len={len(res.text)}")
        if res.status_code in (200, 201):
            resp_data = res.json()
            b64 = resp_data.get('base64')
            if b64:
                import base64 as b64lib
                audio_bytes = b64lib.b64decode(b64)
                
                # Default mimetypes based on requested media_type just in case the API doesn't return one
                default_mime = 'application/octet-stream'
                if media_type == 'audio': default_mime = 'audio/ogg'
                elif media_type == 'image': default_mime = 'image/jpeg'
                elif media_type == 'video': default_mime = 'video/mp4'
                
                mime = resp_data.get('mimetype') or default_mime
                return Response(audio_bytes, mimetype=mime,
                    headers={'Content-Disposition': 'inline', 'Accept-Ranges': 'bytes',
                             'Cache-Control': 'public, max-age=3600'})
        return jsonify({'error': f'Nao foi possivel buscar {media_type}', 'evo_status': res.status_code, 'raw': res.text[:500]}), 502
    except Exception as e:
        print(f"Erro stream_{media_type}: {e}")
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

@app.route('/')
def index_page():
    return send_from_directory(ROOT_DIR, 'index.html')

@app.route('/<path:path>')
def serve_frontend(path):
    if path in ('index.html', 'dashboard.html', 'admin.html'):
        return send_from_directory(ROOT_DIR, path)
    if path.startswith('css/') or path.startswith('js/'):
        return send_from_directory(ROOT_DIR, path)
    return jsonify({'error': 'Not found'}), 404

@socketio.on('connect')
def test_connect():
    print('>>> Cliente conectado ao SocketIO')

@socketio.on('join_company')
def on_join(company_id):
    join_room(company_id)
    print(f'Client joined room: {company_id}')

if __name__ == '__main__':
    port = int(os.getenv('PORT', 3008))
    print(f"Servidor Python rodando na porta {port}...")
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)
