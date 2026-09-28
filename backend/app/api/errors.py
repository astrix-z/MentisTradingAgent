"""Request validation errors as strict JSON (plan §4.3.5, R79).

FastAPI's default handler passes ``exc.errors()`` to a JSON writer that
refuses non-finite floats, so a body carrying ``NaN`` (Python's JSON parser
accepts it) in ``symbols`` or as the command ``payload`` turned into ``500``.
The same ``plain_errors`` the strategy API uses makes it ``422``.
"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.strategy_catalog import plain_errors


async def request_validation_error(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": plain_errors(exc.errors())})


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, request_validation_error)  # type: ignore[arg-type]
