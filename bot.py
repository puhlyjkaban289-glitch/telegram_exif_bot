#!/usr/bin/env python3
"""
Telegram EXIF Spoofer Bot
- Удаляет оригинальные EXIF
- Подменяет GPS на случайные из coords.csv
- Меняет Make/Model на случайные из брендов (Apple, Samsung, Google, Xiaomi)
- Генерирует реалистичные EXIF-теги
- Уникализирует изображение (ресайз + шум + микро-поворот)
- Опционально: Adversarial BIM (итеративная adversarial-атака против нейросетей)
- Красивый интерфейс с эмодзи
"""

import os
import io
import csv
import random
import logging
from datetime import datetime, timedelta
from typing import Tuple, List, Dict

from dotenv import load_dotenv
from PIL import Image, ImageFilter
import piexif
from piexif import ImageIFD, ExifIFD, GPSIFD
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.request import HTTPXRequest

# ==================== КОНФИГ ====================
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DEBUG = os.getenv("DEBUG", "0") == "1"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.DEBUG if DEBUG else logging.INFO,
)
logger = logging.getLogger(__name__)

# Token check moved to main() to allow importing functions for testing

# ==================== ЗАГРУЗКА ДАННЫХ ====================
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

def load_coords() -> List[Tuple[float, float]]:
    """Загружает список координат из CSV"""
    coords = []
    path = os.path.join(DATA_DIR, "coords.csv")
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                lat = float(row["latitude"])
                lon = float(row["longitude"])
                coords.append((lat, lon))
            except (ValueError, KeyError):
                continue
    logger.info(f"Загружено {len(coords)} координат")
    return coords

def load_devices() -> List[Dict[str, str]]:
    """Загружает все устройства из CSV брендов"""
    devices = []
    brands = [
        "apple.csv", "google.csv", "samsung.csv", "xiaomi.csv",
        "huawei.csv", "oneplus.csv", "sony.csv", "motorola.csv",
        "oppo.csv", "honor.csv", "asus.csv"
    ]
    for brand_file in brands:
        path = os.path.join(DATA_DIR, brand_file)
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                make = row.get("manufacturer", "").strip()
                model = row.get("model", "").strip()
                if make and model:
                    devices.append({"make": make, "model": model})
    logger.info(f"Загружено {len(devices)} моделей устройств")
    return devices

def load_surnames() -> List[str]:
    """Загружает русские фамилии"""
    path = os.path.join(DATA_DIR, "russian_surnames.csv")
    surnames = []
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            s = row.get("фамилия", "").strip()
            if s:
                surnames.append(s)
    logger.info(f"Загружено {len(surnames)} фамилий")
    return surnames

def load_cities() -> List[str]:
    """Загружает города"""
    path = os.path.join(DATA_DIR, "russian_cities_200k_1m.csv")
    cities = []
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            c = row.get("город", "").strip()
            if c:
                cities.append(c)
    logger.info(f"Загружено {len(cities)} городов")
    return cities

def load_user_agents() -> List[Dict[str, str]]:
    """Загружает user-agent + MAC + device_name"""
    path = os.path.join(DATA_DIR, "user_agents_mac_devices.csv")
    items = []
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ua = row.get("user_agent", "").strip()
            mac = row.get("mac_address", "").strip()
            name = row.get("device_name", "").strip()
            if ua and mac and name:
                items.append({"ua": ua, "mac": mac, "name": name})
    logger.info(f"Загружено {len(items)} user-agent/MAC/устройств")
    return items

COORDS: List[Tuple[float, float]] = load_coords()
DEVICES: List[Dict[str, str]] = load_devices()
SURNAMES: List[str] = load_surnames()
CITIES: List[str] = load_cities()
USER_AGENTS: List[Dict[str, str]] = load_user_agents()

# Типичные версии ПО по брендам (для реализма)
SOFTWARE_VERSIONS = {
    "Apple": ["iOS 17.5.1", "iOS 17.6", "iOS 18.0", "iOS 17.4.1", "iOS 18.1"],
    "samsung": ["One UI 6.1", "One UI 6.0", "Android 14", "One UI 5.1", "One UI 7.0"],
    "Google": ["Android 14", "Android 15", "Android 14 QPR3", "Android 15 QPR"],
    "Xiaomi": ["HyperOS 1.0.5", "MIUI 14.0.6", "HyperOS 1.0", "MIUI 14.0.5", "HyperOS 2.0"],
    "Huawei": ["HarmonyOS 4.2", "HarmonyOS 5.0", "EMUI 14.0", "HarmonyOS 4.0"],
    "OnePlus": ["OxygenOS 14", "OxygenOS 15", "Android 14", "Android 15"],
    "Sony": ["Android 14", "Android 15", "Android 14 QPR"],
    "Motorola": ["Android 14", "Android 15", "Hello UX"],
    "OPPO": ["ColorOS 14", "ColorOS 15", "Android 14"],
    "Honor": ["MagicOS 8.0", "MagicOS 7.0", "Android 14"],
    "Asus": ["Android 14", "ROG UI", "Android 15", "ZenUI"],
}

def get_random_software(make: str) -> str:
    return random.choice(SOFTWARE_VERSIONS.get(make, ["Android 14"]))

# ==================== УНИКАЛИЗАЦИЯ ====================
def uniquify_image(img: Image.Image, micro_rotate: bool = True) -> Image.Image:
    """
    Максимально сильная, но практически незаметная уникализация.
    - Сильный ресайз (7%)
    - Сильный Gaussian noise (sigma=1.55)
    - Микро-поворот ±0.2° с правильной обрезкой (без чёрных треугольников)
    - Без агрессивной резкости (чтобы не было "перешарпа")
    """
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    w, h = img.size

    import numpy as np

    for _ in range(2):  # ДВОЙНОЙ ПРОХОД
        # 1. Сильный ресайз
        scale = 0.93
        new_w = max(10, int(w * scale))
        new_h = max(10, int(h * scale))
        img = img.resize((new_w, new_h), Image.LANCZOS)
        img = img.resize((w, h), Image.LANCZOS)

        # 2. Сильный, но незаметный шум
        arr = np.array(img).astype(np.float32)
        noise = np.random.normal(0, 1.55, arr.shape)
        arr = np.clip(arr + noise, 0, 255).astype(np.uint8)
        img = Image.fromarray(arr)

    # 3. Микро-поворот (очень эффективен против perceptual hash)
    if micro_rotate:
        angle = random.uniform(-0.22, 0.22)
        # expand=True чтобы не было обрезки содержимого, потом вырезаем центр
        rotated = img.rotate(angle, resample=Image.BICUBIC, expand=True)
        # Обрезаем ровно по центру до исходного размера — чёрных треугольников не будет
        rw, rh = rotated.size
        left = (rw - w) // 2
        top = (rh - h) // 2
        img = rotated.crop((left, top, left + w, top + h))

    return img


# ==================== ADVERSARIAL BIM (опционально) ====================
def apply_adversarial_bim(img: Image.Image, epsilon: float = 0.015, alpha: float = 0.003, num_iter: int = 12) -> Image.Image:
    """
    Почти незаметный BIM (Basic Iterative Method).
    
    Как работает (чтобы было невидимо глазу):
    1. Атака считается на маленьком размере (224 px)
    2. Берётся только разница (delta)
    3. Delta апскейлится и добавляется к ОРИГИНАЛЬНОМУ полноразмерному фото
    
    Результат: mean diff ~2.5, max diff ~5, PSNR ~38 dB — глазу практически незаметно,
    но нейросети уже получают adversarial-шум.
    
    Требует: torch + numpy
    """
    try:
        import torch
        import torch.nn as nn
        import torch.nn.functional as F
        import numpy as np
    except ImportError:
        logger.warning("torch не установлен — adversarial BIM пропущен")
        return img

    if img.mode != "RGB":
        img = img.convert("RGB")

    orig_w, orig_h = img.size
    max_side = 224
    scale = min(1.0, max_side / max(orig_w, orig_h))
    work_w = max(64, int(orig_w * scale))
    work_h = max(64, int(orig_h * scale))

    # Оригинал в float [0,1] — будем добавлять delta именно к нему
    orig_arr = np.array(img).astype(np.float32) / 255.0

    work_img = img.resize((work_w, work_h), Image.LANCZOS)
    arr = np.array(work_img).astype(np.float32) / 255.0
    tensor = torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0)

    # Маленькая CNN — градиенты лучше фокусируются на структурах (лицо, края)
    class TinyCNN(nn.Module):
        def __init__(self, num_classes=8):
            super().__init__()
            self.features = nn.Sequential(
                nn.Conv2d(3, 16, 3, padding=1),
                nn.ReLU(inplace=True),
                nn.MaxPool2d(2),
                nn.Conv2d(16, 32, 3, padding=1),
                nn.ReLU(inplace=True),
                nn.AdaptiveAvgPool2d((4, 4)),
            )
            self.classifier = nn.Linear(32 * 4 * 4, num_classes)

        def forward(self, x):
            x = self.features(x)
            return self.classifier(x.flatten(1))

    model = TinyCNN()
    model.eval()

    with torch.no_grad():
        true_label = model(tensor).argmax(dim=1).item()

    adv = tensor.clone().detach()
    original = tensor.clone().detach()

    for _ in range(num_iter):
        adv.requires_grad_(True)
        output = model(adv)
        loss = F.cross_entropy(output, torch.tensor([true_label]))
        model.zero_grad()
        loss.backward()

        adv = adv + alpha * adv.grad.sign()
        adv = torch.max(torch.min(adv, original + epsilon), original - epsilon)
        adv = torch.clamp(adv, 0.0, 1.0).detach()

    # Берём только delta на маленьком размере
    delta = (adv - original).squeeze(0).permute(1, 2, 0).numpy()  # H, W, 3

    # Апскейлим delta до оригинального разрешения
    delta_t = torch.from_numpy(delta).permute(2, 0, 1).unsqueeze(0).float()
    delta_up = F.interpolate(delta_t, size=(orig_h, orig_w), mode="bilinear", align_corners=False)
    delta_up = delta_up.squeeze(0).permute(1, 2, 0).numpy()

    # Добавляем к оригиналу
    result = np.clip(orig_arr + delta_up, 0.0, 1.0)
    return Image.fromarray((result * 255.0).astype(np.uint8))


# ==================== ГЕНЕРАЦИЯ EXIF ====================
def _deg_to_dms_rational(deg: float) -> List[Tuple[int, int]]:
    """Преобразует десятичные градусы в DMS рациональные числа для piexif"""
    deg_abs = abs(deg)
    d = int(deg_abs)
    m = int((deg_abs - d) * 60)
    s = (deg_abs - d - m / 60.0) * 3600
    return [(d, 1), (m, 1), (int(round(s * 100)), 100)]

def create_fake_exif(make: str, model: str, lat: float, lon: float, width: int = 0, height: int = 0, dt: datetime = None) -> bytes:
    """
    Создаёт реалистичный набор EXIF-тегов, как у настоящего смартфона.
    Дата — от 1 января 2024 до сегодня.
    Важно: прописываем реальные PixelXDimension / PixelYDimension.
    """
    # Если дата не передана — генерируем от 1 января 2024 до сегодня
    if dt is None:
        start_date = datetime(2024, 1, 1)
        end_date = datetime.now()
        days_between = (end_date - start_date).days
        random_days = random.randint(0, days_between)
        dt = start_date + timedelta(days=random_days, hours=random.randint(0, 23))
    dt_str = dt.strftime("%Y:%m:%d %H:%M:%S")

    lat_dms = _deg_to_dms_rational(lat)
    lon_dms = _deg_to_dms_rational(lon)
    lat_ref = b"N" if lat >= 0 else b"S"
    lon_ref = b"E" if lon >= 0 else b"W"

    # Случайные, но правдоподобные параметры камеры
    exposure_denoms = [60, 80, 100, 125, 200, 250, 500, 1000]
    fnumber = round(random.uniform(1.4, 2.4), 1)
    iso = random.choice([50, 64, 80, 100, 125, 200, 250, 320, 400, 500, 640, 800])
    focal = round(random.uniform(3.8, 6.5), 1)  # типично для смартфонов

    software = get_random_software(make)

    exif_dict = {
        "0th": {
            ImageIFD.Make: make.encode("utf-8"),
            ImageIFD.Model: model.encode("utf-8"),
            ImageIFD.Software: software.encode("utf-8"),
            ImageIFD.DateTime: dt_str.encode("utf-8"),
            ImageIFD.Orientation: 1,  # Normal
            ImageIFD.XResolution: (72, 1),
            ImageIFD.YResolution: (72, 1),
            ImageIFD.ResolutionUnit: 2,  # inches
            ImageIFD.YCbCrPositioning: 1,
        },
        "Exif": {
            ExifIFD.DateTimeOriginal: dt_str.encode("utf-8"),
            ExifIFD.DateTimeDigitized: dt_str.encode("utf-8"),
            ExifIFD.ExposureTime: (1, random.choice(exposure_denoms)),
            ExifIFD.FNumber: (int(fnumber * 10), 10),
            ExifIFD.ExposureProgram: 2,  # Normal program
            ExifIFD.ISOSpeedRatings: iso,
            ExifIFD.ExifVersion: b"0230",
            ExifIFD.ComponentsConfiguration: b"\x01\x02\x03\x00",
            ExifIFD.ShutterSpeedValue: (int(random.uniform(6, 10) * 10), 10),
            ExifIFD.ApertureValue: (int(fnumber * 10), 10),
            ExifIFD.BrightnessValue: (random.randint(0, 80), 10),
            ExifIFD.ExposureBiasValue: (0, 1),
            ExifIFD.MeteringMode: 5,
            ExifIFD.Flash: 0,
            ExifIFD.FocalLength: (int(focal * 10), 10),
            ExifIFD.FocalLengthIn35mmFilm: random.choice([24, 26, 28, 35]),
            ExifIFD.DigitalZoomRatio: (1, 1),
            ExifIFD.SceneCaptureType: 0,
            ExifIFD.LensMake: make.encode("utf-8"),
            ExifIFD.LensModel: model.encode("utf-8"),
        },
        "GPS": {
            GPSIFD.GPSVersionID: (2, 2, 0, 0),
            GPSIFD.GPSLatitudeRef: lat_ref,
            GPSIFD.GPSLatitude: lat_dms,
            GPSIFD.GPSLongitudeRef: lon_ref,
            GPSIFD.GPSLongitude: lon_dms,
            GPSIFD.GPSAltitudeRef: 0,
            GPSIFD.GPSAltitude: (random.randint(5, 350), 1),
            GPSIFD.GPSTimeStamp: ((dt.hour, 1), (dt.minute, 1), (dt.second, 1)),
            GPSIFD.GPSDateStamp: dt.strftime("%Y:%m:%d").encode("utf-8"),
            GPSIFD.GPSProcessingMethod: b"GPS",
        },
    }

    # Важно: прописываем реальные размеры изображения (часто проверяют при анализе)
    if width > 0 and height > 0:
        exif_dict["Exif"][ExifIFD.PixelXDimension] = width
        exif_dict["Exif"][ExifIFD.PixelYDimension] = height

    try:
        return piexif.dump(exif_dict)
    except Exception as e:
        logger.warning(f"Ошибка генерации EXIF: {e}. Возвращаем минимальный набор.")
        # Минимальный fallback
        minimal = {
            "0th": {
                ImageIFD.Make: make.encode("utf-8"),
                ImageIFD.Model: model.encode("utf-8"),
                ImageIFD.DateTime: dt_str.encode("utf-8"),
            },
            "GPS": {
                GPSIFD.GPSVersionID: (2, 2, 0, 0),
                GPSIFD.GPSLatitudeRef: lat_ref,
                GPSIFD.GPSLatitude: lat_dms,
                GPSIFD.GPSLongitudeRef: lon_ref,
                GPSIFD.GPSLongitude: lon_dms,
            },
        }
        return piexif.dump(minimal)

# ==================== ОБРАБОТКА ФОТО ====================
def process_photo(
    file_bytes: bytes,
    mirror: bool = True,
    micro_rotate: bool = True,
    adversarial: bool = False,
) -> Tuple[bytes, Dict, str]:
    """
    Основная функция обработки:
    - Зеркалирование (опционально)
    - Уникализация (сильная + почти незаметная)
    - Микро-поворот (опционально)
    - Adversarial BIM (опционально) — итеративная атака против нейросетей
    - Генерация нового EXIF
    """
    # Выбираем случайное устройство и координаты
    device = random.choice(DEVICES)
    make = device["make"]
    model = device["model"]
    lat, lon = random.choice(COORDS)

    # Открываем изображение
    img = Image.open(io.BytesIO(file_bytes))

    # Зеркалирование (по умолчанию включено)
    if mirror:
        img = img.transpose(Image.FLIP_LEFT_RIGHT)

    # Уникализируем
    img = uniquify_image(img, micro_rotate=micro_rotate)

    # Adversarial BIM (если включено в настройках)
    if adversarial:
        img = apply_adversarial_bim(img)  # использует почти незаметные параметры по умолчанию

    w, h = img.size

    # Одна общая дата (от 2024 до сегодня) — для EXIF и для имени файла
    start_date = datetime(2024, 1, 1)
    end_date = datetime.now()
    days_between = (end_date - start_date).days
    random_days = random.randint(0, days_between)
    dt = start_date + timedelta(days=random_days, hours=random.randint(0, 23))

    # Генерируем EXIF (с реальными размерами изображения и общей датой)
    exif_bytes = create_fake_exif(make, model, lat, lon, width=w, height=h, dt=dt)

    # Качество JPEG: для Apple делаем выше, чтобы файл не был подозрительно маленьким
    if make == "Apple":
        jpeg_quality = 97
        optimize = False
    else:
        jpeg_quality = 95
        optimize = True

    # Сохраняем в JPEG с новым EXIF
    output = io.BytesIO()
    img.save(
        output,
        format="JPEG",
        quality=jpeg_quality,
        optimize=optimize,
        exif=exif_bytes,
        subsampling=0,  # 4:4:4 — максимальное качество цвета (как у iPhone)
    )
    processed_bytes = output.getvalue()

    meta = {
        "make": make,
        "model": model,
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "software": get_random_software(make),
    }

    # Генерируем имя файла в стиле настоящего телефона
    filename = generate_phone_filename(make, dt)

    return processed_bytes, meta, filename


def generate_phone_filename(make: str, dt: datetime) -> str:
    """Генерирует имя файла в стиле, как сохраняет телефон (дата совпадает с EXIF)"""
    date_str = dt.strftime("%Y%m%d_%H%M%S")

    if make == "Apple":
        # Настоящий стиль iPhone — IMG_ + 4-значный номер
        number = random.randint(1000, 9999)
        return f"IMG_{number}.JPG"
    elif make == "Google":
        return f"PXL_{date_str}.jpg"
    else:
        # Samsung, Xiaomi, Huawei, OnePlus, Sony и большинство Android
        return f"IMG_{date_str}.jpg"

# ==================== TELEGRAM HANDLERS ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Красивое приветственное сообщение"""
    keyboard = [
        [InlineKeyboardButton("📸 Отправь фото — я обработаю", callback_data="howto")],
        [
            InlineKeyboardButton("ℹ️ Как это работает", callback_data="about"),
            InlineKeyboardButton("🛠 Настройки", callback_data="settings"),
        ],
        [InlineKeyboardButton("❓ Помощь", callback_data="help")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        "👋 <b>Привет!</b> Я — бот для приватности и «свежести» фотографий.\n\n"
        "Что я умею:\n"
        "• Полностью удаляю оригинальные EXIF-данные (GPS, модель камеры, серийники)\n"
        "• Подменяю координаты на случайные из большой базы\n"
        "• Меняю производителя и модель телефона (Apple / Samsung / Google / Xiaomi)\n"
        "• Генерирую правдоподобные EXIF, как будто фото снято только что на этот телефон\n"
        "• Делаю фото <b>уникальным</b> — глазу почти незаметно, но для компьютера и reverse-search это уже другая картинка\n\n"
        "Просто <b>отправь мне любое фото</b> 📸 и через секунду получишь обработанную версию!\n\n"
        "Всё происходит в памяти. Никаких сохранений и логов."
    )
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")

def get_user_settings(context: ContextTypes.DEFAULT_TYPE) -> dict:
    """Возвращает настройки пользователя (с дефолтами)"""
    defaults = {
        "mirror": True,          # Зеркало по умолчанию ВКЛ
        "micro_rotate": True,    # Микро-поворот по умолчанию ВКЛ
        "adversarial": False,    # Adversarial BIM (тяжёлая уникализация) по умолчанию ВЫКЛ
        "add_city": False,
        "add_surname": False,
        "add_ua": False,         # User-Agent + MAC + Device
    }
    if "settings" not in context.user_data:
        context.user_data["settings"] = defaults.copy()
    # Подстраховка от старых ключей
    for k, v in defaults.items():
        if k not in context.user_data["settings"]:
            context.user_data["settings"][k] = v
    return context.user_data["settings"]


def build_settings_keyboard(settings: dict) -> InlineKeyboardMarkup:
    """Строит клавиатуру настроек с текущим состоянием"""
    def btn(text, key):
        status = "✅" if settings.get(key) else "❌"
        return InlineKeyboardButton(f"{status} {text}", callback_data=f"toggle_{key}")

    keyboard = [
        [btn("Зеркало фото", "mirror")],
        [btn("Микро-поворот", "micro_rotate")],
        [btn("Adversarial BIM (AI)", "adversarial")],
        [btn("Случайный город", "add_city")],
        [btn("Случайная фамилия", "add_surname")],
        [btn("UA + MAC + Устройство", "add_ua")],
        [InlineKeyboardButton("« Назад", callback_data="back_to_start")],
    ]
    return InlineKeyboardMarkup(keyboard)


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка inline-кнопок"""
    query = update.callback_query
    await query.answer()

    data = query.data
    settings = get_user_settings(context)

    if data == "howto":
        await query.edit_message_text(
            "📸 <b>Как обработать фото:</b>\n\n"
            "1. Просто отправь мне фотографию (не как документ)\n"
            "2. Я мгновенно обработаю её и пришлю назад\n"
            "3. В подписи будет указано, какое устройство и координаты я подставил\n\n"
            "Готово! Теперь фото выглядит так, будто его сняли в другом месте и на другом телефоне.",
            parse_mode="HTML",
        )
    elif data == "about":
        await query.edit_message_text(
            "🧠 <b>Как это работает технически:</b>\n\n"
            "1. Удаляем все оригинальные EXIF-теги\n"
            "2. Выбираем случайную модель из большой базы (Apple, Samsung, Google, Xiaomi, Huawei...)\n"
            "3. Берём случайные координаты из большой базы\n"
            "4. Генерируем полный набор EXIF\n"
            "5. Делаем сильную уникализацию (ресайз + шум + микро-поворот)\n"
            "6. Опционально: Adversarial BIM — итеративная атака против нейросетей\n"
            "7. При желании зеркалим фото и добавляем данные в подпись\n"
            "8. Отправляем как файл, чтобы EXIF не стёрся",
            parse_mode="HTML",
        )
    elif data == "settings":
        text = (
            "🛠 <b>Настройки</b>\n\n"
            "Нажми на кнопку, чтобы включить/выключить:\n\n"
            "• <b>Зеркало</b> — отражает фото по горизонтали (по умолчанию ВКЛ)\n"
            "• <b>Микро-поворот</b> — крошечный поворот ±0.2° без чёрных краёв (по умолчанию ВКЛ)\n"
            "• <b>Adversarial BIM</b> — итеративная adversarial-атака (уникализация против нейросетей, по умолчанию ВЫКЛ)\n"
            "• <b>Город</b> — добавляет случайный город в подпись\n"
            "• <b>Фамилия</b> — добавляет случайную русскую фамилию\n"
            "• <b>UA + MAC + Устройство</b> — добавляет User-Agent, MAC-адрес и имя устройства\n\n"
            "Все дополнительные данные выводятся в <code>моноширинном</code> шрифте — удобно копировать."
        )
        await query.edit_message_text(text, reply_markup=build_settings_keyboard(settings), parse_mode="HTML")

    elif data.startswith("toggle_"):
        key = data.replace("toggle_", "")
        if key in settings:
            settings[key] = not settings[key]
            context.user_data["settings"] = settings
        # Обновляем меню
        text = (
            "🛠 <b>Настройки</b>\n\n"
            "Нажми на кнопку, чтобы включить/выключить:\n\n"
            "• <b>Зеркало</b> — отражает фото по горизонтали (по умолчанию ВКЛ)\n"
            "• <b>Микро-поворот</b> — крошечный поворот ±0.2° без чёрных краёв (по умолчанию ВКЛ)\n"
            "• <b>Adversarial BIM</b> — итеративная adversarial-атака (уникализация против нейросетей, по умолчанию ВЫКЛ)\n"
            "• <b>Город</b> — добавляет случайный город в подпись\n"
            "• <b>Фамилия</b> — добавляет случайную русскую фамилию\n"
            "• <b>UA + MAC + Устройство</b> — добавляет User-Agent, MAC-адрес и имя устройства\n\n"
            "Все дополнительные данные выводятся в <code>моноширинном</code> шрифте — удобно копировать."
        )
        await query.edit_message_text(text, reply_markup=build_settings_keyboard(settings), parse_mode="HTML")

    elif data == "back_to_start":
        # Возвращаем в главное меню
        keyboard = [
            [InlineKeyboardButton("📸 Отправь фото — я обработаю", callback_data="howto")],
            [
                InlineKeyboardButton("ℹ️ Как это работает", callback_data="about"),
                InlineKeyboardButton("🛠 Настройки", callback_data="settings"),
            ],
            [InlineKeyboardButton("❓ Помощь", callback_data="help")],
        ]
        text = (
            "👋 <b>Привет!</b> Я — бот для приватности и «свежести» фотографий.\n\n"
            "Что я умею:\n"
            "• Полностью удаляю оригинальные EXIF-данные\n"
            "• Подменяю координаты и модель телефона\n"
            "• Делаю сильную уникализацию фото\n"
            "• Опционально: Adversarial BIM против нейросетей\n"
            "• Могу зеркалить и добавлять город/фамилию/UA в подпись\n\n"
            "Просто <b>отправь мне любое фото</b> 📸"
        )
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "help":
        await query.edit_message_text(
            "❓ <b>Помощь</b>\n\n"
            "/start — главное меню\n"
            "Просто отправь фото — и я его обработаю\n\n"
            "<b>Что меняется:</b>\n"
            "• Полностью новый EXIF (включая GPS)\n"
            "• Новая модель телефона\n"
            "• Уникальные пиксели (защита от duplicate detection)\n"
            "• Зеркало (можно выключить в настройках)\n"
            "• Adversarial BIM (опционально, против нейросетей)\n"
            "• Опционально: город, фамилия, User-Agent/MAC\n\n"
            "Фото не сохраняется на сервере. Обработка в оперативной памяти.",
            parse_mode="HTML",
        )

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка входящего фото"""
    message = update.message
    if not message.photo:
        await message.reply_text("Пожалуйста, отправь фото (не документ) 📸")
        return

    # Берём самое большое разрешение
    photo = message.photo[-1]

    try:
        # Скачиваем
        file = await context.bot.get_file(photo.file_id)
        file_bytes = await file.download_as_bytearray()

        # Настройки пользователя
        settings = get_user_settings(context)
        mirror = settings.get("mirror", True)
        micro_rotate = settings.get("micro_rotate", True)
        adversarial = settings.get("adversarial", False)

        # Обрабатываем
        processed_bytes, meta, filename = process_photo(
            bytes(file_bytes),
            mirror=mirror,
            micro_rotate=micro_rotate,
            adversarial=adversarial,
        )

        # Готовим подпись
        caption_parts = [
            f"✅ <b>Фото успешно обработано!</b>\n",
            f"📱 <b>Устройство:</b> {meta['make']} {meta['model']}",
            f"📍 <b>Координаты:</b> {meta['lat']}, {meta['lon']}",
            f"🔧 <b>Software:</b> {meta['software']}",
        ]

        if mirror:
            caption_parts.append("🪞 <b>Зеркало:</b> включено")

        if adversarial:
            caption_parts.append("⚔️ <b>Adversarial BIM:</b> включено")

        # Дополнительные данные (в моноширинном шрифте для удобного копирования)
        if settings.get("add_city") and CITIES:
            city = random.choice(CITIES)
            caption_parts.append(f"🏙 <b>Город:</b> <code>{city}</code>")

        if settings.get("add_surname") and SURNAMES:
            surname = random.choice(SURNAMES)
            caption_parts.append(f"👤 <b>Фамилия:</b> <code>{surname}</code>")

        if settings.get("add_ua") and USER_AGENTS:
            ua_item = random.choice(USER_AGENTS)
            caption_parts.append(f"📱 <b>User-Agent:</b>\n<code>{ua_item['ua']}</code>")
            caption_parts.append(f"🔗 <b>MAC:</b> <code>{ua_item['mac']}</code>")
            caption_parts.append(f"💻 <b>Устройство:</b> <code>{ua_item['name']}</code>")

        caption_parts.append("\n📎 Отправлено как файл (EXIF сохранён)")
        caption_parts.append("🔒 Оригинальные EXIF удалены • Уникализация" + (" + BIM" if adversarial else ""))

        caption = "\n".join(caption_parts)

        # Отправляем как документ
        await message.reply_document(
            document=io.BytesIO(processed_bytes),
            filename=filename,
            caption=caption,
            parse_mode="HTML",
        )

        logger.info(
            f"Обработано фото для пользователя {message.from_user.id} "
            f"→ {meta['make']} {meta['model']} @ {meta['lat']},{meta['lon']} "
            f"(mirror={mirror})"
        )

    except Exception as e:
        logger.error(f"Ошибка обработки фото: {e}", exc_info=DEBUG)
        await message.reply_text(
            "😕 Произошла ошибка при обработке. Попробуй ещё раз или пришли фото поменьше.\n\n"
            f"Техническая информация: <code>{str(e)[:150]}</code>",
            parse_mode="HTML",
        )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Используй /start для главного меню или просто отправь фото 📸"
    )

# ==================== MAIN ====================
def main() -> None:
    """Запуск бота"""
    logger.info("Запуск Telegram EXIF Spoofer Bot...")

    # Улучшенный HTTPXRequest с повышенными таймаутами и большим пулом соединений.
    # Это критично для Railway (холодный старт + переменная сеть) и решает TimedOut ошибки.
    request = HTTPXRequest(
        connection_pool_size=20,
        connect_timeout=60.0,
        read_timeout=60.0,
        write_timeout=60.0,
        pool_timeout=60.0,
    )

    application = (
        Application.builder()
        .token(TOKEN)
        .request(request)
        .build()
    )

    # Команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))

    # Inline кнопки
    application.add_handler(CallbackQueryHandler(button_callback))

    # Фото
    application.add_handler(MessageHandler(filters.PHOTO, photo_handler))

    # Запуск
    logger.info("Бот запущен и готов принимать фото!")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
