import json
import urllib.request
from datetime import date, timedelta

BASE = "http://127.0.0.1:8000"


def call(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


_, s = call("POST", "/api/login", {"role": "patient", "mobile": "9000000001", "name": "Asha Verma"})
tok = s["token"]

st, rows = call("GET", "/api/appointments", token=tok)
active = [r for r in rows if r.get("status") == "booked" and r.get("speciality") == "Cataract"]
if active:
    print("already has booked Cataract:", active)
else:
    today = date.today()
    for i in range(30):
        d = (today + timedelta(days=i + 1)).isoformat()
        if (today + timedelta(days=i + 1)).weekday() == 6:
            continue
        st, s = call("GET", f"/api/slots?doctor_id=D1&date={d}&visit_type=new", token=tok)
        slots = s.get("slots") or []
        if slots:
            st, b = call("POST", "/api/appointments", {
                "doctor_id": "D1", "speciality": "Cataract", "visit_type": "new",
                "date": d, "start_time": slots[0],
                "reason": "Cataract review",
            }, token=tok)
            print("book:", st, b)
            break
