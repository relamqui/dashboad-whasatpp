import os
from app import app, db_sql, Contact, Message

def fix_numbers():
    with app.app_context():
        print("Iniciando correção de números no banco de dados...")
        
        contacts = Contact.query.all()
        updated_contacts = 0
        
        for c in contacts:
            number = c.phone
            # Se tiver 13 digitos, comecar com 55 e o 5º digito for 9 (ex: 5535991345856)
            if number and len(number) == 13 and number.startswith('55') and number[4] == '9':
                new_number = number[:4] + number[5:]
                old_id = c.id
                new_id = f"c_{new_number}_{c.instance}"
                
                print(f"Atualizando contato: {number} -> {new_number}")
                
                # Atualizar via Raw SQL para evitar problemas de integridade com o ORM na Primary Key
                db_sql.session.execute(
                    db_sql.text("UPDATE message SET contact_id = :new_id WHERE contact_id = :old_id"),
                    {"new_id": new_id, "old_id": old_id}
                )
                
                db_sql.session.execute(
                    db_sql.text("UPDATE contact SET id = :new_id, phone = :new_phone, name = :new_name WHERE id = :old_id"),
                    {"new_id": new_id, "new_phone": new_number, "new_name": new_number if c.name == number else c.name, "old_id": old_id}
                )
                
                updated_contacts += 1
                
        db_sql.session.commit()
        print(f"Concluído! {updated_contacts} contatos foram corrigidos.")

if __name__ == '__main__':
    fix_numbers()
