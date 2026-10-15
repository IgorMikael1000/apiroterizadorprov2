import unittest
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError
from psycopg2 import errors
from starlette.requests import Request

import main


class FakeCursor:
    def __init__(self, rowcount=1, execute_error=None, fetch_result=None):
        self.rowcount = rowcount
        self.execute_error = execute_error
        self.fetch_result = fetch_result
        self.executed = None
        self.closed = False

    def execute(self, query, params):
        if self.execute_error:
            raise self.execute_error
        self.executed = (query, params)

    def fetchone(self):
        return self.fetch_result

    def close(self):
        self.closed = True


class FakeConnection:
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


class AtualizarPerfilTests(unittest.TestCase):
    def make_database(self, cursor):
        connection = FakeConnection(cursor)
        return connection, patch.object(main, "get_db_connection", return_value=connection)

    def test_updates_email_for_authenticated_uid_and_commits(self):
        cursor = FakeCursor()
        connection, database_patch = self.make_database(cursor)
        with database_patch:
            result = main.atualizar_perfil(
                main.AtualizacaoPerfil(email="New.User@example.com"),
                firebase_uid="authenticated-uid",
            )

        self.assertEqual(
            result,
            {
                "status": "success",
                "mensagem": "E-mail atualizado com sucesso.",
            },
        )
        self.assertEqual(
            cursor.executed,
            (
                "UPDATE usuarios SET email = %s WHERE firebase_uid = %s",
                ("New.User@example.com", "authenticated-uid"),
            ),
        )
        self.assertTrue(connection.committed)
        self.assertFalse(connection.rolled_back)
        self.assertTrue(cursor.closed)
        self.assertTrue(connection.closed)

    def test_returns_404_when_profile_does_not_exist(self):
        cursor = FakeCursor(rowcount=0)
        connection, database_patch = self.make_database(cursor)
        with database_patch, self.assertRaises(HTTPException) as raised:
            main.atualizar_perfil(
                main.AtualizacaoPerfil(email="user@example.com"),
                firebase_uid="missing-uid",
            )

        self.assertEqual(raised.exception.status_code, 404)
        self.assertTrue(connection.rolled_back)
        self.assertFalse(connection.committed)

    def test_updates_phone_only_and_commits(self):
        cursor = FakeCursor()
        connection, database_patch = self.make_database(cursor)
        with database_patch:
            result = main.atualizar_perfil(
                main.AtualizacaoPerfil(telefone="+5511999999999"),
                firebase_uid="authenticated-uid",
            )

        self.assertEqual(result["mensagem"], "Telefone atualizado com sucesso.")
        self.assertEqual(
            cursor.executed,
            (
                "UPDATE usuarios SET telefone = %s WHERE firebase_uid = %s",
                ("+5511999999999", "authenticated-uid"),
            ),
        )
        self.assertTrue(connection.committed)

    def test_clears_phone_when_explicitly_set_to_null(self):
        cursor = FakeCursor()
        connection, database_patch = self.make_database(cursor)
        with database_patch:
            main.atualizar_perfil(
                main.AtualizacaoPerfil(telefone=None),
                firebase_uid="authenticated-uid",
            )

        self.assertEqual(
            cursor.executed,
            (
                "UPDATE usuarios SET telefone = %s WHERE firebase_uid = %s",
                (None, "authenticated-uid"),
            ),
        )
        self.assertTrue(connection.committed)

    def test_updates_email_and_phone_together(self):
        cursor = FakeCursor()
        connection, database_patch = self.make_database(cursor)
        with database_patch:
            result = main.atualizar_perfil(
                main.AtualizacaoPerfil(
                    email="New.User@example.com",
                    telefone="",
                ),
                firebase_uid="authenticated-uid",
            )

        self.assertEqual(result["mensagem"], "Perfil atualizado com sucesso.")
        self.assertEqual(
            cursor.executed,
            (
                "UPDATE usuarios SET email = %s, telefone = %s "
                "WHERE firebase_uid = %s",
                ("New.User@example.com", "", "authenticated-uid"),
            ),
        )
        self.assertTrue(connection.committed)

    def test_rejects_update_without_fields(self):
        with self.assertRaises(HTTPException) as raised:
            main.atualizar_perfil(
                main.AtualizacaoPerfil(),
                firebase_uid="authenticated-uid",
            )

        self.assertEqual(raised.exception.status_code, 422)

    def test_returns_409_when_email_is_already_used(self):
        cursor = FakeCursor(execute_error=errors.UniqueViolation("duplicate email"))
        connection, database_patch = self.make_database(cursor)
        with database_patch, self.assertRaises(HTTPException) as raised:
            main.atualizar_perfil(
                main.AtualizacaoPerfil(email="taken@example.com"),
                firebase_uid="authenticated-uid",
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertTrue(connection.rolled_back)
        self.assertFalse(connection.committed)

    def test_returns_500_and_rolls_back_on_database_failure(self):
        cursor = FakeCursor(execute_error=RuntimeError("database unavailable"))
        connection, database_patch = self.make_database(cursor)
        with database_patch, self.assertRaises(HTTPException) as raised:
            main.atualizar_perfil(
                main.AtualizacaoPerfil(email="user@example.com"),
                firebase_uid="authenticated-uid",
            )

        self.assertEqual(raised.exception.status_code, 500)
        self.assertTrue(connection.rolled_back)
        self.assertFalse(connection.committed)

    def test_returns_profile_including_phone(self):
        cursor = FakeCursor(
            fetch_result=(
                "Test User",
                "user@example.com",
                "00000000000",
                "device-123",
                "+5511999999999",
            )
        )
        connection, database_patch = self.make_database(cursor)
        with database_patch:
            profile = main.obter_perfil(firebase_uid="authenticated-uid")

        self.assertEqual(profile["telefone"], "+5511999999999")
        self.assertEqual(profile["email"], "user@example.com")
        self.assertEqual(
            cursor.executed,
            (
                "SELECT nome, email, cpf, android_id, telefone "
                "FROM usuarios WHERE firebase_uid = %s",
                ("authenticated-uid",),
            ),
        )
        self.assertTrue(cursor.closed)
        self.assertTrue(connection.closed)

    def test_rejects_invalid_email_and_extra_fields(self):
        with self.assertRaises(ValidationError):
            main.AtualizacaoPerfil(email="not-an-email")

        with self.assertRaises(ValidationError):
            main.AtualizacaoPerfil(
                email="user@example.com",
                firebase_uid="untrusted-client-uid",
            )

    def test_rejects_missing_authentication(self):
        request = Request(
            {
                "type": "http",
                "headers": [],
                "method": "PUT",
                "path": "/atualizar-perfil",
                "query_string": b"",
            }
        )
        with self.assertRaises(HTTPException) as raised:
            main.verify_firebase_token(request)

        self.assertEqual(raised.exception.status_code, 401)

    def test_rejects_invalid_firebase_token(self):
        request = Request(
            {
                "type": "http",
                "headers": [(b"authorization", b"Bearer invalid-token")],
                "method": "PUT",
                "path": "/atualizar-perfil",
                "query_string": b"",
            }
        )
        with patch.object(main, "FIREBASE_APP", object()), patch.object(
            main.auth,
            "verify_id_token",
            side_effect=ValueError("invalid token"),
        ), self.assertRaises(HTTPException) as raised:
            main.verify_firebase_token(request)

        self.assertEqual(raised.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()
