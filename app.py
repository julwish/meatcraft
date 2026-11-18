"""
Sausage Shop Prototype v20 — Clean & Rounded
- Базовые изображения: Pollinations (text2img) с кэшем
- Рецепты: Hugging Face (img2img) → fallback на Pollinations
- Округление сумм до целых рублей
"""

# ============================================
# 1. ИМПОРТЫ
# ============================================
import json
import uuid
import ssl
import os
import hashlib
import base64
import random
import urllib.request
import urllib.error
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from threading import Lock

from flask import Flask, render_template, request, jsonify


# ============================================
# 2. ИНИЦИАЛИЗАЦИЯ FLASK
# ============================================
app = Flask(__name__)

DATA_FILE = Path("data.json")
data_lock = Lock()
STATIC_DIR = Path("static")
STATIC_DIR.mkdir(exist_ok=True)

SSL_CONTEXT = ssl.create_default_context()
SSL_CONTEXT.check_hostname = False
SSL_CONTEXT.verify_mode = ssl.CERT_NONE

MIN_FILE_SIZE = 10 * 1024  # 10 KB — минимальный размер валидного изображения


# ============================================
# 3. HUGGING FACE API (опционально)
# ============================================
def load_hf_token() -> str:
    """Загружает токен Hugging Face из окружения или .env файла"""
    token = os.environ.get("HF_API_TOKEN")
    if token:
        return token

    env_file = Path(".env")
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                if line.startswith("HF_API_TOKEN="):
                    return line.split("=", 1)[1].strip()
    return ""


HF_API_TOKEN = load_hf_token()
if HF_API_TOKEN:
    print(f"✅ Hugging Face token: {HF_API_TOKEN[:10]}...")
else:
    print("⚠️  HF_API_TOKEN не найден. Будет использоваться только Pollinations.")


# ============================================
# 4. СЛОВАРЬ ПЕРЕВОДОВ
# ============================================
INGREDIENT_TRANSLATIONS = {
    "Говядина высший сорт": "premium beef",
    "Свинина": "pork",
    "Индейка (филе)": "turkey fillet",
    "Курица (бедро)": "chicken thigh",
    "Сыр Гауда": "Gouda cheese chunks",
    "Оливки": "black olives",
    "Морковь (соломка)": "carrot sticks",
    "Грецкий орех": "walnut pieces",
    "Чеснок свежий": "fresh garlic",
    "Паприка копчёная": "smoked paprika",
    "Перец чёрный горошек": "black peppercorns",
    "Прованские травы": "Provencal herbs",
    "Натуральная оболочка": "natural casing",
    "Коллагеновая оболочка": "collagen casing",
    "Копчёная оболочка": "smoked casing",
}


def translate_ingredient(russian_name: str) -> str:
    """Переводит название ингредиента на английский"""
    return INGREDIENT_TRANSLATIONS.get(russian_name, russian_name.lower())


# ============================================
# 5. УТИЛИТЫ
# ============================================
def load_data() -> dict:
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_data(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_index_by_id(items: list, item_id: str, key: str = "id") -> int:
    for i, item in enumerate(items):
        if item[key] == item_id:
            return i
    return -1


def is_valid_cached_image(filepath: Path) -> bool:
    if not filepath.exists():
        return False
    return filepath.stat().st_size >= MIN_FILE_SIZE


def compute_recipe_hash(selections: dict, weight_g: int) -> str:
    normalized = {}
    for opt_id in sorted(selections.keys()):
        normalized[opt_id] = sorted(selections[opt_id])
    data_str = json.dumps({"s": normalized, "w": weight_g}, sort_keys=True)
    return hashlib.md5(data_str.encode()).hexdigest()[:12]


# ============================================
# 6. БИЗНЕС-ЛОГИКА
# ============================================
class ConfiguratorEngine:
    def __init__(self, data: dict):
        self.data = data
        self.ingredients = {i["id"]: i for i in data["ingredients"]}
        self.options = {o["id"]: o for o in data["configurator_options"]}
        self.batches_by_ingredient = {}
        for b in data["raw_material_batches"]:
            self.batches_by_ingredient.setdefault(b["ingredient_id"], []).append(b)

    def get_available_ingredients(self) -> dict:
        availability = {}
        for ing_id, ing in self.ingredients.items():
            if not ing["is_active"]:
                availability[ing_id] = {"available_kg": 0, "is_available": False}
                continue
            batches = self.batches_by_ingredient.get(ing_id, [])
            total = sum(
                b["current_quantity"] - b["reserved_quantity"]
                for b in batches if b["status"] == "AVAILABLE"
            )
            availability[ing_id] = {
                "available_kg": round(total, 2),
                "is_available": total > 0.1,
            }
        return availability

    def get_children_filtered_ingredients(self) -> list:
        return [
            ing["id"] for ing in self.data["ingredients"]
            if not ing.get("suitable_for_children", True) or ing.get("is_spicy", False)
        ]

    def validate(self, selections, total_weight_g, mode="STANDARD"):
        errors, warnings = [], []
        settings = self.data["settings"]

        if total_weight_g < settings["min_custom_weight_g"]:
            errors.append(f"Минимальный вес: {settings['min_custom_weight_g']} г")
        if total_weight_g % settings["weight_step_g"] != 0:
            errors.append(f"Вес должен быть кратен {settings['weight_step_g']} г")

        for opt_id, opt in self.options.items():
            selected = selections.get(opt_id, [])
            if opt["is_required"] and not selected:
                errors.append(f"Выберите: {opt['name']}")
            if len(selected) < opt["min_selections"]:
                errors.append(f"Минимум {opt['min_selections']} в группе «{opt['name']}»")
            if len(selected) > opt["max_selections"]:
                errors.append(f"Максимум {opt['max_selections']} в группе «{opt['name']}»")

        availability = self.get_available_ingredients()
        all_selected_ids = []
        for opt_id, ing_ids in selections.items():
            for ing_id in ing_ids:
                all_selected_ids.append(ing_id)
                if not availability.get(ing_id, {}).get("is_available", False):
                    ing = self.ingredients.get(ing_id, {})
                    errors.append(f"«{ing.get('name', ing_id)}» временно недоступен")

        if mode == "CHILDREN":
            forbidden = set(self.get_children_filtered_ingredients())
            for ing_id in all_selected_ids:
                if ing_id in forbidden:
                    ing = self.ingredients.get(ing_id, {})
                    errors.append(f"«{ing.get('name', ing_id)}» не подходит для детского питания")

        for i in range(len(all_selected_ids)):
            for j in range(i + 1, len(all_selected_ids)):
                a, b = all_selected_ids[i], all_selected_ids[j]
                for rule in self.data["compatibility_rules"]:
                    if {rule["ingredient_a_id"], rule["ingredient_b_id"]} == {a, b}:
                        if rule["rule_type"] == "BLOCKED":
                            errors.append(rule["reason"])
                        elif rule["rule_type"] == "WARNING":
                            warnings.append(rule["reason"])

        allergens = set()
        for ing_id in all_selected_ids:
            allergens.update(self.ingredients.get(ing_id, {}).get("allergens", []))
        if mode == "CHILDREN" and allergens:
            warnings.append(f"Режим «Детское»: обнаружены аллергены — {', '.join(allergens)}")

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
            "price": self._calculate_price(selections, total_weight_g),
            "nutrition": self._calculate_nutrition(selections, total_weight_g),
            "allergens": list(allergens),
        }

    def _calculate_price(self, selections, total_weight_g):
        settings = self.data["settings"]
        total_cost = Decimal("0")
        breakdown = {}

        for opt_id, ing_ids in selections.items():
            opt = self.options[opt_id]
            if not ing_ids:
                continue
            group_share = Decimal(str(opt["default_percentage"])) / Decimal("100")
            per = group_share / Decimal(str(len(ing_ids)))
            group_cost = Decimal("0")
            for ing_id in ing_ids:
                ing = self.ingredients[ing_id]
                weight_kg = Decimal(total_weight_g) * per / Decimal("1000")
                group_cost += Decimal(str(ing["price_per_unit"])) * weight_kg
            # Детализация — с копейками
            breakdown[opt["name"]] = float(group_cost.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            total_cost += group_cost

        labor = total_cost * Decimal(str(settings["labor_margin"]))
        total_cost += labor
        breakdown["Работа производства"] = float(labor.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

        retail = total_cost * Decimal(str(settings["retail_margin"]))
        price_per_kg = retail / (Decimal(total_weight_g) / Decimal("1000"))

        # 🆕 ИТОГО округляется до целых рублей (ROUND_HALF_UP — честное математическое округление)
        return {
            "cost": float(total_cost.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
            "retail": float(retail.quantize(Decimal("1"), rounding=ROUND_HALF_UP)),  # ЦЕЛЫЕ РУБЛИ
            "price_per_kg": float(price_per_kg.quantize(Decimal("1"), rounding=ROUND_HALF_UP)),  # ЦЕЛЫЕ РУБЛИ
            "breakdown": breakdown,
        }

    def _calculate_nutrition(self, selections, total_weight_g):
        kcal = proteins = fats = carbs = Decimal("0")
        for opt_id, ing_ids in selections.items():
            opt = self.options[opt_id]
            if not ing_ids:
                continue
            share = Decimal(str(opt["default_percentage"])) / Decimal("100") / Decimal(str(len(ing_ids)))
            for ing_id in ing_ids:
                ing = self.ingredients[ing_id]
                kcal += Decimal(str(ing["kcal_per_100g"])) * share
                proteins += Decimal(str(ing["proteins_per_100g"])) * share
                fats += Decimal(str(ing["fats_per_100g"])) * share
                carbs += Decimal(str(ing["carbs_per_100g"])) * share
        return {
            "kcal": float(kcal.quantize(Decimal("0.1"))),
            "proteins": float(proteins.quantize(Decimal("0.1"))),
            "fats": float(fats.quantize(Decimal("0.1"))),
            "carbs": float(carbs.quantize(Decimal("0.1"))),
        }

    def generate_bom(self, selections, total_weight_g):
        bom = []
        for opt_id, ing_ids in selections.items():
            opt = self.options[opt_id]
            if not ing_ids:
                continue
            per = Decimal(str(opt["default_percentage"])) / Decimal("100") / Decimal(str(len(ing_ids)))
            for ing_id in ing_ids:
                bom.append({
                    "ingredient_id": ing_id,
                    "ingredient_name": self.ingredients[ing_id]["name"],
                    "quantity_g": int(Decimal(total_weight_g) * per),
                })
        return bom


class RawMaterialsWMS:
    def __init__(self, data):
        self.data = data

    def reserve(self, order_id, bom):
        with data_lock:
            reservations = []
            for item in bom:
                ing_id = item["ingredient_id"]
                needed_kg = Decimal(item["quantity_g"]) / Decimal("1000")
                batches = sorted(
                    [b for b in self.data["raw_material_batches"]
                     if b["ingredient_id"] == ing_id and b["status"] == "AVAILABLE"],
                    key=lambda b: b["expiration_date"]
                )
                reserved_total = Decimal("0")
                batches_used = []
                for batch in batches:
                    available = Decimal(str(batch["current_quantity"])) - Decimal(str(batch["reserved_quantity"]))
                    if available <= 0:
                        continue
                    take = min(available, needed_kg - reserved_total)
                    idx = get_index_by_id(self.data["raw_material_batches"], batch["id"])
                    self.data["raw_material_batches"][idx]["reserved_quantity"] = float(
                        Decimal(str(self.data["raw_material_batches"][idx]["reserved_quantity"])) + take
                    )
                    batches_used.append({
                        "batch_id": batch["id"],
                        "batch_number": batch["batch_number"],
                        "expiration_date": batch["expiration_date"],
                        "quantity_g": int(take * 1000),
                    })
                    reserved_total += take
                    if reserved_total >= needed_kg:
                        break
                if reserved_total < needed_kg:
                    return {"success": False, "error": "Недостаточно сырья"}
                reservations.append({
                    "ingredient_id": ing_id,
                    "batches_used": batches_used,
                    "reserved_g": int(reserved_total * 1000),
                })
            save_data(self.data)
            return {"success": True, "reservations": reservations}


class OrderService:
    def __init__(self, data):
        self.data = data

    def create_order(self, items, delivery_slot_id, address, comment=""):
        with data_lock:
            slot_idx = get_index_by_id(self.data["delivery_slots"], delivery_slot_id)
            if slot_idx == -1:
                return {"success": False, "error": "Слот не найден"}
            slot = self.data["delivery_slots"][slot_idx]
            if slot["current_orders"] >= slot["max_orders"]:
                return {"success": False, "error": "Слот переполнен"}

            today_str = datetime.now().strftime("%Y%m%d")
            seq = len([o for o in self.data["orders"] if today_str in o["order_number"]]) + 1
            order_number = f"ORD-{today_str}-{seq:04d}"

            types = {item["product_type"] for item in items}
            if types == {"CUSTOM"}:
                order_type = "CUSTOM"
            elif types == {"STANDARD"}:
                order_type = "STANDARD"
            else:
                order_type = "MIXED"

            order = {
                "id": str(uuid.uuid4()),
                "order_number": order_number,
                "user_id": "demo_user",
                "status": "NEW",
                "order_type": order_type,
                "items": items,
                "custom_recipe_snapshot": next(
                    (i.get("recipe_snapshot") for i in items if i["product_type"] == "CUSTOM"),
                    None
                ),
                "total_weight_g": sum(i["requested_weight_g"] for i in items),
                "total_amount": sum(i["line_total"] for i in items),
                "final_amount": None,
                "delivery_slot_id": delivery_slot_id,
                "delivery_address": address,
                "customer_comment": comment,
                "created_at": datetime.now().isoformat(),
            }

            self.data["orders"].append(order)
            self.data["delivery_slots"][slot_idx]["current_orders"] += 1
            save_data(self.data)
            return {"success": True, "order": order}

    def update_status(self, order_id, new_status):
        with data_lock:
            idx = get_index_by_id(self.data["orders"], order_id)
            if idx == -1:
                return {"success": False, "error": "Заказ не найден"}
            self.data["orders"][idx]["status"] = new_status
            save_data(self.data)
            return {"success": True, "order": self.data["orders"][idx]}


# ============================================
# 7. БАЗОВЫЕ МАРШРУТЫ
# ============================================
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/ingredients")
def api_ingredients():
    data = load_data()
    engine = ConfiguratorEngine(data)
    availability = engine.get_available_ingredients()
    forbidden = set(engine.get_children_filtered_ingredients())
    return jsonify({
        ing["id"]: {
            **ing,
            "available_kg": availability[ing["id"]]["available_kg"],
            "is_available": availability[ing["id"]]["is_available"],
            "forbidden_for_children": ing["id"] in forbidden,
        }
        for ing in data["ingredients"]
    })


@app.route("/api/options")
def api_options():
    return jsonify(load_data()["configurator_options"])


@app.route("/api/validate", methods=["POST"])
def api_validate():
    p = request.json
    return jsonify(ConfiguratorEngine(load_data()).validate(
        p.get("selections", {}),
        p.get("total_weight_g", 1000),
        p.get("mode", "STANDARD"),
    ))


@app.route("/api/slots")
def api_slots():
    data = load_data()
    order_type = request.args.get("order_type", "STANDARD")
    return jsonify({
        "slots": [
            {**s, "available_capacity": s["max_orders"] - s["current_orders"]}
            for s in data["delivery_slots"]
            if s["current_orders"] < s["max_orders"]
            and (order_type != "CUSTOM" or s["is_custom_available"])
        ]
    })


@app.route("/api/orders", methods=["POST"])
def api_create_order():
    p = request.json
    data = load_data()
    order_service = OrderService(data)
    engine = ConfiguratorEngine(data)
    wms = RawMaterialsWMS(data)

    for item in p.get("items", []):
        if item["product_type"] == "CUSTOM":
            bom = engine.generate_bom(
                item["recipe_snapshot"]["selections"],
                item["requested_weight_g"]
            )
            res = wms.reserve("pending", bom)
            if not res["success"]:
                return jsonify({"success": False, "error": res["error"]}), 400

    result = order_service.create_order(
        p.get("items", []),
        p.get("delivery_slot_id"),
        p.get("delivery_address", {}),
        p.get("customer_comment", ""),
    )
    if not result["success"]:
        return jsonify(result), 400

    order_service.update_status(result["order"]["id"], "CONFIRMED")
    return jsonify(result)


@app.route("/api/orders")
def api_list_orders():
    return jsonify({"orders": load_data()["orders"]})


# ============================================
# 8. AI ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЙ
# ============================================

BASE_PROMPTS = {
    "beef": "Freshly cut cross-section of premium beef sausage, cut surface is main focus showing ruby-red meat with fat marbling. NOT whole uncut sausage. Rustic wooden board, food photography, studio lighting, 8k, detailed texture, no text",
    "pork": "Freshly cut cross-section of pork sausage, cut surface is main focus showing pink meat with white fat marbling. NOT whole uncut sausage. Rustic wooden board, food photography, studio lighting, 8k, detailed texture, no text",
    "turkey": "Freshly cut cross-section of turkey sausage, cut surface is main focus showing light beige-pink lean meat. NOT whole uncut sausage. Rustic wooden board, food photography, studio lighting, 8k, detailed texture, no text",
    "chicken": "Freshly cut cross-section of chicken sausage, cut surface is main focus showing pale cream-pink delicate meat. NOT whole uncut sausage. Rustic wooden board, food photography, studio lighting, 8k, detailed texture, no text",
}


def generate_via_pollinations(prompt: str, filepath: Path, seed: int = 42) -> tuple:
    """Генерация через Pollinations (text2img)"""
    encoded_prompt = urllib.request.quote(prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=800&height=400&seed={seed}&nologo=true"

    print(f"🎨 [Pollinations] Генерация: {filepath.name}")
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "image/*,*/*;q=0.8",
            }
        )
        with urllib.request.urlopen(req, timeout=45, context=SSL_CONTEXT) as response:
            if response.status == 402:
                return False, "Лимит бесплатных запросов исчерпан"
            image_data = response.read()
            with open(filepath, "wb") as f:
                f.write(image_data)
            print(f"✅ [Pollinations] Сохранено: {filepath} ({len(image_data)} байт)")
            return True, None
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.reason}"
    except (urllib.error.URLError, TimeoutError, ConnectionResetError) as e:
        return False, f"Timeout/Network error: {e}"
    except Exception as e:
        return False, str(e)


def generate_recipe_img2img(base_image_path: Path, prompt: str, strength: float = 0.6) -> tuple:
    """Генерация через Hugging Face (img2img)"""
    if not HF_API_TOKEN:
        return False, "Hugging Face API token not configured"

    try:
        with open(base_image_path, "rb") as f:
            image_data = f.read()
        image_b64 = base64.b64encode(image_data).decode("utf-8")

        api_url = "https://api-inference.huggingface.co/models/runwayml/stable-diffusion-v1-5"
        headers = {
            "Authorization": f"Bearer {HF_API_TOKEN}",
            "Content-Type": "application/json",
        }
        payload = {
            "inputs": prompt,
            "parameters": {
                "image": f"data:image/jpeg;base64,{image_b64}",
                "strength": strength,
                "guidance_scale": 7.5,
                "num_inference_steps": 30,
            }
        }

        print(f"🎨 [HuggingFace] Отправка img2img запроса...")
        req = urllib.request.Request(
            api_url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST"
        )

        with urllib.request.urlopen(req, timeout=60, context=SSL_CONTEXT) as response:
            result_data = response.read()

            if result_data.startswith(b"\x89PNG") or result_data.startswith(b"\xff\xd8\xff"):
                print(f"✅ [HuggingFace] Изображение получено ({len(result_data)} байт)")
                return True, result_data
            else:
                try:
                    error_json = json.loads(result_data)
                    return False, f"API error: {error_json.get('error', 'Unknown error')}"
                except Exception:
                    return False, "Invalid response from API"

    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="ignore")
        print(f"❌ [HuggingFace] HTTP Error {e.code}: {error_body}")
        return False, f"HTTP {e.code}: {error_body}"
    except urllib.error.URLError as e:
        print(f"❌ [HuggingFace] Network error: {e}")
        return False, f"Network error: {e}"
    except Exception as e:
        print(f"❌ [HuggingFace] Ошибка: {e}")
        return False, str(e)


def build_img2img_prompt(selections: dict, ingredients: dict, options: dict) -> str:
    """Промпт для img2img (HF)"""
    fillers, spices = [], []
    for opt_id, ing_ids in selections.items():
        opt = options.get(opt_id, {})
        for ing_id in ing_ids:
            ing = ingredients.get(ing_id, {})
            ing_name = translate_ingredient(ing.get("name", ""))
            if "FILLER" in opt.get("categories", []):
                fillers.append(ing_name)
            elif "SPICE" in opt.get("categories", []):
                spices.append(ing_name)

    all_inclusions = fillers + spices
    if all_inclusions:
        return (
            f"Add visible chunks of {', '.join(all_inclusions)} into the sausage meat cross-section. "
            f"The ingredients should be naturally embedded in the meat, distributed throughout the cross-section. "
            f"Maintain the same lighting, style, and quality as the original image. "
            f"Photorealistic, highly detailed, professional food photography"
        )
    return (
        f"Enhance the sausage cross-section, make it more appetizing. "
        f"Improve lighting and details. Maintain photorealistic quality. "
        f"Professional food photography, 8k quality"
    )


def build_text2img_prompt(selections: dict, weight_g: int, ingredients: dict, options: dict) -> str:
    """Полный промпт для text2img (Pollinations fallback)"""
    meats, fillers, spices, shell = [], [], [], None
    for opt_id, ing_ids in selections.items():
        opt = options.get(opt_id, {})
        for ing_id in ing_ids:
            ing = ingredients.get(ing_id, {})
            name = translate_ingredient(ing.get("name", ""))
            if "MEAT_BASE" in opt.get("categories", []):
                meats.append(name)
            elif "FILLER" in opt.get("categories", []):
                fillers.append(name)
            elif "SPICE" in opt.get("categories", []):
                spices.append(name)
            elif "SHELL" in opt.get("categories", []):
                shell = name

    meat_desc = f"{meats[0]} meat" if len(meats) == 1 else f"mix of {', '.join(meats)}"
    inclusions = ", ".join(fillers + spices)
    inclusions_desc = (
        f"with visible chunks of {inclusions} embedded in the meat"
        if inclusions else "pure meat with natural fat marbling"
    )
    size_desc = "large thick" if weight_g >= 2000 else ("medium" if weight_g >= 1500 else "compact")

    return (
        f"Photorealistic close-up cross-section of a {size_desc} {meat_desc} sausage "
        f"{inclusions_desc}, {shell or 'natural casing'} on edge. "
        f"Cut surface is main focus showing meat fibers and ingredients inside. "
        f"NOT whole uncut sausage. Rustic wooden board, food photography, studio lighting, "
        f"shallow depth of field, warm tones, 8k, highly detailed, no text"
    )


# ============================================
# 9. AI МАРШРУТЫ
# ============================================
@app.route("/api/generate-base-image")
def api_generate_base_image():
    meat_type = request.args.get("meat_type", "beef")
    filepath = STATIC_DIR / f"sausage_{meat_type}.jpg"

    if is_valid_cached_image(filepath):
        image_url = f"/static/{filepath.name}"
        file_size = filepath.stat().st_size
        print(f"📦 {meat_type}: из кэша ({file_size} байт)")
        return jsonify({
            "success": True,
            "image_url": image_url,
            "meat_type": meat_type,
            "cached": True,
            "file_size": file_size,
        })

    seed = random.randint(1, 999999)
    success, error = generate_via_pollinations(
        BASE_PROMPTS.get(meat_type, BASE_PROMPTS["beef"]),
        filepath,
        seed
    )

    if success:
        file_size = filepath.stat().st_size
        return jsonify({
            "success": True,
            "image_url": f"/static/{filepath.name}",
            "meat_type": meat_type,
            "cached": False,
            "file_size": file_size,
            "seed": seed,
        })
    return jsonify({"success": False, "error": error, "meat_type": meat_type, "cached": False}), 500


@app.route("/api/regenerate-base-image", methods=["POST"])
def api_regenerate_base_image():
    data = request.json or {}
    meat_type = data.get("meat_type", "beef")
    filepath = STATIC_DIR / f"sausage_{meat_type}.jpg"
    seed = random.randint(1, 999999)

    success, error = generate_via_pollinations(
        BASE_PROMPTS.get(meat_type, BASE_PROMPTS["beef"]),
        filepath,
        seed
    )

    if success:
        return jsonify({
            "success": True,
            "image_url": f"/static/{filepath.name}",
            "meat_type": meat_type,
            "seed": seed,
        })
    return jsonify({"success": False, "error": error}), 500


@app.route("/api/generate-recipe-image", methods=["POST"])
def api_generate_recipe_image():
    """
    Генерация AI-изображения рецепта.
    Стратегия:
      1. Если HF_API_TOKEN настроен → пробуем img2img через Hugging Face
      2. Если HF недоступен (ошибка DNS/сети) → fallback на Pollinations text2img
    Кэшируется по хэшу рецепта.
    """
    data = request.json or {}
    selections = data.get("selections", {})
    weight_g = data.get("weight_g", 1000)
    force_regenerate = data.get("regenerate", False)

    if not selections or not selections.get("opt_meat"):
        return jsonify({"success": False, "error": "Выберите хотя бы мясо"}), 400

    engine = ConfiguratorEngine(load_data())

    meat_ids = selections.get("opt_meat", [])
    first_meat_id = meat_ids[0]
    if "pork" in first_meat_id:
        meat_type = "pork"
    elif "turkey" in first_meat_id:
        meat_type = "turkey"
    elif "chicken" in first_meat_id:
        meat_type = "chicken"
    else:
        meat_type = "beef"

    recipe_hash = compute_recipe_hash(selections, weight_g)
    output_path = STATIC_DIR / f"recipe_{recipe_hash}.jpg"

    if not force_regenerate and is_valid_cached_image(output_path):
        image_url = f"/static/{output_path.name}"
        file_size = output_path.stat().st_size
        print(f"📦 Рецепт {recipe_hash}: из кэша ({file_size} байт)")
        return jsonify({
            "success": True,
            "image_url": image_url,
            "recipe_hash": recipe_hash,
            "cached": True,
            "file_size": file_size,
            "method": "cache",
        })

    total_inclusions = (
        len(selections.get("opt_filler", [])) +
        len(selections.get("opt_spice", []))
    )
    if total_inclusions == 0:
        strength = 0.35
    elif total_inclusions <= 2:
        strength = 0.55
    else:
        strength = 0.70

    # === ПОПЫТКА 1: Hugging Face img2img ===
    if HF_API_TOKEN:
        base_image_path = STATIC_DIR / f"sausage_{meat_type}.jpg"
        if base_image_path.exists():
            prompt = build_img2img_prompt(selections, engine.ingredients, engine.options)
            print(f"🎨 Генерация рецепта {recipe_hash} через Hugging Face img2img...")

            success, result = generate_recipe_img2img(base_image_path, prompt, strength)

            if success:
                with open(output_path, "wb") as f:
                    f.write(result)
                file_size = len(result)
                print(f"✅ Рецепт {recipe_hash} сохранён через HF ({file_size} байт)")
                return jsonify({
                    "success": True,
                    "image_url": f"/static/{output_path.name}",
                    "recipe_hash": recipe_hash,
                    "cached": False,
                    "file_size": file_size,
                    "method": "huggingface_img2img",
                    "prompt": prompt,
                    "strength": strength,
                })
            else:
                print(f"⚠️  HF не сработал: {result}. Переключаемся на Pollinations...")

    # === ПОПЫТКА 2: Pollinations text2img (fallback) ===
    prompt = build_text2img_prompt(selections, weight_g, engine.ingredients, engine.options)
    print(f"🎨 Генерация рецепта {recipe_hash} через Pollinations (fallback)...")

    seed = random.randint(1, 999999)
    success, error = generate_via_pollinations(prompt, output_path, seed)

    if success:
        file_size = output_path.stat().st_size
        return jsonify({
            "success": True,
            "image_url": f"/static/{output_path.name}",
            "recipe_hash": recipe_hash,
            "cached": False,
            "file_size": file_size,
            "method": "pollinations_text2img",
            "prompt": prompt,
        })

    return jsonify({
        "success": False,
        "error": error,
        "recipe_hash": recipe_hash,
    }), 500


@app.route("/api/recipe-cache-status")
def api_recipe_cache_status():
    recipes = []
    for f in STATIC_DIR.glob("recipe_*.jpg"):
        if f.stat().st_size >= MIN_FILE_SIZE:
            recipes.append({
                "filename": f.name,
                "hash": f.stem.replace("recipe_", ""),
                "size": f.stat().st_size,
                "url": f"/static/{f.name}",
            })
    return jsonify({"count": len(recipes), "recipes": recipes})


# ============================================
# 10. ТОЧКА ВХОДА (СТРОГО В КОНЦЕ!)
# ============================================
if __name__ == "__main__":
    if not Path("data.backup.json").exists():
        import shutil
        shutil.copy(DATA_FILE, "data.backup.json")

    print("=" * 60)
    print("🌭 SAUSAGE SHOP v20 — Clean & Rounded")
    print("=" * 60)

    base_count = len(list(STATIC_DIR.glob("sausage_*.jpg")))
    recipe_count = len(list(STATIC_DIR.glob("recipe_*.jpg")))
    print(f"📦 В кэше: {base_count} базовых + {recipe_count} рецептов")

    if HF_API_TOKEN:
        print("🤖 Hugging Face img2img: ДОСТУПЕН (с fallback на Pollinations)")
    else:
        print("🤖 Hugging Face img2img: НЕДОСТУПЕН (будет Pollinations)")

    print("💰 Суммы: округляются до целых рублей")
    print("=" * 60)
    print("🌐 http://localhost:5001")
    print("=" * 60)

    app.run(debug=True, port=5001)