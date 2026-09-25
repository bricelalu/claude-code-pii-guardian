"""WSGI entrypoint for a RunPod load-balancing worker.

RunPod routes traffic only to workers whose GET /ping returns 200, and treats 204
as "still initializing". Presidio's app loads the NLP engine and the GLiNER2 model
synchronously, so it is loaded in a background thread (inside the gunicorn worker,
after fork, so CUDA initializes in the process that uses it) while /ping reports 204.
"""
import threading
import traceback

_app = None
_failed = False


def _load():
    global _app, _failed
    try:
        from app import create_app

        _app = create_app()
    except Exception:
        traceback.print_exc()
        _failed = True


threading.Thread(target=_load, daemon=True).start()


def application(environ, start_response):
    path = environ.get("PATH_INFO", "")
    if _failed:
        start_response("500 Internal Server Error", [("Content-Length", "0")])
        return [b""]
    if _app is None:
        start_response("204 No Content" if path == "/ping" else "503 Service Unavailable",
                       [("Content-Length", "0")])
        return [b""]
    if path == "/ping":
        start_response("200 OK", [("Content-Type", "application/json")])
        return [b'{"status":"healthy"}']
    return _app(environ, start_response)
