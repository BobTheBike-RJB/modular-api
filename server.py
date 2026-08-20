# server.py
import importlib.util
import inspect
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
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


class CallRequest(BaseModel):
    args: list = []
    kwargs: dict = {}


@app.get("/")
def list_functions() -> dict:
    """List available functions with their signatures."""
    return {
        key: str(inspect.signature(fn))
        for key, fn in registry.items()
    }


@app.post("/call/{target}")
def call_function(target: str, req: CallRequest):
    fn = registry.get(target)
    if fn is None:
        raise HTTPException(404, f"Unknown function: {target}")
    try:
        return {"result": fn(*req.args, **req.kwargs)}
    except Exception as e:
        raise HTTPException(400, f"{type(e).__name__}: {e}")


load_modules()

if __name__ == "__main__":
    uvicorn.run(app, host=config.HOST, port=config.PORT)