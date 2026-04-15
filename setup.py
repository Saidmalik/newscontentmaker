"""
Quick setup script — run once to verify everything is configured correctly.
"""
import subprocess
import sys
import os
from pathlib import Path

def check_python():
    ver = sys.version_info
    ok = ver >= (3, 11)
    status = "✅" if ok else "❌"
    print(f"{status} Python {ver.major}.{ver.minor}.{ver.micro} {'(OK)' if ok else '(нужен 3.11+)'}")
    return ok

def check_dependencies():
    required = ["feedparser", "anthropic", "dotenv", "requests", "yaml"]
    all_ok = True
    for pkg in required:
        try:
            __import__(pkg if pkg != "dotenv" else "dotenv")
            print(f"  ✅ {pkg}")
        except ImportError:
            print(f"  ❌ {pkg} — не установлен (pip install {pkg})")
            all_ok = False
    return all_ok

def check_env():
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
    required_vars = {
        "ANTHROPIC_API_KEY": "Claude API",
        "ELEVENLABS_API_KEY": "ElevenLabs TTS",
        "ELEVENLABS_VOICE_ID": "ElevenLabs Voice",
        "TELEGRAM_BOT_TOKEN": "Telegram Bot",
        "TELEGRAM_CHAT_ID": "Telegram Chat",
    }
    all_ok = True
    for var, name in required_vars.items():
        val = os.environ.get(var, "")
        if val and val != f"your_{var.lower()}_here":
            print(f"  ✅ {name}: {val[:8]}...")
        else:
            print(f"  ⚠️  {name}: не задан ({var})")
            all_ok = False
    return all_ok

def check_dirs():
    dirs = ["config", "src", "output/audio", "output/content", "data"]
    for d in dirs:
        path = Path(__file__).parent / d
        path.mkdir(parents=True, exist_ok=True)
        print(f"  ✅ {d}/")

def init_db():
    sys.path.insert(0, str(Path(__file__).parent / "src"))
    from database import init_database
    init_database()

if __name__ == "__main__":
    print("\n" + "="*50)
    print("🔧 UZ NEWS BOT — Setup Check")
    print("="*50)

    print("\n📦 Python:")
    check_python()

    print("\n📚 Зависимости:")
    check_dependencies()

    print("\n📁 Директории:")
    check_dirs()

    print("\n🔑 Переменные окружения (.env):")
    check_env()

    print("\n🗃️  База данных:")
    try:
        init_db()
    except Exception as e:
        print(f"  ❌ Ошибка: {e}")

    print("\n" + "="*50)
    print("Если всё ✅ — запускай: python src/main.py")
    print("="*50 + "\n")
