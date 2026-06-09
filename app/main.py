from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="Tuition API", lifespan=lifespan)

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
from app.routers import google, payment, timetable, agent, students  # noqa: E402
app.include_router(google.router, prefix="/google")
app.include_router(payment.router, prefix="/payment")
app.include_router(timetable.router, prefix="/timetable")
app.include_router(agent.router, prefix="/agent")
app.include_router(students.router)
