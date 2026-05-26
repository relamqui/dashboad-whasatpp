import requests

url = "http://localhost:3008/api/bot/tags"
payload = {
    "instance": "corpal",
    "phone": "553591345856",
    "filial": "Matriz",
    "setor": "Comercial"
}
headers = {
    "Content-Type": "application/json"
}
try:
    res = requests.post(url, json=payload, headers=headers)
    print("Status:", res.status_code)
    print("Response:", res.text)
except Exception as e:
    print("Error:", e)
