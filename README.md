
`uv run python -m hypercorn main:app --bind 0.0.0.0:8000`
или
`docker compose up -d`

перед этим не забудь создать .env файл и добавить туда GEMINI_API_KEY 
+ uv sync
