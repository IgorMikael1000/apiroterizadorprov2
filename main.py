import os
import json
import base64
import psycopg2
import psycopg2.extras
from psycopg2 import errors
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from dotenv import load_dotenv
from datetime import datetime, timedelta

from google.oauth2 import service_account
from googleapiclient.discovery import build

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")
PACKAGE_NAME = os.getenv("PACKAGE_NAME", "app.itsolutions.roterizadorpro")
GOOGLE_JSON_STR = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
MIGRATION_SQL = "ALTER TABLE assinaturas ADD COLUMN IF NOT EXISTS purchase_token TEXT;"

app = FastAPI(title="API Motorista Pro v2")

# --- CONFIGURAÇÃO GOOGLE PLAY API ---
def get_android_publisher():
    if not GOOGLE_JSON_STR:
        raise Exception("GOOGLE_SERVICE_ACCOUNT_JSON não configurado no .env.")
    credentials_dict = json.loads(GOOGLE_JSON_STR)
    credentials = service_account.Credentials.from_service_account_info(
        credentials_dict,
        scopes=["https://www.googleapis.com/auth/androidpublisher"]
    )
    return build('androidpublisher', 'v3', credentials=credentials)

# --- MODELOS DE DADOS ---
class UsuarioNovo(BaseModel):
    firebase_uid: str
    nome: str
    email: str
    cpf: str
    android_id: str

class RotaBackup(BaseModel):
    id: str
    firebase_uid: str
    data_inicio_millis: int
    data_fim_millis: int
    tempo_decorrido_segundos: int
    total_paradas: int
    pacotes_entregues: int
    pacotes_falhos: int
    km_rodados: float
    faturamento_bruto: float
    consumo_kml: float
    preco_combustivel: float

class PurchaseVerification(BaseModel):
    firebase_uid: str
    subscription_id: str
    purchase_token: str

# --- CONEXÃO COM O NEON DB ---
def get_db_connection():
    try:
        return psycopg2.connect(DATABASE_URL)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro de conexão com o banco: {str(e)}")

# Banco v2: garante compatibilidade com o modelo de assinatura do Google Play.
def ensure_purchase_token_column():
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(MIGRATION_SQL)
        conn.commit()
    finally:
        cursor.close()
        conn.close()

@app.on_event("startup")
def startup_migration():
    ensure_purchase_token_column()

# --- ROTAS DE USUÁRIO ---
@app.post("/registrar-usuario")
def registrar_usuario(user: UsuarioNovo):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO usuarios (firebase_uid, nome, email, cpf, android_id)
            VALUES (%s, %s, %s, %s, %s)
        """, (user.firebase_uid, user.nome, user.email, user.cpf, user.android_id))

        data_vencimento = datetime.now() + timedelta(days=7)
        cursor.execute("""
            INSERT INTO assinaturas (firebase_uid, status, data_vencimento, purchase_token)
            VALUES (%s, 'TRIAL', %s, NULL)
        """, (user.firebase_uid, data_vencimento))

        conn.commit()
        return {"mensagem": "Conta criada com sucesso! 7 dias grátis ativados.", "status_assinatura": "TRIAL"}
    except errors.UniqueViolation:
        conn.rollback()
        raise HTTPException(status_code=400, detail="Credenciais já utilizadas (CPF, Email ou Dispositivo).")
    finally:
        cursor.close()
        conn.close()

@app.delete("/deletar-usuario/{firebase_uid}")
def deletar_usuario(firebase_uid: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM assinaturas WHERE firebase_uid = %s;", (firebase_uid,))
        cursor.execute("DELETE FROM historico_rotas WHERE firebase_uid = %s;", (firebase_uid,))
        cursor.execute("DELETE FROM usuarios WHERE firebase_uid = %s;", (firebase_uid,))
        conn.commit()
        return {"mensagem": "Usuário e dados excluídos."}
    finally:
        cursor.close()
        conn.close()

@app.get("/status-assinatura/{firebase_uid}")
def status_assinatura(firebase_uid: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT status, data_vencimento FROM assinaturas WHERE firebase_uid = %s;", (firebase_uid,))
        assinatura = cursor.fetchone()

        if assinatura:
            status_atual, data_vencimento = assinatura[0], assinatura[1]
            if datetime.now(data_vencimento.tzinfo) > data_vencimento and status_atual not in ['ATIVO']:
                cursor.execute("UPDATE assinaturas SET status = 'VENCIDA' WHERE firebase_uid = %s", (firebase_uid,))
                conn.commit()
                return {"status": "VENCIDA", "bloquear_app": True}
            return {"status": status_atual, "bloquear_app": False, "vence_em": data_vencimento}
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")
    finally:
        cursor.close()
        conn.close()

# --- INTEGRAÇÃO GOOGLE PLAY BILLING ---
@app.post("/subscription/verify")
def verify_subscription(req: PurchaseVerification):
    try:
        publisher = get_android_publisher()
        sub_info = publisher.purchases().subscriptions().get(
            packageName=PACKAGE_NAME,
            subscriptionId=req.subscription_id,
            token=req.purchase_token
        ).execute()

        payment_state = sub_info.get("paymentState")
        if payment_state not in [1, 2]:
            raise HTTPException(status_code=400, detail="Pagamento não confirmado pela Google.")

        expiry_time_millis = int(sub_info.get("expiryTimeMillis", 0))
        expiry_date = datetime.fromtimestamp(expiry_time_millis / 1000.0)

        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                UPDATE assinaturas
                SET status = 'ATIVO', data_vencimento = %s, purchase_token = %s
                WHERE firebase_uid = %s
            """, (expiry_date, req.purchase_token, req.firebase_uid))
            conn.commit()
            return {"mensagem": "Assinatura validada com sucesso!", "vence_em": expiry_date}
        finally:
            cursor.close()
            conn.close()

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Falha na validação: {str(e)}")

@app.post("/webhooks/google-play")
async def google_play_webhook(request: Request):
    try:
        payload = await request.json()
        message = payload.get("message", {})
        data_base64 = message.get("data", "")

        if data_base64:
            decoded_data = base64.b64decode(data_base64).decode('utf-8')
            notification = json.loads(decoded_data)

            sub_notification = notification.get("subscriptionNotification")
            if sub_notification:
                notification_type = sub_notification.get("notificationType")
                purchase_token = sub_notification.get("purchaseToken")
                subscription_id = sub_notification.get("subscriptionId")

                publisher = get_android_publisher()
                sub_info = publisher.purchases().subscriptions().get(
                    packageName=PACKAGE_NAME,
                    subscriptionId=subscription_id,
                    token=purchase_token
                ).execute()

                expiry_time_millis = int(sub_info.get("expiryTimeMillis", 0))
                expiry_date = datetime.fromtimestamp(expiry_time_millis / 1000.0)

                conn = get_db_connection()
                cursor = conn.cursor()
                try:
                    if notification_type in [2, 4]:
                        cursor.execute("UPDATE assinaturas SET status = 'ATIVO', data_vencimento = %s WHERE purchase_token = %s", (expiry_date, purchase_token))
                    elif notification_type in [3, 12, 13]:
                        cursor.execute("UPDATE assinaturas SET status = 'CANCELADA', data_vencimento = %s WHERE purchase_token = %s", (expiry_date, purchase_token))
                    conn.commit()
                finally:
                    cursor.close()
                    conn.close()

        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

# --- HISTÓRICO DE ROTAS ---
@app.post("/salvar-historico")
def salvar_historico(rota: RotaBackup):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO historico_rotas (
                id, firebase_uid, data_inicio_millis, data_fim_millis, tempo_decorrido_segundos,
                total_paradas, pacotes_entregues, pacotes_falhos, km_rodados,
                faturamento_bruto, consumo_kml, preco_combustivel
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                data_inicio_millis = EXCLUDED.data_inicio_millis, data_fim_millis = EXCLUDED.data_fim_millis,
                tempo_decorrido_segundos = EXCLUDED.tempo_decorrido_segundos, total_paradas = EXCLUDED.total_paradas,
                pacotes_entregues = EXCLUDED.pacotes_entregues, pacotes_falhos = EXCLUDED.pacotes_falhos,
                km_rodados = EXCLUDED.km_rodados, faturamento_bruto = EXCLUDED.faturamento_bruto,
                consumo_kml = EXCLUDED.consumo_kml, preco_combustivel = EXCLUDED.preco_combustivel
        """, (
            rota.id, rota.firebase_uid, rota.data_inicio_millis, rota.data_fim_millis, rota.tempo_decorrido_segundos,
            rota.total_paradas, rota.pacotes_entregues, rota.pacotes_falhos, rota.km_rodados, rota.faturamento_bruto,
            rota.consumo_kml, rota.preco_combustivel
        ))
        conn.commit()
        return {"mensagem": "Histórico salvo/atualizado"}
    finally:
        cursor.close()
        conn.close()

@app.get("/obter-historico")
def obter_historico(firebase_uid: str):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        limite_millis = int((datetime.now() - timedelta(days=180)).timestamp() * 1000)
        cursor.execute("DELETE FROM historico_rotas WHERE firebase_uid = %s AND data_fim_millis < %s", (firebase_uid, limite_millis))
        cursor.execute("SELECT * FROM historico_rotas WHERE firebase_uid = %s AND data_fim_millis >= %s ORDER BY data_fim_millis DESC", (firebase_uid, limite_millis))
        rotas = cursor.fetchall()
        conn.commit()
        return rotas
    finally:
        cursor.close()
        conn.close()

@app.delete("/deletar-historico/{rota_id}")
def deletar_historico(rota_id: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM historico_rotas WHERE id = %s;", (rota_id,))
        conn.commit()
        return {"mensagem": "Deletado"}
    finally:
        cursor.close()
        conn.close()
