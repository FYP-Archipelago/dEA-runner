"""Run the Runner service: ``python -m service``, configured by the environment."""

import uvicorn

from orchestrator.settings import ServiceSettings

from .app import create_app


def main() -> None:
    uvicorn.run(create_app(ServiceSettings.from_env()), host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
