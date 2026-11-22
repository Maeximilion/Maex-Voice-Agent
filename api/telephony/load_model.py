"""The language model of the load test (#841): a stand-in for a hosted model.

    python -m api.telephony.load_model [--port 8080] [--delay 0.25]

Answers the chat completions requests of `agent/llm.py` (`ChatCompletionsLLM`)
after a fixed delay, the time a hosted model takes for a short answer, so a
turn keeps its real timing without a model or a bill. A guest's words become a
menu search, its result the sentence "Gerne. Darf es noch etwas sein?": every
turn makes the two requests and the one tool call of a turn with a tool hop
(docs/20 §11), and the tool runs on the stack's real database.

In operation the model runs outside the server and costs it network, not
processor; so this one does nothing but wait and answer, on a thread per
request. Standard library only, it runs in any Python 3.12 image (the load
test's deploy/loadtest/docker-compose.router.yml). Invented content only, no
token: it answers whoever asks, on a network of the load test.
"""

import argparse
import json
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

SAY = "Gerne. Darf es noch etwas sein?"
INPUT_MARK = "\nEingabe: "


def answer(user_message: str) -> dict[str, Any]:
    """The turn for one request: a search for the guest's words, a sentence
    for a tool's result (the input is then JSON with the field `tool`)."""
    _, _, said = user_message.partition(INPUT_MARK)
    said = said.strip()
    try:
        tool_result = json.loads(said)
    except ValueError:
        tool_result = None
    turn: dict[str, Any] = {
        "say": None,
        "tool": None,
        "args": {},
        "slots": {},
        "not_understood": None,
    }
    if isinstance(tool_result, dict) and "tool" in tool_result:
        turn["say"] = SAY
    else:
        turn["tool"] = "search_menu"
        turn["args"] = {"query": said[:300] or "Speisekarte"}
    return turn


def completion(body: dict[str, Any]) -> dict[str, Any]:
    messages = body.get("messages") or []
    user = next(
        (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
        "",
    )
    content = json.dumps(answer(user), ensure_ascii=False)
    prompt = sum(len(str(m.get("content", ""))) for m in messages)
    return {
        "model": str(body.get("model") or "load"),
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
        # Rough counts, four characters a token: the call log reads them.
        "usage": {"prompt_tokens": prompt // 4, "completion_tokens": len(content) // 4},
    }


def handler(
    delay: float, sleep: Callable[[float], None] = time.sleep
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if not self.path.rstrip("/").endswith("/chat/completions"):
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length))
            except ValueError:
                self.send_error(400)
                return
            sleep(delay)
            data = json.dumps(completion(body)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            # The health check of the Compose file.
            self.send_response(200 if self.path == "/health" else 404)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            pass  # one line per request would be the load of a log, not a model's

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m api.telephony.load_model", description=__doc__
    )
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--delay", type=float, default=0.25)
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer(("0.0.0.0", args.port), handler(args.delay))
    server.daemon_threads = True
    print(f"load model on port {args.port}, {args.delay} s per answer", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
