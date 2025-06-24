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
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER") 
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

app = FastAPI()



async def download_image(url: str, token: Optional[str] = None) -> bytes:
    """Faz o download de uma imagem de uma URL, opcionalmente com um token de autenticação."""
    headers = {}
    if token:
        headers = {"Authorization": f"Bearer {token}"}
    
    response = requests.get(url, headers=headers)
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

# --- Processamento de Texto OCR (Lógica de Negócio) ---
# Esta é uma função simplificada. Na vida real, você precisaria de regex mais robustas
# e talvez machine learning para uma extração precisa.

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

    # Exemplo simples de extração de valor (números com vírgula/ponto)
    # Procura por padrões comuns de valores (ex: 12,34 ou 12.34)
    import re
    
    # Tentativa 1: Busca por valores com R$ na frente ou no final, ou apenas números com 2 casas decimais
    valor_match = re.search(r'R\$\s*(\d{1,3}(?:\.\d{3})*,\d{2})|(\d{1,3}(?:,\d{3})*\.\d{2})', text, re.IGNORECASE)
    if valor_match:
        valor_str = valor_match.group(1) or valor_match.group(2)
        # Substitui vírgula por ponto para conversão e remove pontos de milhar
        valor_str = valor_str.replace('.', '').replace(',', '.')
        try:
            data["valor"] = float(valor_str)
        except ValueError:
            pass # Se não conseguir converter, mantém o "Não encontrado"
    
    # Tentativa 2: Busca por datas (dd/mm/aaaa, dd-mm-aaaa, aaaa-mm-dd)
    data_match = re.search(r'\d{2}[-/]\d{2}[-/]\d{4}|\d{4}[-/]\d{2}[-/]\d{2}', text)
    if data_match:
        data["data"] = data_match.group(0)

    # Tentativa 3: Estabelecimento (muito genérico, precisaria de listas de palavras-chave ou modelos mais complexos)
    # Por agora, não vamos fazer uma extração sofisticada de estabelecimento via OCR.
    # Pode ser melhor pedir para o usuário informar.

    return data

# --- Rotas do FastAPI ---

@app.get("/")
async def read_root():
    return {"message": "Bot de Reembolso está online!"}

# --- Slack Webhook ---
@app.post("/slack/events")
async def slack_events(request: Request):
    req_body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp")
    signature = request.headers.get("X-Slack-Signature")

    if not signature_verifier.is_valid_request(req_body.decode("utf-8"), timestamp, signature):
        raise HTTPException(status_code=403, detail="Invalid Slack request signature")

    # Slack's URL verification challenge
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
            try:
                # Obter informações do arquivo para conseguir a URL de download
                file_info_response = slack_client.files_info(file=file_id)
                file_info = file_info_response["file"]
                download_url = file_info["url_private"]
                file_name = file_info["name"]

                # Baixar imagem
                image_content = await download_image(download_url, token=SLACK_BOT_TOKEN)

                # Extrair texto
                extracted_text = await extract_text_from_image(image_content)
                parsed_data = parse_expense_data(extracted_text)

                # Obter nome do usuário
                user_info = slack_client.users_info(user=user_id)
                user_name = user_info["user"]["real_name"] if user_info and user_info["user"] else f"Usuário {user_id}"

                # Preparar dados para a planilha
                # Adapte a ordem das colunas para a sua planilha!
                row_data = [
                    parsed_data.get("data"),
                    user_name,
                    parsed_data.get("valor"),
                    parsed_data.get("estabelecimento"), # Será "Não encontrado" se não tiver lógica para extrair
                    parsed_data.get("descricao"),
                    "Aguardando", # Ex: Projeto (pode ser perguntado ao usuário depois)
                    download_url # Link para a foto original
                ]
                await add_row_to_sheet(row_data)

                # Enviar confirmação ao usuário
                message = (
                    f"Recebi seu comprovante de gasto. Processado! 🎉\n"
                    f"**Data:** {parsed_data.get('data')}\n"
                    f"**Valor:** R$ {parsed_data.get('valor')}\n"
                    f"**Descrição OCR:** {parsed_data.get('descricao')[:100]}...\n" # Limita a descrição
                    f"Foi adicionado à planilha de reembolsos. Obrigado!"
                )
                slack_client.chat_postMessage(channel=channel_id, text=message)

            except Exception as e:
                print(f"Erro ao processar evento do Slack: {e}")
                slack_client.chat_postMessage(channel=channel_id, text=f"Ops! Houve um erro ao processar seu comprovante. Por favor, tente novamente ou entre em contato com o suporte. Erro: {e}")
        return Response(status_code=200) # Responde OK para o Slack

    # Ignora outros tipos de eventos
    return Response(status_code=200)

# --- WhatsApp Webhook (Twilio) ---
@app.post("/whatsapp/webhook")
async def whatsapp_webhook(request: Request):
    # Twilio envia dados como application/x-www-form-urlencoded
    form_data = await request.form()
    
    incoming_msg = form_data.get('Body', '').lower()
    from_number = form_data.get('From', '') # Formato: whatsapp:+5511987654321
    media_url = form_data.get('MediaUrl0') # URL da primeira mídia, se houver

    resp = MessagingResponse()
    msg = resp.message()

    if media_url:
        try:
            image_content = await download_image(media_url)
            extracted_text = await extract_text_from_image(image_content)
            parsed_data = parse_expense_data(extracted_text)

            # Extrair nome do funcionário (pode ser um mapeamento de número para nome ou pedir no primeiro contato)
            # Por simplicidade, usaremos o número como identificador aqui
            user_identifier = from_number.replace("whatsapp:", "")

            # Preparar dados para a planilha
            row_data = [
                parsed_data.get("data"),
                user_identifier, # Identificador do usuário
                parsed_data.get("valor"),
                parsed_data.get("estabelecimento"),
                parsed_data.get("descricao"),
                "Aguardando",
                media_url # Link para a foto original
            ]
            await add_row_to_sheet(row_data)

            msg.body(f"Recebi seu comprovante de gasto. Processado! 🎉\n"
                     f"Data: {parsed_data.get('data')}\n"
                     f"Valor: R$ {parsed_data.get('valor')}\n"
                     f"Descrição OCR: {parsed_data.get('descricao')[:100]}...\n"
                     f"Foi adicionado à planilha de reembolsos. Obrigado!")
            
        except Exception as e:
            print(f"Erro ao processar evento do WhatsApp: {e}")
            msg.body(f"Ops! Houve um erro ao processar seu comprovante. Por favor, tente novamente ou entre em contato com o suporte. Erro: {e}")
    else:
        msg.body("Olá! Para registrar um gasto, por favor, envie a foto do seu comprovante.")
    
    return Response(content=str(resp), media_type="application/xml")

# --- Execução Local ---
# Para rodar localmente (para testes, precisará de ngrok para os webhooks)
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)