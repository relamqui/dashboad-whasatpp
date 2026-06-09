import sqlite3
import os

# Connect to the SQLite database
DB_PATH = os.path.join(os.getcwd(), 'data', 'wpcrm.db')

if not os.path.exists(DB_PATH):
    print(f"Database not found at {DB_PATH}")
    exit(1)

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

try:
    print("Iniciando backfill das mensagens...")
    
    # Check if sender_id exists
    cursor.execute("PRAGMA table_info(message)")
    columns = [info[1] for info in cursor.fetchall()]
    
    if 'ack' not in columns:
        cursor.execute("ALTER TABLE message ADD COLUMN ack INTEGER DEFAULT 0")
        
    if 'sender_id' not in columns:
        print("Adicionando coluna 'sender_id'...")
        cursor.execute("ALTER TABLE message ADD COLUMN sender_id INTEGER REFERENCES \"user\"(id)")
        conn.commit()

    # Obter contatos e seus responsáveis
    cursor.execute("SELECT id, assigned_to FROM contact WHERE assigned_to IS NOT NULL")
    contacts = cursor.fetchall()

    updated_count = 0
    for contact_id, assigned_to in contacts:
        # Atualizar todas as mensagens 'out' sem sender_id deste contato
        cursor.execute("""
            UPDATE message
            SET sender_id = ?
            WHERE contact_id = ? AND type = 'out' AND sender_id IS NULL
        """, (assigned_to, contact_id))
        updated_count += cursor.rowcount

    conn.commit()
    print(f"Backfill concluído! {updated_count} mensagens antigas foram vinculadas aos atendentes.")

except Exception as e:
    print(f"Erro ao executar o backfill: {e}")
finally:
    conn.close()
