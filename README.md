# A simple, modular API written in Python

Extend simply by adding files & functions to the 'modules' folder.
Great for quickly adding Python-specific functionality to any backend.

## Quickstart

> Assumes that _uv_ is already installed

> [https://docs.astral.sh/uv/](https://docs.astral.sh/uv/)

1. Clone repo

2. Setup venv

   ```bash
   uv venv
   uv pip install -r requirements.txt
   ```

3. Run server

   ```bash
    uv run python server.py
   ```

4. Visit/GET http://127.0.0.1:8000 or http://localhost:8000 in a browser to see a list of available endpoints

5. Try POSTing an endpoint (/call, /submit)

   > Note: Body must contain a dictionary / map / object with one of the following names -- "kwargs", "variables", "params", "inputs", "data", "args_named"

   ```bash
   curl -X POST http://127.0.0.1:8000/submit/url_download.download \
   -H "Content-Type: application/json"    \
   -d '{"kwargs": {"url": "https://www.youtube.com/watch?v=K--d5VQMUvY", "ext": "mp3"}}'
   ```
