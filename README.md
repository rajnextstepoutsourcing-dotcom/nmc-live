# NMC Live

Upload an NMC document (PDF/image) and download the NMC register PDF.

## Local run

```bash
pip install -r requirements.txt
playwright install chromium
uvicorn app:app --reload
```

Open:
- http://127.0.0.1:8000

## Render

This repo includes `render.yaml`.

Set env var:
- `GEMINI_API_KEY` (recommended for scanned images)

Build command:
- `pip install -r requirements.txt && playwright install --with-deps chromium`

Start command:
- `uvicorn app:app --host 0.0.0.0 --port $PORT`
