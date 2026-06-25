import logging
import sys

from DepotManager.config import LOG_FILE
from DepotManager.gui import App

# Configure Logging for the modular application
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("DepotManager.Main")

if __name__ == "__main__":
    logger.info("Initializing modular DepotManager app...")
    app = App()
    app.mainloop()
