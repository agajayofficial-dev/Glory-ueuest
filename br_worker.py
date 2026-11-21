#!/usr/bin/env python3
"""Minimal launcher for the compiled BR worker."""
import asyncio
import json
import os
import sys
os.environ.setdefault('PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION', 'python')
try:
    from midx_guard import verify
    verify()
    import br_worker_compiled
    payload = json.loads(sys.stdin.readline())
    success = asyncio.run(br_worker_compiled.run(payload))
    raise SystemExit(0 if success else 1)
except KeyboardInterrupt:
    raise SystemExit(130)
except Exception as exc:
    print(f'Match worker error: {type(exc).__name__}: {exc}', flush=True)
    raise SystemExit(1)
