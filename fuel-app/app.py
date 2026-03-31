from flask import Flask, render_template, jsonify, request
from urllib.parse import quote_plus
from datetime import datetime
import os
import re
import math
import requests
from bs4 import BeautifulSoup
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)

basedir = os.path.abspath(os.path.dirname(__file__))
instance_dir = os.path.join(basedir, "instance")
os.makedirs(instance_dir, exist_ok=True)

app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(instance_dir, "app.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}

GEOCODE_CACHE = {}

TRANSLATIONS = {
    "ru": {
        "title": "Агрегатор цен топлива в Варшаве",
        "nearest": "📍 Ближайшие",
        "by_price": "💰 По цене",
        "best": "⭐ Лучший вариант",
        "route": "🚗 Проложить маршрут",
        "loading_location": "Определяю местоположение...",
        "no_geo": "Геолокация не поддерживается",
        "allow_geo": "Разрешите доступ к геолокации",
        "nearest_stations": "Ближайшие станции",
        "price_sorted": "Сортировка по цене",
        "best_searching": "Ищу лучший вариант...",
        "best_results": "Лучшие варианты (цена + расстояние)",
        "distance_unknown": "расстояние не найдено",
        "km_suffix": "км от вас",
        "score_prefix": "score",
    },
    "en": {
        "title": "Fuel price aggregator in Warsaw",
        "nearest": "📍 Nearest",
        "by_price": "💰 By price",
        "best": "⭐ Best option",
        "route": "🚗 Route",
        "loading_location": "Getting your location...",
        "no_geo": "Geolocation is not supported",
        "allow_geo": "Allow location access",
        "nearest_stations": "Nearest stations",
        "price_sorted": "Sorted by price",
        "best_searching": "Searching best option...",
        "best_results": "Best options (price + distance)",
        "distance_unknown": "distance unknown",
        "km_suffix": "km from you",
        "score_prefix": "score",
    },
    "pl": {
        "title": "Agregator cen paliwa w Warszawie",
        "nearest": "📍 Najbliższe",
        "by_price": "💰 Po cenie",
        "best": "⭐ Najlepsza opcja",
        "route": "🚗 Wyznacz trasę",
        "loading_location": "Pobieram lokalizację...",
        "no_geo": "Geolokalizacja nie jest obsługiwana",
        "allow_geo": "Trzeba zezwolić na lokalizację",
        "nearest_stations": "Najbliższe stacje",
        "price_sorted": "Sortowanie po cenie",
        "best_searching": "Szukam najlepszej opcji...",
        "best_results": "Najlepsze opcje (cena + odległość)",
        "distance_unknown": "odległość nieznana",
        "km_suffix": "km od Ciebie",
        "score_prefix": "score",
    },
}


class Source(db.Model):
    __tablename__ = "sources"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    base_url = db.Column(db.String(255), nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)


class FuelType(db.Model):
    __tablename__ = "fuel_types"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), nullable=False, unique=True)
    name_ru = db.Column(db.String(50), nullable=False)
    name_pl = db.Column(db.String(50), nullable=False)
    name_en = db.Column(db.String(50), nullable=False)


class Station(db.Model):
    __tablename__ = "stations"
    __table_args__ = (
        db.UniqueConstraint("name_norm", "address_norm", name="uq_station_norm"),
    )

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    name_norm = db.Column(db.String(150), nullable=False, index=True)

    brand = db.Column(db.String(100), nullable=True)
    city = db.Column(db.String(100), nullable=True)

    address = db.Column(db.String(255), nullable=False)
    address_norm = db.Column(db.String(255), nullable=False, index=True)

    lat = db.Column(db.Float, nullable=True)
    lon = db.Column(db.Float, nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class PriceSnapshot(db.Model):
    __tablename__ = "price_snapshots"

    id = db.Column(db.Integer, primary_key=True)
    station_id = db.Column(db.Integer, db.ForeignKey("stations.id"), nullable=False)
    fuel_type_id = db.Column(db.Integer, db.ForeignKey("fuel_types.id"), nullable=False)
    source_id = db.Column(db.Integer, db.ForeignKey("sources.id"), nullable=False)
    price = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(3), default="PLN", nullable=False)
    collected_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


def get_lang():
    lang = request.args.get("lang", "ru").lower()
    if lang not in TRANSLATIONS:
        lang = "ru"
    return lang


def clean_text(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_station_name(text: str) -> str:
    text = clean_text(text).lower()
    text = re.sub(r"[^\w\sąćęłńóśźż0-9]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_station_address(text: str) -> str:
    text = clean_text(text).lower()
    text = re.sub(r"(warszawa)([a-ząćęłńóśźż])", r"\1 \2", text)
    text = text.split("/")[0]
    text = re.sub(r"[^\w\sąćęłńóśźż0-9]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_station_key(name: str, address: str) -> tuple[str, str]:
    return normalize_station_name(name), normalize_station_address(address)


def parse_place(raw_place: str) -> tuple[str, str]:
    raw_place = clean_text(raw_place)
    raw_place = re.sub(r"(Warszawa)([A-ZĄĆĘŁŃÓŚŹŻ])", r"\1 \2", raw_place)

    if "Warszawa" in raw_place:
        before, after = raw_place.split("Warszawa", 1)
        name = clean_text(before)
        after = after.split("/")[0]
        address = clean_text("Warszawa " + after)
        if not address or address == "Warszawa":
            address = raw_place
    else:
        name = raw_place
        address = raw_place

    return name, address


def parse_cenapaliw():
    url = "https://cenapaliw.pl/stationer/e95/mazowieckie/warszawa"
    response = requests.get(url, headers=HEADERS, timeout=20)
    soup = BeautifulSoup(response.text, "html.parser")

    stations = []
    rows = soup.find_all("tr")

    for row in rows[1:]:
        cols = row.find_all("td")
        if len(cols) == 2:
            place_raw = cols[0].text.strip()
            price_text = cols[1].text.strip()

            match = re.search(r"(\d+,\d+)", price_text)
            if match:
                price = float(match.group(1).replace(",", "."))
                name, address = parse_place(place_raw)

                stations.append({
                    "name": name,
                    "address": address,
                    "price": price,
                    "source": "cenapaliw.pl"
                })

    return stations


def google_maps_link(address: str, origin: str | None = None) -> str:
    destination = f"{address}, Poland"
    if origin:
        return (
            "https://www.google.com/maps/dir/?api=1"
            f"&origin={quote_plus(origin)}"
            f"&destination={quote_plus(destination)}"
            "&travelmode=driving"
        )
    return (
        "https://www.google.com/maps/dir/?api=1"
        f"&destination={quote_plus(destination)}"
        "&travelmode=driving"
    )


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(a))


def geocode_address(address: str):
    if address in GEOCODE_CACHE:
        return GEOCODE_CACHE[address]

    queries = [
        f"{address}, Warszawa, Poland",
        f"{address}, Poland",
        address,
    ]

    for q in queries:
        try:
            response = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={
                    "q": q,
                    "format": "jsonv2",
                    "limit": 1
                },
                headers={"User-Agent": "fuel-app/1.0"},
                timeout=10
            )
            response.raise_for_status()
            data = response.json()

            if data:
                lat = float(data[0]["lat"])
                lon = float(data[0]["lon"])
                GEOCODE_CACHE[address] = (lat, lon)
                return lat, lon
        except Exception:
            continue

    GEOCODE_CACHE[address] = None
    return None


def seed_db():
    if not Source.query.filter_by(name="cenapaliw.pl").first():
        db.session.add(Source(
            name="cenapaliw.pl",
            base_url="https://cenapaliw.pl"
        ))

    if not FuelType.query.filter_by(code="95").first():
        db.session.add(FuelType(
            code="95",
            name_ru="АИ-95",
            name_pl="PB95",
            name_en="Unleaded 95"
        ))

    db.session.commit()


def sync_cenapaliw_to_db():
    source = Source.query.filter_by(name="cenapaliw.pl").first()
    fuel = FuelType.query.filter_by(code="95").first()
    parsed = parse_cenapaliw()

    unique_items = {}
    for item in parsed:
        name_norm, address_norm = normalize_station_key(item["name"], item["address"])
        key = (name_norm, address_norm)

        if key not in unique_items or item["price"] < unique_items[key]["price"]:
            unique_items[key] = {
                **item,
                "name_norm": name_norm,
                "address_norm": address_norm,
            }

    saved = 0

    for item in unique_items.values():
        station = Station.query.filter_by(
            name_norm=item["name_norm"],
            address_norm=item["address_norm"]
        ).first()

        if not station:
            station = Station(
                name=item["name"],
                name_norm=item["name_norm"],
                address=item["address"],
                address_norm=item["address_norm"],
                city="Warszawa",
                is_active=True
            )
            db.session.add(station)
            db.session.flush()
        else:
            station.name = item["name"]
            station.address = item["address"]
            station.updated_at = datetime.utcnow()

        coords = geocode_address(item["address"])
        if coords:
            station.lat, station.lon = coords

        latest = (
            PriceSnapshot.query
            .filter_by(station_id=station.id, fuel_type_id=fuel.id, source_id=source.id)
            .order_by(PriceSnapshot.collected_at.desc())
            .first()
        )

        if not latest or abs(latest.price - item["price"]) > 0.0001:
            db.session.add(PriceSnapshot(
                station_id=station.id,
                fuel_type_id=fuel.id,
                source_id=source.id,
                price=item["price"],
                currency="PLN",
                collected_at=datetime.utcnow()
            ))
            saved += 1

    db.session.commit()
    return saved


def get_latest_price_for_station(station_id: int):
    return (
        PriceSnapshot.query
        .filter_by(station_id=station_id)
        .order_by(PriceSnapshot.collected_at.desc())
        .first()
    )


def build_stations(user_lat=None, user_lon=None):
    stations = Station.query.filter_by(is_active=True).all()
    data = []

    for station in stations:
        snap = get_latest_price_for_station(station.id)
        if not snap:
            continue

        distance_km = None
        if user_lat is not None and user_lon is not None and station.lat is not None and station.lon is not None:
            distance_km = round(haversine_km(user_lat, user_lon, station.lat, station.lon), 1)

        data.append({
            "id": station.id,
            "name": station.name,
            "address": station.address,
            "price": snap.price,
            "source": "cenapaliw.pl",
            "distance_km": distance_km,
            "maps_url": google_maps_link(
                station.address,
                origin=f"{user_lat},{user_lon}" if user_lat is not None and user_lon is not None else None
            )
        })

    if user_lat is not None and user_lon is not None:
        data.sort(key=lambda x: (
            x["distance_km"] is None,
            x["distance_km"] if x["distance_km"] is not None else 999999,
            x["price"]
        ))
    else:
        data.sort(key=lambda x: x["price"])

    return data[:10]


def compute_score(station):
    price = station.get("price", 999)
    dist = station.get("distance_km")
    if dist is None:
        dist = 999
    return price + dist * 0.1


@app.route("/")
def index():
    lang = get_lang()
    stations = build_stations()
    return render_template(
        "index.html",
        stations=stations,
        lang=lang,
        t=TRANSLATIONS[lang]
    )


@app.route("/sync")
def sync():
    saved = sync_cenapaliw_to_db()
    return f"OK. Saved snapshots: {saved}"


@app.route("/version")
def version():
    return "v1-sync"


@app.route("/api/stations")
def api_stations():
    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)
    stations = build_stations(lat, lon)
    return jsonify(stations)


@app.route("/api/best")
def api_best():
    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)

    stations = build_stations(lat, lon)

    for s in stations:
        s["score"] = compute_score(s)

    stations.sort(key=lambda x: x["score"])
    return jsonify(stations[:3])


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        seed_db()

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)