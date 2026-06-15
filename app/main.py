from dotenv import load_dotenv

load_dotenv()

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="Tuition API", lifespan=lifespan, redirect_slashes=False)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    first = exc.errors()[0]
    msg = f"{' → '.join(str(l) for l in first['loc'] if l != 'body')}: {first['msg']}"
    return JSONResponse(status_code=422, content={"error": msg})

from app.config import settings  # noqa: E402

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.allowed_origins.split(",")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
@app.get("/health")
async def health():
    return {"status": "ok"}


from app.features.google.router import router as google_router  # noqa: E402
from app.features.students.router import router as students_router  # noqa: E402
from app.features.templates.router import router as templates_router  # noqa: E402
from app.features.payment.router import router as payment_router  # noqa: E402
from app.features.timetable.router import router as timetable_router  # noqa: E402
from app.features.agent.router import router as agent_router  # noqa: E402

app.include_router(google_router, prefix="/google")
app.include_router(students_router, prefix="/students")
app.include_router(templates_router, prefix="/templates")
app.include_router(payment_router, prefix="/payment")
app.include_router(timetable_router, prefix="/timetable")
app.include_router(agent_router, prefix="/agent")
