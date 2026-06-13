import jwt
import time
import argparse

def get_jwt_token(secret: str, scopes: list[str] = None) -> str:
    payload = {
        "sub": "test-user",
        "exp": int(time.time()) + 86400,
        "scopes": scopes or ["items:read", "items:write"],
    }
    return jwt.encode(payload, secret, algorithm="HS256")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--secret", type=str, required=True)
    parser.add_argument("--scopes", type=str, required=False)
    args = parser.parse_args()
    print(get_jwt_token(args.secret))