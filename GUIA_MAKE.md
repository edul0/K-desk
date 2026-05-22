# Guia de Implementação: Automação Low-Code com Make (Integromat)

Este documento detalha o passo a passo para construir a lógica do K-Desk utilizando a plataforma **Make.com**, substituindo a lógica customizada em Python.

Neste cenário, a Vercel atua apenas para hospedar a Interface Web (o Chat), enviando os dados do usuário para o Make, que fará todo o fluxo de tomada de decisão, consulta a planilha e registro de chamados.

## 1. Visão Geral da Arquitetura Make

O fluxo no Make terá a seguinte estrutura principal:

1. **Webhook (Custom Webhook):** Recebe o POST da nossa interface Vercel.
2. **Google Sheets (Search Rows):** Consulta a "Base de Conhecimento" para buscar as regras e perguntas diagnósticas.
3. **OpenAI / Gemini (Create a Chat Completion):** Pede para a Inteligência Artificial avaliar o relato do usuário.
4. **Router:** Cria dois caminhos possíveis baseados na resposta da IA:
   - **Caminho A (Ambiguidade):** Webhook Response retorna as perguntas ao usuário.
   - **Caminho B (Clareza/Registro):** Google Sheets (Add a Row) grava o ticket final e Webhook Response retorna os dados de sucesso para a tela.

---

## 2. Passo a Passo no Make

### Passo 2.1 - Gatilho: Custom Webhook
1. Crie um novo cenário no Make.
2. Adicione o módulo **Webhooks** > **Custom Webhook**.
3. Clique em "Add", dê um nome (ex: `K-Desk Ingestion`) e copie a URL gerada (ex: `https://hook.us1.make.com/xxxxxx`).
4. **Guarde essa URL**. Você deverá colá-la na variável de ambiente `MAKE_WEBHOOK_URL` lá no seu painel da Vercel.
5. Clique em "Determine data structure" e envie uma requisição da nossa interface Vercel (ou via Postman) para que o Make entenda os dados (description, answers, requester_name, etc).

### Passo 2.2 - Base de Conhecimento: Google Sheets
1. Conecte sua conta do Google.
2. Adicione o módulo **Google Sheets** > **Search Rows**.
3. Selecione a planilha que possui os artigos de TI (sua Base de Conhecimento).
4. Opcional: Se quiser filtrar, você pode usar a descrição do usuário para mapear previamente. Ou, para bases pequenas, retorne tudo para passar para a IA.

### Passo 2.3 - O "Cérebro": Inteligência Artificial (OpenAI)
1. Adicione o módulo **OpenAI (ChatGPT)** > **Create a Chat Completion**.
2. Escolha o modelo (ex: `gpt-4o-mini`).
3. No campo *System message*, passe o prompt base:
   > "Você é o K-Desk, um agente de TI. Avalie o chamado do usuário com base na planilha: {{Planilha}}.
   > Se precisar de mais informações, responda com um JSON contendo 'status': 'need_more_info' e as perguntas.
   > Se estiver claro, retorne um JSON com 'status': 'registered', definindo serviço, categoria, prioridade (Critica, Alta, Media, Baixa) e prazo estimado."
4. Certifique-se de configurar a API da IA para **Output JSON Object**.

### Passo 2.4 - O Roteador: Router
1. Adicione o módulo **Router** logo após a IA. Ele divide o fluxo em 2 braços.

### Passo 2.5 - Braço A: Ambiguidade (Falta de Dados)
1. Clique no filtro da linha superior do roteador e coloque a condição: `status` da IA **Equal to** `need_more_info`.
2. Adicione o módulo **Webhooks** > **Webhook Response**.
3. No Body, coloque a variável exata retornada pela IA contendo as perguntas. (O front-end Vercel saberá desenhar isso na tela do chat).

### Passo 2.6 - Braço B: Registro de Ticket Claro
1. Clique no filtro da linha inferior e coloque: `status` da IA **Equal to** `registered`.
2. Adicione o módulo **Google Sheets** > **Add a Row**.
3. Conecte à sua planilha "Registro de Chamados". Mapeie os dados da IA (Nome, Email, Descrição, Serviço, Categoria, Prioridade, Prazo) para as colunas corretas.
4. Adicione um último módulo **Webhooks** > **Webhook Response**.
5. Construa o JSON de sucesso no Body, para a interface Vercel mostrar o "Card" verde de Chamado Criado.
   ```json
   {
     "status": "registered",
     "ticket_id": "{{TicketID_Gerado}}",
     "service": "{{IA.serviço}}",
     "priority": "{{IA.prioridade}}",
     "estimated_resolution_time": "{{IA.prazo}}"
   }
   ```

---

## 3. Integração com o Código (Vercel)

Para que essa ponte funcione de forma invisível para o usuário final, a infraestrutura customizada que já está pronta neste repositório suporta o envio para o Make.

O arquivo `api/index.py` possui uma configuração dinâmica:
Se você cadastrar a variável `MAKE_WEBHOOK_URL` no seu projeto Vercel:
- O agente Python nativo será **desativado**.
- O sistema da Vercel enviará 100% dos eventos do chat para o Make, aguardando o seu Webhook Response.
- O Frontend e os botões da interface (Novo Chamado, Histórico, Drop Desk layout) continuam sendo servidos velozmente pela Vercel.

Dessa forma, unimos o melhor dos dois mundos: A beleza e velocidade de um Front-End customizado (Vercel) rodando sob uma orquestração fácil e maleável por fluxos visuais (Make).
