"""Synthetic OpenRouter/OpenCode protocol differences. Never stores credentials."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, data, kind="application/json"):
        raw = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.reply(200, {"data": [
            {"id": "kimi-k2.5", "context_length": 32768, "supported_parameters": ["tools"],
             "architecture": {"input_modalities": ["text", "image"]}},
            {"id": "gpt-5", "supported_parameters": ["tools"]}, {"id": "claude-sonnet-4-5"}]})

    def do_POST(self):
        b = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        # Test assertions run in memory. Neither authentication nor conversations are logged.
        self.server.requests.append(b)
        model = b.get("model", "")
        if model == "redirect":
            self.send_response(307)
            self.send_header("Location", self.server.redirect)
            self.end_headers()
            return
        if model.startswith("error-"):
            return self.reply(int(model.split("-")[1]), {"error": {"message": "synthetic failure"}})
        if "chat_template_kwargs" in b or any("reasoning_content" in m for m in b.get("messages", [])):
            return self.reply(400, {"error": {"message": "reasoning_content not accepted"}})
        if model == "alternate-limit" and "max_tokens" in b:
            return self.reply(400, {"error": {"message": "use max_completion_tokens"}})
        if model == "no-usage" and "stream_options" in b:
            return self.reply(400, {"error": {"message": "stream_options not accepted"}})
        if not b.get("stream"):
            return self.reply(200, {"choices": [{"message": {"content": "OK"}}]})
        if b.get("tools") and b["tools"][0]["function"]["name"] == "echo" and not any(m["role"] == "tool" for m in b["messages"]):
            name = b["tools"][0]["function"]["name"]
            delta = {"tool_calls": [{"id": "call_synthetic", "type": "function",
                                     "function": {"name": name, "arguments": {"value": 7}}}]}
            finish = "tool_calls"
        else:
            delta, finish = {"content": "OK"}, "stop"
            if model == "probe-check" and any(m["role"] == "tool" for m in b["messages"]):
                delta = {"content": "7"}
        chunks = [{"choices": [{"delta": delta, "finish_reason": None}]}]
        if model != "cut":
            chunks.append({"choices": [{"delta": {}, "finish_reason": finish}]})
        raw = "".join("data: " + json.dumps(c) + "\n\n" for c in chunks)
        if model != "cut":
            raw += "data: [DONE]\n\n"
        self.reply(200, raw.encode(), "text/event-stream")


def make_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    server.requests = []
    return server
