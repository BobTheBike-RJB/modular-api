# server.py
import importlib.util
import inspect
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, model_validator
import uvicorn

import config

app = FastAPI(title="Module Server")
registry: dict[str, callable] = {}


def load_modules() -> None:
    """Discover public functions in every .py file under MODULES_DIR."""
    modules_path = Path(config.MODULES_DIR)
    modules_path.mkdir(exist_ok=True)

    for file in modules_path.glob("*.py"):
        if file.stem.startswith("_"):
            continue

        spec = importlib.util.spec_from_file_location(file.stem, file)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        for name, fn in inspect.getmembers(module, inspect.isfunction):
            if name.startswith("_"):
                continue
            # only functions defined in this file, not imports
            if fn.__module__ == module.__name__:
                registry[f"{file.stem}.{name}"] = fn

KWARGS_ALIASES = {"kwargs", "variables", "params", "inputs", "data", "args_named"}
class CallRequest(BaseModel):
    args: list = []
    kwargs: dict = {}

    @model_validator(mode="before")
    @classmethod
    def normalise_kwargs(cls, values):
        for alias in KWARGS_ALIASES:
            if alias in values and alias != "kwargs":
                values["kwargs"] = values.pop(alias)
                break
        return values

@app.get("/")
def list_functions() -> dict:
    """List available functions with their signatures."""
    return {
        key: str(inspect.signature(fn))
        for key, fn in registry.items()
    }

# Synchronous endpoint
@app.post("/call/{target}")
def call_function(target: str, req: CallRequest):
    fn = registry.get(target)
    if fn is None:
        raise HTTPException(404, f"Unknown function: {target}")
    try:
        return {"result": fn(*req.args, **req.kwargs)}
    except Exception as e:
        raise HTTPException(400, f"{type(e).__name__}: {e}")

# Asynchronous endpoint
import uuid
from concurrent.futures import ThreadPoolExecutor

jobs: dict[str, dict] = {}
executor = ThreadPoolExecutor(max_workers=4)

def _run_job(job_id: str, fn, args, kwargs):
    jobs[job_id]["status"] = "running"
    try:
        jobs[job_id]["result"] = fn(*args, **kwargs)
        jobs[job_id]["status"] = "done"
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = f"{type(e).__name__}: {e}"

@app.post("/submit/{target}")
def submit_function(target: str, req: CallRequest):
    fn = registry.get(target)
    if fn is None:
        raise HTTPException(404, f"Unknown function: {target}")
    job_id = uuid.uuid4().hex
    jobs[job_id] = {"status": "queued", "result": None}
    executor.submit(_run_job, job_id, fn, req.args, req.kwargs)
    return {"job_id": job_id}

@app.get("/job/{job_id}")
def job_status(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown job")
    return job


load_modules()

if __name__ == "__main__":
    uvicorn.run(app, host=config.HOST, port=config.PORT)