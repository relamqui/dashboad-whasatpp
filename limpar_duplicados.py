import os
from sqlalchemy.orm.attributes import flag_modified
from app import app, db_sql, Contact, Message, normalize_br_phone

def run_deduplication():
    with app.app_context():
        contacts = Contact.query.all()
        
        # Group by (normalized_phone, instance)
        groups = {}
        for c in contacts:
            n_phone = normalize_br_phone(c.phone)
            inst = c.instance or "unknown"
            key = (n_phone, inst)
            
            if key not in groups:
                groups[key] = []
            groups[key].append(c)

        merge_count = 0
        deleted_count = 0

        for (n_phone, inst), group in groups.items():
            if len(group) <= 1:
                continue
                
            print(f"\\nEncontrados {len(group)} contatos duplicados para {n_phone} na instância {inst}")
            
            # Tentar encontrar o contato "ideal" (o que tem o ID padronizado corretamente)
            ideal_id = f"c_{n_phone}_{inst}"
            primary = next((c for c in group if c.id == ideal_id), None)
            
            # Se não achar o ideal, pega o primeiro da lista
            if not primary:
                primary = group[0]
            
            print(f"   -\u003e Mantendo conta principal: ID={primary.id} (Nome: {primary.name})")

            # Merge duplicates into primary
            for dupe in group:
                if dupe.id == primary.id:
                    continue
                
                print(f"   -\u003e Mesclando informações e removendo duplicado: ID={dupe.id}")
                
                # Transfere mensagens
                msgs = Message.query.filter_by(contact_id=dupe.id).all()
                for m in msgs:
                    m.contact_id = primary.id
                
                if msgs:
                    print(f"      -\u003e {len(msgs)} mensagens movidas.")
                
                # Junta as tags, se existirem
                if dupe.tags:
                    p_tags = list(primary.tags or [])
                    for t in dupe.tags:
                        if t not in p_tags:
                            p_tags.append(t)
                    primary.tags = p_tags
                    flag_modified(primary, 'tags')
                
                # Soma as mensagens não lidas
                primary.unread = (primary.unread or 0) + (dupe.unread or 0)
                
                # Se o duplicado tem um nome diferente do número, aproveita
                if dupe.name and not normalize_br_phone(dupe.name) and not normalize_br_phone(primary.name) != primary.name:
                     # Basic heuristic: if primary name is just a phone and dupe has a real name, use dupe's name
                     if not dupe.name.startswith('+') and not dupe.name.isdigit():
                         primary.name = dupe.name

                # Deleta o duplicado
                db_sql.session.delete(dupe)
                deleted_count += 1
            
            merge_count += 1

        print(f"\\n--- Resumo ---\nAglutinações realizadas: {merge_count}\nContas duplicadas removidas: {deleted_count}")
        
        # Confirma as alterações no banco
        db_sql.session.commit()
        print("Banco de dados otimizado e salvo com sucesso!")
        return {"merge_count": merge_count, "deleted_count": deleted_count}

if __name__ == "__main__":
    print("Iniciando varredura de deduplicação no banco de dados...")
    run_deduplication()
