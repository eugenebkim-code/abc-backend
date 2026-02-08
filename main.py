from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from typing import Dict, List
import time
import random

# =========================
# CONFIG
# =========================

GOOGLE_SHEET_ID = "1inQlIqBzCl6aFlLQEgyZ_HGFV7F9AdVyVelEM9xjC-E"
SERVICE_ACCOUNT_FILE = "service_account.json"
PHOTOS_ROOT_FOLDER_ID = "1ZweBXYMDAfFB_DtTtAhIsSy2DQnipCOu"
HERO_FOLDER_ID = "1gaPqjlItG0YZcKt78CBKIhOXenjrRSW6"
CACHE_TTL_CARS = 300
CACHE_TTL_META = 1800

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# =========================
# APP
# =========================

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
# GOOGLE CLIENTS
# =========================

import json
import os

service_account_info = json.loads(
    os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
)

credentials = Credentials.from_service_account_info(
    service_account_info,
    scopes=SCOPES,
)

sheets = build("sheets", "v4", credentials=credentials)
drive = build("drive", "v3", credentials=credentials)

# =========================
# CACHE
# =========================

_cache: Dict[str, Dict] = {}


def get_cached(key: str, ttl: int):
    item = _cache.get(key)
    if not item:
        return None
    if time.time() - item["ts"] > ttl:
        return None
    return item["data"]


def set_cache(key: str, data):
    _cache[key] = {
        "data": data,
        "ts": time.time(),
    }

# =========================
# HELPERS
# =========================

def read_sheet(sheet_name: str) -> List[Dict]:
    res = sheets.spreadsheets().values().get(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=sheet_name,
    ).execute()

    values = res.get("values", [])
    if not values:
        return []

    headers = values[0]
    rows = values[1:]

    data = []
    for row in rows:
        item = {}
        for i, h in enumerate(headers):
            item[h] = row[i] if i < len(row) else ""
        data.append(item)

    return data


def parse_int(v):
    try:
        return int(v)
    except Exception:
        return None


def parse_float(v):
    if not v:
        return None
    try:
        return float(str(v).replace("$", "").replace(",", "").strip())
    except Exception:
        return None

# =========================
# DRIVE
# =========================

def load_photos(folder_id: str) -> List[Dict]:
    if not folder_id:
        return []

    res = drive.files().list(
        q=f"'{folder_id}' in parents and trashed = false",
        fields="files(id, name)",
        orderBy="name",
    ).execute()

    photos = []

    for f in res.get("files", []):
        name = f["name"].lower()
        if not (
            name.endswith(".jpg")
            or name.endswith(".jpeg")
            or name.endswith(".png")
        ):
            continue

        photos.append({
            "id": f["id"],
            "url": f"https://lh3.googleusercontent.com/d/{f['id']}=w1200",
        })

    return photos

def load_hero_image() -> str | None:
    try:
        photos = load_photos(HERO_FOLDER_ID)
        return photos[0]["url"] if photos else None
    except Exception as e:
        print("HERO LOAD ERROR:", e)
        return None

# =========================
# LOADERS
# =========================

def load_profile():
    cached = get_cached("profile", CACHE_TTL_META)
    if cached:
        return cached

    rows = read_sheet("profile")
    profile = rows[0] if rows else {}

    profile["hero_image"] = load_hero_image()

    set_cache("profile", profile)
    return profile

def load_cars():
    cached = get_cached("cars", CACHE_TTL_CARS)
    if cached:
        return cached

    rows = read_sheet("cars")
    cars = []

    for r in rows:
        car_id = (r.get("id") or "").strip()
        if not car_id:
            continue

        if r.get("status") == "hidden":
            continue

        cars.append({
            "id": car_id,
            "brand": r.get("brand", "").strip(),
            "model": r.get("model", "").strip(),
            "year": parse_int(r.get("year")),
            "price_usd": parse_float(r.get("price_usd")),
            "price_krw": parse_float(r.get("price_krw")),
            "mileage_km": parse_int(r.get("mileage_km")),
            "engine": r.get("engine", ""),
            "transmission": r.get("transmission", ""),
            "fuel": r.get("fuel", ""),
            "description": r.get("description", ""),
            "status": r.get("status", "active"),
            "photos_folder_id": r.get("photos_folder_id", "").strip(),
        })

    random.shuffle(cars)
    set_cache("cars", cars)
    return cars

# =========================
# API
# =========================

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/api/profile")
def api_profile():
    return load_profile()


from fastapi import Request

@app.get("/api/cars")
def api_cars(request: Request):
    append_user_event(request)

    cars = load_cars()
    result = []

    for c in cars:
        car = dict(c)  # ← ВАЖНО: копия

        photos = load_photos(car.get("photos_folder_id"))
        car["cover_image"] = photos[0]["url"] if photos else None
        car.pop("photos_folder_id", None)

        result.append(car)

    return result


@app.get("/api/cars/{car_id}")
def api_car_detail(car_id: str, request: Request):
    append_user_event(request, car_id=car_id)

    cars = load_cars()
    base = next((c for c in cars if c["id"] == car_id), None)

    if not base:
        raise HTTPException(status_code=404, detail="Car not found")

    car = dict(base)  # ← копия

    photos = load_photos(car.get("photos_folder_id"))
    car["photos"] = [p["url"] for p in photos]
    car["cover_image"] = car["photos"][0] if car["photos"] else None
    car.pop("photos_folder_id", None)

    return car


# =========================
# USERS
# =========================

from datetime import datetime
from fastapi import Request

USERS_SHEET = "users"


def append_user_event(
    request: Request,
    car_id: str | None = None,
):
    try:
        ts = datetime.utcnow().isoformat()
        ip = request.client.host if request.client else ""
        ua = request.headers.get("user-agent", "")
        path = request.url.path

        row = [
            ts,
            ip,
            ua,
            path,
            car_id or "",
        ]

        sheets.spreadsheets().values().append(
            spreadsheetId=GOOGLE_SHEET_ID,
            range=f"{USERS_SHEET}!A:E",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={
                "values": [row]
            },
        ).execute()

    except Exception as e:
        # ❗ логирование не должно ломать API
        print("USER LOG ERROR:", e)

