import json
import urllib.request

BASE = "http://127.0.0.1:8000"


def call(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def chat(tok, message, thread=""):
    st, b = call("POST", "/api/chat", {"message": message, "thread_id": thread}, token=tok)
    print(f"\n>> {message!r} (thread={thread[:8] or '-'})")
    print("   ", st, b.get("type"), "sources=", b.get("sources"))
    print("   text:", (b.get("message") or "")[:220])
    return b


_, s = call("POST", "/api/login", {"role": "patient", "mobile": "9000000001", "name": "Asha Verma"})
tok = s["token"]

# replicate the test exactly: staff attempt first, client-generated thread
_, staff = call("POST", "/api/login", {"role": "staff", "name": "Reception"})
call("POST", "/api/chat", {"message": "hello", "thread_id": "x"}, token=staff.get("token", ""))

from uuid import uuid4
client_thread = uuid4().hex
st, b1 = call("POST", "/api/chat", {"message": "What time does the clinic open on Saturday?",
                                    "thread_id": client_thread}, token=tok)
print("\n>> Q01 with client thread", st, "resp thread=", b1.get("thread_id"),
      "sources=", b1.get("sources"))
print("   text:", (b1.get("message") or "")[:200])

thr = b1.get("thread_id") or uuid4().hex
st, b2 = call("POST", "/api/chat", {"message": "What should I avoid after it?",
                                    "thread_id": thr}, token=tok)
print("\n>> follow-up thread", thr[:8], st, b2.get("type"), "sources=", b2.get("sources"))
print("   text:", (b2.get("message") or "")[:220])
