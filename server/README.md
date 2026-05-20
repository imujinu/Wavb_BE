# Recordoc Backend

FastAPI backend for the first VoiceDoc feature: upload an audio file, transcribe it, and return a Korean summary.

## Setup

```bash
uv sync
copy .env.example .env
```

Set `OPENAI_API_KEY` in `.env`.

## Run

```bash
uv run uvicorn main:app --reload
```

## API

### `GET /health`

Returns:

```json
{ "status": "ok" }
```

### `POST /audio/summarize`

Request: `multipart/form-data`

- `file`: `.m4a`, `.mp3`, `.wav`, or `.webm`

Returns:

```json
{
  "transcript": "음성에서 추출된 텍스트",
  "summary": "요약 결과"
}
```

## Test

```bash
uv run pytest
```
