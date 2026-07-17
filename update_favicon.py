import os
import glob
import re

svg_favicon = '''<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><circle cx='50' cy='50' r='45' fill='%230d5c46'/><text x='50' y='72' font-size='65' font-family='Arial, sans-serif' font-weight='bold' fill='white' text-anchor='middle'>C</text></svg>">'''

html_files = glob.glob(r'd:\PROJETOS SNYKIA\dashboad whasatpp corpal\public\*.html')

for file in html_files:
    with open(file, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Se ja tiver um rel="icon", remove
    content = re.sub(r'<link[^>]+rel=["\']icon["\'][^>]*>\n?', '', content)
    
    # Insere o novo favicon abaixo do <title>
    content = re.sub(r'(<title>.*?</title>)', r'\1\n  ' + svg_favicon, content, flags=re.IGNORECASE)
    
    with open(file, 'w', encoding='utf-8') as f:
        f.write(content)

print(f"Updated {len(html_files)} HTML files with new favicon.")
