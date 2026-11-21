MIDx + Website Dashboard (Termux)
=================================
Chalane ke liye (Termux ke ~/ folder mein):
    python app.py
Phir browser mein kholo:  http://127.0.0.1:8080

Kya hota hai
- app.py chalate hi MIDx terminal ek andar-wale terminal mein start hota hai.
- AUTOPILOT apne aap jawab deta hai:  main menu -> 3 (Start matches), accounts -> all, matches -> 100.
  Match poore hone par menu wapas aata hai to phir 3 ... (loop). Band ho jaye to 15s baad restart.
- Website par live logs, uptime, rounds/matches/errors, "glory" wali lines, Start(3)/Restart/Stop/Autopilot buttons
  aur ek box jisse tum terminal ko haath se jawab bhej sakte ho.

Settings (env):
    PORT=9000                    port badlo (default 8080)
    MIDX_MATCHES=100             matches ki ginti
    MIDX_HOST=0.0.0.0            LAN/doosre device se kholne ke liye (PANEL_KEY=kuch lagao)
    MIDX_AUTOPILOT=0             autopilot band
    MIDX_MANUAL=1 python app.py  purana terminal menu (dashboard ke bina)
Agar autopilot galat jawab de to website ke logs mein sawal dikhega - autopilot.json.example copy karke
autopilot.json banao aur regex badlo.

Dhyan do
- Compiled .so modules sirf Termux (Android ARM) par chalte hain - Render/PC par nahi chalenge.
- .so, manifest.json aur midx_guard.py ko chhua nahi gaya.
- accs.txt mein account tokens hote hain - kisi ko mat do / GitHub par mat daalo.
