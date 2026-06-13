import jwt
import time
import argparse

def get_jwt_token(secret: str) -> str:
    return jwt.encode(
        {
            "sub": "test-user",
            "exp": int(time.time()) + 86400,  # expires in 24 hours
        },
        secret,
        algorithm="HS256",
    )

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--secret", type=str, required=True)
    args = parser.parse_args()
    print(get_jwt_token(args.secret))