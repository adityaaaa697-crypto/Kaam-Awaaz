"""Kaam Awaaz - tiny backend (Python standard library only).

Run:  python3 server.py      then open http://localhost:3456
Data is stored in data.json next to this file, so every phone/tab sees the same state.

Money model (all SIMULATED - no real payment gateway):
  * Employer pays wage + EMP_FEE_PCT when a worker accepts; the money is held in escrow.
  * Worker receives wage - WORKER_FEE_PCT only after BOTH sides confirm the job.
  * Platform earns the two fees. Phone numbers are never shared; contact is in-app chat only.
"""
import json, math, os, re, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data.json")
PORT = int(os.environ.get("PORT", 3456))

# ---- business settings: change here, the whole app follows ----
EMP_FEE_PCT = 0.05     # added on top of the wage, paid by the employer
EMP_FEE_MIN = 10       # minimum employer fee in rupees
WORKER_FEE_PCT = 0.03  # deducted from the wage, paid by the worker
LATE_COMP_PCT = 0.25   # share of wage the worker gets if the employer cancels on the job day
START_WALLET = 2000    # simulated starting balance for each employer
MAX_NOSHOWS = 3        # workers with this many no-shows/withdrawals stop receiving alerts
LOCK = threading.Lock()
# Simulated payment methods. The server never stores card numbers or CVV: only brand + last 4 digits.
METHODS = {"gpay", "paytm", "phonepe", "bhim", "upi", "card"}
DEMO_OTP = "1234"  # simulated SMS OTP
PHONE_RE = re.compile(r"^[6-9]\d{9}$")
UPI_RE = re.compile(r"^[\w.\-]{2,}@[a-zA-Z]{2,}$")


def rnd(x):
    return int(x + 0.5)  # half-up rounding so the browser and server always agree


def emp_fee(wage):
    return max(EMP_FEE_MIN, rnd(wage * EMP_FEE_PCT))


def worker_fee(wage):
    return rnd(wage * WORKER_FEE_PCT)


CHOWKS = [
    {"id": "saket", "en": "Saket", "hi": "साकेत", "lat": 28.5245, "lng": 77.2066},
    {"id": "dwarka", "en": "Dwarka", "hi": "द्वारका", "lat": 28.5921, "lng": 77.0460},
    {"id": "rohini", "en": "Rohini", "hi": "रोहिणी", "lat": 28.7383, "lng": 77.0822},
    {"id": "lajpat", "en": "Lajpat Nagar", "hi": "लाजपत नगर", "lat": 28.5677, "lng": 77.2433},
    {"id": "karolbagh", "en": "Karol Bagh", "hi": "करोल बाग", "lat": 28.6519, "lng": 77.1909},
    {"id": "janakpuri", "en": "Janakpuri", "hi": "जनकपुरी", "lat": 28.6219, "lng": 77.0878},
    {"id": "laxmi", "en": "Laxmi Nagar", "hi": "लक्ष्मी नगर", "lat": 28.6304, "lng": 77.2773},
    {"id": "okhla", "en": "Okhla", "hi": "ओखला", "lat": 28.5355, "lng": 77.2730},
]
CHOWK = {c["id"]: c for c in CHOWKS}
TRADES = {"painter", "electrician", "plumber", "mason", "carpenter", "helper"}

# name_en, name_hi, trade, chowk, confirmed jobs, rating avg  (seeded demo workers)
SEED = [
    ("Ramesh", "रमेश", "painter", "saket", 14, 4.6), ("Sunil", "सुनील", "painter", "saket", 3, 4.0),
    ("Mohan", "मोहन", "painter", "lajpat", 9, 4.2), ("Dinesh", "दिनेश", "painter", "okhla", 0, 0),
    ("Vikram", "विक्रम", "electrician", "saket", 11, 4.8), ("Arjun", "अर्जुन", "electrician", "dwarka", 6, 4.1),
    ("Pappu", "पप्पू", "electrician", "janakpuri", 2, 3.5), ("Rakesh", "राकेश", "plumber", "lajpat", 8, 4.4),
    ("Imran", "इमरान", "plumber", "okhla", 12, 4.7), ("Shyam", "श्याम", "plumber", "saket", 0, 0),
    ("Bablu", "बबलू", "mason", "laxmi", 20, 4.5), ("Gopal", "गोपाल", "mason", "rohini", 7, 4.0),
    ("Suresh", "सुरेश", "mason", "dwarka", 4, 3.8), ("Kishan", "किशन", "helper", "laxmi", 15, 4.3),
    ("Lallan", "ललन", "helper", "karolbagh", 5, 3.9), ("Raju", "राजू", "helper", "saket", 1, 4.0),
    ("Mahesh", "महेश", "carpenter", "karolbagh", 10, 4.6), ("Salim", "सलीम", "carpenter", "okhla", 3, 4.2),
]


def seed():
    workers = []
    for i, (en, hi, trade, chowk, n, r) in enumerate(SEED, 1):
        workers.append({
            "id": f"w{i}", "name": en, "nameHi": hi, "trade": trade, "chowk": chowk,
            "upi": f"{en.lower()}@upi", "phone": f"900000{i:04d}", "confirmed": n, "ratingSum": round(r * n, 2), "ratingCount": n,
            "feesPaid": 0, "earned": 0, "noShows": 0, "seeded": True,
        })
    return {"workers": workers, "requests": [], "employers": {}, "txns": [], "nextW": len(workers) + 1, "nextR": 1}


def load():
    if not os.path.exists(DB):
        save(seed())
    with open(DB, encoding="utf-8") as f:
        d = json.load(f)
    for i, w in enumerate(d["workers"], 1):  # older data files had no phone numbers
        w.setdefault("phone", f"900000{i:04d}")
    return d


def save(d):
    tmp = DB + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, DB)


def rating(w):
    return (w["ratingSum"] + 3 * 3) / (w["ratingCount"] + 3)  # new workers start near 3, real ratings pull it


def score(w):
    """Reputation score: Bayesian rating * 10 + confirmed jobs (capped) - 8 per no-show. Higher = alerted first."""
    return rating(w) * 10 + min(w["confirmed"], 30) - 8 * w.get("noShows", 0)


def km(a, b):
    p = math.pi / 180
    x = math.sin((b["lat"] - a["lat"]) * p / 2) ** 2 + math.cos(a["lat"] * p) * math.cos(b["lat"] * p) * math.sin((b["lng"] - a["lng"]) * p / 2) ** 2
    return 12742 * math.asin(math.sqrt(x))


def wave_size(req):
    return max(req["count"] * 2, 3)


def get_emp(d, eid):
    return d["employers"].setdefault(eid, {"balance": START_WALLET})


def build_request(d, body):
    trade, chowk = body.get("trade"), body.get("chowk")
    if trade not in TRADES or chowk not in CHOWK:
        raise ValueError("trade and chowk required")
    eid = re.sub(r"[^\w-]", "", str(body.get("employerId") or ""))[:24]
    if not eid:
        raise ValueError("employerId required")
    count = max(1, min(int(body.get("count") or 1), 20))
    pay = max(100, min(int(body.get("pay") or 600), 5000))
    emp = get_emp(d, eid)
    need = count * (pay + emp_fee(pay))
    if emp["balance"] < need:
        raise ValueError(f"Wallet balance too low: need Rs {need}, have Rs {emp['balance']}")
    req = {
        "id": f"r{d['nextR']}", "employerId": eid, "trade": trade, "chowk": chowk, "count": count, "pay": pay,
        "date": body.get("date") or time.strftime("%Y-%m-%d"), "created": int(time.time()),
        "heard": (body.get("heard") or "")[:200], "alerts": [],
    }
    d["nextR"] += 1
    ranked = []
    for w in d["workers"]:
        if w["trade"] != trade or w.get("noShows", 0) >= MAX_NOSHOWS:
            continue
        dist = km(CHOWK[w["chowk"]], CHOWK[chowk])
        ranked.append((score(w) - dist * 3, w, dist))  # reputation first, distance as a penalty
    ranked.sort(key=lambda x: -x[0])
    size = wave_size(req)
    for i, (_, w, dist) in enumerate(ranked):
        req["alerts"].append({
            "workerId": w["id"], "rank": i + 1, "distKm": round(dist, 1), "wave": i // size + 1,
            "status": "sent" if i < size else "queued", "employerConfirmed": False, "workerConfirmed": False,
            "rating": None, "escrow": None, "payment": None, "payout": 0, "compensation": 0, "messages": [],
        })
    d["requests"].append(req)
    return req


def find(d, rid):
    return next((r for r in d["requests"] if r["id"] == rid), None)


def alert_of(req, wid):
    return next((a for a in req["alerts"] if a["workerId"] == wid), None)


def worker_of(d, wid):
    return next(w for w in d["workers"] if w["id"] == wid)


OCCUPIED = ("accepted", "done", "disputed")


def held_total(a):
    return a["escrow"]["wage"] + a["escrow"]["empFee"]


def release(a, w):
    """Both sides confirmed (or a dispute was resolved in the worker's favour): pay the worker."""
    e = a["escrow"]
    a["status"], a["payment"], a["payout"] = "done", "released", e["wage"] - e["workerFee"]
    w["confirmed"] += 1
    w["earned"] += a["payout"]
    w["feesPaid"] += e["workerFee"]


def refund(d, req, a, status):
    get_emp(d, req["employerId"])["balance"] += held_total(a)
    a["status"], a["payment"] = status, "refunded"


def mask(text):
    return re.sub(r"\+?\d[\d\s\-]{6,}\d", "[number hidden]", text)  # contact stays inside the app


def act(d, req, action, body):
    wid = body.get("workerId")
    a = alert_of(req, wid) if wid else None
    taken = sum(1 for x in req["alerts"] if x["status"] in OCCUPIED)
    if action == "nextwave":
        for x in [x for x in req["alerts"] if x["status"] == "queued"][:wave_size(req)]:
            x["status"] = "sent"
        return
    if action == "resolve":  # demo "support desk" for disputes
        a = a or next((x for x in req["alerts"] if x["status"] == "disputed"), None)
        if not a or a["status"] != "disputed":
            raise ValueError("nothing to resolve")
        if body.get("outcome") == "pay":
            release(a, worker_of(d, a["workerId"]))
        else:
            refund(d, req, a, "cancelled")
        return
    if a is None:
        raise ValueError("worker not alerted for this request")
    w = worker_of(d, wid)
    if action == "accept":
        if a["status"] != "sent":
            raise ValueError("alert is not open")
        if taken >= req["count"]:
            raise ValueError("Job already filled")
        wage = req["pay"]
        ef, wf = emp_fee(wage), worker_fee(wage)
        emp = get_emp(d, req["employerId"])
        if emp["balance"] < wage + ef:
            raise ValueError("Employer wallet has too little balance for this job")
        emp["balance"] -= wage + ef  # charged from the employer the moment the worker accepts
        a.update(status="accepted", payment="held", escrow={"wage": wage, "empFee": ef, "workerFee": wf})
    elif action == "decline":
        if a["status"] == "sent":
            a["status"] = "declined"
    elif action in ("arrived", "done"):
        if a["status"] != "accepted":
            raise ValueError("job not in progress")
        a["employerConfirmed" if action == "arrived" else "workerConfirmed"] = True
        if a["employerConfirmed"] and a["workerConfirmed"]:
            release(a, w)
    elif action == "cancel":  # employer cancels
        if a["status"] != "accepted":
            raise ValueError("job not in progress")
        if req["date"] <= time.strftime("%Y-%m-%d"):  # late cancel (job is today): worker is compensated
            comp = rnd(a["escrow"]["wage"] * LATE_COMP_PCT)
            get_emp(d, req["employerId"])["balance"] += held_total(a) - comp
            w["earned"] += comp
            a["compensation"], a["status"], a["payment"] = comp, "cancelled", "compensated"
        else:
            refund(d, req, a, "cancelled")
    elif action in ("noshow", "withdraw"):  # employer reports no-show / worker pulls out: full refund, reputation hit
        if a["status"] != "accepted":
            raise ValueError("job not in progress")
        refund(d, req, a, "noshow" if action == "noshow" else "withdrawn")
        w["noShows"] = w.get("noShows", 0) + 1
        if action == "noshow":
            w["ratingSum"] += 1
            w["ratingCount"] += 1
    elif action == "dispute":
        if a["status"] != "accepted":
            raise ValueError("job not in progress")
        a["status"] = "disputed"  # money stays held until support resolves it
    elif action == "message":
        if a["status"] not in ("accepted", "done", "disputed"):
            raise ValueError("chat opens after a worker accepts")
        who = body.get("from")
        text = mask(str(body.get("text") or "").strip())[:200]
        if who not in ("employer", "worker") or not text:
            raise ValueError("empty message")
        a["messages"].append({"from": who, "text": text, "voice": bool(body.get("voice")), "t": int(time.time())})
        del a["messages"][:-100]
    elif action == "rate":
        stars = int(body.get("stars") or 0)
        if a["status"] != "done" or a["rating"] is not None or not 1 <= stars <= 5:
            raise ValueError("cannot rate")
        a["rating"] = stars
        w["ratingSum"] += stars
        w["ratingCount"] += 1
    else:
        raise ValueError("unknown action")


def public_state(d):
    workers = [dict({k: v for k, v in w.items() if k not in ("upi", "phone")}, rating=round(rating(w), 1), score=round(score(w), 1),
                    upiMask=(w["upi"][:2] + "\u2022\u2022" + w["upi"][w["upi"].index("@"):]) if "@" in w.get("upi", "") else "") for w in d["workers"]]
    return {"chowks": CHOWKS, "workers": workers, "requests": d["requests"], "employers": d["employers"], "txns": d.get("txns", [])[-60:],
            "config": {"empPct": EMP_FEE_PCT, "empMin": EMP_FEE_MIN, "workerPct": WORKER_FEE_PCT, "latePct": LATE_COMP_PCT}}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj, ctype="application/json", raw=None):
        data = raw if raw is not None else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/api/state"):
            with LOCK:
                return self.send(200, public_state(load()))
        if self.path in ("/", "/index.html"):
            page = os.path.join(HERE, "index.html")  # flat layout for hosting; falls back to public/ for local dev
            if not os.path.exists(page):
                page = os.path.join(HERE, "public", "index.html")
            with open(page, "rb") as f:
                return self.send(200, None, "text/html", f.read())
        self.send(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self.send(400, {"error": "bad json"})
        try:
            with LOCK:
                d = load()
                p = self.path
                if p == "/api/reset":
                    d = seed()
                    out = {"ok": True}
                elif p == "/api/wallet":  # simulated UPI top-up
                    eid = re.sub(r"[^\w-]", "", str(body.get("employerId") or ""))[:24]
                    amt = int(body.get("amount") or 0)
                    if not eid or not 100 <= amt <= 10000:
                        raise ValueError("amount must be 100-10000")
                    method = body.get("method") or "upi"
                    detail = str(body.get("detail") or "").strip()[:40]
                    if method not in METHODS:
                        raise ValueError("unknown payment method")
                    if method == "card":  # refuse anything that looks like a full card number
                        if len(re.sub(r"\D", "", detail)) > 4:
                            raise ValueError("send only the last 4 digits of a card")
                    elif detail:
                        if not UPI_RE.match(detail):
                            raise ValueError("invalid UPI ID")
                        detail = detail[:2] + "\u2022\u2022" + detail[detail.index("@"):]
                    emp = get_emp(d, eid)
                    emp["balance"] += amt
                    txn = {"id": f"t{len(d.setdefault('txns', [])) + 1}", "employerId": eid, "kind": "topup", "amount": amt,
                           "method": method, "detail": detail, "t": int(time.time()), "status": "success"}
                    d["txns"].append(txn)
                    out = {"balance": emp["balance"], "txn": txn["id"]}
                elif p == "/api/payout":  # worker sets the UPI ID that payouts go to (simulated)
                    upi = str(body.get("upi") or "").strip()[:40]
                    if not UPI_RE.match(upi):
                        raise ValueError("invalid UPI ID")
                    worker_of(d, body.get("workerId"))["upi"] = upi
                    out = {"ok": True}
                elif p == "/api/login":  # simulated phone + OTP login for workers
                    phone = re.sub(r"\D", "", str(body.get("phone") or ""))
                    if not PHONE_RE.match(phone):
                        raise ValueError("enter a valid 10-digit mobile number")
                    if str(body.get("otp")) != DEMO_OTP:
                        raise ValueError("wrong OTP")
                    w = next((x for x in d["workers"] if x["phone"] == phone), None)
                    out = {"workerId": w["id"]} if w else {"new": True}
                elif p == "/api/workers":
                    trade, chowk = body.get("trade"), body.get("chowk")
                    name = (body.get("name") or "").strip()[:30]
                    phone = re.sub(r"\D", "", str(body.get("phone") or ""))
                    if trade not in TRADES or chowk not in CHOWK or not name:
                        raise ValueError("name, trade and chowk required")
                    if not PHONE_RE.match(phone) or any(x["phone"] == phone for x in d["workers"]):
                        raise ValueError("invalid or already registered phone number")
                    w = {"id": f"w{d['nextW']}", "name": name, "nameHi": name, "trade": trade, "chowk": chowk,
                         "upi": (body.get("upi") or "")[:40], "phone": phone, "confirmed": 0, "ratingSum": 0, "ratingCount": 0,
                         "feesPaid": 0, "earned": 0, "noShows": 0, "seeded": False}
                    d["nextW"] += 1
                    d["workers"].append(w)
                    out = {"workerId": w["id"]}
                elif p == "/api/requests":
                    out = {"requestId": build_request(d, body)["id"]}
                else:
                    m = re.fullmatch(r"/api/requests/(r\d+)/(\w+)", p)
                    req = find(d, m.group(1)) if m else None
                    if not req:
                        return self.send(404, {"error": "not found"})
                    act(d, req, m.group(2), body)
                    out = {"ok": True}
                save(d)
            self.send(200, out)
        except (ValueError, KeyError, StopIteration) as e:
            self.send(409, {"error": str(e)})


if __name__ == "__main__":
    load()
    print(f"Kaam Awaaz running on http://localhost:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
