"""
Конфигурация pytest
"""
import os

# Устанавливаем переменные окружения для тестов
os.environ["INETOMETR_ADMIN_TOKEN"] = "test_admin_token_123"
