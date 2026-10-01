import json
import urllib.request

handoffs = json.load(urllib.request.urlopen("http://127.0.0.1:8000/api/handoffs"))
hid = handoffs[0]["id"]
body = json.dumps({"question": "Как искать логи charge_id и что делать если платежи не проходят?"}).encode()
req = urllib.request.Request(
    f"http://127.0.0.1:8000/api/handoffs/{hid}/ask",
    data=body,
    headers={"Content-Type": "application/json"},
)
result = json.load(urllib.request.urlopen(req, timeout=30))
print(result["answer"][:800])
print("CITES", [c["filename"] for c in result["citations"][:5]])
