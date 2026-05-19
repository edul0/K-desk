import json
import os
from pathlib import Path

from flask import Flask, jsonify, request

from agent_core import load_kb, triage

app = Flask(__name__)

# Resolve o CSV relativo ao index.py, independente do working directory na Vercel
KB_FILE = Path(__file__).parent.parent / "data" / "support_knowledge_base.csv"
ARTICLES = load_kb(KB_FILE)


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "name": "K-desk API",
        "status": "online",
        "endpoints": {
            "health": "/api/health",
            "triage": "/api/triage",
            "chat": "/chat"
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

    try:
        from google_sheets_store import append_ticket
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
    except Exception as e:
        import uuid
        ticket_id = f"TKT-{uuid.uuid4().hex[:8].upper()}"
        app.logger.warning(f"Google Sheets indisponível: {e}. Ticket: {ticket_id}")

    return jsonify({
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
    }), 200


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "kb_articles": len(ARTICLES)}), 200


@app.route("/chat", methods=["GET"])
def chat_interface():
    html = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Suporte TI - K-Desk</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #f0f2f5;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
        }
        #chat-container {
            width: 440px;
            height: 620px;
            background: white;
            border-radius: 16px;
            box-shadow: 0 8px 30px rgba(0,0,0,0.12);
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }
        #chat-header {
            background: #0070f3;
            color: white;
            padding: 18px 20px;
            text-align: center;
            font-weight: 600;
            font-size: 15px;
            letter-spacing: 0.3px;
        }
        #chat-header small {
            display: block;
            font-size: 11px;
            opacity: 0.8;
            margin-top: 2px;
            font-weight: 400;
        }
        #chat-box {
            flex: 1;
            padding: 16px;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
            gap: 10px;
            background: #f8f9fa;
        }
        .message {
            max-width: 82%;
            padding: 10px 14px;
            border-radius: 12px;
            font-size: 14px;
            line-height: 1.5;
            white-space: pre-line;
        }
        .bot-msg {
            background: #fff;
            color: #1a1a1a;
            align-self: flex-start;
            border-bottom-left-radius: 3px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.08);
        }
        .user-msg {
            background: #0070f3;
            color: white;
            align-self: flex-end;
            border-bottom-right-radius: 3px;
        }
        .loading-msg {
            background: #fff;
            color: #999;
            align-self: flex-start;
            border-bottom-left-radius: 3px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.08);
            font-style: italic;
        }
        #input-area {
            display: flex;
            padding: 12px 14px;
            border-top: 1px solid #eee;
            background: white;
            gap: 8px;
        }
        #user-input {
            flex: 1;
            padding: 10px 14px;
            border: 1.5px solid #ddd;
            border-radius: 8px;
            outline: none;
            font-size: 14px;
            transition: border-color 0.2s;
        }
        #user-input:focus { border-color: #0070f3; }
        #send-btn {
            padding: 10px 18px;
            background: #0070f3;
            color: white;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            font-weight: 600;
            font-size: 14px;
            transition: background 0.2s;
        }
        #send-btn:hover { background: #005bb5; }
        #send-btn:disabled { background: #aaa; cursor: not-allowed; }
    </style>
</head>
<body>
    <div id="chat-container">
        <div id="chat-header">
            Centro de Suporte Inteligente
            <small>K-Desk &middot; Atendimento Automatizado</small>
        </div>
        <div id="chat-box">
            <div class="message bot-msg">Olá! Descreva o seu problema ou incidente de TI e vou registrar um chamado para você.</div>
        </div>
        <div id="input-area">
            <input type="text" id="user-input" placeholder="Descreva seu problema..." onkeypress="handleKeyPress(event)">
            <button id="send-btn" onclick="sendMessage()">Enviar</button>
        </div>
    </div>

    <script>
        const API_URL = "https://eduardol.app.n8n.cloud/webhook/chat";
        let currentDescription = "";
        let collectedAnswers = {};
        let currentState = "AWAITING_DESCRIPTION";
        let currentQuestionTarget = "";

        function addMessage(text, type) {
            const chatBox = document.getElementById('chat-box');
            const div = document.createElement('div');
            div.className = 'message ' + type;
            div.innerText = text;
            chatBox.appendChild(div);
            chatBox.scrollTop = chatBox.scrollHeight;
            return div;
        }

        async function sendMessage() {
            const input = document.getElementById('user-input');
            const btn = document.getElementById('send-btn');
            const text = input.value.trim();
            if (!text) return;

            addMessage(text, 'user-msg');
            input.value = '';
            btn.disabled = true;

            if (currentState === "AWAITING_DESCRIPTION") {
                currentDescription = text;
            } else if (currentState === "COLLECTING") {
                if (currentQuestionTarget) {
                    collectedAnswers[currentQuestionTarget] = text;
                }
            }

            const loading = addMessage('Analisando...', 'loading-msg');

            try {
                const res = await fetch(API_URL, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        description: currentDescription,
                        answers: collectedAnswers
                    })
                });

                const data = await res.json();
                loading.remove();

                if (data.status === "need_more_info") {
                    currentState = "COLLECTING";
                    const questions = data.questions || [];
                    currentQuestionTarget = questions.find(q => !collectedAnswers[q]);
                    if (currentQuestionTarget) {
                        addMessage(data.message + '\\n\\n👉 ' + currentQuestionTarget, 'bot-msg');
                    }

                } else if (data.status === "missing_required") {
                    currentState = "COLLECTING";
                    const fields = data.required_fields || [];
                    currentQuestionTarget = fields.find(q => !collectedAnswers[q]);
                    if (currentQuestionTarget) {
                        addMessage(data.message + '\\nInforme:\\n👉 ' + currentQuestionTarget, 'bot-msg');
                    }

                } else if (data.status === "registered") {
                    currentState = "FINISHED";
                    let msg = 'Chamado registrado!\\n\\n';
                    msg += 'Ticket: ' + data.ticket_id + '\\n';
                    msg += 'Serviço: ' + data.service + ' / ' + data.category + '\\n';
                    msg += 'Prioridade: ' + data.priority + '\\n';
                    msg += 'Prazo: ' + data.estimated_resolution_time + '\\n\\n';
                    msg += 'Próximos passos:\\n' + data.resolution_steps;
                    if (data.workaround) msg += '\\n\\nContorno:\\n' + data.workaround;
                    if (data.escalation_required) msg += '\\n\\n⚠️ Escalado para analista humano.';
                    addMessage(msg, 'bot-msg');
                    document.getElementById('user-input').disabled = true;
                    btn.disabled = true;

                } else {
                    addMessage(JSON.stringify(data), 'bot-msg');
                }

            } catch (err) {
                loading.remove();
                addMessage('Erro ao conectar. Tente novamente.', 'bot-msg');
                console.error(err);
            }

            btn.disabled = false;
            input.focus();
        }

        function handleKeyPress(e) {
            if (e.key === 'Enter') sendMessage();
        }
    </script>
</body>
</html>"""
    return html, 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
