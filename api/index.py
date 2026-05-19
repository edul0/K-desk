import json
import os
from pathlib import Path

from flask import Flask, jsonify, request

from agent_core import load_kb, triage
# Usando a planilha como exige o escopo do projeto
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

    # Gravação direta no Google Sheets conforme especificado no escopo
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

# --- MUDANÇA CRÍTICA: Interface com Máquina de Estado Baseada em JSON no Frontend ---
@app.route("/chat", methods=["GET"])
def chat_interface():
    # LEMBRETE: Certifique-se de que esta URL aponta para a sua instância real e ativa do n8n
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
            #chat-container {{ width: 420px; height: 600px; background: white; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.1); display: flex; flex-direction: column; overflow: hidden; }}
            #chat-header {{ background: #0070f3; color: white; padding: 15px; text-align: center; font-weight: bold; font-size: 16px; }}
            #chat-box {{ flex: 1; padding: 15px; overflow-y: auto; display: flex; flex-direction: column; gap: 10px; background: #fafafa; }}
            .message {{ max-width: 85%; padding: 10px 14px; border-radius: 8px; font-size: 14px; line-height: 1.4; white-space: pre-line; }}
            .bot-msg {{ background: #e9e9eb; color: #000; align-self: flex-start; border-bottom-left-radius: 0; }}
            .user-msg {{ background: #0070f3; color: white; align-self: flex-end; border-bottom-right-radius: 0; }}
            #input-area {{ display: flex; padding: 12px; border-top: 1px solid #eaeaea; background: white; }}
            input {{ flex: 1; padding: 10px; border: 1px solid #ccc; border-radius: 6px; outline: none; font-size: 14px; }}
            button {{ padding: 10px 16px; margin-left: 10px; background: #0070f3; color: white; border: none; border-radius: 6px; cursor: pointer; font-weight: bold; }}
            button:hover {{ background: #005bb5; }}
        </style>
    </head>
    <body>
        <div id="chat-container">
            <div id="chat-header">Centro de Suporte Inteligente - K-Desk</div>
            <div id="chat-box">
                <div class="message bot-msg">Olá! Descreva detalhadamente o seu problema ou incidente de TI para que eu possa ajudar.</div>
            </div>
            <div id="input-area">
                <input type="text" id="user-input" placeholder="Digite aqui sua mensagem..." onkeypress="handleKeyPress(event)">
                <button onclick="sendMessage()">Enviar</button>
            </div>
        </div>

        <script>
            const webhookUrl = https://eduardol.app.n8n.cloud/webhook-test/chat-suporte
            
            // Variáveis globais para controle do Estado da Conversa (Memória)
            let currentDescription = "";
            let collectedAnswers = {{}};
            let currentState = "AWAITING_DESCRIPTION"; 
            let questionQueue = [];
            let currentQuestionTarget = "";

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

                // Processamento da máquina de estado baseada no input do usuário
                if (currentState === "AWAITING_DESCRIPTION") {{
                    currentDescription = text;
                }} else if (currentState === "COLLECTING_QUESTIONS" || currentState === "COLLECTING_REQUIRED") {{
                    if (currentQuestionTarget) {{
                        collectedAnswers[currentQuestionTarget] = text;
                    }}
                }}

                addMessage('Analisando sua solicitação...', 'bot');
                const loadingMsg = document.getElementById('chat-box').lastChild;

                try {{
                    // O payload agora envia consistentemente a descrição inicial E o mapa acumulado de respostas
                    const response = await fetch(webhookUrl, {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ 
                            description: currentDescription,
                            answers: collectedAnswers
                        }})
                    }});
                    
                    const data = await response.json();
                    loadingMsg.remove(); // Remove o texto de carregamento

                    // Extração correta independente do n8n encapsular a resposta ou enviá-la direta
                    const payload = data.output || data;

                    if (payload.status === "need_more_info") {{
                        currentState = "COLLECTING_QUESTIONS";
                        questionQueue = payload.questions || [];
                        // Identifica qual pergunta da fila ainda não foi respondida
                        currentQuestionTarget = questionQueue.find(q => !collectedAnswers[q]);

                        if (currentQuestionTarget) {{
                            addMessage(`${{payload.message}}\n\n👉 ${{currentQuestionTarget}}`, 'bot');
                        }} else {{
                            // Se as perguntas listadas já possuem chaves, envia novamente para forçar reavaliação
                            addMessage('Processando dados adicionais...', 'bot');
