# K-desk

Agente inteligente de suporte de TI com triagem automática, classificação de prioridade e registro de tickets.

## Produção
- URL: https://files-mentioned-by-the-user-support.vercel.app

## Stack atual
- Vercel (Python/Flask serverless)
- Vercel Postgres (via `DATABASE_URL`)
- Base de conhecimento em CSV (`data/support_knowledge_base.csv`)
- Registro de tickets 100% via SQL (sem n8n/Google Sheets)

## Endpoints
- `GET /api/health`
- `POST /api/triage`
- `POST /api/chat` (compatível com a interface web)
- `POST /api/init-db`

## Variáveis de ambiente
- `DATABASE_URL`
- `KB_CSV_PATH` (opcional, default `data/support_knowledge_base.csv`)

## Importar novo CSV da base

Use o script abaixo para validar o schema e atualizar `data/support_knowledge_base.csv`:

```bash
python scripts/import_kb.py --source "C:/Users/dudul/Downloads/support_knowledge_base (2).csv"
```

Depois disso, redeploy na Vercel (ou reinicie localmente) para carregar a base nova.
