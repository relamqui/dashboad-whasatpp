
import os
import jwt
import uuid
import datetime
import pytz

def get_now():
    return datetime.datetime.now(pytz.timezone('America/Sao_Paulo'))

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

CORPAL_WEBHOOK_URL = 'https://n8n-n8n.ioms5g.easypanel.host/webhook/corpal-metrica'

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
    avatar = db_sql.Column(db_sql.String(10), nullable=True)
    instance = db_sql.Column(db_sql.String(100), nullable=True)
    tags = db_sql.Column(db_sql.JSON, default=['Novo Lead'])
    last_msg = db_sql.Column(db_sql.Text, nullable=True)
    last_msg_time = db_sql.Column(db_sql.String(10), nullable=True)
    unread = db_sql.Column(db_sql.Integer, default=0)
    assigned_to = db_sql.Column(db_sql.Integer, db_sql.ForeignKey('user.id'), nullable=True)
    assigned_name = db_sql.Column(db_sql.String(100), nullable=True)

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
    p = p.split('@')[0]
    p = "".join(filter(str.isdigit, p))
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
        'evolution_url': os.getenv('EVOLUTION_API_URL')
    })

@app.route('/api/bot/tags', methods=['POST'])
def add_bot_tag():
    """Rota para o N8N (ou outro bot) adicionar etiquetas via API"""
    data = request.json
    if not data:
        return jsonify({'error': 'Body vazio'}), 400
        
    phone = data.get('phone')
    inst = data.get('instance')
    filial = data.get('filial')
    setor = data.get('setor')
    custom_tag = data.get('tag')
    
    if not phone or not inst:
        return jsonify({'error': 'phone e instance são obrigatórios'}), 400
        
    phone = normalize_br_phone(str(phone).strip())
    inst = str(inst).strip()
    contact_id = f"c_{phone}_{inst}"
    
    print(f"[BOT/TAGS] Buscando contato: id={contact_id}, phone={phone}, instance={inst}")
    
    # Tentativa 1: busca pelo ID exato
    contact = Contact.query.filter_by(id=contact_id).first()
    
    # Tentativa 2: busca pelo phone + instance (caso o ID tenha alguma diferença)
    if not contact:
        contact = Contact.query.filter_by(phone=phone, instance=inst).first()
        if contact:
            print(f"[BOT/TAGS] Encontrado por phone+instance: {contact.id}")
    
    # Tentativa 3: busca apenas pelo phone (pega o mais recente)
    if not contact:
        contact = Contact.query.filter_by(phone=phone).first()
        if contact:
            print(f"[BOT/TAGS] Encontrado apenas por phone: {contact.id} (instance no banco: {contact.instance})")
    
    if not contact:
        print(f"[BOT/TAGS] Contato não encontrado, criando novo: {contact_id}")
        contact = Contact(
            id=contact_id, name=f"+{phone}", phone=phone,
            avatar=phone[0] if phone else "?", instance=inst,
            tags=['Novo Lead'], last_msg='', last_msg_time='', unread=0
        )
        db_sql.session.add(contact)
        db_sql.session.flush()
        
    new_tags = []
    
    if filial and setor:
        new_tags.append(f"{filial} - {setor}")
    elif filial:
        new_tags.append(filial)
            
    if custom_tag:
        new_tags.append(custom_tag)
        
    if new_tags:
        contact.tags = new_tags
        flag_modified(contact, 'tags')
        db_sql.session.commit()
        print(f"[BOT/TAGS] Tags atualizadas para '{contact.id}': {contact.tags}")
        _inst_room = contact.instance or 'unknown'
        socketio.emit('chat_tags_updated', {
            'id': contact.id,
            'tags': list(contact.tags)
        }, room=f'instance_{_inst_room}')
        socketio.emit('chat_tags_updated', {
            'id': contact.id,
            'tags': list(contact.tags)
        }, room='admin')
    else:
        print(f"[BOT/TAGS] Nenhuma tag para aplicar (filial={filial}, setor={setor}, tag={custom_tag})")
        
    return jsonify({'success': True, 'contact_id': contact.id, 'tags': contact.tags}), 200

# ─── Webhooks Evolution API ────────────────────────────────────────────────────────────

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
            'phone': u.phone,
            'role': u.role,
            'instances': u.instances or []
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
    
    new_user = User(
        name=data.get('name'),
        email=data.get('email'),
        phone=data.get('phone'),
        password=data.get('password'),
        role=data.get('role', 'user') if data.get('role') in ('user', 'gestor') else 'user',
        instances=[],
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

@app.route('/api/admin/filiais', methods=['GET', 'POST'])
@auth_required
@admin_or_gestor_required
def manage_filiais():
    if request.method == 'POST':
        data = request.json
        name = data.get('name')
        instance = data.get('instance')
        
        if not name or not instance:
            return jsonify({'error': 'Nome e Instância são obrigatórios'}), 400
            
        user = User.query.get(request.user['id'])
        if user.role == 'gestor' and instance not in (user.instances or []):
            return jsonify({'error': 'Você não tem permissão para gerenciar esta instância.'}), 403

        nova_filial = Filial(name=name, instance=instance)
        db_sql.session.add(nova_filial)
        db_sql.session.commit()
        return jsonify({'id': nova_filial.id, 'name': nova_filial.name, 'instance': nova_filial.instance}), 201

    user = User.query.get(request.user['id'])
    allowed_instances = user.instances or [] if user.role == 'gestor' else None

    if allowed_instances is not None:
        filiais = Filial.query.filter(Filial.instance.in_(allowed_instances)).all()
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
    if user.role == 'gestor' and filial.instance not in (user.instances or []):
        return jsonify({'error': 'Sem permissão para esta filial.'}), 403

    if request.method == 'PUT':
        data = request.json
        name = data.get('name', filial.name)
        instance = data.get('instance', filial.instance)
        if user.role == 'gestor' and instance not in (user.instances or []):
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
@admin_or_gestor_required
def manage_setores():
    if request.method == 'POST':
        data = request.json
        name = data.get('name')
        filial_id = data.get('filial_id')
        
        if not name or not filial_id:
            return jsonify({'error': 'Nome e Filial são obrigatórios'}), 400

        filial = Filial.query.get(filial_id)
        if not filial:
            return jsonify({'error': 'Filial não encontrada'}), 400

        user = User.query.get(request.user['id'])
        if user.role == 'gestor' and filial.instance not in (user.instances or []):
            return jsonify({'error': 'Sem permissão para esta filial.'}), 403

        novo_setor = Setor(name=name, filial_id=filial_id, filial_name=filial.name)
        db_sql.session.add(novo_setor)
        db_sql.session.commit()
        return jsonify({'id': novo_setor.id, 'name': novo_setor.name, 'filial_id': novo_setor.filial_id, 'filial_name': novo_setor.filial_name}), 201

    user = User.query.get(request.user['id'])
    if user.role == 'gestor':
        allowed_instances = user.instances or []
        allowed_filiais = Filial.query.filter(Filial.instance.in_(allowed_instances)).all()
        allowed_f_ids = [f.id for f in allowed_filiais]
        setores = Setor.query.filter(Setor.filial_id.in_(allowed_f_ids)).all()
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
    if user.role == 'gestor' and filial and filial.instance not in (user.instances or []):
        return jsonify({'error': 'Sem permissão para este setor.'}), 403

    if request.method == 'PUT':
        data = request.json
        new_name = data.get('name', setor.name)
        new_filial_id = data.get('filial_id', setor.filial_id)
        
        if new_filial_id:
            new_filial = Filial.query.get(new_filial_id)
            if user.role == 'gestor' and new_filial and new_filial.instance not in (user.instances or []):
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
    allowed_instances = set(user_req.instances or []) if user_req.role == 'gestor' else None

    if request.method == 'POST':
        data = request.json
        f_id = data.get('filial_id')
        s_id = data.get('setor_id')
        if not f_id or not s_id:
            return jsonify({'error': 'Filial e Setor são obrigatórios'}), 400

        email = data.get('email')
        instances_to_assign = set(data.get('instances', []))

        if allowed_instances is not None:
            if not instances_to_assign.issubset(allowed_instances) or not instances_to_assign:
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
        visible_users = [u for u in all_users if set(u.instances or []).intersection(allowed_instances)]
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
    allowed_instances = set(user_req.instances or []) if user_req.role == 'gestor' else None
    
    if allowed_instances is not None:
        if not set(target_user.instances or []).intersection(allowed_instances):
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

    # --- Instance permission check ---
    if request.user.get('role') != 'admin':
        user_obj_check = User.query.get(request.user['id'])
        allowed = user_obj_check.instances or []
        if inst not in allowed:
            return jsonify({'error': 'Você não tem permissão para enviar mensagens nesta instância.'}), 403

    # --- Chat locking check ---
    contact_id_check = f"c_{number}_{inst}"
    locked_contact = Contact.query.filter_by(id=contact_id_check).first()
    if locked_contact and locked_contact.assigned_to and locked_contact.assigned_to != request.user['id']:
        return jsonify({'error': f'Chat sendo atendido por {locked_contact.assigned_name or "outro atendente"}. Não é possível enviar mensagens.'}), 403

    try:
        now = get_now()
        time_str = now.strftime("%H:%M")
        
        # Chamada para a API externa (Evolution)
        url = f"{os.getenv('EVOLUTION_API_URL')}/message/sendText/{inst}"
        headers = {'apikey': os.getenv('EVOLUTION_API_KEY')}
        payload = {"number": number, "text": text}
        res = requests.post(url, json=payload, headers=headers)
        res_data = res.json()
        
        msg_id = res_data.get('key', {}).get('id') or res_data.get('messageId') or f"out_{int(now.timestamp())}"
        contact_id = f"c_{number}_{inst}"
        
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
                instance=inst
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
    """Envia audio gravado pelo atendente ao cliente via Evolution API."""
    data = request.json
    inst = data.get('instance')
    number = "".join(filter(str.isdigit, str(data.get('number', ''))))
    number = normalize_br_phone(number)
    audio_b64 = data.get('audio', '')

    if not inst or not number or not audio_b64:
        return jsonify({'error': 'instance, number e audio são obrigatórios'}), 400

    try:
        now = get_now()
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
        now = get_now()
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
        now = get_now()
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
        now = get_now()
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
            msg_id = f"bot_{int(get_now().timestamp())}_{str(phone)[-4:]}"
        
        if not inst or not phone or not text:
            return jsonify({'error': 'Faltam campos obrigatorios: instance/instanceId, phone (ou remoteJid), e text'}), 400
            
        now = get_now()
        time_str = now.strftime("%H:%M")
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
                id=contact_id, name=f"+{phone}", phone=phone,
                avatar=phone[0] if phone else "?", instance=inst,
                tags=['Novo Lead', 'BOT'], last_msg=text, last_msg_time=time_str, unread=0
            )
            db_sql.session.add(new_contact)
            
        db_sql.session.flush()

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

            now = get_now()
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
                    instance=instance
                )
                db_sql.session.add(new_msg)
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
    contact.tags = [atendente_tag]
    flag_modified(contact, 'tags')
    
    db_sql.session.commit()
    
    # Corpal Webhook — evento atender
    try:
        now = get_now()
        _filial_a = None
        _setor_a = None
        if user.filial_id:
            _f = Filial.query.get(user.filial_id)
            _filial_a = _f.name if _f else None
        if user.setor_id:
            _s = Setor.query.get(user.setor_id)
            _setor_a = _s.name if _s else None
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
    
    tags = list(contact.tags or [])
    atendente_tag = f"Atendente: {old_name}"
    if atendente_tag in tags:
        tags.remove(atendente_tag)
    contact.tags = tags
    flag_modified(contact, 'tags')
    
    db_sql.session.commit()
    
    # Corpal Webhook — evento finalizar
    try:
        now = get_now()
        _old_user = User.query.get(request.user['id'])
        _filial_r = None
        _setor_r = None
        if _old_user:
            if _old_user.filial_id:
                _f = Filial.query.get(_old_user.filial_id)
                _filial_r = _f.name if _f else None
            if _old_user.setor_id:
                _s = Setor.query.get(_old_user.setor_id)
                _setor_r = _s.name if _s else None
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
    if path.lower().endswith(('.png', '.jpg', '.jpeg', '.svg', '.ico', '.webp')):
        return send_from_directory(ROOT_DIR, path)
    return jsonify({'error': 'Not found'}), 404

@socketio.on('connect')
def test_connect():
    print('>>> Cliente conectado ao SocketIO')

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
