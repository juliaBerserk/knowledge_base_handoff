from fastapi import FastAPI, File, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import WEB_DIR, ensure_dirs
from app.database import init_db
from app.llm import llm_enabled
from app import services

ensure_dirs()
init_db()

app = FastAPI(title="Эстафета", description="База знаний для преемника по документам сотрудника")


class HandoffIn(BaseModel):
    employee_name: str = Field(min_length=2, max_length=120)
    role: str = Field(min_length=2, max_length=160)
    department: str = ""
    successor_name: str = ""
    last_day: str = ""
    notes: str = ""


class QuestionIn(BaseModel):
    question: str = Field(min_length=2, max_length=2000)


@app.get("/api/health")
def health():
    return {"ok": True, "llm": llm_enabled()}


@app.get("/api/handoffs")
def api_list():
    return services.list_handoffs()


@app.post("/api/handoffs")
def api_create(body: HandoffIn):
    return services.create_handoff(body.model_dump())


@app.post("/api/demo")
def api_demo():
    return services.seed_demo()


@app.get("/api/handoffs/{handoff_id}")
def api_get(handoff_id: int):
    return services.get_handoff(handoff_id)


@app.delete("/api/handoffs/{handoff_id}")
def api_delete(handoff_id: int):
    services.delete_handoff(handoff_id)
    return {"ok": True}


@app.post("/api/handoffs/{handoff_id}/documents")
async def api_upload(handoff_id: int, file: UploadFile = File(...)):
    return await services.save_upload(handoff_id, file)


@app.post("/api/handoffs/{handoff_id}/process")
def api_process(handoff_id: int):
    return services.process_handoff(handoff_id)


@app.post("/api/handoffs/{handoff_id}/ask")
def api_ask(handoff_id: int, body: QuestionIn):
    return services.ask(handoff_id, body.question)


@app.get("/api/handoffs/{handoff_id}/playbook.md")
def api_playbook(handoff_id: int):
    md = services.playbook(handoff_id)
    headers = {"Content-Disposition": f'attachment; filename="estafeta-{handoff_id}.md"'}
    return PlainTextResponse(md, media_type="text/markdown; charset=utf-8", headers=headers)


index_file = WEB_DIR / "index.html"
if WEB_DIR.exists():
    app.mount("/assets", StaticFiles(directory=WEB_DIR / "assets"), name="assets")


@app.get("/")
def index():
    return FileResponse(index_file)


@app.get("/{path:path}")
def spa(path: str):
    candidate = WEB_DIR / path
    if candidate.is_file():
        return FileResponse(candidate)
    return FileResponse(index_file)
