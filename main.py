"""BusMed for Android (Kivy). UI only; all logic lives in core.py."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime

from kivy.app import App
from kivy.clock import Clock
from kivy.core.clipboard import Clipboard
from kivy.core.window import Window
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.uix.switch import Switch
from kivy.uix.textinput import TextInput
from kivy.utils import escape_markup, platform

import core

BG = (0.10, 0.11, 0.17, 1)
FIELD = (0.14, 0.16, 0.23, 1)
TEXT = (0.75, 0.79, 0.96, 1)
COLORS = {"ok": "9ece6a", "warn": "e0af68", "err": "f7768e", "info": "c0caf5", "dim": "565f89"}

Window.clearcolor = BG
Window.softinput_mode = "below_target"


def keep_screen_on(on: bool) -> None:
    """Android: stop the screen (and with it the app) from sleeping mid-wait."""
    if platform != "android":
        return
    try:
        from android.runnables import run_on_ui_thread  # type: ignore
        from jnius import autoclass  # type: ignore

        @run_on_ui_thread
        def _apply() -> None:
            activity = autoclass("org.kivy.android.PythonActivity").mActivity
            flag = autoclass("android.view.WindowManager$LayoutParams").FLAG_KEEP_SCREEN_ON
            window = activity.getWindow()
            window.addFlags(flag) if on else window.clearFlags(flag)

        _apply()
    except Exception:
        pass


def vibrate(seconds: float = 1.5) -> None:
    try:
        from plyer import vibrator  # type: ignore
        vibrator.vibrate(time=seconds)
    except Exception:
        pass


def field(text: str = "", hint: str = "", **kw) -> TextInput:
    return TextInput(text=text, hint_text=hint, multiline=False, size_hint_y=None,
                     height=dp(44), background_color=FIELD, foreground_color=TEXT,
                     hint_text_color=(0.4, 0.43, 0.6, 1), cursor_color=TEXT,
                     padding=(dp(10), dp(12)), **kw)


def label(text: str, **kw) -> Label:
    lb = Label(text=text, size_hint_y=None, height=dp(24), color=TEXT, halign="left",
               valign="middle", **kw)
    lb.bind(size=lambda w, _s: setattr(w, "text_size", w.size))
    return lb


def button(text: str, cb, **kw) -> Button:
    b = Button(text=text, size_hint_y=None, height=dp(46), background_normal="",
               background_color=(0.24, 0.28, 0.45, 1), color=TEXT, **kw)
    b.bind(on_release=lambda *_: cb())
    return b


class BusMedApp(App):
    title = "BusMed"

    # ------------------------------------------------------------------ UI #

    def build(self):
        self.catcher: core.Catcher | None = None
        self.lines: list[str] = []

        root = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(6))

        form = BoxLayout(orientation="vertical", spacing=dp(6), size_hint_y=None)
        form.bind(minimum_height=form.setter("height"))

        form.add_widget(label("Session cookie (paste the cookie header value)"))
        self.cookie_in = field(hint="a=b; c=d  or the bare token", password=True)
        form.add_widget(self.cookie_in)

        row = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(6))
        row.add_widget(button("Paste", self.on_paste))
        self.show_btn = button("Show", self.on_toggle_show)
        row.add_widget(self.show_btn)
        row.add_widget(button("Save", self.on_save_cookie))
        row.add_widget(button("Clear", self.on_clear_cookie))
        form.add_widget(row)
        self.cookie_info = label("", markup=True)
        form.add_widget(self.cookie_info)

        row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        self.route_in = field(hint="Route (e.g. Martil)")
        self.time_in = field(hint="HH:MM (site clock, UTC)", input_filter=None)
        self.time_in.size_hint_x = 0.45
        row.add_widget(self.route_in)
        row.add_widget(self.time_in)
        form.add_widget(row)

        row = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(6))
        row.add_widget(label("Home -> campus (off = campus -> home)"))
        self.campus_sw = Switch(size_hint_x=None, width=dp(90))
        row.add_widget(self.campus_sw)
        form.add_widget(row)

        row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        self.workers_in = field(hint="connections (1-4)", input_filter="int")
        self.interval_in = field(hint="ms between requests (min 20)", input_filter="int")
        row.add_widget(self.workers_in)
        row.add_widget(self.interval_in)
        form.add_widget(row)

        row = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(6))
        row.add_widget(button("Status", self.on_status))
        row.add_widget(button("Ticket", self.on_ticket))
        self.catch_btn = button("Catch", self.on_catch)
        self.stop_btn = button("Stop", self.on_stop)
        self.stop_btn.disabled = True
        row.add_widget(self.catch_btn)
        row.add_widget(self.stop_btn)
        form.add_widget(row)

        form_scroll = ScrollView(size_hint_y=0.55, bar_width=dp(3))
        form_scroll.add_widget(form)
        root.add_widget(form_scroll)

        self.log_label = Label(text="", markup=True, size_hint_y=None, color=TEXT,
                               halign="left", valign="top", font_size="13sp")
        self.log_label.bind(
            width=lambda w, _x: setattr(w, "text_size", (w.width, None)),
            texture_size=lambda w, ts: setattr(w, "height", ts[1]))
        self.log_scroll = ScrollView(size_hint_y=0.45, bar_width=dp(3))
        self.log_scroll.add_widget(self.log_label)
        root.add_widget(self.log_scroll)

        self.load_all()
        self.log("Paste your cookie, tap Save, then Status to check it works.", "dim")
        return root

    # ------------------------------------------------------------ storage #

    @property
    def cookie_path(self) -> str:
        return os.path.join(self.user_data_dir, "cookie.txt")

    @property
    def settings_path(self) -> str:
        return os.path.join(self.user_data_dir, "settings.json")

    def load_all(self) -> None:
        s = {"route": "Martil", "time": "20:00", "to_campus": False,
             "workers": 2, "interval_ms": 50}
        try:
            with open(self.settings_path, encoding="utf-8") as fh:
                s.update(json.load(fh))
        except (OSError, ValueError):
            pass
        self.route_in.text = str(s["route"])
        self.time_in.text = str(s["time"])
        self.campus_sw.active = bool(s["to_campus"])
        self.workers_in.text = str(s["workers"])
        self.interval_in.text = str(s["interval_ms"])
        try:
            with open(self.cookie_path, encoding="utf-8") as fh:
                self.cookie_in.text = core.clean_cookie(fh.read())
        except OSError:
            pass
        self.refresh_cookie_info()

    def save_settings(self) -> None:
        data = {"route": self.route_in.text.strip(), "time": self.time_in.text.strip(),
                "to_campus": self.campus_sw.active,
                "workers": self.workers_in.text.strip() or "2",
                "interval_ms": self.interval_in.text.strip() or "50"}
        try:
            os.makedirs(self.user_data_dir, exist_ok=True)
            with open(self.settings_path, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
        except OSError:
            pass

    # ----------------------------------------------------------- cookie UI #

    def refresh_cookie_info(self) -> None:
        cookie = core.clean_cookie(self.cookie_in.text)
        if not cookie:
            self.cookie_info.text = "[color=565f89]no cookie saved[/color]"
            return
        exp = core.jwt_expiry(cookie)
        if exp is None:
            self.cookie_info.text = "[color=e0af68]cookie set (expiry unknown)[/color]"
        elif exp < datetime.now().astimezone():
            self.cookie_info.text = f"[color=f7768e]EXPIRED {exp:%d %b %H:%M}[/color]"
        else:
            self.cookie_info.text = f"[color=9ece6a]valid until {exp:%d %b %H:%M}[/color]"

    def on_paste(self) -> None:
        text = Clipboard.paste() or ""
        if not text.strip():
            self.log("clipboard is empty", "warn")
            return
        self.cookie_in.text = core.clean_cookie(text)
        self.refresh_cookie_info()
        self.log("pasted. Tap Save to keep it.", "info")

    def on_toggle_show(self) -> None:
        self.cookie_in.password = not self.cookie_in.password
        self.show_btn.text = "Show" if self.cookie_in.password else "Hide"

    def on_save_cookie(self) -> None:
        cookie = core.clean_cookie(self.cookie_in.text)
        if not cookie:
            self.log("nothing to save", "warn")
            return
        try:
            os.makedirs(self.user_data_dir, exist_ok=True)
            fd = os.open(self.cookie_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(cookie)
        except OSError as exc:
            self.log(f"could not save cookie: {exc}", "err")
            return
        self.cookie_in.text = cookie
        self.refresh_cookie_info()
        self.log("cookie saved (app-private storage)", "ok")

    def on_clear_cookie(self) -> None:
        self.cookie_in.text = ""
        try:
            os.remove(self.cookie_path)
        except OSError:
            pass
        self.refresh_cookie_info()
        self.log("cookie removed", "info")

    # --------------------------------------------------------------- log #

    def log(self, msg: str, level: str = "info") -> None:
        """Safe to call from any thread."""
        Clock.schedule_once(lambda _dt: self._append(msg, level))

    def _append(self, msg: str, level: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        color = COLORS.get(level, COLORS["info"])
        self.lines.append(f"[color=565f89]{stamp}[/color] [color={color}]"
                          f"{escape_markup(msg)}[/color]")
        self.lines = self.lines[-300:]
        self.log_label.text = "\n".join(self.lines)
        Clock.schedule_once(lambda _dt: setattr(self.log_scroll, "scroll_y", 0), 0.05)

    # ------------------------------------------------------------ actions #

    def build_cfg(self) -> core.Config | None:
        cookie = core.clean_cookie(self.cookie_in.text)
        if not cookie:
            self.log("paste your cookie first", "err")
            return None
        hhmm = self.time_in.text.strip()
        if hhmm and core.parse_hhmm(hhmm) is None:
            self.log("time must look like 20:00", "err")
            return None
        try:
            workers = min(4, max(1, int(self.workers_in.text or 2)))
            interval = max(20, int(self.interval_in.text or 50)) / 1000.0
        except ValueError:
            self.log("connections and interval must be numbers", "err")
            return None
        self.save_settings()
        return core.Config(cookie=cookie, route_name=self.route_in.text.strip(),
                           departure_time=hhmm, to_campus=self.campus_sw.active,
                           sprint_workers=workers, sprint_interval=interval)

    def _in_thread(self, fn) -> None:
        def runner():
            try:
                fn()
            except core.ApiError as exc:
                if exc.status in (401, 403):
                    self.log(f"{exc}. The cookie is probably expired: paste a fresh one.", "err")
                else:
                    self.log(str(exc), "err")
            except Exception as exc:
                self.log(f"{exc.__class__.__name__}: {exc}", "err")
        threading.Thread(target=runner, daemon=True).start()

    def on_status(self) -> None:
        cfg = self.build_cfg()
        if cfg is None:
            return
        self.refresh_cookie_info()

        def work():
            self.log("fetching routes...", "dim")
            for line in core.status_lines(core.Client(cfg), cfg):
                self.log(line, "ok" if line.startswith(">") else "info")
        self._in_thread(work)

    def on_ticket(self) -> None:
        cfg = self.build_cfg()
        if cfg is None:
            return

        def work():
            for line in core.ticket_lines(core.Client(cfg)):
                self.log(line, "ok")
        self._in_thread(work)

    def on_catch(self) -> None:
        if self.catcher is not None:
            return
        cfg = self.build_cfg()
        if cfg is None:
            return
        self.refresh_cookie_info()
        self.catcher = core.Catcher(
            core.Client(cfg), cfg, self.log,
            on_done=lambda r: Clock.schedule_once(lambda _dt: self._finished(r)),
            on_alert=lambda m: (self.log(m, "warn"), vibrate()))
        self.catch_btn.disabled = True
        self.stop_btn.disabled = False
        keep_screen_on(True)
        self.log(f"catching {cfg.route_name or 'any route'} {cfg.departure_time}. "
                 f"Keep the app open.", "info")
        threading.Thread(target=self.catcher.run, daemon=True).start()

    def on_stop(self) -> None:
        if self.catcher is not None:
            self.catcher.stop()
            self.log("stopping...", "warn")

    def _finished(self, result: dict) -> None:
        self.catcher = None
        self.catch_btn.disabled = False
        self.stop_btn.disabled = True
        keep_screen_on(False)
        if result.get("ok"):
            vibrate(2.0)
        self.log("done." if result.get("ok") else "stopped.", "dim")

    # ----------------------------------------------------------- lifecycle #

    def on_pause(self) -> bool:
        return True  # keep the process alive when the app is backgrounded

    def on_resume(self) -> None:
        self.refresh_cookie_info()


if __name__ == "__main__":
    BusMedApp().run()
