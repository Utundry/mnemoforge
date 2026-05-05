"""
Тестовый скрипт для проверки подключения к GLM/Zhipu AI API (z.ai)

Использование:
    python scripts/test_glm_api.py

Предварительно добавьте в .env файл:
    GLM_API_KEY=ваш_ключ_от_https://open.bigmodel.cn/
    GLM_MODEL=glm-4.5-air
"""
import asyncio
import sys
from pathlib import Path

# Добавляем корень проекта в Python path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services.cloud_llm import cloud_available, cloud_complete, cloud_provider


async def test_glm_connection():
    """Тестирует подключение к GLM API."""
    
    print("=" * 60)
    print("Тестирование подключения к GLM/Zhipu AI (z.ai)")
    print("=" * 60)
    
    # Проверка доступности
    print("\n1. Проверка доступности API...")
    if not cloud_available():
        print("❌ GLM API не настроен. Добавьте GLM_API_KEY в .env файл")
        print("   Получите ключ на https://open.bigmodel.cn/")
        return False
    
    print("✅ GLM API настроен")
    print(f"   Провайдер: {cloud_provider()}")
    
    # Простой тест
    print("\n2. Тестовый запрос...")
    try:
        result = await cloud_complete(
            "Привет! Представься одним словом.",
            system="Ты полезный AI-ассистент.",
            max_tokens=50,
            temperature=0.7
        )
        print(f"✅ Ответ от GLM: {result}")
    except Exception as e:
        print(f"❌ Ошибка при запросе: {e}")
        return False
    
    # Более сложный тест
    print("\n3. Тест на генерацию навыка...")
    try:
        skill_prompt = """
        Создай краткое описание навыка "краткое содержание текста":
        - Назначение
        - Основные шаги
        - Рекомендации
        """
        
        result = await cloud_complete(
            skill_prompt,
            system="Ты эксперт по созданию AI-навыков. Пиши кратко и по делу.",
            max_tokens=300,
            temperature=0.5
        )
        print("✅ Навык сгенерирован:")
        print("-" * 60)
        print(result)
        print("-" * 60)
    except Exception as e:
        print(f"❌ Ошибка при генерации навыка: {e}")
        return False
    
    # Тест с различными параметрами
    print("\n4. Тест с низкой температурой (более детерминированный)...")
    try:
        result = await cloud_complete(
            "Напиши 'Hello World' на Python",
            system="Ты программист. Пиши только код без комментариев.",
            max_tokens=100,
            temperature=0.1
        )
        print("✅ Код сгенерирован:")
        print(result)
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return False
    
    print("\n" + "=" * 60)
    print("✅ Все тесты пройдены успешно!")
    print("=" * 60)
    return True


async def main():
    """Главная функция."""
    try:
        success = await test_glm_connection()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\n⚠️  Тест прерван пользователем")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n❌ Неожиданная ошибка: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
