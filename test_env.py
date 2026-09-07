import os
from dotenv import load_dotenv

load_dotenv()

print("=== DEBUG ===")
print(f"TELEGRAM_TOKEN: '{os.getenv('TELEGRAM_TOKEN')}'")
print(f"TELEGRAM_CHAT_ID: '{os.getenv('TELEGRAM_CHAT_ID')}'")
print(f"Beide gesetzt? {bool(os.getenv('TELEGRAM_TOKEN')) and bool(os.getenv('TELEGRAM_CHAT_ID'))}")