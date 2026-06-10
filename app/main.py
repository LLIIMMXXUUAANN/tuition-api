from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="Tuition API", lifespan=lifespan)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    first = exc.errors()[0]
    msg = f"{' → '.join(str(l) for l in first['loc'] if l != 'body')}: {first['msg']}"
    return JSONResponse(status_code=422, content={"error": msg})

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
@app.get("/health")
async def health():
    return {"status": "ok"}


# Routers mounted here as each phase is implemented
from app.routers import google, payment, timetable, agent, students, templates  # noqa: E402
app.include_router(google.router, prefix="/google")
app.include_router(payment.router, prefix="/payment")
app.include_router(timetable.router, prefix="/timetable")
app.include_router(agent.router, prefix="/agent")
app.include_router(templates.router, prefix="/templates")
app.include_router(students.router)
