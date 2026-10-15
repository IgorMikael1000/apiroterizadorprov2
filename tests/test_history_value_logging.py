import asyncio
import json
import unittest
from unittest.mock import MagicMock, patch

from starlette.requests import Request

import main


class SalvarHistoricoLoggingTests(unittest.TestCase):
    def test_logs_app_values_and_api_validated_values(self):
        payload = {
            "id": "route-id",
            "data_inicio_millis": 1,
            "data_fim_millis": 2,
            "tempo_decorrido_segundos": 1,
            "total_paradas": 1,
            "pacotes_entregues": 1,
            "pacotes_falhos": 0,
            "km_rodados": 1,
            "faturamento_bruto": 1,
            "consumo_kml": "10.50",
            "preco_combustivel": "5.89",
        }
        body = json.dumps(payload).encode()

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/salvar-historico",
                "headers": [(b"content-type", b"application/json")],
                "query_string": b"",
            },
            receive,
        )
        cursor = MagicMock(rowcount=1)
        connection = MagicMock()
        connection.cursor.return_value = cursor

        with patch.object(main, "get_db_connection", return_value=connection):
            valores_app = asyncio.run(
                main.capturar_valores_historico_app(request)
            )
            with self.assertLogs(main.logger, level="INFO") as captured:
                result = main.salvar_historico(
                    main.RotaBackup(**payload),
                    valores_app=valores_app,
                    firebase_uid="test-uid",
                )

        self.assertEqual(result, {"mensagem": "Histórico salvo/atualizado"})
        log_output = "\n".join(captured.output)
        self.assertIn(
            "valores recebidos do app={'consumo_kml': '10.50', "
            "'preco_combustivel': '5.89'}",
            log_output,
        )
        self.assertIn(
            "valores validados pela API={'consumo_kml': 10.5, "
            "'preco_combustivel': 5.89}",
            log_output,
        )
        self.assertEqual(cursor.execute.call_args.args[1][-2:], (10.5, 5.89))
        connection.commit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
