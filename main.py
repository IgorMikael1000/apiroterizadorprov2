import os
import json
import base64
import binascii
import logging
import traceback
import firebase_admin
import psycopg2
import psycopg2.extras
from psycopg2 import errors
from firebase_admin import auth, credentials
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone

from google.oauth2 import service_account
from google.auth.transport import requests as google_auth_requests
from google.oauth2 import id_token
from googleapiclient.discovery import build
from googleapiclient.http import HttpRequest

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")
PACKAGE_NAME = os.getenv("PACKAGE_NAME", "app.itsolutions.roterizadorpro")
GOOGLE_JSON_STR = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
FIREBASE_JSON_STR = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID")
GOOGLE_PUBSUB_AUDIENCE = os.getenv("GOOGLE_PUBSUB_AUDIENCE")
GOOGLE_PUBSUB_SERVICE_ACCOUNT_EMAIL = os.getenv("GOOGLE_PUBSUB_SERVICE_ACCOUNT_EMAIL")
MIGRATION_SQL = "ALTER TABLE assinaturas ADD COLUMN IF NOT EXISTS purchase_token TEXT;"
BLOCK_APP_MIGRATION_SQL = (
    "ALTER TABLE assinaturas "
    "ADD COLUMN IF NOT EXISTS bloquear_app BOOLEAN NOT NULL DEFAULT FALSE;"
)
GOOGLE_API_TIMEOUT_SECONDS = 8

app = FastAPI(
    title="API RoterizadorPro v2",
    servers=[{"url": "https://apiroterizadorprov2.vercel.app"}],
)
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


def initialize_firebase_admin():
    if not FIREBASE_JSON_STR:
        logger.warning(
            "FIREBASE_SERVICE_ACCOUNT_JSON não configurada; "
            "rotas autenticadas ficarão indisponíveis."
        )
        return None

    try:
        try:
            return firebase_admin.get_app()
        except ValueError:
            pass

        credential = credentials.Certificate(json.loads(FIREBASE_JSON_STR))
        options = {"projectId": FIREBASE_PROJECT_ID} if FIREBASE_PROJECT_ID else None
        firebase_app = firebase_admin.initialize_app(credential, options=options)
        logger.info("Firebase Admin SDK inicializado com credenciais Firebase.")
        return firebase_app
    except Exception:
        logger.exception("Não foi possível inicializar o Firebase Admin SDK.")
        return None


FIREBASE_APP = initialize_firebase_admin()

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
    model_config = ConfigDict(extra="forbid")

    nome: str
    email: str
    cpf: str

class RotaBackup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
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
    model_config = ConfigDict(extra="forbid")

    subscription_id: str
    purchase_token: str


def verify_firebase_token(request: Request) -> str:
    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        logger.error("Falha: Token JWT ausente no cabeçalho Authorization.")
        raise HTTPException(
            status_code=401,
            detail="Token ausente.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if FIREBASE_APP is None:
        logger.error(
            "Firebase Admin SDK indisponível; configure FIREBASE_SERVICE_ACCOUNT_JSON."
        )
        raise HTTPException(
            status_code=503,
            detail="Serviço de autenticação temporariamente indisponível.",
        )

    try:
        decoded_token = auth.verify_id_token(
            token,
            app=FIREBASE_APP,
            check_revoked=True,
        )
        uid = decoded_token.get("uid")
        if not uid:
            logger.error("Falha na validação do token Firebase: UID ausente no token.")
            raise HTTPException(status_code=401, detail="Token inválido.")
        logger.info("Token Firebase validado com sucesso para o UID: %s", uid)
        return uid
    except HTTPException:
        raise
    except (
        auth.InvalidIdTokenError,
        auth.ExpiredIdTokenError,
        auth.RevokedIdTokenError,
        auth.UserDisabledError,
        ValueError,
    ) as exc:
        logger.warning(
            "Falha na validação do token Firebase (%s): %s",
            type(exc).__name__,
            exc,
        )
        raise HTTPException(
            status_code=401,
            detail="Token inválido ou expirado.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except Exception as exc:
        logger.exception("Falha inesperada ao validar token Firebase.")
        raise HTTPException(
            status_code=503,
            detail="Serviço de autenticação temporariamente indisponível.",
        ) from exc


def verify_google_pubsub_token(request: Request) -> None:
    if not GOOGLE_PUBSUB_AUDIENCE or not GOOGLE_PUBSUB_SERVICE_ACCOUNT_EMAIL:
        logger.error("Audience ou service account do Pub/Sub não configurada.")
        raise HTTPException(
            status_code=503,
            detail="Autenticação do webhook não está configurada.",
        )

    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=401,
            detail="Token OIDC do Pub/Sub ausente ou inválido.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        claims = id_token.verify_oauth2_token(
            token,
            google_auth_requests.Request(),
            audience=GOOGLE_PUBSUB_AUDIENCE,
        )
        if (
            claims.get("email") != GOOGLE_PUBSUB_SERVICE_ACCOUNT_EMAIL
            or claims.get("email_verified") is not True
        ):
            raise HTTPException(status_code=401, detail="Identidade do Pub/Sub inválida.")
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(
            status_code=401,
            detail="Token OIDC do Pub/Sub inválido ou expirado.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except Exception as exc:
        logger.exception("Falha ao validar token OIDC do Pub/Sub.")
        raise HTTPException(
            status_code=503,
            detail="Não foi possível validar a identidade do Pub/Sub.",
        ) from exc

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
        cursor.execute(BLOCK_APP_MIGRATION_SQL)
        conn.commit()
    finally:
        cursor.close()
        conn.close()

@app.on_event("startup")
def startup_migration():
    ensure_purchase_token_column()

# --- ROTAS DE USUÁRIO ---
@app.post("/registrar-usuario")
def registrar_usuario(user: UsuarioNovo, firebase_uid: str = Depends(verify_firebase_token)):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO usuarios (firebase_uid, nome, email, cpf)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (firebase_uid) DO UPDATE SET
                nome = EXCLUDED.nome,
                email = EXCLUDED.email,
                cpf = EXCLUDED.cpf
        """, (firebase_uid, user.nome, user.email, user.cpf))

        data_vencimento = datetime.now() + timedelta(days=7)
        cursor.execute("""
            INSERT INTO assinaturas (firebase_uid, status, data_vencimento, purchase_token)
            VALUES (%s, 'TRIAL', %s, NULL)
            ON CONFLICT (firebase_uid) DO NOTHING
        """, (firebase_uid, data_vencimento))

        cursor.execute(
            "SELECT status FROM assinaturas WHERE firebase_uid = %s",
            (firebase_uid,),
        )
        assinatura = cursor.fetchone()
        conn.commit()
        return {
            "mensagem": "Perfil sincronizado com sucesso.",
            "status_assinatura": assinatura[0] if assinatura else None,
        }
    except errors.UniqueViolation as exc:
        conn.rollback()
        raise HTTPException(
            status_code=409,
            detail="CPF ou e-mail já está associado a outro usuário.",
        ) from exc
    except Exception as exc:
        conn.rollback()
        logger.exception("Falha ao sincronizar perfil do usuário %s.", firebase_uid)
        raise HTTPException(
            status_code=500,
            detail="Não foi possível sincronizar o perfil do usuário.",
        ) from exc
    finally:
        cursor.close()
        conn.close()

@app.delete("/deletar-usuario")
def deletar_usuario(firebase_uid: str = Depends(verify_firebase_token)):
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

@app.get("/status-assinatura")
def status_assinatura(firebase_uid: str = Depends(verify_firebase_token)):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT status, data_vencimento, bloquear_app "
            "FROM assinaturas WHERE firebase_uid = %s;",
            (firebase_uid,),
        )
        assinatura = cursor.fetchone()

        if assinatura:
            status_atual, data_vencimento, bloquear_app = assinatura
            data_expirada = (
                data_vencimento is not None
                and datetime.now(data_vencimento.tzinfo) > data_vencimento
            )
            bloqueado = (
                bloquear_app
                or status_atual in {"INATIVO", "VENCIDO", "VENCIDA"}
                or data_expirada
            )
            if data_expirada and status_atual not in {"INATIVO", "VENCIDO", "VENCIDA"}:
                status_atual = "VENCIDO"
                cursor.execute(
                    "UPDATE assinaturas SET status = 'VENCIDO', bloquear_app = TRUE "
                    "WHERE firebase_uid = %s",
                    (firebase_uid,),
                )
                conn.commit()
            resposta = {"status": status_atual, "bloquear_app": bloqueado}
            if data_vencimento is not None:
                resposta["vence_em"] = data_vencimento
            return resposta
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")
    finally:
        cursor.close()
        conn.close()

# --- INTEGRAÇÃO GOOGLE PLAY BILLING ---
@app.post("/subscription/verify")
def verify_subscription(
    req: PurchaseVerification,
    firebase_uid: str = Depends(verify_firebase_token),
):
    try:
        print(
            f"Iniciando verificação para o usuário: {firebase_uid}, "
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
            print(f"Atualizando assinatura no banco para o usuário: {firebase_uid}")
            cursor.execute("""
                UPDATE assinaturas
                SET status = 'ATIVO', data_vencimento = %s, purchase_token = %s
                WHERE firebase_uid = %s
            """, (expiry_date, req.purchase_token, firebase_uid))
            print(
                "UPDATE de assinatura executado. "
                f"Usuário: {firebase_uid}; linhas afetadas: {cursor.rowcount}"
            )
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Assinatura do usuário não encontrada.")
            conn.commit()
            print(f"Assinatura atualizada com sucesso para o usuário: {firebase_uid}")
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
async def google_play_webhook(
    request: Request,
    _: None = Depends(verify_google_pubsub_token),
):
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
            status = "INATIVO"
            bloquear_app = True
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
                bloquear_app = False
            else:
                status = "VENCIDA"
                bloquear_app = True
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
                    """
                    UPDATE assinaturas
                    SET status = 'INATIVO', bloquear_app = TRUE
                    WHERE purchase_token = %s
                    """,
                    (purchase_token,),
                )
            else:
                cursor.execute(
                    """
                    UPDATE assinaturas
                    SET status = %s, data_vencimento = %s, bloquear_app = %s
                    WHERE purchase_token = %s
                    """,
                    (status, expiry_date, bloquear_app, purchase_token),
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
    except HTTPException as exc:
        logging.exception(
            "Webhook Google Play acknowledged with HTTP 200 after processing error: %s",
            exc.detail,
        )
        return {"status": "error", "detail": exc.detail}
    except Exception as exc:
        logging.exception("Falha ao processar RTDN do Google Play.")
        return {
            "status": "error",
            "detail": "Falha ao processar notificação do Google Play.",
        }

# --- HISTÓRICO DE ROTAS ---
@app.post("/salvar-historico")
def salvar_historico(
    rota: RotaBackup,
    firebase_uid: str = Depends(verify_firebase_token),
):
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
            WHERE historico_rotas.firebase_uid = EXCLUDED.firebase_uid
        """, (
            rota.id, firebase_uid, rota.data_inicio_millis, rota.data_fim_millis, rota.tempo_decorrido_segundos,
            rota.total_paradas, rota.pacotes_entregues, rota.pacotes_falhos, rota.km_rodados, rota.faturamento_bruto,
            rota.consumo_kml, rota.preco_combustivel
        ))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=403, detail="Rota pertence a outro usuário.")
        conn.commit()
        return {"mensagem": "Histórico salvo/atualizado"}
    finally:
        cursor.close()
        conn.close()

@app.get("/obter-historico")
def obter_historico(firebase_uid: str = Depends(verify_firebase_token)):
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
def deletar_historico(
    rota_id: str,
    firebase_uid: str = Depends(verify_firebase_token),
):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "DELETE FROM historico_rotas WHERE id = %s AND firebase_uid = %s;",
            (rota_id, firebase_uid),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Rota não encontrada.")
        conn.commit()
        return {"mensagem": "Deletado"}
    finally:
        cursor.close()
        conn.close()
