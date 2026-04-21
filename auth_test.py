import os
import webbrowser
from kiteconnect import KiteConnect
from dotenv import load_dotenv

# Load credentials from config/.env
load_dotenv("config/.env")

API_KEY    = os.getenv("ZERODHA_API_KEY")
API_SECRET = os.getenv("ZERODHA_API_SECRET")

if not API_KEY or not API_SECRET:
    print("❌ ERROR: ZERODHA_API_KEY or ZERODHA_API_SECRET missing from config/.env")
    exit(1)

kite = KiteConnect(api_key=API_KEY)

# Step 1 — Open login URL in browser
login_url = kite.login_url()
print(f"\n🌐 Opening Zerodha login in browser...")
print(f"   URL: {login_url}\n")
webbrowser.open(login_url)

# Step 2 — User pastes request_token from redirect URL
print("After logging in, Zerodha redirects you to a URL like:")
print("  https://127.0.0.1/?action=login&type=login&status=success&request_token=XXXXXX\n")
request_token = input("Paste the request_token value here: ").strip()

# Step 3 — Generate session
try:
    data = kite.generate_session(request_token, api_secret=API_SECRET)
    access_token = data["access_token"]

    # Save to .env for other modules to use
    env_path = "config/.env"
    with open(env_path, "r") as f:
        lines = f.readlines()
    with open(env_path, "w") as f:
        for line in lines:
            if line.startswith("ZERODHA_ACCESS_TOKEN="):
                f.write(f"ZERODHA_ACCESS_TOKEN={access_token}\n")
            else:
                f.write(line)

    # Step 4 — Verify by fetching profile
    kite.set_access_token(access_token)
    profile = kite.profile()

    print("\n" + "="*45)
    print("  ACCESS TOKEN VALID ✓")
    print("="*45)
    print(f"  Name:      {profile['user_name']}")
    print(f"  Email:     {profile['email']}")
    print(f"  User ID:   {profile['user_id']}")
    print(f"  Broker:    {profile['broker']}")
    print("="*45)
    print(f"\n✅ Token saved to config/.env")
    print("   Engine is ready to run.\n")

except Exception as e:
    print(f"\n❌ ERROR generating session: {e}")
    print("   Check your API_SECRET and that the request_token is fresh.")