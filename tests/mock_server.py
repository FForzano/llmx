import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self): self._send({"data": [{"id": "mock"}]})
    def do_POST(self):
        n = int(self.headers["Content-Length"]); body = json.loads(self.rfile.read(n))
        user = body["messages"][-1]["content"]
        if "reviewer report" in body["messages"][0]["content"]:
            out = '[{"reviewer":1,"comment":1,"text":"x","type":"request"}]'
        else:  # one real finding, one hallucinated quote
            out = '<think>hm</think>```json\n[{"file":"a.py","line":2,"severity":"major","quote":"x = 1 / y","issue":"division by zero"},{"file":"a.py","line":1,"severity":"minor","quote":"this text is not in the file","issue":"invented"}]\n```'
        self._send({"choices": [{"message": {"content": out}}], "usage": {"prompt_tokens": len(user) // 4, "completion_tokens": 20}})
    def _send(self, o):
        b = json.dumps(o).encode(); self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
