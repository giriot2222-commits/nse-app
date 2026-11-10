NSE Day Trade Plan: Android Studio project (Kotlin + Chaquopy). Plans only. No orders. Not financial advice.

EASIEST (no installs): put this folder's contents in a free GitHub repository. GitHub builds the APK for you (Actions tab > latest run > Artifacts > nse-day-trade-apk).

Or build it yourself (on a PC or Mac):
1. Install Android Studio and Python 3 (Chaquopy needs a Python 3.8+ on the PATH at build time).
2. File > Open > this folder (nse-android-app). Let Gradle sync (it downloads Gradle, the Android SDK pieces and Chaquopy).
3. Phone: Settings > About phone > tap Build number 7 times > Developer options > USB debugging. Plug in, press Run.
   Or: Build > Build Bundle(s) / APK(s) > Build APK(s), copy app-debug.apk to the phone, open it (allow "install unknown apps").

First use: My limits (set your own rupee numbers, Save) > Build NSE list > Scan daily > Scan opening range > Plan SYMBOL.
Times are IST. NSE equity hours 9:15 AM to 3:30 PM IST. Entries until 14:45, flat by 15:15 (your limits).

The Python in app/src/main/python (nse.py, universe_in.py) is the pack's own code, unchanged. bridge.py is the only glue.
This project was written without compiling it (no Android SDK in the sandbox it was made in). Expect to fix a version
number or two on first sync. If a Chaquopy or Android Gradle Plugin version is rejected, pick the nearest supported pair
from chaquo.com/chaquopy/doc/current/versions.html.
