"""`python -m bido_back.api` starts the API with uvicorn."""

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "bido_back.api.app:create_app",
        factory=True,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )


if __name__ == "__main__":
    main()
