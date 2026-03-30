import os
os.environ['TESTING'] = 'true'

from app import app, db_sql, User, Filial, Setor
import json

def run_tests():
    client = app.test_client()
    with app.app_context():
        # Ensure clean state for test users
        users_to_del = User.query.filter(User.email.in_(['gestor_test@test.com', 'user_test@test.com'])).all()
        for u in users_to_del:
            db_sql.session.delete(u)
        
        filiais_to_del = Filial.query.filter_by(name='Filial Teste').all()
        for f in filiais_to_del:
            Setor.query.filter_by(filial_id=f.id).delete()
            db_sql.session.delete(f)
            
        db_sql.session.commit()

        # Get Admin token
        admin = User.query.filter_by(role='admin').first()
        if not admin:
            print("ERROR: Admin user not found.")
            return

        res = client.post('/api/auth/login', json={'email': admin.email, 'password': admin.password})
        if res.status_code != 200:
            print("ERROR: Admin login failed.")
            return
            
        admin_token = res.json.get('token')
        admin_headers = {'Authorization': f'Bearer {admin_token}'}

        # 1. Admin creates Gestor
        print("1. Admin creating Gestor...")
        res = client.post('/api/admin/users', headers=admin_headers, json={
            'name': 'Gestor Test', 'email': 'gestor_test@test.com', 'password': '123'
        })
        if res.status_code != 201:
            print("ERROR: Could not create Gestor:", res.json)
            return
        
        gestor_id = res.json['id']
        
        # Admin updates Gestor role to 'gestor' directly in DB since create_user only creates 'user'
        # Wait, the current API doesn't allow changing role in PUT /api/admin/users/<id>.
        # I must do it via DB for the test.
        gestor_user = User.query.get(gestor_id)
        gestor_user.role = 'gestor'
        gestor_user.instances = ['InstanciaAlpha']
        db_sql.session.commit()

        # Gestor login
        res = client.post('/api/auth/login', json={'email': 'gestor_test@test.com', 'password': '123'})
        gestor_token = res.json.get('token')
        gestor_headers = {'Authorization': f'Bearer {gestor_token}'}

        # 2. Gestor tries to create Filial in unauthorized instance
        print("2. Gestor creating Filial (unauthorized)...")
        res = client.post('/api/admin/filiais', headers=gestor_headers, json={
            'name': 'Filial Hack', 'instance': 'InstanciaBeta'
        })
        assert res.status_code == 403, f"Expected 403, got {res.status_code}"
        
        # 3. Gestor creates Filial in authorized instance
        print("3. Gestor creating Filial (authorized)...")
        res = client.post('/api/admin/filiais', headers=gestor_headers, json={
            'name': 'Filial Teste', 'instance': 'InstanciaAlpha'
        })
        assert res.status_code == 201, f"Expected 201, got {res.status_code}: {res.json}"
        filial_id = res.json['id']

        # 4. Gestor creates Setor
        print("4. Gestor creating Setor...")
        res = client.post('/api/admin/setores', headers=gestor_headers, json={
            'name': 'Vendas', 'filial_id': filial_id
        })
        assert res.status_code == 201, f"Expected 201, got {res.status_code}: {res.json}"
        setor_id = res.json['id']

        # 5. Gestor creates Sub-User
        print("5. Gestor creating Sub-User...")
        res = client.post('/api/gestor/users', headers=gestor_headers, json={
            'name': 'Vendedor 1', 'email': 'user_test@test.com', 'password': '123',
            'instances': ['InstanciaAlpha'], 'filial_id': filial_id, 'setor_id': setor_id
        })
        assert res.status_code == 201, f"Expected 201, got {res.status_code}: {res.json}"
        user_id = res.json['id']

        # 6. Verify GET lists
        res = client.get('/api/gestor/users', headers=gestor_headers)
        assert len([u for u in res.json if u['email'] == 'user_test@test.com']) == 1

        print("ALL TESTS PASSED SUCCESSFULLY!")

if __name__ == '__main__':
    run_tests()
