"""Hotel Manager - QR table menu, ordering, kitchen board, M-Pesa, admin.
Run:  python app.py      (or start.bat / start.sh)
Guests scan a table QR -> /t/<code>.  Kitchen: /kitchen   Admin: /admin   (PINs are set in .env / Settings)
"""
import os, io, re, csv, json, math, time, base64, socket, sqlite3, secrets, hmac, zipfile, datetime, threading
from functools import wraps
from urllib.parse import quote
from flask import (Flask, request, session, redirect, jsonify, render_template, send_file, send_from_directory,
                   g, flash, url_for, abort, Response)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash
import requests
import importer, tunnel

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_env():
    p = os.path.join(HERE, ".env")
    if os.path.exists(p):
        for ln in open(p, encoding="utf-8-sig"):
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()
PORT = int(os.getenv("PORT", "5000"))
DB = os.path.join(HERE, "hotel.db")
UPLOADS = os.path.join(HERE, "uploads")
os.makedirs(UPLOADS, exist_ok=True)


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try: s.connect(("10.255.255.255", 1)); return s.getsockname()[0]
    except Exception: return "127.0.0.1"
    finally: s.close()


def _secret():
    k = os.getenv("SECRET_KEY", "")
    if k and k != "change-me": return k
    p = os.path.join(HERE, ".secret_key")          # auto-generated once, so logins survive restarts
    if not os.path.exists(p): open(p, "w").write(secrets.token_hex(32))
    return open(p).read().strip()


app = Flask(__name__)
app.secret_key = _secret()
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(MAX_CONTENT_LENGTH=25 * 1024 * 1024, MAX_FORM_MEMORY_SIZE=8 * 1024 * 1024, MAX_FORM_PARTS=5000,
                  SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True,
                  PERMANENT_SESSION_LIFETIME=datetime.timedelta(hours=12))
STATUS = ["new", "preparing", "ready", "served"]
ENV_DEFAULTS = dict(
    hotel=os.getenv("HOTEL_NAME", "Grand Hotel"), currency="KES", service_pct="0", brand="#0a7d3b",
    till=os.getenv("MPESA_TILL", ""), ngrok_token=os.getenv("NGROK_AUTHTOKEN", ""), ngrok_domain=os.getenv("NGROK_DOMAIN", ""),
    public_url=os.getenv("BASE_URL", ""), tagline="Scan, order, relax.",
)
MP = dict(KEY=os.getenv("MPESA_KEY", ""), SECRET=os.getenv("MPESA_SECRET", ""), SHORTCODE=os.getenv("MPESA_SHORTCODE", "174379"),
          PASSKEY=os.getenv("MPESA_PASSKEY", ""), CALLBACK=os.getenv("MPESA_CALLBACK", ""), SANDBOX=os.getenv("MPESA_SANDBOX", "1") == "1")


# ------------------------------------------------------------------ database
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB, timeout=15); g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close(_):
    d = g.pop("db", None)
    if d: d.close()


def init():
    c = sqlite3.connect(DB)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS menu(id INTEGER PRIMARY KEY, name, category, price REAL, descr, available INT DEFAULT 1);
    CREATE TABLE IF NOT EXISTS tables(id INTEGER PRIMARY KEY, code TEXT UNIQUE, room TEXT, floor TEXT, side TEXT, number TEXT,
      active INT DEFAULT 1, created TEXT);
    CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, tbl INT, items, total REAL, status DEFAULT 'new',
      paid INT DEFAULT 0, phone, note, created, mpesa_ref, checkout_id);
    CREATE TABLE IF NOT EXISTS calls(id INTEGER PRIMARY KEY, tbl INT, kind, done INT DEFAULT 0, created);""")

    def addcol(tbl, col, decl):
        if col not in [r[1] for r in c.execute(f"PRAGMA table_info({tbl})")]:
            c.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {decl}")
    for col, decl in [("table_id", "INT"), ("label", "TEXT"), ("token", "TEXT"), ("subtotal", "REAL"), ("charge", "REAL DEFAULT 0"),
                      ("guest", "TEXT"), ("pay_method", "TEXT"), ("pay_note", "TEXT"), ("updated", "TEXT")]:
        addcol("orders", col, decl)
    addcol("calls", "table_id", "INT"); addcol("calls", "label", "TEXT"); addcol("menu", "image", "TEXT")
    for (oid,) in c.execute("SELECT id FROM orders WHERE token IS NULL OR token=''").fetchall():   # upgrade older databases
        c.execute("UPDATE orders SET token=?, subtotal=COALESCE(subtotal,total) WHERE id=?", (secrets.token_urlsafe(8), oid))
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_orders_token ON orders(token)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_orders_created ON orders(created)")
    c.commit(); c.close()


def S(key):
    r = db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r is not None else ENV_DEFAULTS.get(key, "")


def set_S(key, value):
    db().execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    db().commit()


def public_url():
    t = tunnel.status()
    return t["url"] or S("public_url").strip().rstrip("/") or f"http://{lan_ip()}:{PORT}"


def link_mode():
    t = tunnel.status()
    if t["url"]: return "ngrok"
    return "custom" if S("public_url").strip() else "lan"


def mpesa_callback():
    if MP["CALLBACK"]: return MP["CALLBACK"]
    u = public_url()
    return u + "/mpesa/callback" if u.startswith("https://") else ""


def demo_payments():
    return not (MP["KEY"] and mpesa_callback())


# ------------------------------------------------------------------ small helpers
def money(v):
    v = float(v or 0)
    return f"{v:,.0f}" if abs(v - round(v)) < 0.005 else f"{v:,.2f}"


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def label_of(t):
    parts = [p for p in (t["room"], t["floor"], t["side"]) if p and str(p).strip()]
    return " · ".join(parts + [f"Table {t['number']}"])


def table_by_code(code):
    return db().execute("SELECT * FROM tables WHERE code=?", (str(code or "").strip().lower(),)).fetchone() if code else None


def new_code():
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    while True:
        c = "".join(secrets.choice(alphabet) for _ in range(6))
        if not c.isdigit() and not db().execute("SELECT 1 FROM tables WHERE code=?", (c,)).fetchone(): return c


def ordered_menu(only_available=False):
    q = ("SELECT m.* FROM menu m JOIN (SELECT category, MIN(id) mid FROM menu GROUP BY category) c ON c.category=m.category "
         + ("WHERE m.available=1 " if only_available else "") + "ORDER BY c.mid, m.id")
    return db().execute(q).fetchall()


def client_ip():
    return request.remote_addr or "?"


_hits, _hl = {}, threading.Lock()


def limited(key, n, per):
    t = time.time()
    with _hl:
        lst = [x for x in _hits.get(key, []) if t - x < per]
        if len(lst) >= n: _hits[key] = lst; return True
        lst.append(t); _hits[key] = lst
        if len(_hits) > 5000: _hits.clear()
    return False


def safe_next(n, default):
    return n if n and n.startswith("/") and not n.startswith("//") else default


def need(role):
    r = session.get("role")
    return r == "admin" or r == role


def staff(role="kitchen", api=False):
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            if not need(role):
                if api: return jsonify(error="login required"), 403
                return redirect(url_for("login", next=request.full_path.rstrip("?")))
            return f(*a, **k)
        return w
    return deco


@app.context_processor
def inject():
    return dict(HOTEL=S("hotel"), CUR=S("currency"), BRAND=S("brand"), TAGLINE=S("tagline"), role=session.get("role"), money=money)


@app.after_request
def headers(r):
    r.headers.setdefault("X-Content-Type-Options", "nosniff")
    r.headers.setdefault("Referrer-Policy", "same-origin")
    if request.path.startswith(("/admin", "/kitchen", "/api/kitchen", "/login")): r.headers["Cache-Control"] = "no-store"
    return r


def qr_bytes(url, box=12):
    import qrcode
    q = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=box, border=2)
    q.add_data(url); q.make(fit=True)
    buf = io.BytesIO(); q.make_image(fill_color="black", back_color="white").save(buf, "PNG"); return buf.getvalue()


def table_url(t):
    return f"{public_url()}/t/{t['code']}"


# ================================================================== customer
@app.route("/")
def home():
    return render_template("home.html", tables=db().execute("SELECT 1 FROM tables LIMIT 1").fetchone())


@app.route("/t/<code>")
def table_menu(code):
    t = table_by_code(code)
    if not t and code.isdigit():                             # old printed QR codes (/t/5): accept if the number is unique
        rows = db().execute("SELECT * FROM tables WHERE number=? AND active=1", (code,)).fetchall()
        if len(rows) == 1: return redirect(url_for("table_menu", code=rows[0]["code"]))
    if not t or not t["active"]: return render_template("notfound.html"), 404
    items = ordered_menu()
    cats = list(dict.fromkeys(i["category"] for i in items))
    data = {str(i["id"]): dict(n=i["name"], p=i["price"], a=bool(i["available"])) for i in items}
    return render_template("menu.html", t=t, label=label_of(t), items=items, cats=cats, data=data, svc=float(S("service_pct") or 0))


@app.post("/api/order")
def api_order():
    if limited(("order", client_ip()), 8, 60): return jsonify(error="Too many orders in a short time. Please wait a minute or call a waiter."), 429
    d = request.get_json(silent=True) or {}
    t = table_by_code(d.get("code"))
    if not t or not t["active"]: return jsonify(error="This table QR is no longer active. Please ask our staff."), 400
    rows = {r["id"]: r for r in db().execute("SELECT * FROM menu")}
    merged = {}
    for it in (d.get("items") or [])[:80]:
        try: i, q = int(it["id"]), int(it["qty"])
        except (KeyError, TypeError, ValueError): continue
        if q > 0: merged[i] = min(50, merged.get(i, 0) + q)
    items = []
    for i, q in merged.items():
        r = rows.get(i)
        if not r: continue
        if not r["available"]: return jsonify(error=f"Sorry, “{r['name']}” just sold out. Please remove it."), 409
        items.append(dict(id=i, name=r["name"], price=r["price"], qty=q))
    if not items: return jsonify(error="Your order is empty."), 400
    sub = sum(i["price"] * i["qty"] for i in items)
    pct = float(S("service_pct") or 0)
    charge = round(sub * pct / 100)
    tok = secrets.token_urlsafe(8)
    cur = db().execute("INSERT INTO orders(tbl,table_id,label,items,subtotal,charge,total,note,guest,created,token) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (t["number"], t["id"], label_of(t), json.dumps(items), sub, charge, sub + charge,
                        str(d.get("note") or "")[:300], str(d.get("guest") or "")[:60], now(), tok))
    db().commit()
    return jsonify(id=cur.lastrowid, token=tok)


def order_by_token(tok):
    return db().execute("SELECT * FROM orders WHERE token=?", (tok,)).fetchone()


@app.route("/o/<token>")
def track(token):
    o = order_by_token(token)
    if not o: return render_template("notfound.html"), 404
    t = db().execute("SELECT code FROM tables WHERE id=?", (o["table_id"],)).fetchone() if o["table_id"] else None
    return render_template("order.html", o=o, items=json.loads(o["items"]), tcode=t["code"] if t else None,
                           label=o["label"] or f"Table {o['tbl']}", till=S("till"), demo=demo_payments(), steps=STATUS)


@app.get("/api/order/<token>")
def api_get(token):
    o = order_by_token(token)
    if not o: return jsonify(error="not found"), 404
    return jsonify(status=o["status"], paid=bool(o["paid"]), note=o["pay_note"] or "", method=o["pay_method"] or "")


@app.post("/api/call")
def api_call():
    if limited(("call", client_ip()), 6, 60): return jsonify(error="Please wait a moment - staff were already notified."), 429
    d = request.get_json(silent=True) or {}
    t = table_by_code(d.get("code"))
    kind = d.get("kind") if d.get("kind") in ("Waiter", "Bill", "Water", "Cleaning") else "Waiter"
    if not t or not t["active"]: return jsonify(error="invalid table"), 400
    db().execute("INSERT INTO calls(tbl,table_id,label,kind,created) VALUES(?,?,?,?,?)", (t["number"], t["id"], label_of(t), kind, now()))
    db().commit(); return jsonify(ok=1)


# ================================================================== payments
def stk_push(phone, amount, ref):
    if demo_payments(): return {"demo": True}
    base = "https://sandbox.safaricom.co.ke" if MP["SANDBOX"] else "https://api.safaricom.co.ke"
    tok = requests.get(base + "/oauth/v1/generate?grant_type=client_credentials", auth=(MP["KEY"], MP["SECRET"]), timeout=20).json()["access_token"]
    ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    pw = base64.b64encode((MP["SHORTCODE"] + MP["PASSKEY"] + ts).encode()).decode()
    body = dict(BusinessShortCode=MP["SHORTCODE"], Password=pw, Timestamp=ts, TransactionType="CustomerPayBillOnline",
                Amount=int(math.ceil(amount)), PartyA=phone, PartyB=MP["SHORTCODE"], PhoneNumber=phone,
                CallBackURL=mpesa_callback(), AccountReference="Order" + ref, TransactionDesc=S("hotel")[:20])
    return requests.post(base + "/mpesa/stkpush/v1/processrequest", json=body, headers={"Authorization": "Bearer " + tok}, timeout=30).json()


def norm_phone(p):
    p = "".join(ch for ch in p if ch.isdigit())
    if p.startswith("0"): p = "254" + p[1:]
    if p.startswith("7") or p.startswith("1"): p = "254" + p
    return p


@app.post("/pay/<token>")
def pay(token):
    o = order_by_token(token)
    if not o: return jsonify(msg="Order not found."), 404
    if o["paid"]: return jsonify(msg="This order is already paid. Thank you!")
    if o["status"] == "cancelled": return jsonify(msg="This order was cancelled.")
    if limited(("pay", client_ip()), 6, 120): return jsonify(msg="Too many attempts. Please wait a moment.")
    phone = norm_phone((request.get_json(silent=True) or {}).get("phone", ""))
    if len(phone) != 12 or not phone.startswith("254"): return jsonify(msg="Enter a valid Safaricom number, e.g. 0712345678.")
    try: r = stk_push(phone, o["total"], str(o["id"]))
    except Exception as e: return jsonify(msg=f"Payment service error: {e}")
    if r.get("demo"):
        db().execute("UPDATE orders SET paid=1,phone=?,mpesa_ref='DEMO',pay_method='demo',pay_note='' WHERE id=?", (phone, o["id"])); db().commit()
        return jsonify(msg="DEMO MODE: payment simulated as successful.")
    if r.get("ResponseCode") == "0":
        db().execute("UPDATE orders SET phone=?,checkout_id=?,pay_note='' WHERE id=?", (phone, r["CheckoutRequestID"], o["id"])); db().commit()
        return jsonify(msg="Enter your M-Pesa PIN on the prompt on your phone.")
    return jsonify(msg=r.get("errorMessage") or "Could not start payment.")


@app.post("/mpesa/callback")
def callback():
    try:
        cb = request.get_json(force=True)["Body"]["stkCallback"]
        o = db().execute("SELECT * FROM orders WHERE checkout_id=?", (cb["CheckoutRequestID"],)).fetchone()
        if o:
            if cb["ResultCode"] == 0:
                meta = {i["Name"]: i.get("Value") for i in cb["CallbackMetadata"]["Item"]}
                if float(meta.get("Amount") or 0) + 0.5 >= o["total"]:
                    db().execute("UPDATE orders SET paid=1,mpesa_ref=?,pay_method='mpesa',pay_note='' WHERE id=?", (meta.get("MpesaReceiptNumber"), o["id"]))
                else:
                    db().execute("UPDATE orders SET pay_note=? WHERE id=?", (f"Partial payment received ({meta.get('Amount')}). Please pay the balance or ask staff.", o["id"]))
            else:
                db().execute("UPDATE orders SET pay_note=? WHERE id=?", (str(cb.get("ResultDesc", "Payment was not completed"))[:120], o["id"]))
            db().commit()
    except Exception:
        pass
    return jsonify(ResultCode=0, ResultDesc="OK")


# ================================================================== login / kitchen
def pin_ok(role, pin):
    stored = S(role + "_pin_hash")
    if stored: return check_password_hash(stored, pin)
    return hmac.compare_digest(pin.encode(), os.getenv(role.upper() + "_PIN", "1111" if role == "kitchen" else "2222").encode())


@app.route("/login", methods=["GET", "POST"])
def login():
    err = None
    nxt = safe_next(request.args.get("next"), "/kitchen")
    if request.method == "POST":
        ip = client_ip()
        fails = [x for x in _hits.get(("lf", ip), []) if time.time() - x < 600]
        if len(fails) >= 6: err = "Too many wrong PINs. Try again in 10 minutes."
        else:
            pin = request.form.get("pin", "")
            role = "admin" if pin_ok("admin", pin) else "kitchen" if pin_ok("kitchen", pin) else None
            if role:
                session.clear(); session["role"] = role; session.permanent = True
                if role == "kitchen" and nxt.startswith("/admin"): nxt = "/kitchen"
                return redirect(nxt)
            _hits[("lf", ip)] = fails + [time.time()]; err = "Wrong PIN."
    return render_template("login.html", err=err)


@app.route("/logout")
def logout():
    session.clear(); return redirect("/")


def order_view(o):
    d = dict(o); d["items"] = json.loads(o["items"]); d["label"] = o["label"] or f"Table {o['tbl']}"
    head, _, tail = d["label"].rpartition(" · "); d["num"] = tail or d["label"]; d["where"] = head
    try: d["mins"] = max(0, int((datetime.datetime.now() - datetime.datetime.strptime(o["created"][:19], "%Y-%m-%d %H:%M:%S" if len(o["created"]) > 16 else "%Y-%m-%d %H:%M")).total_seconds() // 60))
    except ValueError: d["mins"] = 0
    return d


@app.route("/kitchen")
@staff("kitchen")
def kitchen():
    return render_template("kitchen.html")


@app.get("/api/kitchen")
@staff("kitchen", api=True)
def api_kitchen():
    orders = [order_view(o) for o in db().execute("SELECT * FROM orders WHERE status NOT IN ('served','cancelled') ORDER BY id")]
    calls = [dict(c) for c in db().execute("SELECT * FROM calls WHERE done=0 ORDER BY id")]
    for c in calls: c["label"] = c["label"] or f"Table {c['tbl']}"
    return jsonify(orders=orders, calls=calls)


def _set(oid, sql, args=()):
    db().execute(sql.replace("{u}", "updated=?"), tuple(args) + (now(), oid)); db().commit()


@app.post("/api/advance/<int:oid>")
@staff("kitchen", api=True)
def advance(oid):
    o = db().execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()
    if not o or o["status"] not in STATUS: return jsonify(ok=0), 404
    _set(oid, "UPDATE orders SET status=?,{u} WHERE id=?", (STATUS[min(STATUS.index(o["status"]) + 1, 3)],)); return jsonify(ok=1)


@app.post("/api/cancel/<int:oid>")
@staff("kitchen", api=True)
def api_cancel(oid):
    _set(oid, "UPDATE orders SET status='cancelled',{u} WHERE id=?"); return jsonify(ok=1)


@app.post("/api/markpaid/<int:oid>")
@staff("kitchen", api=True)
def api_markpaid(oid):
    _set(oid, "UPDATE orders SET paid=1,pay_method=COALESCE(NULLIF(pay_method,''),'cash'),{u} WHERE id=?"); return jsonify(ok=1)


@app.post("/api/call_done/<int:cid>")
@staff("kitchen", api=True)
def api_call_done(cid):
    db().execute("UPDATE calls SET done=1 WHERE id=?", (cid,)); db().commit(); return jsonify(ok=1)


# ================================================================== admin: dashboard & orders
def day_stats(day):
    os_ = db().execute("SELECT * FROM orders WHERE created LIKE ? AND status!='cancelled'", (day + "%",)).fetchall()
    cnt = {}
    for o in os_:
        for i in json.loads(o["items"]): cnt[i["name"]] = cnt.get(i["name"], 0) + i["qty"]
    return dict(n=len(os_), paid=sum(o["total"] for o in os_ if o["paid"]), unpaid=sum(o["total"] for o in os_ if not o["paid"]),
                top=sorted(cnt.items(), key=lambda x: -x[1])[:6])


@app.route("/admin")
@staff("admin")
def admin():
    today = datetime.date.today()
    s = day_stats(today.isoformat())
    week = []
    for k in range(6, -1, -1):
        d = today - datetime.timedelta(days=k)
        r = db().execute("SELECT COALESCE(SUM(total),0) v, COUNT(*) n FROM orders WHERE created LIKE ? AND status!='cancelled'", (d.isoformat() + "%",)).fetchone()
        week.append(dict(day=d.strftime("%a"), v=r["v"], n=r["n"]))
    mx = max([w["v"] for w in week] + [1])
    active = db().execute("SELECT COUNT(*) FROM orders WHERE status NOT IN ('served','cancelled')").fetchone()[0]
    return render_template("admin_dashboard.html", s=s, week=week, mx=mx, active=active, nm=db().execute("SELECT COUNT(*) FROM menu").fetchone()[0],
                           nt=db().execute("SELECT COUNT(*) FROM tables WHERE active=1").fetchone()[0],
                           calls=db().execute("SELECT * FROM calls WHERE done=0 ORDER BY id").fetchall(), nav="dash", **conn_info())


def default_pins():
    """True while a staff PIN is still the well-known default (1111 / 2222)."""
    bad = lambda role, dflt: not S(role + "_pin_hash") and os.getenv(role.upper() + "_PIN", dflt) == dflt
    return bad("admin", "2222") or bad("kitchen", "1111")


def conn_info():
    return dict(mode=link_mode(), pub=public_url(), tun=tunnel.status(), demo=demo_payments(), static=bool(tunnel.status()["static"]), defpin=default_pins())


@app.route("/admin/orders")
@staff("admin")
def admin_orders():
    a = request.args
    today = datetime.date.today().isoformat()
    d1, d2 = a.get("from") or today, a.get("to") or a.get("from") or today
    q, args = "SELECT * FROM orders WHERE substr(created,1,10) BETWEEN ? AND ?", [d1, d2]
    if a.get("status"): q += " AND status=?"; args.append(a["status"])
    if a.get("paid") in ("0", "1"): q += " AND paid=?"; args.append(int(a["paid"]))
    if a.get("q"): q += " AND (label LIKE ? OR guest LIKE ? OR CAST(id AS TEXT)=?)"; args += [f"%{a['q']}%", f"%{a['q']}%", a["q"].lstrip("#")]
    rows = [order_view(o) for o in db().execute(q + " ORDER BY id DESC LIMIT 400", args)]
    tot = sum(o["total"] for o in rows if o["status"] != "cancelled")
    return render_template("admin_orders.html", orders=rows, d1=d1, d2=d2, f=a, tot=tot, nav="orders", statuses=STATUS + ["cancelled"])


@app.post("/admin/orders/<int:oid>/<action>")
@staff("admin")
def admin_order_action(oid, action):
    if action == "paid": _set(oid, "UPDATE orders SET paid=1,pay_method=COALESCE(NULLIF(pay_method,''),'cash'),{u} WHERE id=?")
    elif action == "unpaid": _set(oid, "UPDATE orders SET paid=0,pay_method=NULL,{u} WHERE id=?")
    elif action == "cancel": _set(oid, "UPDATE orders SET status='cancelled',{u} WHERE id=?")
    elif action == "status" and request.form.get("status") in STATUS + ["cancelled"]: _set(oid, "UPDATE orders SET status=?,{u} WHERE id=?", (request.form["status"],))
    return redirect(safe_next(request.form.get("back"), "/admin/orders"))


@app.route("/admin/orders/<int:oid>/receipt")
@staff("admin")
def receipt(oid):
    o = db().execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone() or abort(404)
    return render_template("receipt.html", o=order_view(o), now=now())


def _safe_cell(v):
    v = "" if v is None else str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@") else v


@app.route("/admin/orders.csv")
@staff("admin")
def export():
    out = io.StringIO(); w = csv.writer(out)
    w.writerow(["id", "created", "table", "guest", "items", "subtotal", "service_charge", "total", "status", "paid", "payment_method", "phone", "mpesa_ref", "note"])
    for o in db().execute("SELECT * FROM orders ORDER BY id"):
        its = "; ".join(f"{i['qty']}x {i['name']}" for i in json.loads(o["items"]))
        w.writerow([_safe_cell(x) for x in [o["id"], o["created"], o["label"] or o["tbl"], o["guest"], its, o["subtotal"] if o["subtotal"] is not None else o["total"],
                                           o["charge"] or 0, o["total"], o["status"], "yes" if o["paid"] else "no", o["pay_method"], o["phone"], o["mpesa_ref"], o["note"]]])
    return Response("﻿" + out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=orders.csv"})


@app.post("/admin/call/<int:i>")
@staff("admin")
def call_done(i):
    db().execute("UPDATE calls SET done=1 WHERE id=?", (i,)); db().commit(); return redirect("/admin")


# ================================================================== admin: tables & QR codes
def _clean(v, n=40): return re.sub(r"\s+", " ", (v or "").strip())[:n]


@app.route("/admin/tables", methods=["GET", "POST"])
@staff("admin")
def admin_tables():
    if request.method == "POST":
        f = request.form
        room, floor, side, num, to = _clean(f.get("room")), _clean(f.get("floor")), _clean(f.get("side")), _clean(f.get("number"), 12), _clean(f.get("to"), 12)
        if not num: flash("Enter the table number.", "err"); return redirect("/admin/tables")
        nums = [num]
        if to:
            if not (num.isdigit() and to.isdigit()) or int(to) < int(num) or int(to) - int(num) > 199: flash("For a range, use whole numbers (e.g. 1 to 20, max 200 at once).", "err"); return redirect("/admin/tables")
            nums = [str(n) for n in range(int(num), int(to) + 1)]
        ids, dup = [], 0
        for n in nums:
            if db().execute("SELECT 1 FROM tables WHERE lower(room)=lower(?) AND lower(floor)=lower(?) AND lower(side)=lower(?) AND lower(number)=lower(?)", (room, floor, side, n)).fetchone(): dup += 1; continue
            cur = db().execute("INSERT INTO tables(code,room,floor,side,number,created) VALUES(?,?,?,?,?,?)", (new_code(), room, floor, side, n, now())); ids.append(cur.lastrowid)
        db().commit()
        flash(f"Created {len(ids)} table QR code{'s' if len(ids) != 1 else ''}." + (f" {dup} already existed and were skipped." if dup else ""), "ok" if ids else "err")
        return redirect("/admin/tables?new=" + ",".join(map(str, ids)))
    rows = db().execute("SELECT * FROM tables ORDER BY room, floor, side, CAST(number AS INTEGER), number").fetchall()
    new = {int(x) for x in request.args.get("new", "").split(",") if x.isdigit()}
    groups = {}
    for t in rows: groups.setdefault(" · ".join(p for p in (t["room"], t["floor"], t["side"]) if p) or "No room set", []).append(t)
    opts = lambda col: [r[0] for r in db().execute(f"SELECT DISTINCT {col} FROM tables WHERE {col}!='' ORDER BY {col}")]
    return render_template("admin_tables.html", groups=groups, new=new, rooms=opts("room"), floors=opts("floor"), sides=opts("side"),
                           total=len(rows), nav="tables", label=label_of, turl=table_url, **conn_info())


@app.post("/admin/tables/<int:i>/<action>")
@staff("admin")
def table_action(i, action):
    t = db().execute("SELECT * FROM tables WHERE id=?", (i,)).fetchone() or abort(404)
    if action == "toggle": db().execute("UPDATE tables SET active=1-active WHERE id=?", (i,))
    elif action == "delete": db().execute("DELETE FROM tables WHERE id=?", (i,)); flash(f"Deleted {label_of(t)}. Its printed QR code no longer works.", "ok")
    elif action == "regen": db().execute("UPDATE tables SET code=? WHERE id=?", (new_code(), i)); flash(f"New QR created for {label_of(t)}. Reprint it - the old one stopped working.", "ok")
    elif action == "edit":
        f = request.form
        if _clean(f.get("number"), 12): db().execute("UPDATE tables SET room=?,floor=?,side=?,number=? WHERE id=?", (_clean(f.get("room")), _clean(f.get("floor")), _clean(f.get("side")), _clean(f.get("number"), 12), i))
    db().commit(); return redirect("/admin/tables")


@app.post("/admin/tables/bulk")
@staff("admin")
def tables_bulk():
    ids = [int(x) for x in request.form.getlist("id") if x.isdigit()]
    act = request.form.get("act")
    if ids and act in ("delete", "enable", "disable"):
        ph = ",".join("?" * len(ids))
        if act == "delete": db().execute(f"DELETE FROM tables WHERE id IN ({ph})", ids)
        else: db().execute(f"UPDATE tables SET active=? WHERE id IN ({ph})", [1 if act == "enable" else 0] + ids)
        db().commit(); flash(f"Done: {act} {len(ids)} table(s).", "ok")
    return redirect("/admin/tables")


def _selected_tables():
    ids = [int(x) for x in request.values.getlist("id") if x.isdigit()]
    if ids: return db().execute(f"SELECT * FROM tables WHERE id IN ({','.join('?' * len(ids))}) ORDER BY room,floor,side,CAST(number AS INTEGER)", ids).fetchall()
    return db().execute("SELECT * FROM tables WHERE active=1 ORDER BY room,floor,side,CAST(number AS INTEGER)").fetchall()


@app.route("/admin/qr/print")
@staff("admin")
def qr_print():
    cols = request.args.get("cols", "3")
    return render_template("qr_print.html", tables=_selected_tables(), cols=cols if cols in ("1", "2", "3", "4") else "3", label=label_of, turl=table_url,
                           show_url=request.args.get("url", "1") == "1", cut=request.args.get("cut", "1") == "1", msg=request.args.get("msg", ""), sel=request.args.getlist("id"))


@app.route("/admin/qr/zip")
@staff("admin")
def qr_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for t in _selected_tables():
            z.writestr(re.sub(r"[^A-Za-z0-9]+", "_", label_of(t)).strip("_") + ".png", qr_bytes(table_url(t), 14))
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True, download_name="table_qr_codes.zip")


@app.route("/qr/<code>.png")
def qr(code):
    t = table_by_code(code) or abort(404)
    r = send_file(io.BytesIO(qr_bytes(table_url(t), int(request.args.get("box", 12)) if request.args.get("box", "12").isdigit() else 12)), mimetype="image/png")
    if request.args.get("dl"): r.headers["Content-Disposition"] = f'attachment; filename="{re.sub(r"[^A-Za-z0-9]+", "_", label_of(t)).strip("_")}.png"'
    r.headers["Cache-Control"] = "no-cache"; return r


# ================================================================== admin: menu
def save_image(f):
    from PIL import Image
    if not f or not f.filename: return None
    try:
        im = Image.open(f.stream); im.load()
    except Exception:
        flash("That image couldn't be read. Use JPG, PNG or WEBP.", "err"); return None
    im = im.convert("RGB"); im.thumbnail((900, 900))
    name = f"m{secrets.token_hex(6)}.jpg"; im.save(os.path.join(UPLOADS, name), "JPEG", quality=82, optimize=True); return name


def drop_image(name):
    if name and re.fullmatch(r"m[0-9a-f]{12}\.jpg", name):
        try: os.remove(os.path.join(UPLOADS, name))
        except OSError: pass


@app.route("/media/<name>")
def media(name):
    r = send_from_directory(UPLOADS, name); r.headers["Cache-Control"] = "public, max-age=86400"; return r


def fnum(v):
    try: return max(0.0, float(str(v).replace(",", "")))
    except (ValueError, TypeError): return 0.0


@app.get("/admin/menu")
@staff("admin")
def menu_admin():
    menu = ordered_menu()
    return render_template("admin_menu.html", menu=menu, cats=list(dict.fromkeys(m["category"] for m in menu)), nav="menu")


@app.post("/admin/menu")
@staff("admin")
def add_menu():
    f = request.form
    if not f.get("name", "").strip(): return redirect("/admin/menu")
    img = save_image(request.files.get("image"))
    db().execute("INSERT INTO menu(name,category,price,descr,image) VALUES(?,?,?,?,?)",
                 (f["name"].strip(), f.get("category", "").strip() or "Menu", fnum(f.get("price")), f.get("descr", "").strip(), img))
    db().commit(); flash(f"Added {f['name'].strip()}.", "ok"); return redirect("/admin/menu")


@app.post("/admin/edit/<int:i>")
@staff("admin")
def edit_menu(i):
    f = request.form
    old = db().execute("SELECT image FROM menu WHERE id=?", (i,)).fetchone() or abort(404)
    img = old["image"]
    new = save_image(request.files.get("image"))
    if new: drop_image(img); img = new
    elif f.get("remove_image"): drop_image(img); img = None
    db().execute("UPDATE menu SET name=?,category=?,price=?,descr=?,available=?,image=? WHERE id=?",
                 (f["name"].strip(), f.get("category", "").strip() or "Menu", fnum(f.get("price")), f.get("descr", "").strip(), 1 if f.get("available") else 0, img, i))
    db().commit(); flash("Saved.", "ok"); return redirect("/admin/menu#m" + str(i))


@app.post("/admin/toggle/<int:i>")
@staff("admin")
def toggle(i):
    db().execute("UPDATE menu SET available=1-available WHERE id=?", (i,)); db().commit()
    return jsonify(ok=1) if request.is_json or request.headers.get("X-Requested-With") else redirect("/admin/menu")


@app.post("/admin/del/<int:i>")
@staff("admin")
def delete(i):
    r = db().execute("SELECT image FROM menu WHERE id=?", (i,)).fetchone()
    if r: drop_image(r["image"])
    db().execute("DELETE FROM menu WHERE id=?", (i,)); db().commit(); flash("Item deleted.", "ok"); return redirect("/admin/menu")


@app.post("/admin/menu/category")
@staff("admin")
def category_action():
    cat, act = request.form.get("category", ""), request.form.get("act")
    if act in ("hide", "show"): db().execute("UPDATE menu SET available=? WHERE category=?", (1 if act == "show" else 0, cat)); flash(f"{cat}: all items {'shown' if act == 'show' else 'hidden'}.", "ok")
    elif act == "delete":
        for r in db().execute("SELECT image FROM menu WHERE category=?", (cat,)): drop_image(r["image"])
        db().execute("DELETE FROM menu WHERE category=?", (cat,)); flash(f"Deleted category {cat}.", "ok")
    elif act == "rename" and request.form.get("new", "").strip(): db().execute("UPDATE menu SET category=? WHERE category=?", (request.form["new"].strip(), cat)); flash("Category renamed.", "ok")
    db().commit(); return redirect("/admin/menu")


@app.post("/admin/menu/clear")
@staff("admin")
def menu_clear():
    if request.form.get("confirm", "").strip().upper() == "DELETE":
        for r in db().execute("SELECT image FROM menu"): drop_image(r["image"])
        db().execute("DELETE FROM menu"); db().commit(); flash("Menu cleared.", "ok")
    else: flash("Type DELETE to confirm clearing the whole menu.", "err")
    return redirect("/admin/menu")


def _xlsx(rows, sheet="Menu"):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    wb = Workbook(); ws = wb.active; ws.title = sheet
    ws.append(["Category", "Item", "Price", "Description", "Available"])
    for r in rows: ws.append(r)
    for c in ws[1]: c.font = Font(bold=True, color="FFFFFF"); c.fill = PatternFill("solid", fgColor="0A7D3B")
    for col, w in zip("ABCDE", (18, 34, 12, 50, 11)): ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    return wb


@app.route("/admin/menu/export.xlsx")
@staff("admin")
def menu_export():
    wb = _xlsx([[m["category"], m["name"], m["price"], m["descr"] or "", "yes" if m["available"] else "no"] for m in ordered_menu()])
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    return send_file(buf, as_attachment=True, download_name="menu.xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/admin/menu/template.xlsx")
@staff("admin")
def menu_template():
    wb = _xlsx([["Breakfast", "Mandazi & Tea", 150, "Two mandazi with masala tea", "yes"], ["Breakfast", "Full English", 650, "Eggs, sausage, bacon, toast", "yes"],
                ["Food", "Chicken Pilau", 550, "Spiced rice with chicken", "yes"], ["Food", "Ugali Nyama Choma", 800, "", "yes"],
                ["Drinks", "Soda 300ml", 100, "", "yes"], ["Drinks", "Fresh Juice", 250, "Mango, passion or pineapple", "yes"],
                ["Services", "Laundry (per item)", 100, "Returned within 24h", "yes"]])
    ws = wb.create_sheet("Instructions")
    for line in ["HOW TO USE THIS FILE", "", "1. Replace the sample rows on the 'Menu' sheet with your own - one item per row.",
                 "2. Columns: Category, Item, Price (numbers only), Description (optional), Available (yes/no, optional).",
                 "3. Save as .xlsx and upload it in Admin > Menu > Import.",
                 "Tip: you can also use one sheet per category (name the sheet 'Drinks') and leave the Category column out.",
                 "Tip: Word (.docx) menus work too - use headings for categories and lines like  'Chicken Pilau .... 550'."]: ws.append([line])
    ws.column_dimensions["A"].width = 100; ws["A1"].font = ws["A1"].font.copy(bold=True)
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    return send_file(buf, as_attachment=True, download_name="menu_template.xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/admin/import", methods=["GET", "POST"])
@staff("admin")
def import_menu():
    kw = dict(rows=None, warnings=[], err=None, nav="menu", existing=db().execute("SELECT COUNT(*) FROM menu").fetchone()[0], ai=bool(os.getenv("ANTHROPIC_API_KEY", "").strip()))
    if request.method == "POST":
        files = [f for f in request.files.getlist("file") if f and f.filename]
        try:
            if not files: raise ValueError("Choose a file first.")
            if len(files) > 1 or os.path.splitext(files[0].filename.lower())[1] in (".jpg", ".jpeg", ".png", ".webp", ".pdf"):
                exts = {os.path.splitext(f.filename.lower())[1] for f in files}
                if not exts <= {".jpg", ".jpeg", ".png", ".webp", ".pdf"}: raise ValueError("Upload ONE Excel/Word/CSV file, or one or more photos.")
                mimes = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".pdf": "application/pdf"}
                kw["rows"], kw["warnings"] = importer.from_images([(f.filename, mimes[os.path.splitext(f.filename.lower())[1]], f.read()) for f in files],
                                                                  os.getenv("ANTHROPIC_API_KEY", "").strip(), os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5"))
            else:
                kw["rows"], kw["warnings"] = importer.parse_upload(files[0].filename, files[0].read())
            if not kw["rows"]: kw["rows"] = None; raise ValueError("No menu items were found. Check that the file has a name and a price for each item (see the template).")
            kw["fname"] = files[0].filename
        except ValueError as e: kw["err"] = str(e)
        except Exception as e: kw["err"] = f"Couldn't read that file ({type(e).__name__}: {e}). If it's an Excel/Word file, make sure it opens normally and is saved as .xlsx / .docx."
    return render_template("admin_import.html", **kw)


@app.post("/admin/import/save")
@staff("admin")
def import_save():
    try: rows = json.loads(request.form.get("payload", "[]"))
    except ValueError: rows = []
    d, add, upd = db(), 0, 0
    if request.form.get("mode") == "replace":
        for r in d.execute("SELECT image FROM menu"): drop_image(r["image"])
        d.execute("DELETE FROM menu")
    for r in rows:
        nm, cat = str(r.get("name", "")).strip()[:120], str(r.get("category", "")).strip()[:60] or "Menu"
        if not nm: continue
        pr, ds, av = fnum(r.get("price")), str(r.get("descr", "")).strip()[:300], 0 if str(r.get("available", 1)) == "0" else 1
        ex = d.execute("SELECT id FROM menu WHERE lower(name)=lower(?) AND lower(category)=lower(?)", (nm, cat)).fetchone()
        if ex: d.execute("UPDATE menu SET price=?,descr=CASE WHEN ?!='' THEN ? ELSE descr END,available=? WHERE id=?", (pr, ds, ds, av, ex["id"])); upd += 1
        else: d.execute("INSERT INTO menu(name,category,price,descr,available) VALUES(?,?,?,?,?)", (nm, cat, pr, ds, av)); add += 1
    d.commit(); flash(f"Menu imported: {add} added, {upd} updated.", "ok")
    return redirect("/admin/menu")


# ================================================================== admin: settings & ngrok
@app.route("/admin/settings", methods=["GET", "POST"])
@staff("admin")
def settings():
    if request.method == "POST":
        f, sec = request.form, request.form.get("section")
        if sec == "general":
            set_S("hotel", _clean(f.get("hotel"), 60) or "Hotel"); set_S("currency", _clean(f.get("currency"), 6) or "KES"); set_S("tagline", _clean(f.get("tagline"), 80))
            set_S("service_pct", "%g" % min(30.0, fnum(f.get("service_pct")))); set_S("till", _clean(f.get("till"), 20))
            if re.fullmatch(r"#[0-9a-fA-F]{6}", f.get("brand", "")): set_S("brand", f["brand"])
            flash("Settings saved.", "ok")
        elif sec == "link":
            set_S("ngrok_token", f.get("ngrok_token", "").strip()); set_S("ngrok_domain", f.get("ngrok_domain", "").strip())
            set_S("public_url", f.get("public_url", "").strip().rstrip("/"))
            if f.get("act") == "stop": tunnel.stop(); flash("ngrok tunnel stopped.", "ok")
            elif S("ngrok_token"):
                url = tunnel.start(S("ngrok_token"), S("ngrok_domain"), PORT)
                flash(f"Connected! Guests can now open {url}" if url else "Saved, but ngrok could not connect: " + tunnel.status()["error"], "ok" if url else "err")
            else: flash("Link settings saved.", "ok")
        elif sec == "pins":
            for role in ("kitchen", "admin"):
                p = f.get(role + "_pin", "").strip()
                if p:
                    if len(p) < 4: flash(f"The {role} PIN must be at least 4 characters.", "err"); return redirect("/admin/settings")
                    set_S(role + "_pin_hash", generate_password_hash(p))
            flash("PIN(s) updated.", "ok")
        return redirect("/admin/settings")
    return render_template("admin_settings.html", nav="settings", s={k: S(k) for k in ENV_DEFAULTS}, **conn_info())


@app.route("/healthz")
def healthz(): return "ok"


def boot_tunnel():
    with app.app_context():
        if S("ngrok_token").strip(): tunnel.start(S("ngrok_token"), S("ngrok_domain"), PORT)


def banner():
    with app.app_context():
        t, pub = tunnel.status(), public_url()
        print("\n  ============================================================")
        print(f"   {S('hotel')} - QR ordering is running")
        print("  ============================================================")
        print(f"   Admin  : {pub}/admin     (PIN from .env / Settings)")
        print(f"   Kitchen: {pub}/kitchen")
        print(f"   Local  : http://localhost:{PORT}/admin")
        if t["url"]:
            print(f"   ngrok  : CONNECTED -> guests anywhere can scan the table QR codes" + ("" if t["static"] else "\n   WARNING: no NGROK_DOMAIN set - this link changes on every restart, so printed QR codes would stop working."))
        else:
            print("   ngrok  : not connected" + (f" ({t['error']})" if t["error"] else " (add your Authtoken in Admin > Settings)") + " - QR codes work on the hotel Wi-Fi only.")
        print("  ============================================================\n")


init()
if __name__ == "__main__":
    boot_tunnel(); banner()
    try:
        from waitress import serve; serve(app, host="0.0.0.0", port=PORT, threads=8)
    except ImportError:
        app.run(host="0.0.0.0", port=PORT)