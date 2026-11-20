[app]
title = BusMed
package.name = busmed
package.domain = ma.hamza
source.dir = .
source.include_exts = py
version = 0.1.0

requirements = python3,kivy==2.3.0,requests,urllib3,idna,charset-normalizer,certifi,plyer

orientation = portrait

# background worker (keeps running with the screen off / app closed)
services = catcher:service.py:foreground
fullscreen = 0

# INTERNET: without it every request fails silently
# VIBRATE: alert when the ticket is booked
# WAKE_LOCK: lets the keep-screen-on flag work reliably
android.permissions = INTERNET,VIBRATE,WAKE_LOCK,FOREGROUND_SERVICE,POST_NOTIFICATIONS
android.api = 33
android.minapi = 24
android.archs = arm64-v8a
android.accept_sdk_license = True

[buildozer]
log_level = 2
warn_on_root = 1
