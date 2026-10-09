import os
import json
import base64
import binascii
import logging
import traceback
import psycopg2
import psycopg2.extras
from psycopg2 import errors
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import HttpRequest

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")
PACKAGE_NAME = os.getenv("PACKAGE_NAME", "app.itsolutions.roterizadorpro")
GOOGLE_JSON_STR = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
MIGRATION_SQL = "ALTER TABLE assinaturas ADD COLUMN IF NOT EXISTS purchase_token TEXT;"
GOOGLE_API_TIMEOUT_SECONDS = 8

app = FastAPI(
    title="API RoterizadorPro v2",
    servers=[{"url": "https://apiroterizadorprov2.vercel.app"}],
)
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# --- CONFIGURAÇÃO GOOGLE PLAY API ---
def build_google_request(
    http,
    postproc,
    uri,
    method="GET",
    body=None,
    headers=None,
    methodId=None,
    resumable=None,
):
    transport = getattr(http, "http", http)
    transport.timeout = GOOGLE_API_TIMEOUT_SECONDS
    return HttpRequest(
        http,
        postproc,
        uri,
        method=method,
        body=body,
        headers=headers,
        methodId=methodId,
        resumable=resumable,
    )


def get_android_publisher():
    if not GOOGLE_JSON_STR:
        raise Exception("GOOGLE_SERVICE_ACCOUNT_JSON não configurado no .env.")
    credentials_dict = json.loads(GOOGLE_JSON_STR)
    credentials = service_account.Credentials.from_service_account_info(
        credentials_dict,
        scopes=["https://www.googleapis.com/auth/androidpublisher"]
    )
    return build(
        'androidpublisher',
        'v3',
        credentials=credentials,
        requestBuilder=build_google_request,
    )

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
        print(
            f"Iniciando verificação para o usuário: {req.firebase_uid}, "
            f"produto: {req.subscription_id}"
        )
        publisher = get_android_publisher()
        sub_info = publisher.purchases().subscriptionsv2().get(
            packageName=PACKAGE_NAME,
            token=req.purchase_token
        ).execute()
        print(
            "Resposta da Google Play recebida com sucesso. "
            f"Estado da assinatura: {sub_info.get('subscriptionState')}"
        )

        subscription_state = sub_info.get("subscriptionState")
        if subscription_state not in [
            "SUBSCRIPTION_STATE_ACTIVE",
            "SUBSCRIPTION_STATE_IN_GRACE_PERIOD",
        ]:
            print(
                "Verificação rejeitada: estado da assinatura não permite ativação. "
                f"Estado recebido: {subscription_state!r}"
            )
            raise HTTPException(status_code=400, detail="Pagamento não confirmado pela Google.")

        line_items = sub_info.get("lineItems", [])
        matching_line_items = [
            item
            for item in line_items
            if item.get("productId") == req.subscription_id
        ]
        if not matching_line_items:
            returned_product_ids = [
                item.get("productId") for item in line_items
            ]
            print(
                "Verificação rejeitada: productId não corresponde. "
                f"Recebido no pedido: {req.subscription_id!r}; "
                f"productId(s) da Google: {returned_product_ids!r}"
            )
            raise HTTPException(status_code=400, detail="Produto da assinatura não corresponde.")

        expiry_time = matching_line_items[0].get("expiryTime")
        if not expiry_time:
            print(
                "Verificação rejeitada: expiryTime ausente no item correspondente. "
                f"productId: {req.subscription_id!r}; "
                f"expiryTime recebido: {expiry_time!r}"
            )
            raise HTTPException(status_code=400, detail="Data de expiração não encontrada na Google.")
        expiry_date = datetime.fromisoformat(
            expiry_time.replace("Z", "+00:00")
        ).astimezone(timezone.utc).replace(tzinfo=None)

        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            print(f"Atualizando assinatura no banco para o usuário: {req.firebase_uid}")
            cursor.execute("""
                UPDATE assinaturas
                SET status = 'ATIVO', data_vencimento = %s, purchase_token = %s
                WHERE firebase_uid = %s
            """, (expiry_date, req.purchase_token, req.firebase_uid))
            print(
                "UPDATE de assinatura executado. "
                f"Usuário: {req.firebase_uid}; linhas afetadas: {cursor.rowcount}"
            )
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Assinatura do usuário não encontrada.")
            conn.commit()
            print(f"Assinatura atualizada com sucesso para o usuário: {req.firebase_uid}")
            return {
                "status": "success",
                "message": "Assinatura verificada e atualizada com sucesso",
                "mensagem": "Assinatura validada com sucesso!",
                "vence_em": expiry_date,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()

    except HTTPException as e:
        if e.status_code >= 500:
            print(f"ERRO CRÍTICO EM /subscription/verify: {e.detail}")
            traceback.print_exc()
        raise
    except Exception as e:
        error_trace = traceback.format_exc()
        print(f"ERRO COMPLETO NA VERIFICAÇÃO:\n{error_trace}")
        raise HTTPException(status_code=500, detail=f"Erro interno: {str(e)}")

@app.post("/webhooks/google-play")
async def google_play_webhook(request: Request):
    try:
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logging.warning("Webhook Google Play rejeitado: corpo não contém JSON válido.")
            raise HTTPException(status_code=400, detail="Payload JSON inválido.") from exc

        if not isinstance(payload, dict):
            logging.warning("Webhook Google Play rejeitado: envelope Pub/Sub inválido.")
            raise HTTPException(status_code=400, detail="Envelope Pub/Sub inválido.")
        logging.info("Webhook Google Play recebido.")

        message = payload.get("message")
        data_base64 = message.get("data") if isinstance(message, dict) else None
        if not isinstance(data_base64, str) or not data_base64:
            logging.warning("RTDN rejeitada: envelope Pub/Sub sem message.data.")
            raise HTTPException(status_code=400, detail="Campo message.data ausente.")

        try:
            decoded_data = base64.b64decode(data_base64, validate=True).decode("utf-8")
            notification = json.loads(decoded_data)
        except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
            logging.warning("RTDN rejeitada: message.data não contém Base64/JSON válido: %s", exc)
            raise HTTPException(status_code=400, detail="message.data inválido.") from exc

        if not isinstance(notification, dict):
            logging.warning("RTDN rejeitada: conteúdo decodificado não é um objeto JSON.")
            raise HTTPException(status_code=400, detail="Notificação inválida.")

        if "testNotification" in notification:
            logging.info("RTDN de teste do Pub/Sub recebida.")
            return {"status": "ok"}

        sub_notification = notification.get("subscriptionNotification")
        if not isinstance(sub_notification, dict):
            logging.info("RTDN sem subscriptionNotification; evento reconhecido e ignorado.")
            return {"status": "ok"}

        try:
            notification_type = int(sub_notification.get("notificationType"))
        except (TypeError, ValueError) as exc:
            logging.warning("RTDN rejeitada: notificationType ausente ou inválido.")
            raise HTTPException(status_code=400, detail="notificationType inválido.") from exc

        purchase_token = sub_notification.get("purchaseToken")
        token_suffix = (
            purchase_token[-4:]
            if isinstance(purchase_token, str) and len(purchase_token) >= 4
            else "indisponível"
        )
        logging.info(
            "Webhook recebido! Tipo: %s, Token: ***%s",
            notification_type,
            token_suffix,
        )

        if notification_type not in {2, 3, 12, 13}:
            logging.info("RTDN de assinatura ignorada: tipo %s não tratado.", notification_type)
            return {"status": "ignored", "notification_type": notification_type}

        subscription_id = sub_notification.get("subscriptionId")
        if not isinstance(purchase_token, str) or not purchase_token:
            logging.warning("RTDN rejeitada: purchaseToken ausente para tipo %s.", notification_type)
            raise HTTPException(status_code=400, detail="purchaseToken ausente.")

        if notification_type in {12, 13}:
            status = "inativo"
            expiry_date = None
            logging.info(
                "RTDN tipo %s: desativando assinatura identificada pelo token ***%s.",
                notification_type,
                token_suffix,
            )
        else:
            if not isinstance(subscription_id, str) or not subscription_id:
                logging.warning(
                    "RTDN rejeitada: subscriptionId ausente para tipo %s.",
                    notification_type,
                )
                raise HTTPException(status_code=400, detail="subscriptionId ausente.")
            logging.info(
                "RTDN tipo %s: consultando estado atual para subscriptionId=%s.",
                notification_type,
                subscription_id,
            )
            publisher = get_android_publisher()
            sub_info = publisher.purchases().subscriptionsv2().get(
                packageName=PACKAGE_NAME,
                token=purchase_token,
            ).execute()
            line_items = sub_info.get("lineItems", [])
            matching_items = [
                item for item in line_items
                if item.get("productId") == subscription_id
            ]
            if not matching_items or not matching_items[0].get("expiryTime"):
                logging.error(
                    "RTDN tipo %s: item/expiração não encontrado para subscriptionId=%s.",
                    notification_type,
                    subscription_id,
                )
                raise HTTPException(
                    status_code=502,
                    detail="Não foi possível obter a expiração da assinatura na Google Play.",
                )

            expiry_time = matching_items[0]["expiryTime"]
            expiry_date = datetime.fromisoformat(
                expiry_time.replace("Z", "+00:00")
            ).astimezone(timezone.utc).replace(tzinfo=None)
            auto_renew_enabled = matching_items[0].get(
                "autoRenewingPlan", {}
            ).get("autoRenewEnabled")
            current_state = sub_info.get("subscriptionState")
            has_access = (
                current_state in {
                    "SUBSCRIPTION_STATE_ACTIVE",
                    "SUBSCRIPTION_STATE_IN_GRACE_PERIOD",
                    "SUBSCRIPTION_STATE_CANCELED",
                }
                and expiry_date > datetime.now(timezone.utc).replace(tzinfo=None)
            )
            if has_access:
                status = "ATIVO"
            else:
                status = "VENCIDA"
                expiry_date = datetime.now(timezone.utc).replace(tzinfo=None)
            if notification_type == 3:
                logging.info(
                    "RTDN tipo 3: estado Google=%s, autoRenewEnabled=%s; "
                    "acesso=%s até %s.",
                    current_state,
                    auto_renew_enabled,
                    "mantido" if status == "ATIVO" else "revogado",
                    expiry_date,
                )
            else:
                logging.info(
                    "RTDN tipo 2: estado Google=%s, nova expiração=%s, acesso=%s.",
                    current_state,
                    expiry_date,
                    "mantido" if status == "ATIVO" else "revogado",
                )

        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            if notification_type in {12, 13}:
                cursor.execute(
                    "UPDATE assinaturas SET status = 'inativo' WHERE purchase_token = %s",
                    (purchase_token,),
                )
            else:
                cursor.execute(
                    """
                    UPDATE assinaturas
                    SET status = %s, data_vencimento = %s
                    WHERE purchase_token = %s
                    """,
                    (status, expiry_date, purchase_token),
                )
            rows_updated = cursor.rowcount
            logging.info(
                "RTDN tipo %s: UPDATE executado no Neon; linhas afetadas: %s.",
                notification_type,
                rows_updated,
            )
            if rows_updated == 0:
                conn.rollback()
                logging.warning(
                    "RTDN tipo %s sem assinatura correspondente no Neon para token ***%s.",
                    notification_type,
                    token_suffix,
                )
                raise HTTPException(
                    status_code=503,
                    detail="Assinatura ainda não está disponível para atualização.",
                )
            conn.commit()
            logging.info(
                "RTDN tipo %s processada: status=%s, linhas atualizadas=%s.",
                notification_type,
                status,
                rows_updated,
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()

        return {"status": "ok", "notification_type": notification_type}
    except HTTPException:
        raise
    except Exception as exc:
        logging.exception("Falha ao processar RTDN do Google Play.")
        raise HTTPException(
            status_code=500,
            detail="Falha ao processar notificação do Google Play.",
        ) from exc

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
