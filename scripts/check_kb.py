import json
import urllib.request

h = json.load(urllib.request.urlopen("http://127.0.0.1:8000/api/handoffs"))[0]
detail = json.load(urllib.request.urlopen(f"http://127.0.0.1:8000/api/handoffs/{h['id']}"))
print("knowledge", len(detail["knowledge"]))
print("categories", sorted({i["category"] for i in detail["knowledge"]}))
for i in detail["knowledge"][:5]:
    print("-", i["category"], i["title"][:60])
play = urllib.request.urlopen(f"http://127.0.0.1:8000/api/handoffs/{h['id']}/playbook.md").read().decode("utf-8")
print("playbook starts:", play.splitlines()[0])
print("hunter2" in play, "sk-demo" in play)
