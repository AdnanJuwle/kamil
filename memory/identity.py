import json
import os
from datetime import datetime
from core.config import DATA_DIR

IDENTITY_PATH = os.path.join(DATA_DIR, "identity.json")

DEFAULT_IDENTITY = {
    "name": None,
    "preferences": {},
    "skills": [],
    "context": {},
    "created_at": None,
    "updated_at": None
}

def load_identity() -> dict:
    try:
        with open(IDENTITY_PATH) as f:
            return json.load(f)
    except:
        return DEFAULT_IDENTITY.copy()

def save_identity(identity: dict):
    identity["updated_at"] = datetime.now().isoformat()
    os.makedirs(os.path.dirname(IDENTITY_PATH), exist_ok=True)
    with open(IDENTITY_PATH, 'w') as f:
        json.dump(identity, f, indent=2)

def update_identity(key: str, value):
    identity = load_identity()
    if identity["created_at"] is None:
        identity["created_at"] = datetime.now().isoformat()
    identity[key] = value
    save_identity(identity)
    return identity

def is_new_user() -> bool:
    identity = load_identity()
    return identity.get("name") is None