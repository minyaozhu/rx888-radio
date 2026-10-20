import logging

import uvicorn

from .config import CONFIG

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("ka9q").setLevel(logging.WARNING)

uvicorn.run("rxapp.server:app", host=CONFIG.host, port=CONFIG.port, log_level="info")
