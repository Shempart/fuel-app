from flask import Flask, render_template, jsonify, request
from urllib.parse import quote_plus
import os
import re
import math
import requests
from bs4 import BeautifulSoup

app = Flask(__name__)

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
        "language": "Язык",
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
        "language": "Language",
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
        "language": "Język",
    },
}


def get_lang():
    lang = request.args.get("lang", "ru").lower()
    if lang not in TRANSLATIONS:
        lang = "ru"
    return lang


def t():
    return TRANSLATIONS[get_lang()]


def clean_text(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


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


def build_stations(user_lat=None, user_lon=None):
    stations = parse_cenapaliw()

    for station in stations:
        station["maps_url"] = google_maps_link(
            station["address"],
            origin=f"{user_lat},{user_lon}" if user_lat is not None and user_lon is not None else None
        )

        if user_lat is not None and user_lon is not None:
            coords = geocode_address(station["address"])
            if coords:
                station["distance_km"] = round(
                    haversine_km(user_lat, user_lon, coords[0], coords[1]),
                    1
                )
            else:
                station["distance_km"] = None
        else:
            station["distance_km"] = None

    if user_lat is not None and user_lon is not None:
        stations.sort(key=lambda x: (
            x["distance_km"] is None,
            x["distance_km"] if x["distance_km"] is not None else 999999,
            x["price"]
        ))
    else:
        stations.sort(key=lambda x: x["price"])

    return stations[:10]


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
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)