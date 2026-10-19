import unittest
from unittest.mock import patch

import main


class ScriptedCursor:
    def __init__(self, rows=(), rowcount=1):
        self.rows = rows
        self.rowcount = rowcount
        self.queries = []
        self.closed = False

    def execute(self, query, params=None):
        self.queries.append((query, params))

    def fetchall(self):
        return self.rows

    def close(self):
        self.closed = True


class ScriptedConnection:
    def __init__(self, cursor):
        self.fake_cursor = cursor
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self.fake_cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class FCMActivationTests(unittest.TestCase):
    def test_token_update_is_scoped_to_authenticated_firebase_uid(self):
        cursor = ScriptedCursor()
        connection = ScriptedConnection(cursor)
        with patch.object(main, "get_db_connection", return_value=connection):
            response = main.atualizar_fcm_token(
                main.FCMTokenUpdate(fcm_token="device-token"),
                firebase_uid="firebase-user",
            )

        self.assertEqual(response, {"mensagem": "Token FCM atualizado com sucesso."})
        self.assertEqual(
            cursor.queries,
            [
                (
                    "UPDATE usuarios SET fcm_token = %s WHERE firebase_uid = %s",
                    ("device-token", "firebase-user"),
                )
            ],
        )
        self.assertTrue(connection.committed)

    def test_activation_cron_filters_and_sends_notifications_to_selected_tokens(self):
        cursor = ScriptedCursor(rows=[("token-one",), ("token-two",)])
        connection = ScriptedConnection(cursor)
        with (
            patch.object(main, "get_db_connection", return_value=connection),
            patch.object(main.messaging, "send") as send,
        ):
            response = main.ativar_novos_usuarios()

        self.assertEqual(
            response,
            {"mensagem": "Notificações enviadas.", "quantidade": 2},
        )
        query = cursor.queries[0][0]
        self.assertIn("usuarios.fcm_token IS NOT NULL", query)
        self.assertIn("JOIN dispositivos_trial", query)
        self.assertIn("dispositivos_trial.trial_ativado_em < NOW() - INTERVAL '24 hours'", query)
        self.assertIn("NOT EXISTS", query)
        self.assertIn(
            "historico_rotas.firebase_uid = usuarios.firebase_uid",
            query,
        )
        self.assertEqual(
            [call.args[0].token for call in send.call_args_list],
            ["token-one", "token-two"],
        )
        notification = send.call_args_list[0].args[0].notification
        self.assertEqual(notification.title, "📦 Faça sua primeira rota hoje!")
        self.assertEqual(
            notification.body,
            "Você já tem o Roterizador Pro no celular. Crie sua primeira rota e "
            "veja como é fácil economizar horas de trabalho e combustível!",
        )
        self.assertTrue(cursor.closed)
        self.assertTrue(connection.closed)
