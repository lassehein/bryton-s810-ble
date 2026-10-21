"""Wind and temperature for a ride from the Open-Meteo forecast (free, no key; https://open-meteo.com).

Only the forecast is used (today up to about two weeks ahead). The 10 m wind is scaled down to rider height with a
fixed factor, which is a rough estimate. Needs internet access on the computer that runs the MCP server.
"""
import json
import math
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

API = "https://api.open-meteo.com/v1/forecast"
WIND_AT_RIDER = 0.7  # rider height (~1.5 m) vs the 10 m forecast wind, open terrain (estimate)


class WeatherError(ValueError):
    pass


def _get(url, timeout=15):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def fetch_weather(lat, lon, day, start_time="10:00", hours=3.0, fetch=_get, today=None):
    """Mean wind (vector mean over the ride) and temperature for the hours of the ride.

    day: YYYY-MM-DD, start_time: HH:MM local time at the route, hours: expected ride duration.
    -> {"wind_ms", "wind_from_deg", "temp_c", "gust_ms", "source"}; wind_ms is already scaled to rider height.
    """
    try:
        d = date.fromisoformat(day)
        start = datetime.strptime(start_time, "%H:%M")
    except ValueError:
        raise WeatherError("ride_date must be YYYY-MM-DD and start_time HH:MM") from None
    today = today or date.today()
    if not today <= d <= today + timedelta(days=15):
        raise WeatherError("the forecast only covers today until about 15 days ahead: give wind_kmh, wind_from_deg and "
                           "temperature_c yourself for other days")
    end_day = d + timedelta(days=1 if start.hour + hours >= 24 else 0)
    query = urllib.parse.urlencode({
        "latitude": f"{lat:.3f}", "longitude": f"{lon:.3f}", "timezone": "auto", "wind_speed_unit": "ms",
        "hourly": "temperature_2m,wind_speed_10m,wind_direction_10m,wind_gusts_10m",
        "start_date": d.isoformat(), "end_date": end_day.isoformat()})
    try:
        h = fetch(f"{API}?{query}")["hourly"]
        times = [datetime.fromisoformat(t) for t in h["time"]]
    except (OSError, KeyError, ValueError) as e:
        raise WeatherError(f"weather request failed ({type(e).__name__}: {e})") from None
    t0 = datetime.combine(d, start.time())
    t1 = t0 + timedelta(hours=max(hours, 0.5))
    idx = [i for i, t in enumerate(times) if t0 - timedelta(minutes=30) <= t < t1 + timedelta(minutes=30)]
    if not idx:
        raise WeatherError("the forecast has no hours for that time")
    u = sum(-h["wind_speed_10m"][i] * math.sin(math.radians(h["wind_direction_10m"][i])) for i in idx) / len(idx)
    v = sum(-h["wind_speed_10m"][i] * math.cos(math.radians(h["wind_direction_10m"][i])) for i in idx) / len(idx)
    speed = math.hypot(u, v)
    from_deg = (math.degrees(math.atan2(-u, -v)) % 360) if speed > 1e-6 else 0.0
    return {"wind_ms": round(speed * WIND_AT_RIDER, 2), "wind_from_deg": round(from_deg),
            "temp_c": round(sum(h["temperature_2m"][i] for i in idx) / len(idx), 1),
            "gust_ms": round(max(h["wind_gusts_10m"][i] for i in idx) * WIND_AT_RIDER, 1),
            "source": "Open-Meteo forecast"}
