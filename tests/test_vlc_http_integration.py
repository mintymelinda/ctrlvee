import base64
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from src.services.vlc_controller import VLCController


class VLCStubState:
    def __init__(self):
        self.password = "integration-password"
        self.state = "paused"
        self.current_item = "101"
        self.requests = []
        self.status_code = 200
        self.xml_override = None


class VLCStubServer(ThreadingHTTPServer):
    def __init__(self, address, handler, state):
        self.state = state
        super().__init__(address, handler)


class VLCHTTPIntegrationTests(unittest.TestCase):
    """Exercise the controller against an HTTP-level VLC protocol double."""

    def setUp(self):
        self.state = VLCStubState()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(handler_self):
                expected = "Basic " + base64.b64encode(
                    f":{handler_self.server.state.password}".encode()
                ).decode()
                if handler_self.headers.get("Authorization") != expected:
                    handler_self.send_response(401)
                    handler_self.end_headers()
                    return

                parsed = urlsplit(handler_self.path)
                params = parse_qs(parsed.query)
                handler_self.server.state.requests.append((parsed.path, params))

                if handler_self.server.state.status_code != 200:
                    handler_self.send_response(handler_self.server.state.status_code)
                    handler_self.end_headers()
                    return

                if handler_self.server.state.xml_override is not None:
                    body = handler_self.server.state.xml_override
                elif parsed.path.endswith("/playlist.xml"):
                    body = (
                        '<playlist><leaf id="101" name="First" current="current"/>'
                        '<leaf id="202" name="Second"/></playlist>'
                    )
                else:
                    command = params.get("command", [None])[0]
                    if command == "pl_play":
                        handler_self.server.state.state = "playing"
                        if "id" in params:
                            handler_self.server.state.current_item = params["id"][0]
                    elif command == "pl_pause":
                        handler_self.server.state.state = "paused"
                    elif command == "pl_stop":
                        handler_self.server.state.state = "stopped"
                    body = (
                        "<root><state>"
                        + handler_self.server.state.state
                        + "</state><time>12</time></root>"
                    )

                encoded = body.encode("utf-8")
                handler_self.send_response(200)
                handler_self.send_header("Content-Type", "application/xml")
                handler_self.send_header("Content-Length", str(len(encoded)))
                handler_self.end_headers()
                handler_self.wfile.write(encoded)

            def log_message(self, *_args):
                pass

        self.server = VLCStubServer(("127.0.0.1", 0), Handler, self.state)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)

        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.controller = VLCController(
            host="127.0.0.1",
            port=self.server.server_address[1],
            password=self.state.password,
            queue_backup_file=str(Path(self.temp_dir.name) / "queue.json"),
        )

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_status_and_playlist_are_parsed_from_real_http_responses(self):
        status = self.controller.get_status()
        playlist = self.controller.get_playlist()

        self.assertEqual(status.findtext("state"), "paused")
        self.assertEqual(playlist.find(".//leaf[@id='202']").get("name"), "Second")
        self.assertEqual([request[0] for request in self.state.requests], [
            "/requests/status.xml",
            "/requests/playlist.xml",
        ])

    def test_playback_commands_send_expected_vlc_query_parameters(self):
        self.assertIsNotNone(self.controller.play())
        self.assertIsNotNone(self.controller.pause())
        self.assertIsNotNone(self.controller.seek(42))

        commands = [params for _, params in self.state.requests]
        self.assertEqual([params["command"][0] for params in commands], [
            "pl_play", "pl_pause", "seek",
        ])
        self.assertEqual(commands[2]["val"], ["42"])
        self.assertEqual(self.state.state, "paused")

    def test_item_selection_uses_live_playlist_id_and_sends_targeted_play(self):
        result = self.controller.run_sync("play_item_and_capture", "202")

        self.assertTrue(result["success"])
        self.assertEqual(result["item_id"], "202")
        self.assertEqual(result["position"], 2)
        command_path, params = self.state.requests[-1]
        self.assertEqual(command_path, "/requests/status.xml")
        self.assertEqual(params, {"command": ["pl_play"], "id": ["202"]})
        self.assertEqual(self.state.current_item, "202")

    def test_invalid_credentials_and_http_or_xml_errors_fail_closed(self):
        invalid_controller = VLCController(
            host="127.0.0.1",
            port=self.server.server_address[1],
            password="wrong-password",
            queue_backup_file=str(Path(self.temp_dir.name) / "invalid-queue.json"),
        )
        self.assertIsNone(invalid_controller.get_status())

        self.state.status_code = 503
        self.assertIsNone(self.controller.get_status())

        self.state.status_code = 200
        self.state.xml_override = "<root><state>"
        self.assertIsNone(self.controller.get_status())


if __name__ == "__main__":
    unittest.main()
