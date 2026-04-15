"""
KRCE Bus Tracking System — Production Backend
K. Ramakrishnan College of Engineering, Trichy, Tamil Nadu
FastAPI + SQLite + WebSocket real-time GPS
"""

import asyncio, hashlib, json, logging, os, time, uuid, csv, io
from datetime import datetime, timedelta, date
from math import atan2, cos, radians, sin, sqrt
from typing import Dict, List, Optional

import jwt, aiosqlite, uvicorn
from fastapi import (
    Depends, FastAPI, HTTPException, Query, Request,
    WebSocket, WebSocketDisconnect, status
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

# ═══════════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════════
COLLEGE_NAME   = "K. Ramakrishnan College of Engineering"
COLLEGE_SHORT  = "KRCE"
COLLEGE_CITY   = "Trichy, Tamil Nadu"
# KRCE campus coordinates (Samayapuram, Trichy)
COLLEGE_LAT    = 10.9601
COLLEGE_LON    = 78.8078

SECRET_KEY     = os.getenv("JWT_SECRET", "krce-bus-secret-2024-change-in-prod")
ALGORITHM      = "HS256"
TOKEN_HOURS    = 24
DB_PATH        = "krce_bus.db"
VEHICLE_TTL    = 300   # seconds before driver considered offline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("KRCE-BUS")

# ═══════════════════════════════════════════════════════════════════
#  APP SETUP
# ═══════════════════════════════════════════════════════════════════
limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="KRCE Bus System", version="1.0.0", docs_url="/api/docs")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(GZipMiddleware, minimum_size=500)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ═══════════════════════════════════════════════════════════════════
#  LIVE STATE  (fast in-memory, synced from driver WebSocket)
# ═══════════════════════════════════════════════════════════════════
# bus_id → {lat, lon, speed, heading, passengers, status, updated_at, driver_name}
live_buses: Dict[str, dict] = {}
# user_id → WebSocket  (all connected clients)
ws_pool: Dict[str, WebSocket] = {}
# user_id → last seen timestamp
last_seen: Dict[str, float] = {}

# ═══════════════════════════════════════════════════════════════════
#  UTILITIES
# ═══════════════════════════════════════════════════════════════════
def _hash(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

def haversine(lat1, lon1, lat2, lon2) -> float:
    """Distance in metres between two GPS points."""
    R = 6_371_000
    φ1, φ2 = radians(lat1), radians(lat2)
    dφ, dλ = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dφ / 2) ** 2 + cos(φ1) * cos(φ2) * sin(dλ / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))

def today() -> str:
    return date.today().isoformat()

# ═══════════════════════════════════════════════════════════════════
#  DATABASE  — init + seed
# ═══════════════════════════════════════════════════════════════════
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    email         TEXT UNIQUE NOT NULL,
    phone         TEXT,
    role          TEXT NOT NULL,          -- admin|committee|driver|student|staff|parent
    college_id    TEXT,
    rfid_card     TEXT UNIQUE,
    bus_id        TEXT,                   -- assigned bus (student/driver/staff)
    parent_of     TEXT,                   -- college_id of child (parent role)
    licence_no    TEXT,                   -- drivers only
    password_hash TEXT NOT NULL,
    is_active     INTEGER DEFAULT 1,
    created_at    TEXT DEFAULT (datetime('now')),
    last_login    TEXT
);

CREATE TABLE IF NOT EXISTS buses (
    id          TEXT PRIMARY KEY,
    number      TEXT NOT NULL UNIQUE,     -- e.g. TN-01
    route_name  TEXT NOT NULL,
    driver_id   TEXT,
    capacity    INTEGER DEFAULT 50,
    stops       TEXT DEFAULT '[]',        -- JSON array of stop names
    is_active   INTEGER DEFAULT 1,
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS attendance (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    bus_id      TEXT NOT NULL,
    tap_type    TEXT NOT NULL,            -- boarded | exited
    tap_time    TEXT DEFAULT (datetime('now')),
    stop_name   TEXT,
    lat         REAL DEFAULT 0,
    lon         REAL DEFAULT 0,
    date        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS registrations (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    name         TEXT NOT NULL,
    email        TEXT,
    college_id   TEXT,
    role         TEXT,
    requested_bus TEXT,
    rfid_card    TEXT,
    phone        TEXT,
    status       TEXT DEFAULT 'pending',  -- pending|approved|rejected
    submitted_at TEXT DEFAULT (datetime('now')),
    reviewed_by  TEXT,
    reviewed_at  TEXT,
    notes        TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    message     TEXT NOT NULL,
    alert_type  TEXT DEFAULT 'info',      -- info|delay|emergency
    target_role TEXT DEFAULT 'all',
    target_bus  TEXT,
    sent_by     TEXT,
    sent_at     TEXT DEFAULT (datetime('now')),
    is_resolved INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT,
    action      TEXT,
    detail      TEXT,
    ip          TEXT,
    ts          TEXT DEFAULT (datetime('now'))
);
"""

SEED_DATA = {
    "users": [
        # id, name, email, phone, role, college_id, rfid, bus_id, parent_of, licence, pw_hash
        ("admin01",   "Admin Krishnamurthy", "admin@krce.ac.in",        "9840100001", "admin",     None,      None,    None,    None,     None,        _hash("admin@krce")),
        ("comm01",    "Dr. Senthil Kumar",   "committee@krce.ac.in",    "9840100002", "committee", "FAC001",  None,    None,    None,     None,        _hash("comm@krce")),
        # Drivers
        ("drv01",     "Rajan S.",            "rajan@krce.ac.in",        "9840111111", "driver",    None,      None,    "B01",   None,     "TN-DL-001", _hash("driver@123")),
        ("drv02",     "Murugan K.",          "murugan@krce.ac.in",      "9840122222", "driver",    None,      None,    "B02",   None,     "TN-DL-002", _hash("driver@123")),
        ("drv03",     "Selvam P.",           "selvam@krce.ac.in",       "9840133333", "driver",    None,      None,    "B03",   None,     "TN-DL-003", _hash("driver@123")),
        ("drv04",     "Arun M.",             "arun@krce.ac.in",         "9840144444", "driver",    None,      None,    "B04",   None,     "TN-DL-004", _hash("driver@123")),
        ("drv05",     "Suresh T.",           "suresh.d@krce.ac.in",     "9840155555", "driver",    None,      None,    "B05",   None,     "TN-DL-005", _hash("driver@123")),
        # Students
        ("stu01",     "Aravind Kumar",       "aravind@krce.ac.in",      "9841100001", "student",   "21CS001", "RF001", "B01",   None,     None,        _hash("student@123")),
        ("stu02",     "Priya Devi",          "priya@krce.ac.in",        "9841100002", "student",   "21EC002", "RF002", "B02",   None,     None,        _hash("student@123")),
        ("stu03",     "Karthikeyan M",       "karthik@krce.ac.in",      "9841100003", "student",   "21ME003", "RF003", "B03",   None,     None,        _hash("student@123")),
        ("stu04",     "Nandhini R",          "nandhini@krce.ac.in",     "9841100004", "student",   "21CS004", "RF004", "B01",   None,     None,        _hash("student@123")),
        ("stu05",     "Anitha S",            "anitha@krce.ac.in",       "9841100005", "student",   "21IT006", "RF005", "B04",   None,     None,        _hash("student@123")),
        ("stu06",     "Ravi Shankar",        "ravi@krce.ac.in",         "9841100006", "student",   "22CS007", "RF006", "B05",   None,     None,        _hash("student@123")),
        ("stu07",     "Meena P",             "meena@krce.ac.in",        "9841100007", "student",   "22EC008", "RF007", "B02",   None,     None,        _hash("student@123")),
        ("stu08",     "Saran B",             "saran@krce.ac.in",        "9841100008", "student",   "22ME010", "RF008", "B03",   None,     None,        _hash("student@123")),
        # Staff
        ("fac01",     "Dr. Radha L",         "radha@krce.ac.in",        "9841200001", "staff",     "FAC002",  "RF009", "B01",   None,     None,        _hash("staff@123")),
        ("fac02",     "Dr. Senthil Kumar",   "senthil@krce.ac.in",      "9841200002", "staff",     "FAC001",  "RF010", "B02",   None,     None,        _hash("staff@123")),
        # Parents
        ("par01",     "Suresh Kumar",        "suresh.p@gmail.com",      "9841300001", "parent",    None,      None,    None,    "21CS001", None,        _hash("parent@123")),
        ("par02",     "Meenakshi Devi",      "meenakshi@gmail.com",     "9841300002", "parent",    None,      None,    None,    "21EC002", None,        _hash("parent@123")),
    ],
    "buses": [
        # id, number, route_name, driver_id, capacity, stops_json
        ("B01", "TN-01", "Route A — Woraiyur", "drv01", 50,
         '["KRCE Campus","Samayapuram","Woraiyur Bus Stand","Woraiyur Town","Gandhi Market","KRCE Campus"]'),
        ("B02", "TN-02", "Route B — Srirangam", "drv02", 45,
         '["KRCE Campus","Panjappur","Srirangam","Cauvery Bridge","K.K. Nagar","KRCE Campus"]'),
        ("B03", "TN-03", "Route C — Ariyamangalam", "drv03", 50,
         '["KRCE Campus","Thuvakudi","Ariyamangalam","Cantonment","Collector Office","KRCE Campus"]'),
        ("B04", "TN-04", "Route D — Chatram Bus Stand", "drv04", 40,
         '["KRCE Campus","Palakarai","Chatram Bus Stand","Central","Junction","KRCE Campus"]'),
        ("B05", "TN-05", "Route E — Mannarpuram", "drv05", 55,
         '["KRCE Campus","Thillai Nagar","Mannarpuram","Rockfort","Chinthamani","KRCE Campus"]'),
    ],
    "alerts": [
        (str(uuid.uuid4())[:8], "Welcome to KRCE Bus Tracker",
         "The new real-time bus tracking system is now live. Your bus location updates every 5 seconds.",
         "info", "all", None, "admin01"),
        (str(uuid.uuid4())[:8], "Route A — Minor Delay",
         "Bus TN-01 is running approximately 10 minutes late due to traffic near Woraiyur Junction.",
         "delay", "all", "B01", "admin01"),
    ],
}


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)

        cur = await db.execute("SELECT COUNT(*) FROM users")
        if (await cur.fetchone())[0] == 0:
            await db.executemany(
                "INSERT INTO users (id,name,email,phone,role,college_id,rfid_card,bus_id,parent_of,licence_no,password_hash) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                SEED_DATA["users"]
            )
            await db.executemany(
                "INSERT INTO buses (id,number,route_name,driver_id,capacity,stops) VALUES (?,?,?,?,?,?)",
                SEED_DATA["buses"]
            )
            await db.executemany(
                "INSERT INTO alerts (id,title,message,alert_type,target_role,target_bus,sent_by) VALUES (?,?,?,?,?,?,?)",
                SEED_DATA["alerts"]
            )
            # Seed some attendance for today
            td = today()
            att_seed = [
                (str(uuid.uuid4()), "stu01", "B01", "boarded", "Woraiyur Bus Stand", 10.7905, 78.7047, td),
                (str(uuid.uuid4()), "stu04", "B01", "boarded", "Samayapuram",        10.9310, 78.8130, td),
                (str(uuid.uuid4()), "fac01", "B01", "boarded", "Woraiyur Bus Stand", 10.7905, 78.7047, td),
                (str(uuid.uuid4()), "stu02", "B02", "boarded", "Srirangam",          10.8631, 78.6933, td),
                (str(uuid.uuid4()), "stu07", "B02", "boarded", "K.K. Nagar",         10.8176, 78.6960, td),
                (str(uuid.uuid4()), "stu03", "B03", "boarded", "Ariyamangalam",      10.8280, 78.7380, td),
                (str(uuid.uuid4()), "stu08", "B03", "boarded", "Thuvakudi",          10.8730, 78.7680, td),
                (str(uuid.uuid4()), "stu05", "B04", "boarded", "Chatram Bus Stand",  10.8096, 78.6964, td),
                (str(uuid.uuid4()), "stu06", "B05", "boarded", "Mannarpuram",        10.8182, 78.7030, td),
            ]
            await db.executemany(
                "INSERT INTO attendance (id,user_id,bus_id,tap_type,stop_name,lat,lon,date) VALUES (?,?,?,?,?,?,?,?)",
                att_seed
            )
        await db.commit()
    logger.info("Database ready — %s", DB_PATH)


# ═══════════════════════════════════════════════════════════════════
#  AUTH
# ═══════════════════════════════════════════════════════════════════
security = HTTPBearer(auto_error=False)


def make_token(uid: str, name: str, role: str, bus_id: str = "", extra: dict = None) -> str:
    payload = {
        "sub": uid, "name": name, "role": role,
        "bus_id": bus_id,
        "exp": datetime.utcnow() + timedelta(hours=TOKEN_HOURS),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def verify_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired — please log in again")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token")


async def current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(security)
) -> dict:
    if not creds:
        raise HTTPException(401, "Authentication required")
    return verify_token(creds.credentials)


async def admin_only(u=Depends(current_user)):
    if u["role"] not in ("admin", "committee"):
        raise HTTPException(403, "Admin / Committee access only")
    return u


# ═══════════════════════════════════════════════════════════════════
#  PYDANTIC MODELS
# ═══════════════════════════════════════════════════════════════════
class LoginReq(BaseModel):
    email: str
    password: str


class RegisterReq(BaseModel):
    name: str
    email: str
    password: str
    phone: str = ""
    role: str
    college_id: str = ""
    rfid_card: str = ""
    requested_bus: str = ""
    parent_child_id: str = ""   # college_id of child (parent only)

    @field_validator("role")
    @classmethod
    def vr(cls, v):
        allowed = {"student", "staff", "parent"}
        if v not in allowed:
            raise ValueError(f"Role must be one of {allowed}")
        return v


class BusUpsert(BaseModel):
    number: str
    route_name: str
    driver_id: str = ""
    capacity: int = 50
    stops: List[str] = []


class AlertCreate(BaseModel):
    title: str
    message: str
    alert_type: str = "info"
    target_role: str = "all"
    target_bus: str = ""


class RegAction(BaseModel):
    reg_id: str
    action: str        # approved | rejected
    bus_id: str = ""
    rfid_card: str = ""
    notes: str = ""


class RfidTap(BaseModel):
    rfid_card: str
    bus_id: str
    stop_name: str = ""
    lat: float = 0.0
    lon: float = 0.0


class GpsUpdate(BaseModel):
    lat: float
    lon: float
    speed: float = 0.0
    heading: float = 0.0
    passengers: int = 0


# ═══════════════════════════════════════════════════════════════════
#  STARTUP / CLEANUP
# ═══════════════════════════════════════════════════════════════════
@app.on_event("startup")
async def on_start():
    await init_db()
    asyncio.create_task(_stale_cleaner())
    logger.info("KRCE Bus System started")


async def _stale_cleaner():
    while True:
        await asyncio.sleep(60)
        now = time.time()
        stale = [k for k, v in last_seen.items() if now - v > VEHICLE_TTL]
        for uid in stale:
            last_seen.pop(uid, None)
        # Mark buses whose drivers are offline
        for bid, info in live_buses.items():
            drv = info.get("driver_id", "")
            if drv and drv not in last_seen:
                info["status"] = "offline"


# ═══════════════════════════════════════════════════════════════════
#  AUTH ENDPOINTS
# ═══════════════════════════════════════════════════════════════════
@app.get("/healthz")
async def health():
    return {"ok": True, "live_buses": len(live_buses), "ws": len(ws_pool)}


@app.post("/api/auth/login")
@limiter.limit("15/minute")
async def login(req: LoginReq, request: Request):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM users WHERE email=? AND is_active=1", (req.email,)
        )
        row = await cur.fetchone()
    if not row or row["password_hash"] != _hash(req.password):
        raise HTTPException(401, "Invalid email or password")

    u = dict(row)
    extra = {
        "college_id": u.get("college_id") or "",
        "rfid_card":  u.get("rfid_card") or "",
        "parent_of":  u.get("parent_of") or "",
        "phone":      u.get("phone") or "",
    }
    token = make_token(u["id"], u["name"], u["role"], u.get("bus_id") or "", extra)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET last_login=datetime('now') WHERE id=?", (u["id"],))
        await db.execute(
            "INSERT INTO audit_log(user_id,action,ip) VALUES(?,?,?)",
            (u["id"], "login", request.client.host if request.client else "")
        )
        await db.commit()

    return {
        "token":      token,
        "user_id":    u["id"],
        "name":       u["name"],
        "role":       u["role"],
        "bus_id":     u.get("bus_id") or "",
        "college_id": u.get("college_id") or "",
        "rfid_card":  u.get("rfid_card") or "",
        "parent_of":  u.get("parent_of") or "",
        "phone":      u.get("phone") or "",
    }


@app.post("/api/auth/register")
@limiter.limit("5/minute")
async def register(req: RegisterReq, request: Request):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id FROM users WHERE email=?", (req.email,))
        if await cur.fetchone():
            raise HTTPException(400, "Email already registered")
        rid = str(uuid.uuid4())[:8]
        await db.execute(
            "INSERT INTO registrations "
            "(id,user_id,name,email,college_id,role,requested_bus,rfid_card,phone) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (rid, "pending_" + rid, req.name, req.email,
             req.college_id, req.role, req.requested_bus, req.rfid_card, req.phone)
        )
        await db.commit()
    return {"status": "ok", "message": "Registration submitted. Await admin approval.", "reg_id": rid}


@app.get("/api/auth/demo")
async def demo_creds():
    """Return demo credentials shown on login page."""
    return {
        "admin":     [{"email": "admin@krce.ac.in",     "password": "admin@krce",   "role": "Admin"}],
        "committee": [{"email": "committee@krce.ac.in", "password": "comm@krce",    "role": "Committee"}],
        "drivers":   [
            {"email": "rajan@krce.ac.in",   "password": "driver@123", "role": "Driver", "bus": "TN-01"},
            {"email": "murugan@krce.ac.in", "password": "driver@123", "role": "Driver", "bus": "TN-02"},
        ],
        "students":  [
            {"email": "aravind@krce.ac.in", "password": "student@123", "role": "Student", "bus": "TN-01"},
            {"email": "priya@krce.ac.in",   "password": "student@123", "role": "Student", "bus": "TN-02"},
        ],
        "staff":     [{"email": "radha@krce.ac.in",  "password": "staff@123",  "role": "Staff"}],
        "parents":   [{"email": "suresh.p@gmail.com", "password": "parent@123", "role": "Parent"}],
    }


# ═══════════════════════════════════════════════════════════════════
#  ADMIN — STATS
# ═══════════════════════════════════════════════════════════════════
@app.get("/api/admin/stats")
async def admin_stats(u=Depends(admin_only)):
    td = today()
    async with aiosqlite.connect(DB_PATH) as db:
        def n(q, *a):
            return db.execute(q, a)
        c = lambda q, *a: db.execute(q, a)

        r1  = await (await c("SELECT COUNT(*) FROM users WHERE role IN ('student','staff')")).fetchone()
        r2  = await (await c("SELECT COUNT(*) FROM buses WHERE is_active=1")).fetchone()
        r3  = await (await c(
            "SELECT COUNT(DISTINCT user_id) FROM attendance WHERE date=? AND tap_type='boarded'", td
        )).fetchone()
        r4  = await (await c("SELECT COUNT(*) FROM registrations WHERE status='pending'")).fetchone()
        r5  = await (await c("SELECT COUNT(*) FROM users WHERE role='driver'")).fetchone()
        r6  = await (await c("SELECT COUNT(*) FROM alerts WHERE is_resolved=0")).fetchone()
    return {
        "total_students":     r1[0],
        "active_buses":       r2[0],
        "boarded_today":      r3[0],
        "pending_regs":       r4[0],
        "total_drivers":      r5[0],
        "active_alerts":      r6[0],
        "live_buses":         sum(1 for b in live_buses.values() if b.get("status") != "offline"),
    }


# ═══════════════════════════════════════════════════════════════════
#  ADMIN — BUSES
# ═══════════════════════════════════════════════════════════════════
@app.get("/api/admin/buses")
async def admin_buses(u=Depends(admin_only)):
    td = today()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT b.*, u.name as driver_name, u.phone as driver_phone
            FROM buses b LEFT JOIN users u ON b.driver_id=u.id
            WHERE b.is_active=1 ORDER BY b.number
        """)
        rows = await cur.fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["stops"] = json.loads(d.get("stops") or "[]")
        d["live"]  = live_buses.get(d["id"])
        td_count_cur = None
        async with aiosqlite.connect(DB_PATH) as db:
            cur2 = await db.execute(
                "SELECT COUNT(DISTINCT user_id) FROM attendance WHERE bus_id=? AND date=? AND tap_type='boarded'",
                (d["id"], td)
            )
            d["boarded_today"] = (await cur2.fetchone())[0]
        result.append(d)
    return result


@app.post("/api/admin/buses")
async def create_bus(req: BusUpsert, u=Depends(admin_only)):
    bid = "B" + str(uuid.uuid4())[:6]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO buses (id,number,route_name,driver_id,capacity,stops) VALUES (?,?,?,?,?,?)",
            (bid, req.number, req.route_name, req.driver_id or None,
             req.capacity, json.dumps(req.stops))
        )
        await db.commit()
    return {"status": "ok", "bus_id": bid}


@app.put("/api/admin/buses/{bus_id}")
async def update_bus(bus_id: str, req: BusUpsert, u=Depends(admin_only)):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE buses SET number=?,route_name=?,driver_id=?,capacity=?,stops=? WHERE id=?",
            (req.number, req.route_name, req.driver_id or None,
             req.capacity, json.dumps(req.stops), bus_id)
        )
        await db.commit()
    return {"status": "ok"}


@app.delete("/api/admin/buses/{bus_id}")
async def delete_bus(bus_id: str, u=Depends(admin_only)):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE buses SET is_active=0 WHERE id=?", (bus_id,))
        await db.commit()
    return {"status": "ok"}


# ═══════════════════════════════════════════════════════════════════
#  ADMIN — USERS
# ═══════════════════════════════════════════════════════════════════
@app.get("/api/admin/users")
async def admin_users(role: str = "", u=Depends(admin_only)):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if role:
            cur = await db.execute(
                "SELECT id,name,email,phone,role,college_id,rfid_card,bus_id,is_active,created_at "
                "FROM users WHERE role=? ORDER BY name", (role,)
            )
        else:
            cur = await db.execute(
                "SELECT id,name,email,phone,role,college_id,rfid_card,bus_id,is_active,created_at "
                "FROM users ORDER BY role,name"
            )
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


@app.post("/api/admin/users/{uid}/toggle")
async def toggle_user(uid: str, u=Depends(admin_only)):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT is_active FROM users WHERE id=?", (uid,))
        row = await cur.fetchone()
        if not row:
            raise HTTPException(404, "User not found")
        new_val = 0 if row[0] else 1
        await db.execute("UPDATE users SET is_active=? WHERE id=?", (new_val, uid))
        await db.commit()
    return {"status": "ok", "is_active": new_val}


# ═══════════════════════════════════════════════════════════════════
#  ADMIN — DRIVERS
# ═══════════════════════════════════════════════════════════════════
@app.get("/api/admin/drivers")
async def admin_drivers(u=Depends(admin_only)):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT u.id,u.name,u.phone,u.licence_no,u.bus_id,u.is_active,
                   b.number as bus_number, b.route_name
            FROM users u LEFT JOIN buses b ON u.bus_id=b.id
            WHERE u.role='driver' ORDER BY u.name
        """)
        rows = await cur.fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["is_online"] = d["id"] in last_seen
        result.append(d)
    return result


# ═══════════════════════════════════════════════════════════════════
#  ADMIN — ATTENDANCE
# ═══════════════════════════════════════════════════════════════════
@app.get("/api/admin/attendance")
async def admin_attendance(
    date_filter: str = "", bus_id: str = "", u=Depends(admin_only)
):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        q = """
            SELECT a.*, u.name as student_name, u.college_id,
                   b.number as bus_number, b.route_name
            FROM attendance a
            JOIN users u ON a.user_id=u.id
            JOIN buses b ON a.bus_id=b.id
            WHERE 1=1
        """
        params = []
        if date_filter:
            q += " AND a.date=?"; params.append(date_filter)
        if bus_id:
            q += " AND a.bus_id=?"; params.append(bus_id)
        q += " ORDER BY a.tap_time DESC LIMIT 300"
        cur = await db.execute(q, params)
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


@app.get("/api/admin/attendance/export")
async def export_attendance(
    date_filter: str = "", bus_id: str = "", u=Depends(admin_only)
):
    rows = await admin_attendance(date_filter=date_filter, bus_id=bus_id, u=u)
    output = io.StringIO()
    w = csv.writer(output)
    w.writerow(["Student Name", "College ID", "Bus", "Route", "Tap Type", "Stop", "Time", "Date"])
    for r in rows:
        w.writerow([
            r["student_name"], r["college_id"], r["bus_number"],
            r["route_name"], r["tap_type"], r["stop_name"],
            r["tap_time"], r["date"]
        ])
    output.seek(0)
    filename = f"attendance_{date_filter or today()}.csv"
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode()),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


# ═══════════════════════════════════════════════════════════════════
#  ADMIN — REGISTRATIONS
# ═══════════════════════════════════════════════════════════════════
@app.get("/api/admin/registrations")
async def admin_regs(status: str = "pending", u=Depends(admin_only)):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM registrations WHERE status=? ORDER BY submitted_at DESC",
            (status,)
        )
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


@app.post("/api/admin/registrations/action")
async def reg_action(req: RegAction, u=Depends(admin_only)):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM registrations WHERE id=?", (req.reg_id,))
        reg = await cur.fetchone()
        if not reg:
            raise HTTPException(404, "Registration not found")
        reg = dict(reg)

        await db.execute(
            "UPDATE registrations SET status=?,reviewed_by=?,reviewed_at=datetime('now'),notes=? WHERE id=?",
            (req.action, u["sub"], req.notes, req.reg_id)
        )
        if req.action == "approved":
            new_uid = reg["role"][:3] + str(uuid.uuid4())[:6]
            pw_hash = _hash(reg["email"].split("@")[0] + "@123")
            await db.execute(
                "INSERT INTO users (id,name,email,phone,role,college_id,rfid_card,bus_id,password_hash) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (new_uid, reg["name"], reg["email"], reg.get("phone"),
                 reg["role"], reg.get("college_id"), req.rfid_card or None,
                 req.bus_id or None, pw_hash)
            )
        await db.commit()

    msg = "approved" if req.action == "approved" else "rejected"
    return {"status": "ok", "message": f"Registration {msg}"}


# ═══════════════════════════════════════════════════════════════════
#  ADMIN — ALERTS
# ═══════════════════════════════════════════════════════════════════
@app.get("/api/admin/alerts")
async def admin_alerts(u=Depends(admin_only)):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM alerts ORDER BY sent_at DESC LIMIT 50")
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


@app.post("/api/admin/alerts")
async def send_alert(req: AlertCreate, u=Depends(admin_only)):
    aid = str(uuid.uuid4())[:8]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO alerts (id,title,message,alert_type,target_role,target_bus,sent_by) "
            "VALUES (?,?,?,?,?,?,?)",
            (aid, req.title, req.message, req.alert_type,
             req.target_role, req.target_bus or None, u["sub"])
        )
        await db.commit()
    # Broadcast to all connected WebSocket clients
    payload = json.dumps({
        "type": "alert",
        "id": aid, "title": req.title,
        "message": req.message,
        "alert_type": req.alert_type,
    })
    for uid, ws in list(ws_pool.items()):
        try:
            await ws.send_text(payload)
        except Exception:
            pass
    return {"status": "ok", "alert_id": aid}


@app.post("/api/admin/alerts/{aid}/resolve")
async def resolve_alert(aid: str, u=Depends(admin_only)):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE alerts SET is_resolved=1 WHERE id=?", (aid,))
        await db.commit()
    return {"status": "ok"}


# ═══════════════════════════════════════════════════════════════════
#  PASSENGER — PUBLIC BUS DATA
# ═══════════════════════════════════════════════════════════════════
@app.get("/api/buses")
async def get_buses(u=Depends(current_user)):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id,number,route_name,driver_id,capacity,stops FROM buses WHERE is_active=1"
        )
        rows = await cur.fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["stops"] = json.loads(d.get("stops") or "[]")
        d["live"]  = live_buses.get(d["id"])
        result.append(d)
    return result


@app.get("/api/buses/{bus_id}/live")
async def bus_live(bus_id: str, u=Depends(current_user)):
    return live_buses.get(bus_id) or {"status": "offline"}


@app.get("/api/buses/{bus_id}/passengers")
async def bus_passengers(bus_id: str, u=Depends(current_user)):
    td = today()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT u.name, u.college_id, u.rfid_card, a.tap_type, a.tap_time, a.stop_name
            FROM attendance a JOIN users u ON a.user_id=u.id
            WHERE a.bus_id=? AND a.date=? AND a.tap_type='boarded'
            ORDER BY a.tap_time
        """, (bus_id, td))
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════════
#  PASSENGER — MY DATA
# ═══════════════════════════════════════════════════════════════════
@app.get("/api/my/attendance")
async def my_attendance(u=Depends(current_user)):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT a.*, b.number as bus_number, b.route_name
            FROM attendance a JOIN buses b ON a.bus_id=b.id
            WHERE a.user_id=? ORDER BY a.tap_time DESC LIMIT 40
        """, (u["sub"],))
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


@app.get("/api/my/child-attendance")
async def child_attendance(u=Depends(current_user)):
    child_cid = u.get("parent_of", "")
    if not child_cid:
        raise HTTPException(403, "Not linked to any child")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id FROM users WHERE college_id=?", (child_cid,)
        )
        row = await cur.fetchone()
        if not row:
            raise HTTPException(404, "Child not found")
        child_uid = row[0]
        cur2 = await db.execute("""
            SELECT a.*, b.number as bus_number, b.route_name,
                   u.name as child_name
            FROM attendance a
            JOIN buses b ON a.bus_id=b.id
            JOIN users u ON a.user_id=u.id
            WHERE a.user_id=? ORDER BY a.tap_time DESC LIMIT 40
        """, (child_uid,))
        rows = await cur2.fetchall()
    return [dict(r) for r in rows]


@app.get("/api/alerts")
async def get_alerts(u=Depends(current_user)):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM alerts WHERE is_resolved=0 "
            "AND (target_role='all' OR target_role=?) "
            "ORDER BY sent_at DESC LIMIT 20",
            (u["role"],)
        )
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════════
#  RFID TAP  (called by hardware or mobile driver app)
# ═══════════════════════════════════════════════════════════════════
@app.post("/api/rfid/tap")
async def rfid_tap(req: RfidTap, u=Depends(current_user)):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id,name FROM users WHERE rfid_card=? AND is_active=1", (req.rfid_card,)
        )
        stu = await cur.fetchone()
        if not stu:
            raise HTTPException(404, "RFID card not registered")
        td = today()
        cur2 = await db.execute(
            "SELECT tap_type FROM attendance WHERE user_id=? AND date=? ORDER BY tap_time DESC LIMIT 1",
            (stu["id"], td)
        )
        last = await cur2.fetchone()
        tap_type = "exited" if (last and last[0] == "boarded") else "boarded"

        await db.execute(
            "INSERT INTO attendance (id,user_id,bus_id,tap_type,stop_name,lat,lon,date) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), stu["id"], req.bus_id, tap_type,
             req.stop_name, req.lat, req.lon, td)
        )
        await db.commit()

    # Update live passenger count
    if req.bus_id in live_buses:
        delta = 1 if tap_type == "boarded" else -1
        live_buses[req.bus_id]["passengers"] = max(
            0, live_buses[req.bus_id].get("passengers", 0) + delta
        )
    return {"status": "ok", "tap_type": tap_type, "student_name": dict(stu)["name"]}


# ═══════════════════════════════════════════════════════════════════
#  GPS UPDATE (REST fallback — used by mobile driver app)
# ═══════════════════════════════════════════════════════════════════
@app.post("/api/driver/gps")
@limiter.limit("30/minute")
async def driver_gps(req: GpsUpdate, request: Request, u=Depends(current_user)):
    if u["role"] != "driver":
        raise HTTPException(403, "Drivers only")
    bus_id = u.get("bus_id", "")
    if not bus_id:
        raise HTTPException(400, "No bus assigned to this driver")
    if not (-90 <= req.lat <= 90 and -180 <= req.lon <= 180):
        raise HTTPException(422, "Invalid coordinates")

    live_buses[bus_id] = {
        "bus_id":     bus_id,
        "driver_id":  u["sub"],
        "driver_name": u["name"],
        "lat":        req.lat,
        "lon":        req.lon,
        "speed":      req.speed,
        "heading":    req.heading,
        "passengers": req.passengers,
        "updated_at": time.time(),
        "status":     "moving" if req.speed > 2 else "idle",
    }
    last_seen[u["sub"]] = time.time()
    return {"status": "ok"}


# ═══════════════════════════════════════════════════════════════════
#  WEBSOCKET  (real-time GPS + alert push)
# ═══════════════════════════════════════════════════════════════════
@app.websocket("/ws")
async def websocket_ep(ws: WebSocket, token: str = Query(...)):
    try:
        payload = verify_token(token)
    except HTTPException:
        await ws.close(code=1008)
        return

    uid    = payload["sub"]
    role   = payload["role"]
    name   = payload["name"]
    bus_id = payload.get("bus_id", "")

    await ws.accept()
    ws_pool[uid] = ws
    last_seen[uid] = time.time()

    # Drivers: init live_buses entry
    if role == "driver" and bus_id:
        if bus_id not in live_buses:
            live_buses[bus_id] = {
                "bus_id": bus_id, "driver_id": uid, "driver_name": name,
                "lat": COLLEGE_LAT, "lon": COLLEGE_LON,
                "speed": 0, "heading": 0, "passengers": 0,
                "updated_at": time.time(), "status": "idle",
            }

    logger.info(f"WS+ {uid} ({role})")

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            mtype = msg.get("type")

            # ── Driver sends real GPS from phone ──────────────────
            if mtype == "gps" and role == "driver" and bus_id:
                lat = float(msg.get("lat", 0))
                lon = float(msg.get("lon", 0))
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    continue
                spd  = float(msg.get("speed", 0))
                head = float(msg.get("heading", 0))
                pax  = int(msg.get("passengers", live_buses.get(bus_id, {}).get("passengers", 0)))

                live_buses[bus_id].update({
                    "lat": lat, "lon": lon, "speed": round(spd, 1),
                    "heading": round(head, 1), "passengers": pax,
                    "updated_at": time.time(),
                    "status": "moving" if spd > 2 else "idle",
                })
                last_seen[uid] = time.time()

            # ── Heartbeat ─────────────────────────────────────────
            elif mtype == "ping":
                await ws.send_text(json.dumps({"type": "pong", "ts": time.time()}))
                last_seen[uid] = time.time()

    except WebSocketDisconnect:
        logger.info(f"WS- {uid} ({role})")
    except Exception as e:
        logger.warning(f"WS err {uid}: {e}")
    finally:
        ws_pool.pop(uid, None)
        last_seen.pop(uid, None)
        if role == "driver" and bus_id and bus_id in live_buses:
            live_buses[bus_id]["status"] = "offline"


# ═══════════════════════════════════════════════════════════════════
#  HTML ROUTES
# ═══════════════════════════════════════════════════════════════════
@app.get("/", response_class=HTMLResponse)
async def root(req: Request):
    return templates.TemplateResponse("passenger.html", {"request": req})


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(req: Request):
    return templates.TemplateResponse("admin.html", {"request": req})


if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True, log_level="info")
