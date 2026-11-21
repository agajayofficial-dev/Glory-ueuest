#!/usr/bin/env python3
"""
MIDx launcher.

  python app.py                 -> website dashboard (port 8080) + AUTO: menu 3 (Start matches),
                                   accounts = all, matches = 100 ... aur phir se, loop mein
  MIDX_MANUAL=1 python app.py   -> purana terminal menu (koi dashboard nahi)

Compiled modules (.so), manifest.json aur midx_guard.py ko yahan chhua nahi gaya.
"""
import os
os.environ.setdefault('PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION', 'python')

if os.environ.get('MIDX_MANUAL') == '1':
    from midx_terminal import run
    run()
else:
    import web_panel
    web_panel.main()
