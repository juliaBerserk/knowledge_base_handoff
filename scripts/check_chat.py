import json
import urllib.request

h = json.load(urllib.request.urlopen("http://127.0.0.1:8000/api/handoffs"))[0]
detail = json.load(urllib.request.urlopen(f"http://127.0.0.1:8000/api/handoffs/{h['id']}"))
print("messages", len(detail["messages"]))
for m in detail["messages"]:
    print(m["role"], m["content"][:120].replace("\n", " "))
