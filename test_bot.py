#!/usr/bin/env python3
"""
Скрипт для тестирования бота
"""
import os
import sys
import json
from pathlib import Path

def test_environment():
    """Проверяет переменные окружения"""
    print("🔍 Проверка переменных окружения...")
    
    # Проверяем наличие .env файла
    env_file = Path(".env")
    if not env_file.exists():
        print("❌ Файл .env не найден!")
        print("📝 Создайте файл .env с содержимым:")
        print("BOT_TOKEN=your_bot_token_here")
        print("MIN_PLAYERS=3")
        print("DATA_DIR=.")
        return False
    
    # Проверяем токен
    from dotenv import load_dotenv
    load_dotenv()
    
    token = os.getenv("BOT_TOKEN")
    if not token or token == "your_bot_token_here":
        print("❌ Токен бота не настроен!")
        print("📝 Установите правильный токен в файле .env")
        return False
    
    print("✅ Переменные окружения настроены")
    return True

def test_data_files():
    """Проверяет файлы с данными"""
    print("\n🔍 Проверка файлов данных...")
    
    files_to_check = ["situations.json", "witnesses.json", "conclusions.json"]
    
    for filename in files_to_check:
        file_path = Path(filename)
        if not file_path.exists():
            print(f"❌ Файл {filename} не найден!")
            return False
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, list) or len(data) == 0:
                print(f"❌ Файл {filename} пуст или имеет неправильный формат!")
                return False
            print(f"✅ {filename}: {len(data)} записей")
        except json.JSONDecodeError as e:
            print(f"❌ Ошибка в JSON файле {filename}: {e}")
            return False
        except Exception as e:
            print(f"❌ Ошибка при чтении {filename}: {e}")
            return False
    
    return True

def test_dependencies():
    """Проверяет зависимости"""
    print("\n🔍 Проверка зависимостей...")
    
    required_modules = ["aiogram", "aiohttp", "dotenv"]
    
    for module in required_modules:
        try:
            if module == "dotenv":
                import dotenv
            elif module == "aiogram":
                import aiogram
            elif module == "aiohttp":
                import aiohttp
            print(f"✅ {module} установлен")
        except ImportError:
            print(f"❌ {module} не установлен!")
            return False
    
    return True

def test_bot_import():
    """Проверяет импорт бота"""
    print("\n🔍 Проверка импорта бота...")
    
    try:
        # Проверяем, что можем импортировать модули
        import config
        print("✅ config.py импортирован")
        
        # Проверяем токен
        if not config.TOKEN:
            print("❌ Токен не найден в config")
            return False
        
        print("✅ Токен найден в config")
        return True
        
    except Exception as e:
        print(f"❌ Ошибка при импорте: {e}")
        return False

def main():
    """Основная функция тестирования"""
    print("🤖 Тестирование бота для ролевой игры 'Суд'")
    print("=" * 50)
    
    tests = [
        test_environment,
        test_dependencies,
        test_data_files,
        test_bot_import
    ]
    
    passed = 0
    total = len(tests)
    
    for test in tests:
        if test():
            passed += 1
        else:
            print(f"\n❌ Тест не пройден!")
            break
    
    print("\n" + "=" * 50)
    if passed == total:
        print("🎉 Все тесты пройдены! Бот готов к запуску.")
        print("\n📋 Для запуска бота выполните:")
        print("python3 bot.py")
    else:
        print(f"❌ Пройдено {passed}/{total} тестов")
        print("🔧 Исправьте ошибки перед запуском бота")
        sys.exit(1)

if __name__ == "__main__":
    main()
