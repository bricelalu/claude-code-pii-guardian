#!/usr/bin/env python3
"""Mock OpenAI-compatible server for testing PII masking.

Reads the customers_leaked table and returns it in the response,
so we can verify that LiteLLM masks PII in the response.
"""
import json
import sqlite3
import http.server
import socketserver
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "leaked_customers.db"
PORT = 8082


class MockOpenAIHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_response(404)
            self.end_headers()
            return

        content_length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(content_length)

        # Read the database
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("SELECT * FROM customers_leaked")
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()

        # Build the response in OpenAI format
        response = {
            "id": "chatcmpl-mock-001",
            "object": "chat.completion",
            "created": 1790607233,
            "model": "claude-sonnet-5",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(rows, indent=2)
                    },
                    "finish_reason": "stop"
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 1000,
                "total_tokens": 1100
            }
        }

        response_body = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        self.wfile.write(response_body)

    def log_message(self, format, *args):
        # Suppress default logging
        pass


if __name__ == "__main__":
    with socketserver.TCPServer(("", PORT), MockOpenAIHandler) as httpd:
        print(f"Mock OpenAI server running on port {PORT}")
        httpd.serve_forever()
