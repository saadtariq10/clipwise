"""`python -m app`: start the server and open the browser."""
import threading
import webbrowser

import uvicorn

from .config import HOST, PORT, VERSION


def main() -> None:
    from . import logs

    logs.setup()
    from .main import app

    url = f"http://{HOST}:{PORT}"
    print(f"\n  Clipwise {VERSION} is running at {url}\n  Keep this window open. Press Ctrl+C to stop.\n")
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
