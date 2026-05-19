import json
import os
from pathlib import Path

from flask import Flask, jsonify, request

from agent_core import load_kb, triage
# CORREÇÃO: Usando a planilha como exige o escopo, não o Postgres.
from google_sheets_store import append_ticket

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

    # Gravação direta no Google Sheets
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

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "kb_articles": len(ARTICLES)}), 200

# --- MUDANÇA: Rota do Chat HTML inserida aqui com a sua URL do n8n ---
@app.route("/chat", methods=["GET"])
def chat_interface():
    # ATENÇÃO: A sua URL de teste foi inserida aqui. 
    # Quando for para produção, ative o fluxo no n8n e tire o "-test" dessa URL.
    N8N_WEBHOOK_URL = "https://eduardol.app.n8n.cloud/webhook-test/chat-suporte"
    
    html = f"""
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Suporte TI - K-Desk</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background-color: #f4f4f9; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }}
            #chat-container {{ width: 400px; height: 600px; background: white; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.1); display: flex; flex-direction: column; overflow: hidden; }}
            #chat-header {{ background: #0070f3; color: white; padding: 15px; text-align: center; font-weight: bold; }}
            #chat-box {{ flex: 1; padding: 15px; overflow-y: auto; display: flex; flex-direction: column; gap: 10px; }}
            .message {{ max-width: 80%; padding: 10px 14px; border-radius: 8px; font-size: 14px; line-height: 1.4; }}
            .bot-msg {{ background: #f1f1f1; align-self: flex-start; border-bottom-left-radius: 0; }}
            .user-msg {{ background: #0070f3; color: white; align-self: flex-end; border-bottom-right-radius: 0; }}
            #input-area {{ display: flex; padding: 10px; border-top: 1px solid #eaeaea; background: #fafafa; }}
            input {{ flex: 1; padding: 10px; border: 1px solid #ccc; border-radius: 6px; outline: none; }}
            button {{ padding: 10px 15px; margin-left: 10px; background: #0070f3; color: white; border: none; border-radius: 6px; cursor: pointer; font-weight: bold; }}
            button:hover {{ background: #005bb5; }}
        </style>
    </head>
    <body>
        <div id="chat-container">
            <div id="chat-header">Agente Inteligente TI</div>
            <div id="chat-box">
                <div class="message bot-msg">Olá! Descreva o seu problema ou dúvida de TI.</div>
            </div>
            <div id="input-area">
                <input type="text" id="user-input" placeholder="Digite sua solicitação..." onkeypress="handleKeyPress(event)">
                <button onclick="sendMessage()">Enviar</button>
            </div>
        </div>

        <script>
            const webhookUrl = "{N8N_WEBHOOK_URL}";

            function addMessage(text, sender) {{
                const chatBox = document.getElementById('chat-box');
                const msgDiv = document.createElement('div');
                msgDiv.className = `message ${{sender === 'user' ? 'user-msg' : 'bot-msg'}}`;
                msgDiv.innerText = text;
                chatBox.appendChild(msgDiv);
                chatBox.scrollTop = chatBox.scrollHeight;
            }}

            async function sendMessage() {{
                const input = document.getElementById('user-input');
                const text = input.value.trim();
                if (!text) return;

                addMessage(text, 'user');
                input.value = '';
                addMessage('A processar...', 'bot'); 

                try {{
                    const response = await fetch(webhookUrl, {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ message: text }})
                    }});
                    const data = await response.json();
                    
                    // Remove a mensagem de "A processar..."
                    const chatBox = document.getElementById('chat-box');
                    chatBox.removeChild(chatBox.lastChild);

                    // Assume que o n8n devolve um JSON com a chave 'resposta'
                    addMessage(data.resposta || JSON.stringify(data), 'bot');
                }} catch (error) {{
                    const chatBox = document.getElementById('chat-box');
                    chatBox.removeChild(chatBox.lastChild);
                    addMessage('Erro ao contactar o servidor.', 'bot');
                    console.error(error);
                }}
            }}

            function handleKeyPress(e) {{
                if (e.key === 'Enter') sendMessage();
            }}
        </script>
    </body>
    </html>
    """
    return html, 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
