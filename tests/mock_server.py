import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
N = {"find": 0, "commit": 0, "test": 0}
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self): self._send({"data": [{"id": "mock"}]})
    def do_POST(self):
        n = int(self.headers["Content-Length"]); body = json.loads(self.rfile.read(n))
        sysm, user, msgs = body["messages"][0]["content"], body["messages"][-1]["content"], body["messages"]
        if len(user) > 3000 and "CTXLIMIT" in sysm + user[:0]:   # not used
            pass
        if "reviewer report" in sysm:
            out = '[{"reviewer":1,"comment":1,"text":"x","type":"request"}]'
        elif "review code" in sysm or "strict colleague" in sysm:
            N["find"] += 1   # pass 1: real finding + invented one; pass 2: the same real finding + another real one
            real = '{"file":"a.py","line":2,"severity":"major","quote":"x = 1 / y","issue":"division by zero"}'
            if N["find"] % 2: out = '<think>hm</think>```json\n[' + real + ',{"file":"a.py","line":1,"severity":"minor","quote":"this text is not in the file","issue":"invented"}]\n```'
            else: out = '[' + real + ',{"file":"a.py","line":1,"severity":"minor","quote":"import os","issue":"unused import"}]'
        elif "git commit message" in sysm:
            if len(msgs) > 2: out = "Fix division in a.py"            # repair round
            else: out = "Fix the division by zero error that happens when the divisor variable y is zero in a.py."   # > 72 chars
        elif "tests requested" in sysm:
            out = "```python\nprint('ok')\n```" if len(msgs) > 2 else "```python\nprint('bad')\n```"
        elif "log or test output" in sysm:
            if "Part " in user and "NONE" in user: out = "NONE" if "needle" not in user else "`needle line` here"
            else: out = "first error: `ERROR real thing`\ninvented: `ERROR not in the input`\nplain line"
        else: out = "ok"
        self._send({"choices": [{"message": {"content": out}}], "usage": {"prompt_tokens": len(user) // 4, "completion_tokens": 20}})
    def _send(self, o):
        b = json.dumps(o).encode(); self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
