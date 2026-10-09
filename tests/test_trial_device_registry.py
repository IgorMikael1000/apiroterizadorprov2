import unittest
from unittest.mock import patch

from fastapi import HTTPException

import main


class ScriptedCursor:
    def __init__(self, fetch_results=(), subscription_rowcount=1):
        self.fetch_results = iter(fetch_results)
        self.subscription_rowcount = subscription_rowcount
        self.rowcount = 1
        self.queries = []
        self.closed = False

    def execute(self, query, params=None):
        self.queries.append((query, params))
        if "INSERT INTO assinaturas" in query:
            self.rowcount = self.subscription_rowcount

    def fetchone(self):
        return next(self.fetch_results)

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


class TrialDeviceRegistryTests(unittest.TestCase):
    def fake_user(self, android_id="device-123"):
        return main.UsuarioNovo(
            nome="Test",
            email="trial-test@example.com",
            cpf="00000000000",
            android_id=android_id,
        )

    def run_registration(self, cursor, user=None):
        connection = ScriptedConnection(cursor)
        with patch.object(main, "get_db_connection", return_value=connection):
            response = main.registrar_usuario(
                user or self.fake_user(),
                uid="firebase-user",
            )
        return response, connection

    def test_new_device_receives_trial_and_is_registered_atomically(self):
        cursor = ScriptedCursor(fetch_results=[(False,), ("TRIAL",)])
        response, connection = self.run_registration(cursor)

        self.assertEqual(response["status_assinatura"], "TRIAL")
        self.assertTrue(
            any("INSERT INTO dispositivos_trial" in query for query, _ in cursor.queries)
        )
        self.assertTrue(connection.committed)
        self.assertFalse(connection.rolled_back)

    def test_previously_trialed_device_is_inactive_and_not_reinserted(self):
        cursor = ScriptedCursor(fetch_results=[(True,), ("INATIVO",)])
        response, connection = self.run_registration(cursor)

        self.assertEqual(response["status_assinatura"], "INATIVO")
        self.assertFalse(
            any("INSERT INTO dispositivos_trial" in query for query, _ in cursor.queries)
        )
        self.assertTrue(connection.committed)

    def test_existing_subscription_does_not_mark_a_new_device_as_trialed(self):
        cursor = ScriptedCursor(
            fetch_results=[(False,), ("ATIVO",)],
            subscription_rowcount=0,
        )
        response, _ = self.run_registration(cursor)

        self.assertEqual(response["status_assinatura"], "ATIVO")
        self.assertFalse(
            any("INSERT INTO dispositivos_trial" in query for query, _ in cursor.queries)
        )

    def test_whitespace_only_android_id_is_rejected(self):
        with self.assertRaises(HTTPException) as raised:
            main.registrar_usuario(
                self.fake_user(android_id="   "),
                uid="firebase-user",
            )

        self.assertEqual(raised.exception.status_code, 422)

    def test_account_deletion_does_not_delete_trial_device_registry(self):
        cursor = ScriptedCursor()
        connection = ScriptedConnection(cursor)
        with patch.object(main, "get_db_connection", return_value=connection):
            result = main.deletar_usuario(firebase_uid="firebase-user")

        self.assertEqual(result, {"mensagem": "Usuário e dados excluídos."})
        deleted_tables = [
            query for query, _ in cursor.queries if query.lstrip().startswith("DELETE")
        ]
        self.assertEqual(len(deleted_tables), 3)
        self.assertTrue(all("dispositivos_trial" not in query for query in deleted_tables))
        self.assertTrue(connection.committed)

    def test_startup_migration_creates_and_backfills_registry(self):
        cursor = ScriptedCursor()
        connection = ScriptedConnection(cursor)
        with patch.object(main, "get_db_connection", return_value=connection):
            main.ensure_purchase_token_column()

        executed_sql = "\n".join(query for query, _ in cursor.queries)
        self.assertIn("IF to_regclass('dispositivos_trial') IS NULL", executed_sql)
        self.assertIn("CREATE TABLE dispositivos_trial", executed_sql)
        self.assertIn("trial_ativado_em TIMESTAMPTZ", executed_sql)
        self.assertIn("INSERT INTO dispositivos_trial (android_id)", executed_sql)
        self.assertIn("ON CONFLICT (android_id) DO NOTHING", executed_sql)
        self.assertTrue(connection.committed)


if __name__ == "__main__":
    unittest.main()
