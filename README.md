# MEATCRAFT — Конструктор колбасы с AI-визуализацией

Интерактивное веб-приложение для кастомизации рецепта колбасы с фотореалистичным AI-превью и расчётом стоимости.

## 🚀 Возможности

- **AI-визуализация** — генерация фотореалистичных срезов колбасы через Pollinations.ai
- **Умный fallback** — автоматическое переключение между AI-сервисами при проблемах с сетью
- **WMS/OMS** — управление слотами доставки
- **Два режима** — "Стандарт" и "Детское питание" с фильтрацией аллергенов
- **Адаптивный UI** — работает на десктопах и мобильных устройствах

## 🛠️ Технологии

- **Backend:** Python, Flask
- **Frontend:** HTML5, CSS3, JavaScript, Canvas API
- **AI:** Pollinations.ai (text2img), Hugging Face (img2img, опционально)
- **Хранение:** JSON + файловая система

## 📦 Установка

```bash
git clone https://github.com/julwish/meatcraft.git
cd meatcraft
pip install -r requirements.txt
python app.py
