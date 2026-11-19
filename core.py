"""BusMed core: API client, route/time logic, booking sprint. No UI imports."""
from __future__ import annotations

import base64
import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import requests

DEFAULT_BASE_URL = "https://bus-med.1337.ma/api"
DEFAULT_ORIGIN = "https://bus-med.1337.ma"
DEFAULT_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
)


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #

@dataclass
class Config:
    base_url: str = DEFAULT_BASE_URL
    user_agent: str = DEFAULT_UA
    cookie: str = ""
    route_name: str = "Martil"
    route_id: int | None = None
    departure_time: str = "20:00"      # "HH:MM" as shown by the site (UTC)
    to_campus: bool = False
    times_are_utc: bool = True
    sprint_workers: int = 2
    sprint_interval: float = 0.05
    sprint_lead_seconds: float = 3.0
    sprint_max_seconds: float = 120.0
    poll_seconds: int = 30
    lead_minutes: list[int] = field(default_factory=lambda: [20, 10, 5])
    dry_run: bool = False


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

class ApiError(RuntimeError):
    def __init__(self, status: int, payload: Any, url: str = "") -> None:
        self.status = status
        self.payload = payload
        self.url = url
        msg = ""
        if isinstance(payload, dict):
            msg = str(payload.get("message") or payload.get("error") or "")
        elif payload:
            msg = str(payload)
        self.msg = msg[:300]
        super().__init__(f"HTTP {status}: {self.msg}" if self.msg else f"HTTP {status}")

    @property
    def message(self) -> str:
        return self.msg


def unwrap(payload: Any) -> Any:
    if isinstance(payload, dict):
        for key in ("data", "items", "results"):
            if key in payload:
                return payload[key]
    return payload


def as_list(payload: Any) -> list[dict]:
    data = unwrap(payload)
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


def clean_cookie(raw: str) -> str:
    """Accept what people actually paste: the bare value, 'cookie: a=b', or a
    multi-line blob. Collapse whitespace and drop a leading 'cookie:' label."""
    raw = " ".join((raw or "").split())
    raw = re.sub(r"^(?i:cookie)\s*:\s*", "", raw)
    return raw.strip().strip('"').strip("'")


def jwt_expiry(cookie: str) -> datetime | None:
    """Read (never verify) the exp claim of a JWT inside the cookie."""
    for blob in re.findall(r"[\w-]{8,}\.[\w-]{8,}\.[\w-]{4,}", cookie or ""):
        payload = blob.split(".")[1]
        try:
            data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        except (ValueError, json.JSONDecodeError):
            continue
        exp = data.get("exp") if isinstance(data, dict) else None
        if isinstance(exp, (int, float)):
            return datetime.fromtimestamp(exp, timezone.utc).astimezone()
    return None


class Client:
    """One requests.Session = one keep-alive connection. The sprint uses several."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.session = self.new_session()

    def new_session(self) -> requests.Session:
        s = requests.Session()
        cookie = self.cfg.cookie
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": self.cfg.user_agent,
            "Origin": DEFAULT_ORIGIN,
            "Referer": f"{DEFAULT_ORIGIN}/home",
        }
        # A pasted DevTools cookie is "a=b; c=d"; a bare JWT goes as a bearer token.
        if "=" in cookie:
            headers["Cookie"] = cookie
        elif cookie:
            headers["Authorization"] = f"Bearer {cookie}"
        s.headers.update(headers)
        return s

    @staticmethod
    def decode(resp: requests.Response, url: str) -> Any:
        text = resp.text or ""
        if resp.status_code == 403 and "Just a moment" in text:
            raise ApiError(403, {"message": "Cloudflare challenge. Open the site in your "
                                            "browser, then copy a fresh cookie."}, url)
        try:
            payload = json.loads(text) if text.strip() else None
        except json.JSONDecodeError:
            payload = text
        if not 200 <= resp.status_code < 300:
            raise ApiError(resp.status_code, payload, url)
        if isinstance(payload, dict) and int(payload.get("statusCode", 200) or 200) >= 400:
            raise ApiError(int(payload["statusCode"]), payload, url)
        return payload

    def call(self, method: str, path: str, body: Any = None,
             session: requests.Session | None = None, timeout: float = 15) -> Any:
        url = f"{self.cfg.base_url}{path}"
        s = session or self.session
        try:
            resp = s.request(method, url, json=body, timeout=timeout)
        except requests.RequestException as exc:
            raise ApiError(0, f"network error: {exc.__class__.__name__}", url) from exc
        return self.decode(resp, url)

    def get(self, path: str, **kw: Any) -> Any:
        return self.call("GET", path, **kw)

    def post(self, path: str, body: Any = None, **kw: Any) -> Any:
        return self.call("POST", path, body or {}, **kw)


# --------------------------------------------------------------------------- #
# time + route logic (ported 1:1)
# --------------------------------------------------------------------------- #

def parse_hhmm(value: str) -> tuple[int, int] | None:
    m = re.match(r"^\s*(\d{1,2}):(\d{2})", str(value or ""))
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    return (hh, mm) if 0 <= hh < 24 and 0 <= mm < 60 else None


def parse_iso(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def fmt_delta(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


@dataclass
class Route:
    raw: dict
    id: int
    name: str
    departure: str
    available: str
    capacity: int

    @classmethod
    def from_api(cls, d: dict) -> "Route":
        bus = d.get("bus") or {}
        rid = d.get("id")
        return cls(
            raw=d,
            id=int(rid) if rid is not None else 0,
            name=str(d.get("name") or "?").strip(),
            departure=str(d.get("departure_time") or "")[:5],
            available=str(d.get("available_time") or "")[:5],
            capacity=int(bus.get("capacity") or 0),
        )


def next_occurrence(hhmm: str, now: datetime, utc: bool = True) -> datetime | None:
    p = parse_hhmm(hhmm)
    if p is None:
        return None
    hh, mm = p
    if utc:
        utc_now = now.astimezone(timezone.utc)
        target = utc_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target < utc_now:
            target += timedelta(days=1)
        return target.astimezone(now.tzinfo)
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target < now:
        target += timedelta(days=1)
    return target


def combine_time(day: datetime, hhmm: str, utc: bool = True) -> datetime | None:
    p = parse_hhmm(hhmm)
    if p is None:
        return None
    hh, mm = p
    if utc:
        base = day.astimezone(timezone.utc)
        return base.replace(hour=hh, minute=mm, second=0, microsecond=0).astimezone(day.tzinfo)
    return day.replace(hour=hh, minute=mm, second=0, microsecond=0)


def last_occurrence(hhmm: str, now: datetime, utc: bool = True) -> datetime | None:
    stamp = combine_time(now, hhmm, utc)
    if stamp is None:
        return None
    return stamp - timedelta(days=1) if stamp > now else stamp


def departure_and_window(route: Route, cfg: Config, now: datetime,
                         live_dep: datetime | None = None
                         ) -> tuple[datetime | None, datetime | None]:
    dep_at = live_dep or next_occurrence(route.departure, now, cfg.times_are_utc)
    if dep_at is None:
        return None, None
    if live_dep is None:
        recent = last_occurrence(route.departure, now, cfg.times_are_utc)
        if (recent is not None and now - recent <= timedelta(minutes=5)
                and dep_at - recent > timedelta(hours=12)):
            dep_at = recent
    avail_at = combine_time(dep_at, route.available, cfg.times_are_utc)
    if avail_at is not None and avail_at > dep_at:
        avail_at -= timedelta(days=1)
    return dep_at, avail_at


def pick_route(routes: list[Route], cfg: Config, now: datetime,
               live_stamps: dict[int, datetime] | None = None) -> Route | None:
    live_stamps = live_stamps or {}

    def departure_of(r: Route) -> datetime | None:
        return live_stamps.get(r.id) or next_occurrence(r.departure, now, cfg.times_are_utc)

    cands = routes
    if cfg.route_id is not None:
        cands = [r for r in cands if r.id == cfg.route_id]
    if not cands:
        return None
    if cfg.route_name:
        needle = cfg.route_name.casefold()
        named = [r for r in cands if needle in r.name.casefold()]
        if not named and cfg.route_id is None:
            return None
        cands = named or cands
    if cfg.departure_time:
        wanted = cfg.departure_time[:5]
        exact = [r for r in cands if r.departure[:5] == wanted]
        if exact:
            cands = exact
        want = next_occurrence(cfg.departure_time, now, cfg.times_are_utc)
        if want is not None:
            cands = sorted(cands, key=lambda r: abs((departure_of(r) - want).total_seconds())
                           if departure_of(r) else float("inf"))
    else:
        cands = sorted(cands, key=lambda r: departure_of(r) or now)
    return cands[0] if cands else None


def load_routes(client: Client) -> tuple[list[Route], list[dict], dict[int, datetime]]:
    routes = [Route.from_api(d) for d in as_list(client.get("/departure/upcoming"))]
    try:
        current = as_list(client.get("/departure/current"))
    except ApiError:
        current = []
    seen = {r.id for r in routes}
    stamps: dict[int, datetime] = {}
    for dep in current:
        owner = dep.get("route") or {}
        stamp = parse_iso(dep.get("departure_time"))
        try:
            rid = int(owner.get("id"))
        except (TypeError, ValueError):
            continue
        if stamp is not None:
            stamps[rid] = stamp
        if rid not in seen and owner.get("name"):
            routes.append(Route.from_api(owner))
            seen.add(rid)
    return routes, current, stamps


def find_live_departure(current: list[dict], route: Route, now: datetime
                        ) -> tuple[dict, datetime] | None:
    best: tuple[dict, datetime] | None = None
    for dep in current:
        owner = dep.get("route") or {}
        try:
            rid = int(owner.get("id"))
        except (TypeError, ValueError):
            continue
        if rid != route.id:
            continue
        stamp = parse_iso(dep.get("departure_time")) or combine_time(
            now, str(dep.get("departure_time") or ""))
        if stamp is None or abs((stamp - now).total_seconds()) > 8 * 3600:
            continue
        if best is None or abs(stamp - now) < abs(best[1] - now):
            best = (dep, stamp)
    return best


# --------------------------------------------------------------------------- #
# the catcher
# --------------------------------------------------------------------------- #

class Catcher:
    """Sleep until the window, sprint on N keep-alive sessions, book, report.

    `log(msg, level)` is called from worker threads; the UI must marshal it.
    """

    def __init__(self, client: Client, cfg: Config,
                 log: Callable[[str, str], None],
                 on_done: Callable[[dict], None] | None = None,
                 on_alert: Callable[[str], None] | None = None) -> None:
        self.client = client
        self.cfg = cfg
        self.log = log
        self.on_done = on_done or (lambda r: None)
        self.on_alert = on_alert or (lambda m: None)
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()

    def _sleep(self, seconds: float) -> None:
        self.stop_event.wait(max(0.0, seconds))

    # -- main loop --------------------------------------------------------- #

    def run(self) -> None:
        result: dict = {"ok": False}
        try:
            result = self._run()
        except ApiError as exc:
            if exc.status in (401, 403):
                self.log(f"session rejected ({exc}). Paste a fresh cookie.", "err")
            else:
                self.log(f"error: {exc}", "err")
            result = {"ok": False, "error": str(exc)}
        except Exception as exc:  # never let the thread die silently
            self.log(f"unexpected error: {exc.__class__.__name__}: {exc}", "err")
            result = {"ok": False, "error": str(exc)}
        finally:
            self.on_done(result)

    def _run(self) -> dict:
        cfg = self.cfg
        exp = jwt_expiry(cfg.cookie)
        notified: set[int] = set()
        while not self.stop_event.is_set():
            now = datetime.now().astimezone()
            routes, current, stamps = load_routes(self.client)
            target = pick_route(routes, cfg, now, stamps)
            if target is None:
                self.log("no route matches your name/time. Check them with Status.", "err")
                return {"ok": False}
            live = find_live_departure(current, target, now)
            dep_at, window_at = departure_and_window(
                target, cfg, now, live[1] if live else None)
            if dep_at is None:
                self.log("could not work out the departure time", "err")
                return {"ok": False}
            if exp is not None and exp < dep_at:
                self.log(f"your cookie expires {exp:%H:%M}, before the bus ({dep_at:%H:%M}). "
                         f"Paste a fresher one.", "warn")
            if now > dep_at + timedelta(minutes=2) and live is None:
                self.log("that bus has already left.", "warn")
                return {"ok": False}

            for m in cfg.lead_minutes:
                if m not in notified and 0 < (dep_at - now).total_seconds() <= m * 60:
                    notified.add(m)
                    self.on_alert(f"bus in {m} min")

            to_window = (window_at - now).total_seconds() if window_at else 0.0
            if live is not None or to_window <= cfg.sprint_lead_seconds:
                return self._sprint(target, window_at)

            self.log(f"{target.name} {target.departure}: window opens in "
                     f"{fmt_delta(to_window)}", "info")
            # sleep precisely to the sprint start, never past it
            self._sleep(min(cfg.poll_seconds, to_window - cfg.sprint_lead_seconds))
        return {"ok": False, "stopped": True}

    # -- sprint ------------------------------------------------------------ #

    def _sprint(self, target: Route, window_at: datetime | None) -> dict:
        cfg = self.cfg
        deadline = time.monotonic() + cfg.sprint_max_seconds
        finished = threading.Event()
        claimed = threading.Event()
        out: dict[str, Any] = {"ok": False}
        workers = max(1, int(cfg.sprint_workers))
        sessions = [self.client.new_session() for _ in range(workers)]
        counter = {"n": 0}
        self.log(f"sprinting: {workers} connection(s), one request every "
                 f"{cfg.sprint_interval * 1000:.0f} ms each", "ok")

        def poll(session: requests.Session, stagger: float) -> None:
            if stagger:
                time.sleep(stagger)
            while (not self.stop_event.is_set() and not finished.is_set()
                   and time.monotonic() < deadline):
                try:
                    current = as_list(self.client.get("/departure/current", session=session,
                                                      timeout=10))
                except ApiError as exc:
                    if exc.status in (401, 403):
                        out["fatal"] = exc
                        finished.set()
                        return
                    time.sleep(cfg.sprint_interval)
                    continue
                counter["n"] += 1
                found = find_live_departure(current, target, datetime.now().astimezone())
                if found is None:
                    time.sleep(cfg.sprint_interval)
                    continue
                if claimed.is_set():
                    return
                claimed.set()
                self._book(session, found, target, deadline, out, finished)
                return

        threads = [threading.Thread(target=poll, args=(s, i * cfg.sprint_interval), daemon=True)
                   for i, s in enumerate(sessions)]
        for t in threads:
            t.start()
        last = 0.0
        while not finished.is_set() and not self.stop_event.is_set():
            if time.monotonic() >= deadline:
                break
            if time.monotonic() - last > 5:
                last = time.monotonic()
                self.log(f"  {counter['n']} requests so far", "dim")
            finished.wait(0.25)
        finished.set()
        for s in sessions:
            s.close()
        if "fatal" in out:
            raise out["fatal"]
        if not out.get("ok") and not self.stop_event.is_set():
            self.log(f"no seat after {cfg.sprint_max_seconds:g}s.", "warn")
        return out

    def _book(self, session: requests.Session, found: tuple[dict, datetime], target: Route,
              deadline: float, out: dict, finished: threading.Event) -> None:
        cfg = self.cfg
        dep, stamp = found
        dep_id = dep.get("id")
        taken = len(dep.get("tickets") or [])
        cap = int(((dep.get("route") or {}).get("bus") or {}).get("capacity") or 0)
        self.log(f"departure {dep_id} is live ({taken}/{cap} taken), booking", "ok")
        if cfg.dry_run:
            self.log("[dry-run] would POST /tickets/book", "warn")
            out.update(ok=True, dry_run=True)
            finished.set()
            return
        delay = cfg.sprint_interval
        first = True
        while not self.stop_event.is_set() and time.monotonic() < deadline:
            try:
                resp = self.client.post("/tickets/book",
                                        {"departure_id": dep_id, "to_campus": cfg.to_campus},
                                        session=session, timeout=10)
            except ApiError as exc:
                if exc.status in (401, 403):
                    out["fatal"] = exc
                    finished.set()
                    return
                if "already" in exc.message.lower():
                    self.log("you already hold a ticket for this bus.", "ok")
                    out.update(ok=True, existing=True)
                    finished.set()
                    return
                if first:
                    self.log(f"booking rejected ({exc.message or exc.status}), retrying", "warn")
                    first = False
            else:
                t = unwrap(resp) or {}
                seat = t.get("position", "?") if isinstance(t, dict) else "?"
                code = (t.get("hash") or t.get("code") or "?") if isinstance(t, dict) else "?"
                self.log(f"BOOKED  {target.name} {stamp:%H:%M}  seat #{seat}  code {code}", "ok")
                self.on_alert("ticket booked")
                out.update(ok=True, seat=seat, code=code, route=target.name)
                finished.set()
                return
            time.sleep(delay)
            delay = min(1.0, delay * 1.5)
        finished.set()


# --------------------------------------------------------------------------- #
# one-shot helpers for the UI buttons
# --------------------------------------------------------------------------- #

def status_lines(client: Client, cfg: Config) -> list[str]:
    now = datetime.now().astimezone()
    routes, current, stamps = load_routes(client)
    if not routes:
        return ["no routes returned"]
    out = []
    target = pick_route(routes, cfg, now, stamps)
    for r in sorted(routes, key=lambda r: (r.name, r.departure)):
        dep_at, win = departure_and_window(r, cfg, now, stamps.get(r.id))
        left = fmt_delta((dep_at - now).total_seconds()) if dep_at else "?"
        mark = ">" if target and r.id == target.id else " "
        line = f"{mark} {r.name} {r.departure}  in {left}"
        if win:
            line += f"  window {win:%H:%M}"
        if r.id in stamps:
            line += "  LIVE"
        out.append(line)
    return out


def ticket_lines(client: Client) -> list[str]:
    data = unwrap(client.get("/tickets/latest"))
    if not isinstance(data, dict) or not data:
        return ["no ticket on your account"]
    dep = data.get("departure") or {}
    route = dep.get("route") or {}
    stamp = parse_iso(dep.get("departure_time"))
    lines = [f"status: {data.get('status', '?')}   seat: {data.get('position', '?')}",
             f"code: {data.get('hash') or '?'}"]
    if stamp:
        lines.insert(0, f"{route.get('name', '?')} at {stamp:%Y-%m-%d %H:%M}")
    return lines
