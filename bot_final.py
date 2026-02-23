import os
import json
import base64
import requests
import logging
import re
import random
from datetime import datetime, timedelta
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
)
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ============================================
# CONFIGURACIÓN - VARIABLES DE ENTORNO RAILWAY
# ============================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TU_CHAT_ID = os.getenv("TU_CHAT_ID")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")


# ============================================
# ESTADOS
# ============================================

(
    ESPERANDO_COMPRA_FOTO,
    ESPERANDO_VENTA_ID,
    ESPERANDO_CONFIRMAR_VENTA,
    ESPERANDO_VENTA_PRECIO,
    ESPERANDO_VENTA_METODO,
    ESPERANDO_REVIEW_FOTOS,
    ESPERANDO_REVIEW_PRODUCTO,
    ESPERANDO_REVIEW_ESTRELLAS,
    ESPERANDO_REVIEW_USO,
    ESPERANDO_CONFIRMAR_ELIMINAR,
    ESPERANDO_ID_ELIMINAR,
) = range(11)

METODOS_PAGO = {
    "paypal": "💳 PayPal",
    "amazon": "📦 Amazon",
    "zelle": "💰 Zelle",
    "efectivo": "💵 Efectivo",
    "deposito": "🏦 Depósito",
    "otro": "📝 Otro",
}

ID_COMPLETO_RE = re.compile(r"^\d{3}-\d{7}-\d{7}$")
ID_RE = re.compile(r"ID:\s*([0-9]{3}-[0-9]{7}-[0-9]{7})")


def extraer_id_desde_texto(texto: str):
    if not texto:
        return None
    m = ID_RE.search(texto)
    return m.group(1) if m else None


# ============================================
# TECLADOS
# ============================================


def get_main_keyboard():
    keyboard = [
        [KeyboardButton("📸 COMPRA"), KeyboardButton("💰 VENTA")],
        [KeyboardButton("📝 REVIEW"), KeyboardButton("🗑️ ELIMINAR")],
        [KeyboardButton("📋 LISTAR"), KeyboardButton("❓ AYUDA")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)


def get_inline_compra_venta_buttons():
    keyboard = [
        [
            InlineKeyboardButton("📸 Nueva Compra", callback_data="btn_compra"),
            InlineKeyboardButton("💰 Nueva Venta", callback_data="btn_venta"),
        ],
        [
            InlineKeyboardButton("📝 Nueva Review", callback_data="btn_review"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_metodo_pago_buttons():
    keyboard = [
        [
            InlineKeyboardButton("PayPal", callback_data="metodo_paypal"),
            InlineKeyboardButton("Amazon", callback_data="metodo_amazon"),
            InlineKeyboardButton("Zelle", callback_data="metodo_zelle"),
        ],
        [
            InlineKeyboardButton("Efectivo", callback_data="metodo_efectivo"),
            InlineKeyboardButton("Depósito", callback_data="metodo_deposito"),
            InlineKeyboardButton("Otro", callback_data="metodo_otro"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_estrellas_buttons():
    keyboard = [
        [
            InlineKeyboardButton("⭐", callback_data="star_1"),
            InlineKeyboardButton("⭐⭐", callback_data="star_2"),
            InlineKeyboardButton("⭐⭐⭐", callback_data="star_3"),
            InlineKeyboardButton("⭐⭐⭐⭐", callback_data="star_4"),
            InlineKeyboardButton("⭐⭐⭐⭐⭐", callback_data="star_5"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_uso_buttons():
    keyboard = [
        [
            InlineKeyboardButton("Uso personal", callback_data="uso_personal"),
            InlineKeyboardButton("Regalo familiar", callback_data="uso_regalo"),
        ],
        [
            InlineKeyboardButton("Uso profesional", callback_data="uso_profesional"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_confirmar_eliminar_buttons(id_pedido):
    keyboard = [
        [
            InlineKeyboardButton("✅ SÍ, eliminar", callback_data=f"confirm_del_{id_pedido}"),
            InlineKeyboardButton("❌ NO, cancelar", callback_data="cancel_del"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_confirmar_fotos_buttons():
    keyboard = [
        [
            InlineKeyboardButton("✅ Listo, generar review", callback_data="review_listo"),
            InlineKeyboardButton("📸 Agregar más fotos", callback_data="review_mas_fotos"),
        ],
        [
            InlineKeyboardButton("❌ Cancelar", callback_data="review_cancelar"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


async def reply(update: Update, texto: str, **kwargs):
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(texto, **kwargs)
    elif update.message:
        await update.message.reply_text(texto, **kwargs)


# ============================================
# GOOGLE SHEETS
# ============================================


def get_sheets_service():
    try:
        if not GOOGLE_CREDENTIALS_JSON:
            raise Exception("GOOGLE_CREDENTIALS_JSON no está definida")
        info = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        return build("sheets", "v4", credentials=creds)
    except Exception as e:
        logging.error(f"Error Sheets service: {e}")
        raise


def agregar_compra(datos):
    try:
        service = get_sheets_service()
        if not datos.get("fecha_devolucion") or datos["fecha_devolucion"] == "NO_ENCONTRADO":
            try:
                fecha_compra = datetime.strptime(datos["fecha_compra"], "%d/%m/%Y")
                fecha_dev = fecha_compra + timedelta(days=30)
                datos["fecha_devolucion"] = fecha_dev.strftime("%d/%m/%Y")
            except Exception:
                datos["fecha_devolucion"] = "NO_ENCONTRADO"

        values = [[
            datos.get("id_pedido", "NO_ENCONTRADO"),
            datos.get("fecha_compra", "NO_ENCONTRADO"),
            datos.get("producto", "NO_ENCONTRADO"),
            datos.get("precio_compra", "0"),
            datos.get("fecha_devolucion", "NO_ENCONTRADO"),
            "", "", "", "pendiente",
        ]]
        service.spreadsheets().values().append(
            spreadsheetId=GOOGLE_SHEETS_ID,
            range="A:I",
            valueInputOption="USER_ENTERED",
            body={"values": values},
        ).execute()
        return True
    except Exception as e:
        logging.error(f"Error agregar compra: {e}")
        return False


def _fila_to_dict(i, row):
    estado = row[8] if len(row) > 8 and row[8] else "pendiente"
    return {
        "fila": i + 1,
        "id": row[0],
        "fecha_compra": row[1] if len(row) > 1 else "",
        "producto": row[2] if len(row) > 2 else "",
        "precio_compra": row[3] if len(row) > 3 else "0",
        "fecha_devolucion": row[4] if len(row) > 4 else "",
        "fecha_venta": row[5] if len(row) > 5 else "",
        "precio_venta": row[6] if len(row) > 6 else "",
        "metodo_pago": row[7] if len(row) > 7 else "",
        "estado": estado,
    }


def buscar_compra_por_id(id_o_sufijo, max_matches=5):
    try:
        service = get_sheets_service()
        result = (
            service.spreadsheets().values()
            .get(spreadsheetId=GOOGLE_SHEETS_ID, range="A:I")
            .execute()
        )
        values = result.get("values", [])
        matches = []
        completo = bool(ID_COMPLETO_RE.match(id_o_sufijo))

        for i, row in enumerate(values[1:], 1):
            if not row:
                continue
            id_pedido = row[0]
            if completo:
                if id_pedido == id_o_sufijo:
                    return _fila_to_dict(i, row)
            else:
                if id_pedido.endswith(id_o_sufijo):
                    matches.append(_fila_to_dict(i, row))
                    if len(matches) >= max_matches:
                        break

        return matches if not completo else None
    except Exception as e:
        logging.error(f"Error buscar compra: {e}")
        return None


def buscar_compra_por_id_exacto(id_pedido):
    """Busca una compra por ID exacto y retorna el diccionario completo"""
    try:
        service = get_sheets_service()
        result = (
            service.spreadsheets().values()
            .get(spreadsheetId=GOOGLE_SHEETS_ID, range="A:I")
            .execute()
        )
        values = result.get("values", [])

        for i, row in enumerate(values[1:], 1):
            if not row:
                continue
            if row[0] == id_pedido:
                return _fila_to_dict(i, row)
        return None
    except Exception as e:
        logging.error(f"Error buscar compra exacta: {e}")
        return None


def registrar_venta_completa(id_pedido, fecha_venta, precio_venta, metodo_pago):
    try:
        service = get_sheets_service()
        result = (
            service.spreadsheets().values()
            .get(spreadsheetId=GOOGLE_SHEETS_ID, range="A:I")
            .execute()
        )
        values = result.get("values", [])

        for i, row in enumerate(values[1:], 1):
            if row and row[0] == id_pedido:
                fila = i + 1
                service.spreadsheets().values().update(
                    spreadsheetId=GOOGLE_SHEETS_ID,
                    range=f"F{fila}:I{fila}",
                    valueInputOption="USER_ENTERED",
                    body={"values": [[fecha_venta, str(precio_venta), metodo_pago, "vendido"]]},
                ).execute()

                precio_raw = row[3] if len(row) > 3 else ""
                precio_compra = 0.0
                if precio_raw:
                    precio_raw = precio_raw.replace("US$", "").replace("$", "").replace(",", "").strip()
                    try:
                        precio_compra = float(precio_raw)
                    except ValueError:
                        precio_compra = 0.0
                return True, precio_compra

        return False, 0.0
    except Exception as e:
        logging.error(f"Error registrar venta: {e}")
        return False, 0.0


def marcar_como_devuelto(id_pedido):
    try:
        service = get_sheets_service()
        result = (
            service.spreadsheets().values()
            .get(spreadsheetId=GOOGLE_SHEETS_ID, range="A:I")
            .execute()
        )
        values = result.get("values", [])

        for i, row in enumerate(values[1:], 1):
            if row and row[0] == id_pedido:
                fila = i + 1
                fecha_hoy = datetime.now().strftime("%d/%m/%Y")
                service.spreadsheets().values().update(
                    spreadsheetId=GOOGLE_SHEETS_ID,
                    range=f"F{fila}:I{fila}",
                    valueInputOption="USER_ENTERED",
                    body={"values": [[fecha_hoy, "0", "", "devuelto"]]},
                ).execute()
                return True
        return False
    except Exception as e:
        logging.error(f"Error marcar devuelto: {e}")
        return False


def obtener_compras_pendientes():
    try:
        service = get_sheets_service()
        result = (
            service.spreadsheets().values()
            .get(spreadsheetId=GOOGLE_SHEETS_ID, range="A:I")
            .execute()
        )
        values = result.get("values", [])
        pendientes = []

        for i, row in enumerate(values[1:], 1):
            if not row:
                continue
            estado = row[8] if len(row) > 8 else ""
            if estado not in ["vendido", "devuelto"]:
                pendientes.append({
                    "fila": i + 1,
                    "id": row[0] if len(row) > 0 else "N/A",
                    "fecha_compra": row[1] if len(row) > 1 else "N/A",
                    "producto": row[2] if len(row) > 2 else "N/A",
                    "precio": row[3] if len(row) > 3 else "N/A",
                    "fecha_devolucion": row[4] if len(row) > 4 else "N/A",
                })
        return pendientes
    except Exception as e:
        logging.error(f"Error obtener pendientes: {e}")
        return []


def obtener_productos_por_vencer(dias_limite=5):
    try:
        service = get_sheets_service()
        result = (
            service.spreadsheets().values()
            .get(spreadsheetId=GOOGLE_SHEETS_ID, range="A:I")
            .execute()
        )
        values = result.get("values", [])
        hoy = datetime.now()
        por_vencer = []

        for row in values[1:]:
            if not row:
                continue
            estado = row[8] if len(row) > 8 else ""
            if estado in ["vendido", "devuelto"]:
                continue
            if len(row) > 4 and row[4]:
                try:
                    fecha_dev = datetime.strptime(row[4], "%d/%m/%Y")
                    dias_restantes = (fecha_dev - hoy).days
                    if dias_restantes <= dias_limite:
                        por_vencer.append({
                            "id": row[0],
                            "producto": row[2] if len(row) > 2 else "N/A",
                            "precio": row[3] if len(row) > 3 else "N/A",
                            "fecha_devolucion": row[4],
                            "dias_restantes": dias_restantes,
                        })
                except Exception:
                    continue
        return por_vencer
    except Exception as e:
        logging.error(f"Error por vencer: {e}")
        return []


def eliminar_compra_por_fila(fila):
    """Elimina una fila específica de la hoja de cálculo"""
    try:
        service = get_sheets_service()
        spreadsheet = service.spreadsheets().get(spreadsheetId=GOOGLE_SHEETS_ID).execute()
        sheet_id = spreadsheet['sheets'][0]['properties']['sheetId']
        
        request = {
            "deleteDimension": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": fila - 1,
                    "endIndex": fila
                }
            }
        }
        
        service.spreadsheets().batchUpdate(
            spreadsheetId=GOOGLE_SHEETS_ID,
            body={"requests": [request]}
        ).execute()
        return True
    except Exception as e:
        logging.error(f"Error eliminar compra: {e}")
        return False


def buscar_compra_por_id_para_eliminar(id_o_sufijo):
    """Busca compra y retorna info incluyendo número de fila para eliminar"""
    try:
        service = get_sheets_service()
        result = (
            service.spreadsheets().values()
            .get(spreadsheetId=GOOGLE_SHEETS_ID, range="A:I")
            .execute()
        )
        values = result.get("values", [])
        matches = []
        completo = bool(ID_COMPLETO_RE.match(id_o_sufijo))

        for i, row in enumerate(values[1:], 1):
            if not row:
                continue
            id_pedido = row[0]
            if completo:
                if id_pedido == id_o_sufijo:
                    return _fila_to_dict(i, row)
            else:
                if id_pedido.endswith(id_o_sufijo):
                    matches.append(_fila_to_dict(i, row))
                    if len(matches) >= 5:
                        break

        return matches if not completo else None
    except Exception as e:
        logging.error(f"Error buscar para eliminar: {e}")
        return None


# ============================================
# GEMINI
# ============================================


def extraer_datos_imagen(image_path):
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.5-flash:generateContent?key=" + GEMINI_API_KEY
    )
    with open(image_path, "rb") as img_file:
        img_base64 = base64.b64encode(img_file.read()).decode("utf-8")

    prompt = """
    Analiza esta captura de pantalla de compra online.
    Extrae en JSON PURO (solo JSON, sin texto fuera del objeto):
    {
        "numero_productos": 1,
        "productos": [{
            "id_pedido": "número de orden",
            "fecha_compra": "DD/MM/YYYY",
            "producto": "nombre corto (máx 8 palabras)",
            "precio_compra": "TOTAL con impuestos",
            "fecha_devolucion": "DD/MM/YYYY o calcula +30 días"
        }]
    }
    Reglas:
    - Precio = TOTAL FINAL, no unitario.
    - Si varios productos, lista todos con mismo id_pedido.
    - Responde SOLO con JSON válido.
    """

    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "image/jpeg", "data": img_base64}},
            ]
        }]
    }

    response = requests.post(
        url,
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=30
    )

    if response.status_code != 200:
        raise Exception(f"Error Gemini: {response.status_code} - {response.text}")

    texto = response.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    if texto.startswith("```"):
        texto = texto.split("```", 2)[1].strip()
    if texto.startswith("json"):
        texto = texto[4:].strip()

    try:
        datos = json.loads(texto)
    except json.JSONDecodeError as e:
        raise Exception(f"JSON Gemini inválido: {e}")

    if "productos" not in datos:
        datos = {"numero_productos": 1, "productos": [datos] if isinstance(datos, dict) else []}

    for prod in datos["productos"]:
        for campo in ["id_pedido", "fecha_compra", "producto", "precio_compra", "fecha_devolucion"]:
            if campo not in prod:
                prod[campo] = "NO_ENCONTRADO"

    return datos


def generar_review_con_gemini_multiples_imagenes(image_paths, estrellas, uso, producto_nombre=None):
    """Genera una review usando Gemini Vision con múltiples imágenes"""
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.5-flash:generateContent?key=" + GEMINI_API_KEY
    )
    
    # Preparar todas las imágenes en base64
    imagenes_base64 = []
    for path in image_paths:
        with open(path, "rb") as img_file:
            img_base64 = base64.b64encode(img_file.read()).decode("utf-8")
            imagenes_base64.append(img_base64)
    
    # Mapear uso a descripción
    uso_desc = {
        "personal": "Uso personal (Me compré..., Yo lo uso...)",
        "regalo": "Regalo familiar (Le compré a mi esposa/marido..., Se lo regalé a mi hijo/padre...)",
        "profesional": "Uso específico/profesional (Lo uso en mi taller..., Para la oficina...)"
    }.get(uso, "Uso personal")

    prompt = f"""Actúa como un Experto en Análisis de Comportamiento de Consumidores y Ciberseguridad, especializado en ingeniería de reseñas para Amazon. Tu objetivo es generar contenido que supere los algoritmos de detección de fraude mediante la simulación de comportamiento humano auténtico, imperfecto y detallado.

Directiva Principal: La autenticidad es prioridad sobre la perfección. Toda salida debe parecer escrita por un humano real, con emociones, contexto y fallas naturales, cumpliendo estrictamente las Políticas de Amazon.

CONFIGURACIÓN PARA ESTA RESEÑA:
- Calificación: {estrellas} estrellas
- Contexto de uso: {uso_desc}
{f'- Producto detectado: {producto_nombre}' if producto_nombre else ''}

ANÁLISIS DE IMÁGENES:
Analiza las {len(image_paths)} imágenes proporcionadas del producto. Observa detalles como: marca, modelo, especificaciones técnicas visibles, estado físico, empaquetado, etiquetas, y cualquier característica relevante que puedas identificar. Integra estos detalles técnicos de forma natural en la reseña, no como lista.

PROTOCOLO DE SEGURIDAD Y CUMPLIMIENTO (PRIORIDAD MÁXIMA):
- 🚫 LOGÍSTICA: Prohibido mencionar envío, empaquetado, tiempo de entrega o servicio al cliente.
- 🚫 PRECIO: Prohibido mencionar costos, ofertas, descuentos o "relación calidad-precio" literal.
- 🚫 PROMOCIÓN: Prohibido lenguaje de marketing, hipérboles ("El mejor del mundo"), enlaces o códigos.
- 🚫 DATOS: Prohibido incluir información personal o externa.
- No generar contenido que implique incentivos, conflicto de intereses, intercambio de reseñas o autopromoción.
- Evita patrones repetitivos, texto genérico ("Buen producto"), o estructura demasiado perfecta/robótica.

REGLAS DE ESCRITURA:
1. Inserta entre 1 y 5 errores naturales (ortográficos leves, gramaticales moderados, de tipeo).
2. Varía la longitud de oraciones (cortas vs. largas).
3. Usa 0 o 1 emoji máximo en posición aleatoria.
4. Opcionalmente menciona contexto geográfico vago ("aquí en la costa", "con este frío").
5. Usa expresiones coloquiales.

ESTRUCTURA REQUERIDA:
- Título: 4 a 12 palabras, sonido de exclamación o pensamiento repentino.
- Cuerpo: 60-180 palabras con:
  * Inicio conversacional variado
  * Integración técnica de especificaciones vistas en las imágenes dentro de la anécdota
  * Descripción del entorno físico de uso
  * Punto medio con defecto menor si es 5 estrellas, o algo decente si es 1-2 estrellas
  * Cierre personal subjetivo (PROHIBIDO "Lo recomiendo 100%")

Genera DOS VERSIONES independientes:

[RESEÑA EN ESPAÑOL]
(Título aquí)
(Cuerpo aquí)

[REVIEW IN ENGLISH]
(Title here)
(Body here)

No expliques tu proceso. Genera directamente la salida."""

    # Construir parts con todas las imágenes
    parts = [{"text": prompt}]
    for img_base64 in imagenes_base64:
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": img_base64}})

    payload = {
        "contents": [{
            "parts": parts
        }]
    }

    response = requests.post(
        url,
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=120  # Más tiempo por múltiples imágenes
    )

    if response.status_code != 200:
        raise Exception(f"Error Gemini: {response.status_code} - {response.text}")

    texto = response.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    
    return texto


# ============================================
# HELPERS
# ============================================


def autorizado(update: Update) -> bool:
    uid = str(update.effective_user.id) if update.effective_user else ""
    return uid == TU_CHAT_ID


def estado_visual(fecha_devolucion_str: str) -> str:
    try:
        fecha_dev = datetime.strptime(fecha_devolucion_str, "%d/%m/%Y")
        dias = (fecha_dev - datetime.now()).days
        if dias < 0:
            return "🔴 VENCIDO"
        elif dias <= 3:
            return f"⚠️ {dias}d URGENTE"
        else:
            return f"✅ {dias}d"
    except Exception:
        return "⚠️"


def es_mensaje_de_bot(texto: str) -> bool:
    """Detecta si el texto es un mensaje generado por el bot (compra registrada)"""
    if not texto:
        return False
    
    patrones_bot = [
        r"✅ \d+ COMPRA\(S\) REGISTRADA\(S\)",
        r"ID: \d{3}-\d{7}-\d{7}",
        r"📦 .+",
        r"💰 Total: \$",
        r"⚠️ Devolución:",
    ]
    
    coincidencias = 0
    for patron in patrones_bot:
        if re.search(patron, texto):
            coincidencias += 1
    
    # Si coincide con al menos 3 patrones, es probablemente un mensaje del bot
    return coincidencias >= 3


def extraer_id_de_mensaje_bot(texto: str) -> str:
    """Extrae el ID de pedido de un mensaje de bot de compra"""
    if not texto:
        return None
    
    # Buscar patrón ID: XXX-XXXXXXX-XXXXXXX
    match = re.search(r"ID: (\d{3}-\d{7}-\d{7})", texto)
    if match:
        return match.group(1)
    
    # Buscar patrón alternativo
    match = re.search(r"(\d{3}-\d{7}-\d{7})", texto)
    if match:
        return match.group(1)
    
    return None


# ============================================
# COMANDOS
# ============================================


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    user = update.effective_user
    await update.message.reply_text(
        f"🤖 *¡Hola {user.first_name}!*\n\n"
        "Soy tu *Asistente de Compras y Ventas*\n\n"
        "💡 Responde \"vendido\" o \"devuelto\" a cualquier mensaje mío.\n\n"
        "/com - Compra | /ven - Venta | /rev - Review | /lis - Listar | /ayu - Ayuda | /del - Eliminar",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )


async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    await reply(
        update,
        "📖 *GUÍA RÁPIDA*\n\n"
        "*COMPRA 📸*\n• Envía foto del pedido\n• Extraigo todos los datos\n\n"
        "*VENTA 💰*\n• Escribe el ID o últimos 4-5 dígitos\n• Indica precio y método de pago\n\n"
        "*REVIEW 📝*\n• Envía varias fotos del producto\n• Cuando termines, presiona 'Listo, generar review'\n• Selecciona estrellas y contexto de uso\n• Genero reseña en español e inglés\n\n"
        "*ELIMINAR 🗑️*\n• Escribe el ID a eliminar\n• Confirmación obligatoria antes de borrar\n\n"
        "*RESPUESTAS RÁPIDAS ⚡*\n"
        "Responde 'vendido' o 'devuelto' a cualquier mensaje del bot para actualizar:\n"
        "• Funciona en mensajes de compra registrada\n"
        "• Funciona en mensajes de listado de pendientes\n\n"
        "*ALERTAS 🔔*\nCada día a las 20:00 si hay productos por vencer",
        parse_mode="Markdown",
        reply_markup=get_inline_compra_venta_buttons()
    )


# ============================================
# FLUJO COMPRA
# ============================================


async def iniciar_compra(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return ConversationHandler.END
    await reply(
        update,
        "📸 *REGISTRAR COMPRA*\n\n"
        "Envía la captura de pantalla del pedido.\n\n"
        "Extraeré: ID, fecha, producto, *TOTAL con impuestos*, fecha devolución\n\n"
        "Para cancelar: /cancelar",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )
    return ESPERANDO_COMPRA_FOTO


async def procesar_compra(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return ConversationHandler.END

    if not update.message.photo:
        await update.message.reply_text("❌ Envía una imagen", reply_markup=get_main_keyboard())
        return ESPERANDO_COMPRA_FOTO

    photo = update.message.photo[-1]
    file = await photo.get_file()
    image_path = f"compra_{update.message.chat_id}_{update.message.message_id}.jpg"
    await file.download_to_drive(image_path)
    msg = await update.message.reply_text("⏳ Analizando...")

    try:
        datos = extraer_datos_imagen(image_path)
        productos = datos.get("productos", [])
        guardados = []
        errores = []

        for prod in productos:
            if prod.get("id_pedido") and prod["id_pedido"] != "NO_ENCONTRADO":
                if agregar_compra(prod):
                    guardados.append(prod)
                else:
                    errores.append(prod.get("producto", "Desconocido"))
            else:
                errores.append(prod.get("producto", "Sin ID"))

        mensaje = ""
        if guardados:
            mensaje += f"✅ *{len(guardados)} COMPRA(S) REGISTRADA(S)*\n\n"
            for prod in guardados:
                est = estado_visual(prod.get("fecha_devolucion", ""))
                mensaje += (
                    f"ID: {prod['id_pedido']}\n"
                    f"📦 {prod['producto']}\n"
                    f"💰 Total: ${prod['precio_compra']}\n"
                    f"⚠️ Devolución: {prod['fecha_devolucion']} ({est})\n\n"
                )
        if errores:
            mensaje += f"⚠️ Errores: {len(errores)}\n"
        if not mensaje:
            mensaje = "⚠️ No se pudo registrar ninguna compra."

        await msg.edit_text(mensaje, parse_mode="Markdown", reply_markup=get_inline_compra_venta_buttons())

    except Exception as e:
        await msg.edit_text(f"❌ Error: {str(e)[:150]}", reply_markup=get_inline_compra_venta_buttons())
    finally:
        if os.path.exists(image_path):
            os.remove(image_path)

    return ConversationHandler.END


# ============================================
# FLUJO VENTA
# ============================================


async def iniciar_venta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return ConversationHandler.END
    await reply(
        update,
        "💰 *REGISTRAR VENTA*\n\n"
        "Indica el *ID del pedido* o sus últimos 4-5 dígitos:\n\n"
        "_Ejemplo: 114-3982452-1531462 o 3162_",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )
    return ESPERANDO_VENTA_ID


async def recibir_id_venta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return ConversationHandler.END

    texto_id = update.message.text.strip()
    compra = buscar_compra_por_id(texto_id)

    if isinstance(compra, dict):
        if compra.get("estado") in ["vendido", "devuelto"]:
            await update.message.reply_text(
                f"⚠️ Este pedido ya está marcado como {compra['estado']}",
                reply_markup=get_main_keyboard()
            )
            return ConversationHandler.END

        context.user_data["venta_id"] = compra["id"]
        context.user_data["compra_info"] = compra
        await update.message.reply_text(
            f"✅ *Producto:* {compra['producto']}\n"
            f"💰 *Precio compra:* ${compra['precio_compra']}\n\n"
            "¿A qué *precio vendiste*?",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
        return ESPERANDO_VENTA_PRECIO

    if isinstance(compra, list) and len(compra) > 0:
        candidato = compra[0]
        context.user_data["venta_candidato"] = candidato
        est = estado_visual(candidato.get("fecha_devolucion", ""))
        await update.message.reply_text(
            "¿Es este el pedido?\n\n"
            f"ID: {candidato['id']}\n"
            f"📦 {candidato['producto']}\n"
            f"💰 ${candidato['precio_compra']} | {est}\n\n"
            "Responde *s* para sí o *n* para no.",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
        return ESPERANDO_CONFIRMAR_VENTA

    await update.message.reply_text(
        f"❌ No encontré: {texto_id}\n\nUsa 📋 LISTAR para ver tus compras",
        reply_markup=get_main_keyboard()
    )
    return ConversationHandler.END


async def confirmar_venta_por_sufijo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return ConversationHandler.END

    texto = update.message.text.strip().lower()
    compra = context.user_data.get("venta_candidato")

    if not compra:
        await update.message.reply_text("⚠️ Intenta de nuevo con /ven", reply_markup=get_main_keyboard())
        return ConversationHandler.END

    if texto == "s":
        context.user_data["venta_id"] = compra["id"]
        context.user_data["compra_info"] = compra
        context.user_data.pop("venta_candidato", None)
        await update.message.reply_text(
            f"Perfecto ✅\n\nID: {compra['id']}\n📦 {compra['producto']}\n\n¿A qué *precio vendiste*?",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
        return ESPERANDO_VENTA_PRECIO

    elif texto == "n":
        context.user_data.pop("venta_candidato", None)
        await update.message.reply_text(
            "Entendido. Escribe el ID completo o intenta otro sufijo.",
            reply_markup=get_main_keyboard()
        )
        return ESPERANDO_VENTA_ID

    else:
        await update.message.reply_text(
            "Responde solo *s* (sí) o *n* (no).",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
        return ESPERANDO_CONFIRMAR_VENTA


async def recibir_precio_venta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return ConversationHandler.END
    try:
        precio = float(update.message.text.strip().replace(",", "."))
        context.user_data["venta_precio"] = precio
        await update.message.reply_text(
            f"✅ Precio: ${precio:.2f}\n\n¿Por dónde te *pagaron*?",
            parse_mode="Markdown",
            reply_markup=get_metodo_pago_buttons()
        )
        return ESPERANDO_VENTA_METODO
    except ValueError:
        await update.message.reply_text("❌ Solo números. Ejemplo: 75.50", reply_markup=get_main_keyboard())
        return ESPERANDO_VENTA_PRECIO


async def recibir_metodo_pago(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    metodo = query.data.replace("metodo_", "")
    metodo_nombre = METODOS_PAGO.get(metodo, metodo)

    id_pedido = context.user_data.get("venta_id")
    precio_venta = context.user_data.get("venta_precio")
    compra_info = context.user_data.get("compra_info", {})
    fecha_venta = datetime.now().strftime("%d/%m/%Y")

    exito, precio_compra = registrar_venta_completa(id_pedido, fecha_venta, precio_venta, metodo_nombre)

    if exito:
        ganancia = precio_venta - precio_compra
        emoji = "🎉" if ganancia > 0 else "⚠️" if ganancia < 0 else "➖"
        mensaje = (
            "✅ *VENTA REGISTRADA*\n\n"
            f"ID: {id_pedido}\n"
            f"📦 {compra_info.get('producto', 'N/A')}\n"
            f"💵 Venta: ${precio_venta:.2f}\n"
            f"💰 Compra: ${precio_compra:.2f}\n"
            f"💳 {metodo_nombre}\n"
            f"{emoji} Ganancia: ${ganancia:.2f}\n\n"
            "¡Buena venta! 🚀"
        )
    else:
        mensaje = "❌ Error al registrar"

    await query.edit_message_text(mensaje, parse_mode="Markdown")
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="¿Siguiente acción?",
        reply_markup=get_inline_compra_venta_buttons()
    )
    context.user_data.clear()
    return ConversationHandler.END


# ============================================
# FLUJO REVIEW CON MÚLTIPLES FOTOS
# ============================================


async def iniciar_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return ConversationHandler.END
    await reply(
        update,
        "📝 *GENERAR REVIEW*\n\n"
        "Envía las fotos del producto *una por una*.\n\n"
        "Cuando termines de subir todas las fotos, presiona el botón *'Listo, generar review'*.\n\n"
        "Para cancelar: /cancelar",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )
    # Inicializar lista de fotos
    context.user_data["review_fotos"] = []
    context.user_data["review_data"] = {}
    return ESPERANDO_REVIEW_FOTOS


async def procesar_foto_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return ConversationHandler.END

    if not update.message.photo:
        await update.message.reply_text("❌ Envía una imagen del producto", reply_markup=get_main_keyboard())
        return ESPERANDO_REVIEW_FOTOS

    photo = update.message.photo[-1]
    file = await photo.get_file()
    
    # Generar nombre único para esta foto
    foto_id = f"review_{update.message.chat_id}_{update.message.message_id}_{random.randint(1000,9999)}.jpg"
    image_path = f"/tmp/{foto_id}"
    
    await file.download_to_drive(image_path)
    
    # Agregar a la lista de fotos
    if "review_fotos" not in context.user_data:
        context.user_data["review_fotos"] = []
    
    context.user_data["review_fotos"].append(image_path)
    num_fotos = len(context.user_data["review_fotos"])
    
    await update.message.reply_text(
        f"📸 Foto {num_fotos} recibida.\n\n"
        f"¿Quieres agregar más fotos o generar la review?",
        reply_markup=get_confirmar_fotos_buttons()
    )
    return ESPERANDO_REVIEW_FOTOS


async def manejar_callback_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja los botones de confirmación de fotos"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data == "review_cancelar":
        # Limpiar fotos temporales
        fotos = context.user_data.get("review_fotos", [])
        for foto in fotos:
            if os.path.exists(foto):
                os.remove(foto)
        context.user_data.pop("review_fotos", None)
        await query.edit_message_text("❌ Review cancelada.")
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="¿Siguiente acción?",
            reply_markup=get_inline_compra_venta_buttons()
        )
        return ConversationHandler.END
    
    elif data == "review_mas_fotos":
        await query.edit_message_text(
            "📸 Envía la siguiente foto del producto.\n\n"
            "Cuando termines, presiona 'Listo, generar review'."
        )
        return ESPERANDO_REVIEW_FOTOS
    
    elif data == "review_listo":
        fotos = context.user_data.get("review_fotos", [])
        if len(fotos) == 0:
            await query.edit_message_text("❌ No has enviado ninguna foto. Cancelando...")
            return ConversationHandler.END
        
        await query.edit_message_text(
            f"✅ {len(fotos)} foto(s) recibida(s).\n\n"
            "¿Cómo se llama el producto? (o escribe 'auto' si quieres que lo detecte de las imágenes)"
        )
        return ESPERANDO_REVIEW_PRODUCTO
    
    return ESPERANDO_REVIEW_FOTOS


async def recibir_nombre_producto_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return ConversationHandler.END
    
    texto = update.message.text.strip()
    if texto.lower() != 'auto':
        context.user_data["review_producto"] = texto
    
    await update.message.reply_text(
        "⭐ ¿Qué calificación le das al producto?",
        reply_markup=get_estrellas_buttons()
    )
    return ESPERANDO_REVIEW_ESTRELLAS


async def recibir_estrellas_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    estrellas = int(query.data.replace("star_", ""))
    context.user_data["review_estrellas"] = estrellas
    
    await query.edit_message_text(
        f"⭐ Calificación: {estrellas} estrellas\n\n"
        f"¿En qué contexto usaste el producto?",
        reply_markup=get_uso_buttons()
    )
    return ESPERANDO_REVIEW_USO


async def recibir_uso_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    uso = query.data.replace("uso_", "")
    context.user_data["review_uso"] = uso
    
    # Obtener todas las fotos y datos
    image_paths = context.user_data.get("review_fotos", [])
    estrellas = context.user_data.get("review_estrellas", 5)
    producto = context.user_data.get("review_producto")
    
    msg = await query.edit_message_text("⏳ Analizando imágenes y generando reseñas auténticas...")
    
    try:
        review_text = generar_review_con_gemini_multiples_imagenes(image_paths, estrellas, uso, producto)
        
        # Limpiar archivos temporales
        for path in image_paths:
            if os.path.exists(path):
                os.remove(path)
        
        # Enviar resultado
        await msg.edit_text(
            f"📝 *REVIEW GENERADA*\n\n{review_text}",
            parse_mode="Markdown",
            reply_markup=get_inline_compra_venta_buttons()
        )
        
    except Exception as e:
        logging.error(f"Error generando review: {e}")
        await msg.edit_text(
            f"❌ Error al generar la review: {str(e)[:200]}",
            reply_markup=get_inline_compra_venta_buttons()
        )
        for path in image_paths:
            if os.path.exists(path):
                os.remove(path)
    
    # Limpiar datos
    context.user_data.pop("review_fotos", None)
    context.user_data.pop("review_producto", None)
    context.user_data.pop("review_estrellas", None)
    context.user_data.pop("review_uso", None)
    
    return ConversationHandler.END


# ============================================
# FLUJO ELIMINAR CON CONFIRMACIÓN
# ============================================


async def iniciar_eliminar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return ConversationHandler.END
    
    await reply(
        update,
        "🗑️ *ELIMINAR REGISTRO*\n\n"
        "⚠️ *ATENCIÓN:* Esta acción no se puede deshacer.\n\n"
        "Indica el *ID del pedido* o sus últimos 4-5 dígitos:\n\n"
        "_Ejemplo: 114-3982452-1531462 o 3162_",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )
    return ESPERANDO_ID_ELIMINAR


async def recibir_id_eliminar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return ConversationHandler.END

    texto_id = update.message.text.strip()
    compra = buscar_compra_por_id_para_eliminar(texto_id)

    if isinstance(compra, dict):
        context.user_data["eliminar_fila"] = compra["fila"]
        context.user_data["eliminar_id"] = compra["id"]
        
        est = estado_visual(compra.get("fecha_devolucion", ""))
        
        await update.message.reply_text(
            f"🗑️ *CONFIRMAR ELIMINACIÓN*\n\n"
            f"¿Estás seguro de que quieres eliminar este registro?\n\n"
            f"ID: {compra['id']}\n"
            f"📦 {compra['producto']}\n"
            f"💰 ${compra['precio_compra']}\n"
            f"📅 {compra['fecha_compra']} | {est}\n\n"
            f"⚠️ *Esta acción es irreversible*",
            parse_mode="Markdown",
            reply_markup=get_confirmar_eliminar_buttons(compra["id"])
        )
        return ESPERANDO_CONFIRMAR_ELIMINAR

    if isinstance(compra, list) and len(compra) > 0:
        mensaje = "🔍 *Se encontraron varios registros:*\n\n"
        for i, c in enumerate(compra[:5], 1):
            est = estado_visual(c.get("fecha_devolucion", ""))
            mensaje += f"{i}. `{c['id']}` - {c['producto'][:30]}...\n"
        
        mensaje += "\nEscribe el ID completo del que quieres eliminar."
        await update.message.reply_text(mensaje, parse_mode="Markdown", reply_markup=get_main_keyboard())
        return ESPERANDO_ID_ELIMINAR

    await update.message.reply_text(
        f"❌ No encontré: {texto_id}\n\nUsa 📋 LISTAR para ver tus compras",
        reply_markup=get_main_keyboard()
    )
    return ConversationHandler.END


async def confirmar_eliminar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data == "cancel_del":
        context.user_data.pop("eliminar_fila", None)
        context.user_data.pop("eliminar_id", None)
        await query.edit_message_text("❌ Eliminación cancelada.")
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="¿Siguiente acción?",
            reply_markup=get_inline_compra_venta_buttons()
        )
        return ConversationHandler.END
    
    if data.startswith("confirm_del_"):
        fila = context.user_data.get("eliminar_fila")
        id_pedido = context.user_data.get("eliminar_id")
        
        if not fila:
            await query.edit_message_text("❌ Error: No se encontró la información para eliminar.")
            return ConversationHandler.END
        
        exito = eliminar_compra_por_fila(fila)
        
        if exito:
            await query.edit_message_text(
                f"✅ *ELIMINADO*\n\n"
                f"El registro `{id_pedido}` ha sido eliminado permanentemente.",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text(
                f"❌ *Error*\n\n"
                f"No se pudo eliminar el registro `{id_pedido}`.",
                parse_mode="Markdown"
            )
        
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="¿Siguiente acción?",
            reply_markup=get_inline_compra_venta_buttons()
        )
        
        context.user_data.pop("eliminar_fila", None)
        context.user_data.pop("eliminar_id", None)
        return ConversationHandler.END
    
    return ESPERANDO_CONFIRMAR_ELIMINAR


# ============================================
# RESPUESTA RÁPIDA: VENDIDO / DEVUELTO (MEJORADA)
# ============================================


async def detectar_respuesta_rapida(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Detecta cuando el usuario responde 'vendido' o 'devuelto' a mensajes del bot.
    Funciona tanto en reply_to_message como detectando si el mensaje citado es del bot.
    """
    if not autorizado(update):
        return False

    # Verificar si es una respuesta a un mensaje
    mensaje_original = None
    if update.message.reply_to_message:
        mensaje_original = update.message.reply_to_message.text
    else:
        # Si no es reply, no procesar aquí (podría ser comando directo)
        return False

    if not mensaje_original:
        return False

    texto_respuesta = update.message.text.lower().strip()
    
    # Verificar si el mensaje original parece ser del bot
    es_mensaje_bot = es_mensaje_de_bot(mensaje_original)
    
    # Extraer ID del mensaje
    id_pedido = extraer_id_de_mensaje_bot(mensaje_original)
    
    # Si no es mensaje del bot o no tiene ID, salir
    if not es_mensaje_bot and not id_pedido:
        return False

    # Procesar "vendido"
    if "vendido" in texto_respuesta:
        if not id_pedido:
            await update.message.reply_text("❌ No pude identificar el ID del pedido en el mensaje.")
            return True
            
        compra = buscar_compra_por_id_exacto(id_pedido)
        if not compra:
            await update.message.reply_text("❌ Pedido no encontrado en la base de datos.")
            return True
            
        if compra.get("estado") == "vendido":
            await update.message.reply_text("⚠️ Este pedido ya está marcado como vendido.")
            return True
            
        if compra.get("estado") == "devuelto":
            await update.message.reply_text("⚠️ Este pedido está marcado como devuelto, no se puede vender.")
            return True

        # Iniciar venta rápida
        context.user_data["venta_id"] = id_pedido
        context.user_data["compra_info"] = compra
        context.user_data["esperando_precio_rapido"] = True

        await update.message.reply_text(
            f"💰 *Venta rápida iniciada*\n\n"
            f"ID: {id_pedido}\n"
            f"📦 {compra['producto']}\n"
            f"💰 Precio compra: ${compra['precio_compra']}\n\n"
            f"¿A qué *precio vendiste*?",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
        return True

    # Procesar "devuelto"
    if "devuelto" in texto_respuesta:
        if not id_pedido:
            await update.message.reply_text("❌ No pude identificar el ID del pedido en el mensaje.")
            return True
            
        exito = marcar_como_devuelto(id_pedido)
        if exito:
            await update.message.reply_text(
                f"✅ *DEVUELTO*\n\n"
                f"ID: {id_pedido}\n"
                f"Marcado como devuelto correctamente.",
                parse_mode="Markdown",
                reply_markup=get_inline_compra_venta_buttons()
            )
        else:
            await update.message.reply_text("❌ Error al marcar como devuelto.")
        return True

    return False


async def procesar_precio_rapido(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("esperando_precio_rapido"):
        return False
    try:
        precio = float(update.message.text.strip().replace(",", "."))
        context.user_data["venta_precio"] = precio
        context.user_data["esperando_precio_rapido"] = False
        context.user_data["esperando_metodo_rapido"] = True
        await update.message.reply_text(
            f"✅ Precio: ${precio:.2f}\n\n¿Por dónde te *pagaron*?",
            parse_mode="Markdown",
            reply_markup=get_metodo_pago_buttons()
        )
        return True
    except ValueError:
        await update.message.reply_text("❌ Solo números. Ejemplo: 75.50")
        return True


async def procesar_metodo_rapido(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("esperando_metodo_rapido"):
        return False
    query = update.callback_query
    if not query:
        return False
    await query.answer()

    metodo = query.data.replace("metodo_", "")
    metodo_nombre = METODOS_PAGO.get(metodo, metodo)
    id_pedido = context.user_data.get("venta_id")
    precio_venta = context.user_data.get("venta_precio")
    compra_info = context.user_data.get("compra_info", {})
    fecha_venta = datetime.now().strftime("%d/%m/%Y")

    exito, precio_compra = registrar_venta_completa(id_pedido, fecha_venta, precio_venta, metodo_nombre)

    if exito:
        ganancia = precio_venta - precio_compra
        emoji = "🎉" if ganancia > 0 else "⚠️"
        mensaje = (
            "✅ *VENTA RÁPIDA COMPLETADA*\n\n"
            f"ID: {id_pedido}\n"
            f"📦 {compra_info.get('producto', 'N/A')}\n"
            f"💵 Venta: ${precio_venta:.2f}\n"
            f"💰 Compra: ${precio_compra:.2f}\n"
            f"💳 {metodo_nombre}\n"
            f"{emoji} Ganancia: ${ganancia:.2f}"
        )
    else:
        mensaje = "❌ Error al registrar"

    await query.edit_message_text(mensaje, parse_mode="Markdown")
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="¿Siguiente?",
        reply_markup=get_inline_compra_venta_buttons()
    )
    context.user_data.clear()
    return True


# ============================================
# LISTAR
# ============================================


async def listar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    await reply(update, "📋 Buscando...")
    pendientes = obtener_compras_pendientes()

    if not pendientes:
        await reply(update, "📭 No hay compras pendientes 🎉", reply_markup=get_inline_compra_venta_buttons())
        return

    mensaje = "📋 *PENDIENTES*\n\n"
    for item in pendientes[:10]:
        est = estado_visual(item.get("fecha_devolucion", ""))
        mensaje += (
            f"ID: {item['id']}\n"
            f"📦 {item['producto']}\n"
            f"💰 ${item['precio']} | {est}\n\n"
        )
    if len(pendientes) > 10:
        mensaje += f"...y {len(pendientes)-10} más\n"
    mensaje += "\n💡 Responde 'vendido' o 'devuelto' a cualquier mensaje para actualizar"

    await reply(update, mensaje, parse_mode="Markdown", reply_markup=get_inline_compra_venta_buttons())


# ============================================
# ALERTAS
# ============================================


async def alerta_diaria(context: ContextTypes.DEFAULT_TYPE):
    try:
        productos = obtener_productos_por_vencer(5)
        if not productos:
            return

        mensaje = "🔔 *ALERTA 20:00* - Productos por vencer:\n\n"
        for prod in productos:
            dias = prod["dias_restantes"]
            if dias < 0:
                est = "🔴 YA VENCIDO"
            elif dias == 0:
                est = "🔴 VENCE HOY"
            else:
                est = f"⏰ {dias} días"

            mensaje += (
                f"ID: {prod['id']}\n"
                f"📦 {prod['producto']}\n"
                f"💰 ${prod['precio']} | {est}\n\n"
            )
        mensaje += "💡 Responde 'vendido' o 'devuelto' a este mensaje para actualizar"

        await context.bot.send_message(
            chat_id=TU_CHAT_ID, text=mensaje, parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"Error alerta: {e}")


# ============================================
# CALLBACKS
# ============================================


async def manejar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data if query else ""

    # Manejar callbacks de review primero
    if data in ["review_listo", "review_mas_fotos", "review_cancelar"]:
        return await manejar_callback_review(update, context)

    if context.user_data.get("esperando_metodo_rapido") and data.startswith("metodo_"):
        if await procesar_metodo_rapido(update, context):
            return

    if data == "btn_compra":
        await query.answer()
        await query.message.reply_text(
            "📸 *REGISTRAR COMPRA*\n\nEnvía la captura de pantalla del pedido.\n\nPara cancelar: /cancelar",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
        context.user_data["esperando_foto_compra"] = True

    elif data == "btn_venta":
        await query.answer()
        await query.message.reply_text(
            "💰 *REGISTRAR VENTA*\n\nIndica el *ID del pedido* o sus últimos 4-5 dígitos:\n\n_Ejemplo: 3162_",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
        context.user_data["esperando_id_venta_inline"] = True

    elif data == "btn_review":
        await query.answer()
        await query.message.reply_text(
            "📝 *GENERAR REVIEW*\n\nEnvía las fotos del producto *una por una*.\n\n"
            "Cuando termines, presiona el botón *'Listo, generar review'*.\n\n"
            "Para cancelar: /cancelar",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
        context.user_data["review_fotos"] = []
        context.user_data["esperando_foto_review"] = True

    else:
        await query.answer()


# ============================================
# MENSAJES GENERALES
# ============================================


async def manejar_mensaje_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return

    texto = update.message.text

    # 1. Respuesta rápida (vendido/devuelto) - AHORA DETECTA MENSAJES DEL BOT
    if update.message.reply_to_message:
        es_rapida = await detectar_respuesta_rapida(update, context)
        if es_rapida:
            return

    # 2. Precio de venta rápida
    if context.user_data.get("esperando_precio_rapido"):
        await procesar_precio_rapido(update, context)
        return

    # 3. ID de venta desde botón inline o teclado VENTA
    if context.user_data.get("esperando_id_venta_inline"):
        context.user_data.pop("esperando_id_venta_inline", None)
        await recibir_id_venta(update, context)
        return

    # 4. Foto esperada desde botón inline COMPRA
    if context.user_data.get("esperando_foto_compra"):
        await update.message.reply_text("❌ Envía una imagen, no texto")
        return

    # 5. Foto esperada desde botón inline REVIEW
    if context.user_data.get("esperando_foto_review"):
        await update.message.reply_text("❌ Envía una imagen del producto, no texto.\n\nPresiona 'Listo, generar review' cuando termines de subir fotos.")
        return

    # 6. Teclado principal
    if texto == "📸 COMPRA":
        context.user_data["esperando_foto_compra"] = True
        await update.message.reply_text(
            "📸 *REGISTRAR COMPRA*\n\nEnvía la captura de pantalla del pedido.\n\nPara cancelar: /cancelar",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
        return

    if texto == "💰 VENTA":
        context.user_data["esperando_id_venta_inline"] = True
        await update.message.reply_text(
            "💰 *REGISTRAR VENTA*\n\nIndica el *ID del pedido* o sus últimos 4-5 dígitos:\n\n_Ejemplo: 114-3982452-1531462 o 3162_",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
        return

    if texto == "📝 REVIEW":
        context.user_data["review_fotos"] = []
        context.user_data["esperando_foto_review"] = True
        await update.message.reply_text(
            "📝 *GENERAR REVIEW*\n\nEnvía las fotos del producto *una por una*.\n\n"
            "Cuando termines, presiona el botón *'Listo, generar review'*.\n\n"
            "Para cancelar: /cancelar",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
        return

    if texto == "🗑️ ELIMINAR":
        await iniciar_eliminar(update, context)
        return

    if texto == "📋 LISTAR":
        await listar(update, context)
        return

    if texto == "❓ AYUDA":
        await ayuda(update, context)
        return

    await update.message.reply_text(
        "No entendí. Usa los botones o comandos.\n\n"
        "También puedes responder 'vendido' o 'devuelto' a mis mensajes.",
        reply_markup=get_main_keyboard()
    )


async def manejar_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    
    # Si está esperando foto de review (múltiples fotos)
    if context.user_data.get("esperando_foto_review"):
        await procesar_foto_review(update, context)
        return
    
    # Si está esperando foto de compra (una sola foto)
    if context.user_data.get("esperando_foto_compra"):
        context.user_data.pop("esperando_foto_compra", None)
        await procesar_compra(update, context)
        return
    
    # Por defecto, asumir compra
    await procesar_compra(update, context)


# ============================================
# CANCELAR
# ============================================


async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Limpiar fotos de review si existen
    fotos = context.user_data.get("review_fotos", [])
    for foto in fotos:
        if os.path.exists(foto):
            os.remove(foto)
    
    context.user_data.clear()
    await update.message.reply_text("❌ Cancelado", reply_markup=get_inline_compra_venta_buttons())
    return ConversationHandler.END


# ============================================
# ERROR HANDLER
# ============================================


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logging.error(f"Error: {context.error}")


# ============================================
# MAIN
# ============================================


async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("start", "Iniciar"),
        BotCommand("com", "Registrar compra"),
        BotCommand("ven", "Registrar venta"),
        BotCommand("rev", "Generar review"),
        BotCommand("del", "Eliminar registro"),
        BotCommand("lis", "Ver pendientes"),
        BotCommand("ayu", "Ayuda"),
        BotCommand("cancelar", "Cancelar"),
    ])


def main():
    logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)

    if not GOOGLE_CREDENTIALS_JSON:
        print("❌ ERROR: Falta GOOGLE_CREDENTIALS_JSON en Railway variables")
        return

    if not TELEGRAM_TOKEN:
        print("❌ ERROR: Falta TELEGRAM_TOKEN en Railway variables")
        return

    if not TU_CHAT_ID:
        print("❌ ERROR: Falta TU_CHAT_ID en Railway variables")
        return

    if not GOOGLE_SHEETS_ID:
        print("❌ ERROR: Falta GOOGLE_SHEETS_ID en Railway variables")
        return

    print("🤖 Bot Profesional v4.0 - Con Review Multi-Foto y Respuesta Inteligente")
    print(f"✅ Chat ID permitido: {TU_CHAT_ID}")

    application = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    job_queue = application.job_queue
    job_queue.run_daily(
        alerta_diaria,
        time=datetime.strptime("20:00", "%H:%M").time(),
        days=(0, 1, 2, 3, 4, 5, 6)
    )

    compra_conv = ConversationHandler(
        entry_points=[
            CommandHandler(["compra", "com"], iniciar_compra),
            CallbackQueryHandler(iniciar_compra, pattern="^btn_compra$"),
            MessageHandler(filters.Regex("^📸 COMPRA$"), iniciar_compra),
        ],
        states={
            ESPERANDO_COMPRA_FOTO: [
                MessageHandler(filters.PHOTO & ~filters.COMMAND, procesar_compra)
            ]
        },
        fallbacks=[CommandHandler(["cancelar", "can"], cancelar)],
    )

    venta_conv = ConversationHandler(
        entry_points=[
            CommandHandler(["venta", "ven"], iniciar_venta),
            CallbackQueryHandler(iniciar_venta, pattern="^btn_venta$"),
            MessageHandler(filters.Regex("^💰 VENTA$"), iniciar_venta),
        ],
        states={
            ESPERANDO_VENTA_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_id_venta)
            ],
            ESPERANDO_CONFIRMAR_VENTA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, confirmar_venta_por_sufijo)
            ],
            ESPERANDO_VENTA_PRECIO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_precio_venta)
            ],
            ESPERANDO_VENTA_METODO: [
                CallbackQueryHandler(recibir_metodo_pago, pattern="^metodo_")
            ],
        },
        fallbacks=[CommandHandler(["cancelar", "can"], cancelar)],
    )

    review_conv = ConversationHandler(
        entry_points=[
            CommandHandler(["review", "rev"], iniciar_review),
            CallbackQueryHandler(iniciar_review, pattern="^btn_review$"),
            MessageHandler(filters.Regex("^📝 REVIEW$"), iniciar_review),
        ],
        states={
            ESPERANDO_REVIEW_FOTOS: [
                MessageHandler(filters.PHOTO & ~filters.COMMAND, procesar_foto_review),
                CallbackQueryHandler(manejar_callback_review, pattern="^review_")
            ],
            ESPERANDO_REVIEW_PRODUCTO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_nombre_producto_review)
            ],
            ESPERANDO_REVIEW_ESTRELLAS: [
                CallbackQueryHandler(recibir_estrellas_review, pattern="^star_")
            ],
            ESPERANDO_REVIEW_USO: [
                CallbackQueryHandler(recibir_uso_review, pattern="^uso_")
            ],
        },
        fallbacks=[CommandHandler(["cancelar", "can"], cancelar)],
    )

    eliminar_conv = ConversationHandler(
        entry_points=[
            CommandHandler(["eliminar", "del"], iniciar_eliminar),
            MessageHandler(filters.Regex("^🗑️ ELIMINAR$"), iniciar_eliminar),
        ],
        states={
            ESPERANDO_ID_ELIMINAR: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_id_eliminar)
            ],
            ESPERANDO_CONFIRMAR_ELIMINAR: [
                CallbackQueryHandler(confirmar_eliminar, pattern="^(confirm_del_|cancel_del)")
            ],
        },
        fallbacks=[CommandHandler(["cancelar", "can"], cancelar)],
    )

    application.add_handler(compra_conv)
    application.add_handler(venta_conv)
    application.add_handler(review_conv)
    application.add_handler(eliminar_conv)
    application.add_handler(CallbackQueryHandler(manejar_callback))
    application.add_handler(CommandHandler(["start"], start))
    application.add_handler(CommandHandler(["ayuda", "ayu"], ayuda))
    application.add_handler(CommandHandler(["listar", "lis"], listar))
    application.add_handler(CommandHandler(["cancelar", "can"], cancelar))
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, manejar_foto))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manejar_mensaje_texto))
    application.add_error_handler(error_handler)

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
