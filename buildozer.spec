[app]
title = BusMed
package.name = busmed
package.domain = ma.hamza
source.dir = .
source.include_exts = py
version = 0.1.0

requirements = python3,kivy==2.3.0,requests,urllib3,idna,charset-normalizer,certifi,plyer

orientation = portrait
fullscreen = 0

# background worker (keeps running with the screen off / app closed)
services = catcher:service.py:foreground

android.permissions = INTERNET,VIBRATE,WAKE_LOCK,FOREGROUND_SERVICE,POST_NOTIFICATIONS
android.api = 33
android.minapi = 24
android.archs = arm64-v8a
android.accept_sdk_license = True
android.ndk = 25b

# pin python-for-android to a stable release (the default now builds Python 3.14)
p4a.branch = v2024.01.21

[buildozer]
log_level = 1
warn_on_root = 1
