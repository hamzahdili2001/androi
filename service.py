"""Background foreground-service entry point (runs in its own process).

Started by main.py via android.AndroidService. The argument is the app's data
dir; the job (config incl. cookie) is read from job.json there. Talks back to the
UI through service.log (JSON lines) and status.json. Stop = create stop.flag.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402

DATA_DIR = (os.environ.get("PYTHON_SERVICE_ARGUMENT") or "").strip().strip('"')


def p(name: str) -> str:
    return os.path.join(DATA_DIR, name)


def emit(msg: str, level: str = "info") -> None:
    try:
        with open(p("service.log"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps([time.time(), level, msg]) + "\n")
    except OSError:
        pass


def set_status(state: str, **extra) -> None:
    try:
        tmp = p("status.json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"state": state, "pid": os.getpid(), "t": time.time(), **extra}, fh)
        os.replace(tmp, p("status.json"))
    except OSError:
        pass


# ---- Android helpers (all best-effort; absent on desktop) ----------------- #

def _ctx():
    from jnius import autoclass  # type: ignore
    return autoclass("org.kivy.android.PythonService").mService


def acquire_wakelock():
    try:
        from jnius import autoclass  # type: ignore
        ctx = _ctx()
        pm = ctx.getSystemService(autoclass("android.content.Context").POWER_SERVICE)
        wl = pm.newWakeLock(1, "busmed:catch")  # 1 = PARTIAL_WAKE_LOCK
        wl.acquire(14 * 3600 * 1000)            # safety timeout: 14h
        return wl
    except Exception:
        return None


def release_wakelock(wl) -> None:
    try:
        if wl is not None and wl.isHeld():
            wl.release()
    except Exception:
        pass


def vibrate(ms: int = 1500) -> None:
    try:
        ctx = _ctx()
        v = ctx.getSystemService("vibrator")
        v.vibrate(ms)
    except Exception:
        pass


def notify(title: str, text: str) -> None:
    try:
        from jnius import autoclass  # type: ignore
        ctx = _ctx()
        Context = autoclass("android.content.Context")
        nm = ctx.getSystemService(Context.NOTIFICATION_SERVICE)
        sdk = autoclass("android.os.Build$VERSION").SDK_INT
        Builder = autoclass("android.app.Notification$Builder")
        if sdk >= 26:
            Channel = autoclass("android.app.NotificationChannel")
            nm.createNotificationChannel(Channel("busmed_alerts", "BusMed alerts", 4))
            b = Builder(ctx, "busmed_alerts")
        else:
            b = Builder(ctx)
        b.setContentTitle(title).setContentText(text)
        b.setSmallIcon(ctx.getApplicationInfo().icon).setAutoCancel(True)
        nm.notify(int(time.time()) % 100000, b.build())
    except Exception:
        pass


def main() -> None:
    if not DATA_DIR:
        return
    emit("service started", "dim")
    set_status("running")
    wl = acquire_wakelock()
    result: dict = {"ok": False}
    try:
        with open(p("job.json"), encoding="utf-8") as fh:
            cfg = core.Config(**json.load(fh))
        client = core.Client(cfg)

        def on_alert(msg: str) -> None:
            emit(msg, "alert")
            vibrate()
            notify("BusMed", msg)

        def on_done(r: dict) -> None:
            result.update(r)

        catcher = core.Catcher(client, cfg, emit, on_done=on_done, on_alert=on_alert)

        def watch_stop() -> None:
            while not os.path.exists(p("stop.flag")):
                time.sleep(1)
            emit("stop requested", "warn")
            catcher.stop()

        threading.Thread(target=watch_stop, daemon=True).start()
        emit(f"catching {cfg.route_name or 'any route'} {cfg.departure_time}", "info")
        catcher.run()
    except Exception as exc:
        emit(f"service error: {exc.__class__.__name__}: {exc}", "err")
        result = {"ok": False, "error": str(exc)}
    finally:
        release_wakelock(wl)
        try:
            os.remove(p("job.json"))  # don't leave a second copy of the cookie around
        except OSError:
            pass
        if result.get("ok"):
            notify("BusMed: ticket booked",
                   f"seat {result.get('seat', '?')}  code {result.get('code', '?')}")
            vibrate(2500)
        set_status("done", ok=bool(result.get("ok")))
        emit("service finished", "dim")


main()
