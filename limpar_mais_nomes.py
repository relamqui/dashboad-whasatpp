from app import app, db_sql, Contact

def remove_plus_from_names():
    with app.app_context():
        # Busca contatos com '+'
        contacts = Contact.query.filter(Contact.name.like('+%') | Contact.phone.like('+%')).all()
        
        updated = 0
        for c in contacts:
            changed = False
            if c.name and c.name.startswith('+'):
                c.name = c.name.replace('+', '').strip()
                changed = True
                
            if c.phone and c.phone.startswith('+'):
                c.phone = c.phone.replace('+', '').strip()
                changed = True
                
            if changed:
                updated += 1
                
        if updated > 0:
            db_sql.session.commit()
            print(f"Limpeza concluída. {updated} contatos atualizados na base oficial.")
        else:
            print("Nenhum contato encontrado com '+' na base oficial.")

if __name__ == '__main__':
    remove_plus_from_names()
