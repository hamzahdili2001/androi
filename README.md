# BusMed (Android)

Kivy port of busmed.py. `core.py` = logic (tested against tools/mock_api.py), `main.py` = UI.

## Build (Linux or WSL2 Ubuntu)
    sudo apt install -y git zip unzip openjdk-17-jdk python3-pip autoconf libtool pkg-config \
        zlib1g-dev libncurses-dev cmake libffi-dev libssl-dev
    pip install --user buildozer cython
    cd busmed_android
    buildozer android debug          # first run downloads SDK/NDK, ~20-40 min
    # APK ends up in ./bin/ ; install with: adb install -r bin/*.apk
    # (or copy the APK to the phone and open it)

## Use
1. Log in at https://bus-med.1337.ma in the phone browser, copy the `cookie:` header value
   (or desktop DevTools -> send it to yourself).
2. Paste -> Save. The green line shows when the 12h session expires.
3. Status to check routes, Catch to wait for the window and book. Keep the app open and the screen on.

The cookie is stored only in the app's private storage and is sent only to bus-med.1337.ma.

## Background mode
Catch starts `service.py` as an Android foreground service (persistent notification + CPU wake
lock), so it keeps running with the screen off or the app closed, until the ticket is booked,
the bus leaves, you tap Stop, or the cookie is rejected. Tap "Battery settings" once and set
BusMed to Unrestricted. On Xiaomi/Oppo/Samsung also allow autostart / lock the app in Recents.
