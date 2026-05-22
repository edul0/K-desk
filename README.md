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
- `GET /api/tickets?status=Aberto|Em andamento|Finalizado`
- `POST /api/tickets/{ticket_id}/status`
- `GET /api/tickets/export.csv`

## Variáveis de ambiente
- `DATABASE_URL`
- `KB_CSV_PATH` (opcional, default `data/support_knowledge_base.csv`)
- `GEMINI_API_KEY` (opcional, para assistência de IA)
- `GEMINI_MODEL` (opcional, default `gemini-1.5-flash`)

> Segurança: não coloque chave de API direto no código. Configure em variável de ambiente no Vercel.

## Importar novo CSV da base

Use o script abaixo para validar o schema e atualizar `data/support_knowledge_base.csv`:

```bash
python scripts/import_kb.py --source "C:/Users/dudul/Downloads/support_knowledge_base (2).csv"
```

Depois disso, redeploy na Vercel (ou reinicie localmente) para carregar a base nova.
