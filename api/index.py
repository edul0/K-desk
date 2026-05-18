import json
import os
from pathlib import Path

from flask import Flask, jsonify, request

from agent_core import load_kb, triage
from vercel_sql_store import append_ticket, init_db

app = Flask(__name__)

KB_FILE = os.environ.get("KB_CSV_PATH", "data/support_knowledge_base.csv")
ARTICLES = load_kb(Path(KB_FILE))


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "name": "K-desk API",
        "status": "online",
        "endpoints": {
            "health": "/api/health",
            "triage": "/api/triage",
            "init_db": "/api/init-db"
        }
    }), 200


@app.route("/api/triage", methods=["POST"])
def triage_route():
    data = request.get_json(silent=True)
    if not data:
        raw = request.get_data(cache=False, as_text=True) or "{}"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {}
    requester_name = (data.get("requester_name") or "Usuário não identificado").strip()
    requester_email = (data.get("requester_email") or "não informado").strip()
    description = (data.get("description") or "").strip()
    answers = data.get("answers") or {}

    if not description:
        return jsonify({"error": "Campo 'description' é obrigatório"}), 400

    status, payload = triage(description, answers, ARTICLES)

    if status in {"need_more_info", "missing_required"}:
        return jsonify({"status": status, **payload}), 200

    article = payload["article"]
    priority = payload["priority"]
    eta = payload["eta"]
    escalation = payload["escalation"]

    ticket_id = append_ticket(
        requester_name=requester_name,
        requester_email=requester_email,
        description=description,
        kb_article_id=article.article_id,
        service=article.service,
        category=article.category,
        priority=priority,
        estimated_resolution_time=eta,
        escalation_required=escalation,
        escalation_reason=article.escalation_criteria if escalation else "",
        collected_fields=answers,
    )

    return jsonify(
        {
            "status": "registered",
            "ticket_id": ticket_id,
            "kb_article_id": article.article_id,
            "kb_article_title": article.title,
            "service": article.service,
            "category": article.category,
            "priority": priority,
            "estimated_resolution_time": eta,
            "escalation_required": escalation,
            "escalation_criteria": article.escalation_criteria if escalation else "",
            "resolution_steps": article.resolution_steps,
            "workaround": article.workaround,
        }
    ), 200


@app.route("/api/init-db", methods=["GET", "POST"])
def init_db_route():
    init_db()
    return jsonify({"ok": True, "message": "Database initialized"}), 200


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "kb_articles": len(ARTICLES)}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
