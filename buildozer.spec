[app]
title = BusMed
package.name = busmed
package.domain = ma.hamza
source.dir = .
source.include_exts = py
version = 0.1.0

requirements = python3,kivy==2.3.0,cython==0.29.34,requests,certifi,plyer

orientation = portrait
fullscreen = 0

# INTERNET: without it every request fails silently
# VIBRATE: alert when the ticket is booked
# WAKE_LOCK: lets the keep-screen-on flag work reliably
android.permissions = INTERNET,VIBRATE,WAKE_LOCK
android.api = 34
android.minapi = 24
android.archs = arm64-v8a
android.accept_sdk_license = True

[buildozer]
log_level = 2
warn_on_root = 1
p4a.branch = develop
