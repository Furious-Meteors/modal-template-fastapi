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
    parser.add_argument(
        "--scopes",
        type=str,
        required=False,
        help="Comma-separated scopes (default: items:read,items:write)",
    )
    args = parser.parse_args()
    scopes = [s.strip() for s in args.scopes.split(",") if s.strip()] if args.scopes else None
    print(get_jwt_token(args.secret, scopes))