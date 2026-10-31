"""Local development server for validating dashboard data rendering.

Use this only when running the dashboard locally to verify that the generated
JSON data loads correctly and displays as expected in index.html.
"""

from pathlib import Path
from typing import Dict

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

app = FastAPI(title="LLM-Booster Dashboard")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def dashboard_file(relative_path: str) -> FileResponse:
    file_path = (BASE_DIR / relative_path).resolve()
    try:
        file_path.relative_to(BASE_DIR)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="File not found") from exc
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path)


def data_file(relative_path: str) -> FileResponse:
    file_path = (DATA_DIR / relative_path).resolve()
    try:
        file_path.relative_to(DATA_DIR)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Data file not found") from exc
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Data file not found")
    return FileResponse(file_path)


@app.get("/healthz")
def healthz() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/")
@app.get("/index")
def index() -> FileResponse:
    return FileResponse(BASE_DIR / "index.html")


@app.get("/index.html")
def index_html() -> FileResponse:
    return dashboard_file("index.html")


@app.get("/login.html")
def login_html() -> FileResponse:
    return dashboard_file("login.html")


@app.get("/data/index")
@app.get("/data/index.json")
def data_index() -> FileResponse:
    return data_file("index.json")


@app.get("/data/aggregated/index")
@app.get("/data/aggregated/index.json")
def aggregated_index() -> FileResponse:
    return data_file("aggregated/index.json")


@app.get("/data/aggregated/{file_name}")
def aggregated_file(file_name: str) -> FileResponse:
    return data_file(f"aggregated/{file_name}")


@app.get("/data/kernel-breakdown/index")
@app.get("/data/kernel-breakdown/index.json")
def kernel_breakdown_index() -> FileResponse:
    return data_file("kernel-breakdown/index.json")


@app.get("/data/kernel-breakdown/{file_name}")
def kernel_breakdown_file(file_name: str) -> FileResponse:
    return data_file(f"kernel-breakdown/{file_name}")


@app.get("/data/kernel-module-edits")
@app.get("/data/kernel-module-edits.json")
@app.get("/data/kernel_module_edits.json")
def kernel_module_edits() -> FileResponse:
    return data_file("kernel_module_edits.json")


@app.get("/data/{file_path:path}")
def data_path(file_path: str) -> FileResponse:
    return data_file(file_path)


# Serve index.html, login.html, data/*.json, and any future static dashboard files.
app.mount("/", StaticFiles(directory=BASE_DIR, html=True), name="dashboard-static")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
