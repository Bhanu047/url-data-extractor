"""Start the app and open its local UI in the default browser."""
import sys

if sys.version_info < (3, 11):
    raise SystemExit('Python 3.11 or newer is required.')

import threading
import webbrowser
from app import make_server


if __name__ == '__main__':
    try:
        server = make_server()
    except OSError as exc:
        raise SystemExit(f'Could not start: {exc}. Close an existing instance or run app.py --port 9000.')
    address = f'http://127.0.0.1:{server.server_port}'
    print(f'URL Data Extractor is running at {address}')
    print('Leave this window open. Press Ctrl+C to stop.')
    threading.Timer(0.5, lambda: webbrowser.open(address)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
