import json
import os
from datetime import datetime

import psycopg


CREATE_TABLE_SQL = '''
CREATE TABLE IF NOT EXISTS tickets (
    id BIGSERIAL PRIMARY KEY,
    ticket_id TEXT UNIQUE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    requester_name TEXT NOT NULL,
    requester_email TEXT NOT NULL,
    description TEXT NOT NULL,
    kb_article_id TEXT NOT NULL,
    service TEXT NOT NULL,
    category TEXT NOT NULL,
    priority TEXT NOT NULL,
    estimated_resolution_time TEXT NOT NULL,
    escalation_required BOOLEAN NOT NULL,
    escalation_reason TEXT,
    collected_fields_json JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'Aberto'
);
'''


def _conn():
    db_url = os.environ.get("DATABASE_URL", "").strip()
    if not db_url:
        raise RuntimeError("Missing DATABASE_URL")
    return psycopg.connect(db_url)


def init_db() -> None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
        conn.commit()


def _next_ticket_id() -> str:
    return f"TKT-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"


def append_ticket(
    requester_name: str,
    requester_email: str,
    description: str,
    kb_article_id: str,
    service: str,
    category: str,
    priority: str,
    estimated_resolution_time: str,
    escalation_required: bool,
    escalation_reason: str,
    collected_fields: dict,
) -> str:
    ticket_id = _next_ticket_id()
    now = datetime.utcnow()

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
            cur.execute(
                '''
                INSERT INTO tickets (
                    ticket_id, created_at, requester_name, requester_email, description,
                    kb_article_id, service, category, priority, estimated_resolution_time,
                    escalation_required, escalation_reason, collected_fields_json, status
                ) VALUES (
                    %(ticket_id)s, %(created_at)s, %(requester_name)s, %(requester_email)s, %(description)s,
                    %(kb_article_id)s, %(service)s, %(category)s, %(priority)s, %(estimated_resolution_time)s,
                    %(escalation_required)s, %(escalation_reason)s, %(collected_fields_json)s::jsonb, 'Aberto'
                )
                ''',
                {
                    "ticket_id": ticket_id,
                    "created_at": now,
                    "requester_name": requester_name,
                    "requester_email": requester_email,
                    "description": description,
                    "kb_article_id": kb_article_id,
                    "service": service,
                    "category": category,
                    "priority": priority,
                    "estimated_resolution_time": estimated_resolution_time,
                    "escalation_required": escalation_required,
                    "escalation_reason": escalation_reason,
                    "collected_fields_json": json.dumps(collected_fields, ensure_ascii=False),
                },
            )
        conn.commit()

    return ticket_id


def list_tickets(status: str | None = None, limit: int = 200) -> list[dict]:
    query = """
        SELECT ticket_id, created_at, requester_name, requester_email, description,
               kb_article_id, service, category, priority, estimated_resolution_time,
               escalation_required, escalation_reason, status
        FROM tickets
    """
    params: dict = {"limit": int(limit)}
    if status:
        query += " WHERE status = %(status)s "
        params["status"] = status
    query += " ORDER BY created_at DESC LIMIT %(limit)s"

    with _conn() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(CREATE_TABLE_SQL)
            cur.execute(query, params)
            return cur.fetchall()


def update_ticket_status(ticket_id: str, status: str) -> bool:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
            cur.execute(
                """
                UPDATE tickets
                SET status = %(status)s
                WHERE ticket_id = %(ticket_id)s
                """,
                {"status": status, "ticket_id": ticket_id},
            )
            updated = cur.rowcount > 0
        conn.commit()
    return updated
