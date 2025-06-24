import os
import io
import requests
from typing import Optional
import json
from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

from google.cloud import vision
from google.oauth2 import service_account
from googleapiclient.discovery import build

from slack_sdk.web import WebClient
from slack_sdk.signature import SignatureVerifier
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient

# Carrega variáveis de ambiente do .env
load_dotenv()

# --- Configurações das APIs ---
# Google Cloud Vision
# O GOOGLE_APPLICATION_CREDENTIALS deve apontar para o arquivo JSON da conta de serviço
# ex: os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "credentials.json" já é feito pela gcloud-sdk

SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/cloud-platform']

creds_json = os.getenv("GOOGLE_CREDENTIALS")

if not creds_json:
    raise Exception("Variável GOOGLE_CREDENTIALS não encontrada!")

creds = service_account.Credentials.from_service_account_info(
    json.loads(creds_json),
    scopes=SCOPES
)

# Google Sheets cliente
sheets_service = build('sheets', 'v4', credentials=creds)
SPREADSHEET_ID = os.getenv("GOOGLE_SHEET_ID")

# Google Vision client
vision_client = vision.ImageAnnotatorClient(credentials=creds)

# Slack
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")
slack_client = WebClient(token=SLACK_BOT_TOKEN)
signature_verifier = SignatureVerifier(SLACK_SIGNING_SECRET)

# Twilio (WhatsApp)
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER") # Seu número Twilio WhatsApp
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

app = FastAPI()

# --- Funções Auxiliares (mantidas asíncronas para download, OCR e Sheets) ---

async def download_image(url: str, token: Optional[str] = None) -> bytes:
    """Faz o download de uma imagem de uma URL, opcionalmente com um token de autenticação."""
    headers = {}
    if token:
        headers = {"Authorization": f"Bearer {token}"}
    
    # Adicionando um timeout para a requisição de download, caso a imagem esteja lenta.
    response = requests.get(url, headers=headers, timeout=10) # 10 segundos de timeout
    response.raise_for_status()  # Levanta um erro para status de resposta ruins (4xx ou 5xx)
    return response.content

async def extract_text_from_image(image_content: bytes) -> str:
    """Extrai texto de uma imagem usando Google Cloud Vision API."""
    image = vision.Image(content=image_content)
    response = vision_client.document_text_detection(image=image)
    
    if response.full_text_annotation:
        return response.full_text_annotation.text
    return ""

async def add_row_to_sheet(data: list):
    """Adiciona uma nova linha à planilha do Google."""
    body = {
        'values': [data]
    }
    result = sheets_service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range="A1", # Assume que você está adicionando no final da primeira aba
        valueInputOption="USER_ENTERED",
        body=body
    ).execute()
    print(f"Linha adicionada: {result.get('updatedCells')} células.")

def parse_expense_data(text: str) -> dict:
    """
    Tenta extrair valor, data e estabelecimento do texto OCR.
    Esta é uma implementação muito básica e pode precisar de ajustes
    com base nos comprovantes reais.
    """
    data = {
        "valor": "Não encontrado",
        "data": "Não encontrada",
        "estabelecimento": "Não encontrado",
        "descricao": text.strip() # O texto completo como descrição inicial
    }

    import re
    
    # Exemplo simples de extração de valor (números com vírgula/ponto)
    # Procura por padrões comuns de valores (ex: 12,34 ou 12.34)
    valor_match = re.search(r'R\$\s*(\d{1,3}(?:\.\d{3})*,\d{2})|(\d{1,3}(?:,\d{3})*\.\d{2})', text, re.IGNORECASE)
    if valor_match:
        valor_str = valor_match.group(1) or valor_match.group(2)
        valor_str = valor_str.replace('.', '').replace(',', '.')
        try:
            data["valor"] = float(valor_str)
        except ValueError:
            pass
    
    # Tentativa 2: Busca por datas (dd/mm/aaaa, dd-mm-aaaa, aaaa-mm-dd)
    data_match = re.search(r'\d{2}[-/]\d{2}[-/]\d{4}|\d{4}[-/]\d{2}[-/]\d{2}', text)
    if data_match:
        data["data"] = data_match.group(0)

    return data


# --- Nova Função para Processamento em Background ---
async def process_slack_expense_in_background(
    file_id: str, channel_id: str, user_id: str, slack_token: str
):
    """Processa o comprovante de despesa enviado via Slack em segundo plano."""
    try:
        # Re-inicializa o cliente Slack para usar aqui, se necessário, ou passe o existente
        # Para evitar problemas com o ciclo de vida da conexão, é melhor usar o cliente global ou passar.
        # Aqui, estamos usando o cliente global, que é seguro se ele for inicializado uma vez.

        file_info_response = slack_client.files_info(file=file_id)
        file_info = file_info_response["file"]
        download_url = file_info["url_private"]

        image_content = await download_image(download_url, token=slack_token)
        extracted_text = await extract_text_from_image(image_content)
        parsed_data = parse_expense_data(extracted_text)

        user_info = slack_client.users_info(user=user_id)
        user_name = user_info["user"]["real_name"] if user_info and user_info["user"] else f"Usuário {user_id}"

        row_data = [
            parsed_data.get("data"),
            user_name,
            parsed_data.get("valor"),
            parsed_data.get("estabelecimento"),
            parsed_data.get("descricao"),
            "Aguardando",
            download_url
        ]
        await add_row_to_sheet(row_data)

        message = (
            f"Recebi seu comprovante de gasto. Processado! 🎉\n"
            f"**Data:** {parsed_data.get('data')}\n"
            f"**Valor:** R$ {parsed_data.get('valor')}\n"
            f"**Descrição OCR:** {parsed_data.get('descricao')[:100]}...\n"
            f"Foi adicionado à planilha de reembolsos. Obrigado!"
        )
        slack_client.chat_postMessage(channel=channel_id, text=message)

    except Exception as e:
        print(f"Erro no processamento de background do Slack: {e}")
        slack_client.chat_postMessage(channel=channel_id, text=f"Ops! Houve um erro no processamento do seu comprovante. Por favor, tente novamente ou entre em contato com o suporte. Erro: {e}")

async def process_whatsapp_expense_in_background(
    from_number: str, media_url: str
):
    """Processa o comprovante de despesa enviado via WhatsApp em segundo plano."""
    try:
        image_content = await download_image(media_url)
        extracted_text = await extract_text_from_image(image_content)
        parsed_data = parse_expense_data(extracted_text)

        user_identifier = from_number.replace("whatsapp:", "")

        row_data = [
            parsed_data.get("data"),
            user_identifier,
            parsed_data.get("valor"),
            parsed_data.get("estabelecimento"),
            parsed_data.get("descricao"),
            "Aguardando",
            media_url
        ]
        await add_row_to_sheet(row_data)

        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER,
            to=from_number,
            body=(f"Recebi seu comprovante de gasto. Processado! 🎉\n"
                  f"Data: {parsed_data.get('data')}\n"
                  f"Valor: R$ {parsed_data.get('valor')}\n"
                  f"Descrição OCR: {parsed_data.get('descricao')[:100]}...\n"
                  f"Foi adicionado à planilha de reembolsos. Obrigado!"))

    except Exception as e:
        print(f"Erro no processamento de background do WhatsApp: {e}")
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER,
            to=from_number,
            body=(f"Ops! Houve um erro no processamento do seu comprovante. Por favor, tente novamente ou entre em contato com o suporte. Erro: {e}"))


# --- Rotas do FastAPI ---

@app.get("/")
async def read_root():
    return {"message": "Bot de Reembolso está online!"}

# --- Slack Webhook ---
@app.post("/slack/events")
async def slack_events(request: Request, background_tasks: BackgroundTasks): # Adicione background_tasks
    req_body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp")
    signature = request.headers.get("X-Slack-Signature")

    if not signature_verifier.is_valid_request(req_body.decode("utf-8"), timestamp, signature):
        raise HTTPException(status_code=403, detail="Invalid Slack request signature")

    request_data = await request.json()
    if "challenge" in request_data:
        return {"challenge": request_data["challenge"]}

    event = request_data.get("event")
    event_type = event.get("type")

    if event_type == "file_shared":
        file_id = event.get("file_id")
        channel_id = event.get("channel_id")
        user_id = event.get("user_id")

        if file_id:
            # Adiciona a tarefa de processamento ao background
            background_tasks.add_task(
                process_slack_expense_in_background,
                file_id, channel_id, user_id, SLACK_BOT_TOKEN
            )
            # Responde imediatamente ao Slack para evitar timeout
            return Response(status_code=200, content="Processing your request in the background.")

    return Response(status_code=200) # Responde OK para outros eventos

# --- WhatsApp Webhook (Twilio) ---
@app.post("/whatsapp/webhook")
async def whatsapp_webhook(request: Request, background_tasks: BackgroundTasks): # Adicione background_tasks
    form_data = await request.form()
    
    incoming_msg = form_data.get('Body', '').lower()
    from_number = form_data.get('From', '')
    media_url = form_data.get('MediaUrl0')

    if media_url:
        # Adiciona a tarefa de processamento ao background
        background_tasks.add_task(
            process_whatsapp_expense_in_background,
            from_number, media_url
        )
        # Responde imediatamente ao Twilio (Twilio espera TwiML XML, então a resposta é vazia)
        # Twilio tem um timeout maior (15 segundos), mas é boa prática responder rápido.
        return Response(content=str(MessagingResponse()), media_type="application/xml")
    else:
        # Para mensagens sem mídia, responde pedindo uma foto
        resp = MessagingResponse()
        msg = resp.message()
        msg.body("Olá! Para registrar um gasto, por favor, envie a foto do seu comprovante.")
        return Response(content=str(resp), media_type="application/xml")

# --- Execução Local ---
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)