"""API process entrypoint."""

import uvicorn

from vlytics.api.app import app


def main() -> None:
    """Run the API server."""

    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
