"""Entry point: python -m hub"""
import uvicorn

from . import config, store
from .app import create_app


def main() -> None:
    config.check()
    store.load()
    uvicorn.run(create_app(), host="0.0.0.0", port=config.HUB_PORT, log_level="info",
                proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()
