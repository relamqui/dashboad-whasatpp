import re
file_path = r'd:\PROJETOS SNYKIA\dashboad whasatpp corpal\public\js\dashboard.js'
with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

content = re.sub(r'(newMsg\.id\s*=\s*realId;)\s*\}', r'\1\n      renderMessages(currentChat.messages);\n    }', content)

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(content)
