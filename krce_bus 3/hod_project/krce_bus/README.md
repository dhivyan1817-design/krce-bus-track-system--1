# KRCE Bus Tracking System
## K. Ramakrishnan College of Engineering, Trichy, Tamil Nadu

---

## Quick Start

```bash
# 1. Install Python dependencies
pip install -r requirements.txt

# 2. Run the server
python server.py

# OR with uvicorn directly
uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```

Open in browser:
- **Passenger App:** http://localhost:8000/
- **Admin Portal:** http://localhost:8000/admin
- **API Docs:** http://localhost:8000/api/docs

---

## Login Credentials

### Admin Portal (`/admin`)
| Role        | Email                     | Password      |
|-------------|---------------------------|---------------|
| Admin       | admin@krce.ac.in          | admin@krce    |
| Committee   | committee@krce.ac.in      | comm@krce     |

### Passenger App (`/`)
| Role     | Email                   | Password      | Bus   |
|----------|-------------------------|---------------|-------|
| Student  | aravind@krce.ac.in      | student@123   | TN-01 |
| Student  | priya@krce.ac.in        | student@123   | TN-02 |
| Staff    | radha@krce.ac.in        | staff@123     | TN-01 |
| Driver   | rajan@krce.ac.in        | driver@123    | TN-01 |
| Driver   | murugan@krce.ac.in      | driver@123    | TN-02 |
| Parent   | suresh.p@gmail.com      | parent@123    | —     |

---

## How Real Driver GPS Works

Drivers log in through the **Passenger App** (`/`) on their phone.
The app uses the **browser Geolocation API** to read the phone's GPS.
Location is sent every second via **WebSocket** to the server.
All connected students and parents see the bus move on the map in real-time.

### For a dedicated mobile app (React Native / Flutter):
Make a POST request every few seconds to:

```
POST /api/driver/gps
Authorization: Bearer <driver_jwt_token>
Content-Type: application/json

{
  "lat": 10.9601,
  "lon": 78.8078,
  "speed": 35.5,
  "heading": 180.0,
  "passengers": 38
}
```

Or connect via WebSocket and send:
```json
{ "type": "gps", "lat": 10.9601, "lon": 78.8078, "speed": 35.5, "heading": 180, "passengers": 38 }
```

WebSocket URL: `ws://your-server/ws?token=<driver_jwt_token>`

---

## RFID Integration (Hardware)

When a student taps their RFID card on the bus reader, call:

```
POST /api/rfid/tap
Authorization: Bearer <driver_jwt_token>
Content-Type: application/json

{
  "rfid_card": "RF001",
  "bus_id": "B01",
  "stop_name": "Woraiyur Bus Stand",
  "lat": 10.7905,
  "lon": 78.7047
}
```

The system automatically detects boarded vs. exited based on last tap.

---

## Project Structure

```
krce_bus/
├── server.py           ← FastAPI backend (ALL logic here)
├── requirements.txt    ← Python dependencies
├── krce_bus.db         ← SQLite database (auto-created on first run)
├── templates/
│   ├── admin.html      ← Admin & Committee portal
│   └── passenger.html  ← Student / Driver / Parent app
└── static/             ← CSS/JS assets (empty, assets inline)
```

## Database Tables

| Table           | Description                              |
|----------------|------------------------------------------|
| `users`         | All users (admin, committee, driver, student, staff, parent) |
| `buses`         | Fleet with routes and stops              |
| `attendance`    | Every RFID tap (boarded/exited)          |
| `registrations` | Pending student registrations            |
| `alerts`        | Committee announcements                  |
| `audit_log`     | Auth events for security audit           |

## Share Publicly (ngrok)

```bash
# Install ngrok from ngrok.com, then:
ngrok http 8000
# Share the generated URL with everyone
```

## Production Deployment

1. Change `JWT_SECRET` environment variable:
   ```bash
   export JWT_SECRET="your-very-secret-random-string-here"
   python server.py
   ```
2. Use nginx as reverse proxy with SSL (HTTPS required for phone GPS)
3. Run with: `uvicorn server:app --host 0.0.0.0 --port 8000 --workers 4`

> **Important:** Phone GPS only works on HTTPS in modern browsers.
> Use ngrok (it provides HTTPS) or a proper SSL certificate.
