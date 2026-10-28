from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def home():
    return {"message": "Support chatbot is live"}

@app.get("/health")
def health():
    return {"status": "ok"}
