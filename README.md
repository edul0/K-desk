# K-desk

Agente inteligente de suporte de TI com triagem automática, classificação de prioridade e registro de tickets.

## Produção
- URL: https://files-mentioned-by-the-user-support.vercel.app

## Stack atual
- Vercel (Python/Flask serverless)
- Vercel Postgres (via `DATABASE_URL`)
- Base de conhecimento em CSV (`data/support_knowledge_base.csv`)

## Endpoints
- `GET /api/health`
- `POST /api/triage`
- `POST /api/init-db`

## Variáveis de ambiente
- `DATABASE_URL`
- `KB_CSV_PATH` (opcional, default `data/support_knowledge_base.csv`)
