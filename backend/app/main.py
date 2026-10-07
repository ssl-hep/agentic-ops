from fastapi import FastAPI

app = FastAPI(title="agentic-ops backend")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
