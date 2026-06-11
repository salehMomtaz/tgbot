# generate_session.py
import asyncio
from pyrogram import Client

async def main():
    print("=========================================")
    print("   Pyrogram Premium String Session Maker ")
    print("=========================================")
    print("This script helps you log in to your personal Telegram Premium")
    print("account and generate a String Session. Copy this string and paste")
    print("it into config.py to enable 4GB uploads.\n")
    
    api_id = int(input("Enter your API ID (from my.telegram.org): ").strip())
    api_hash = input("Enter your API Hash (from my.telegram.org): ").strip()
    
    # We start a temporary, in-memory client to generate the key
    async with Client(":memory:", api_id=api_id, api_hash=api_hash) as app:
        session_string = await app.export_session_string()
        print("\n" + "="*41)
        print(" SUCCESS! Copy your PREMIUM_STRING_SESSION below:")
        print("="*41 + "\n")
        print(session_string)
        print("\n" + "="*41)
        print("Keep this string safe! Do not share it publicly.")

if __name__ == "__main__":
    asyncio.run(main())
