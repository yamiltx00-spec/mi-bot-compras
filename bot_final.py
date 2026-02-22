import os
import json
import base64
import requests
import logging
import re
import random
import string
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
    ESPERANDO_CONFIRMAR_BORRADO,
    CHAT_IA_ACTIVO,
) = range(8)

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

# ============================================
# TECLADOS
# ============================================

def get_main_keyboard():
    keyboard = [
        [KeyboardButton("📸 COMPRA"), KeyboardButton("💰 VENTA"), KeyboardButton("⭐ REVIEW")],
        [KeyboardButton("📋 LISTAR"), KeyboardButton("🗑️ BORRAR"), KeyboardButton("🤖 CHAT IA")],
        [KeyboardButton("❓ AYUDA")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)


def get_inline_compra_venta_buttons():
    keyboard = [
        [
            InlineKeyboardButton("📸 Compra", callback_data="btn_compra"),
            InlineKeyboardButton("💰 Venta", callback_data="btn_venta"),
            InlineKeyboardButton("⭐ Review", callback_data="btn_review"),
        ],
        [
            InlineKeyboardButton("🤖 Chat IA", callback_data="btn_chat_ia"),
            InlineKeyboardButton("🗑️ Borrar", callback_data="btn_borrar"),
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


def get_confirmar_borrado_buttons(pedido_id):
    keyboard = [
        [
            InlineKeyboardButton("✅ Sí, borrar", callback_data=f"confirm_borrar_{pedido_id}"),
            InlineKeyboardButton("❌ Cancelar", callback_data="cancelar_borrado"),
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
            "", "", "", "pendiente", "",
        ]]
        service.spreadsheets().values().append(
            spreadsheetId=GOOGLE_SHEETS_ID,
            range="A:J",
            valueInputOption="USER_ENTERED",
            body={"values": values},
        ).execute()
        return True
    except Exception as e:
        logging.error(f"Error agregar compra: {e}")
        return False


def _fila_to_dict(i, row):
    estado = row[8] if len(row) > 8 and row[8] else "pendiente"
    review = row[9] if len(row) > 9 and row[9] else ""
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
        "review": review,
    }


def buscar_compra_por_id(id_o_sufijo, max_matches=5):
    try:
        service = get_sheets_service()
        result = (
            service.spreadsheets().values()
            .get(spreadsheetId=GOOGLE_SHEETS_ID, range="A:J")
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


def registrar_venta_completa(id_pedido, fecha_venta, precio_venta, metodo_pago):
    try:
        service = get_sheets_service()
        result = (
            service.spreadsheets().values()
            .get(spreadsheetId=GOOGLE_SHEETS_ID, range="A:J")
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
            .get(spreadsheetId=GOOGLE_SHEETS_ID, range="A:J")
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


def guardar_review(id_pedido, review_text):
    """Guarda la review generada en la columna J"""
    try:
        service = get_sheets_service()
        result = (
            service.spreadsheets().values()
            .get(spreadsheetId=GOOGLE_SHEETS_ID, range="A:J")
            .execute()
        )
        values = result.get("values", [])

        for i, row in enumerate(values[1:], 1):
            if row and row[0] == id_pedido:
                fila = i + 1
                service.spreadsheets().values().update(
                    spreadsheetId=GOOGLE_SHEETS_ID,
                    range=f"J{fila}",
                    valueInputOption="USER_ENTERED",
                    body={"values": [[review_text]]},
                ).execute()
                return True
        return False
    except Exception as e:
        logging.error(f"Error guardar review: {e}")
        return False


def borrar_compra(id_pedido):
    """Elimina una fila completa de Google Sheets"""
    try:
        service = get_sheets_service()
        result = (
            service.spreadsheets().values()
            .get(spreadsheetId=GOOGLE_SHEETS_ID, range="A:J")
            .execute()
        )
        values = result.get("values", [])

        for i, row in enumerate(values[1:], 1):
            if row and row[0] == id_pedido:
                fila = i + 1
                # Borrar el contenido de la fila (no la fila en sí, pero la dejamos vacía)
                service.spreadsheets().values().clear(
                    spreadsheetId=GOOGLE_SHEETS_ID,
                    range=f"A{fila}:J{fila}",
                ).execute()
                return True, fila
        return False, None
    except Exception as e:
        logging.error(f"Error borrar compra: {e}")
        return False, None


def obtener_compras_pendientes():
    try:
        service = get_sheets_service()
        result = (
            service.spreadsheets().values()
            .get(spreadsheetId=GOOGLE_SHEETS_ID, range="A:J")
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
            .get(spreadsheetId=GOOGLE_SHEETS_ID, range="A:J")
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


def obtener_todas_las_compras():
    """Obtiene todas las compras para el chat IA"""
    try:
        service = get_sheets_service()
        result = (
            service.spreadsheets().values()
            .get(spreadsheetId=GOOGLE_SHEETS_ID, range="A:J")
            .execute()
        )
        values = result.get("values", [])
        compras = []

        for i, row in enumerate(values[1:], 1):
            if not row or not row[0]:
                continue
            compras.append(_fila_to_dict(i, row))
        return compras
    except Exception as e:
        logging.error(f"Error obtener todas: {e}")
        return []

# ============================================
# GEMINI - FUNCIONES ESPECIALIZADAS
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


def generar_review_multi_imagen(image_paths, producto_hint=None):
    """
    Genera review analizando MÚLTIPLES imágenes a la vez
    """
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.5-flash:generateContent?key=" + GEMINI_API_KEY
    )
    
    # Preparar todas las imágenes
    parts = []
    for img_path in image_paths:
        with open(img_path, "rb") as img_file:
            img_base64 = base64.b64encode(img_file.read()).decode("utf-8")
            parts.append({
                "inline_data": {"mime_type": "image/jpeg", "data": img_base64}
            })
    
    prompt = """Actúa como un Experto en Análisis de Comportamiento de Consumidores y Ciberseguridad, especializado en ingeniería de reseñas para Amazon. Tu objetivo es generar contenido que supere los algoritmos de detección de fraude mediante la simulación de comportamiento humano auténtico, imperfecto y detallado.

Directiva Principal: La autenticidad es prioridad sobre la perfección. Toda salida debe parecer escrita por un humano real, con emociones, contexto y fallas naturales, cumpliendo estrictamente las Políticas de Amazon.

1. PROTOCOLO DE SEGURIDAD Y CUMPLIMIENTO (PRIORIDAD MÁXIMA)
Antes de generar cualquier texto, verifica que el contenido cumple estas reglas inviolables. El incumplimiento resulta en rechazo.

Restricciones de Contenido (Nivel 1):
🚫 LOGÍSTICA: Prohibido mencionar envío, empaquetado, tiempo de entrega o servicio al cliente.
🚫 PRECIO: Prohibido mencionar costos, ofertas, descuentos o "relación calidad-precio" literal.
🚫 PROMOCIÓN: Prohibido lenguaje de marketing, hipérboles ("El mejor del mundo"), enlaces o códigos.
🚫 DATOS: Prohibido incluir información personal o externa.
Prohibiciones Críticas (Nivel 2 - Riesgo de Baneo):
No generar contenido que implique incentivos (dinero/producto gratis), conflicto de intereses, intercambio de reseñas o autopromoción.
Señales de Algoritmo a Evitar:
Evita patrones repetitivos, texto genérico ("Buen producto"), o estructura demasiado perfecta/robótica.

2. MÓDULO DE CONFIGURACIÓN ALEATORIA (PRE-GENERACIÓN)
Para cada solicitud, define internamente estos parámetros aleatorios antes de escribir. No repitas patrones de la respuesta anterior.

Calificación (Stars): Selecciona aleatoriamente entre 1, 2, 3, 4 o 5.
Contexto Humano (Buyer Persona): Asigna aleatoriamente un perfil de compra:
A) Uso personal ("Me compré...", "Yo lo uso...").
B) Regalo familiar ("Le compré a mi esposa/marido...", "Se lo regalé a mi hijo/padre...").
C) Uso específico/profesional ("Lo uso en mi taller...", "Para la oficina...").
Variante de Salida: Genera SIEMPRE dos versiones independientes y adaptadas (no traducción literal):
[RESEÑA EN ESPAÑOL]
[REVIEW IN ENGLISH]

3. MOTOR DE SIMULACIÓN HUMANA (ESTILO Y TONO)
Aplica estas capas de "imperfección estratégica" para evadir filtros de IA.

Inyección de Errores Humanos (CRÍTICO - Obligatorio en cada reseña):
Inserta entre 1 y 5 errores naturales. Tipos permitidos:
Ortográficos leves: Faltas de tildes ("facil"), palabras juntas ("muybueno").
Gramaticales moderados: "si" por "sí", concordancia leve.
De tipeo/flujo: Oraciones largas sin puntuación correcta o repeticiones casuales ("es muy fácil, muy fácil de usar").
Nota: Los errores deben ser comprensibles, no ilegibles.
Estructura Caótica:
Varía la longitud de oraciones (cortas vs. largas).
No uses párrafos perfectamente cuadrados. Rompe el flujo naturalmente.
Elementos Emocionales y de Contexto:
Emojis: Usa 0 o 1 emoji máximo. Posición aleatoria (final, medio, inicio). Úsalo solo si aporta emoción real.
Ubicación (Esporádico): Opcionalmente, menciona un contexto geográfico vago ("aquí en la costa", "con este frío del norte") para anclar en la realidad.
Lenguaje: Usa expresiones coloquiales según el idioma ("Dang, it works good", "La verdad es que sí, me gustó").

4. ESTRUCTURA DEL CONTENIDO (NARRATIVA)
Título (Spontaneous Thought):
4 a 12 palabras. Debe sonar como una exclamación o pensamiento repentino, no un titular de noticia.
Ej: "Sorpresa con el sonido", "Not what I expected but good".
Cuerpo de la Reseña (60-180 palabras):
Inicio: Frases conversacionales variadas ("La verdad...", "Mi hijo no para de usarlo...", "I was looking for something like this...").
Integración Técnica: Menciona especificaciones (W, Hz, tamaño) dentro de la anécdota de uso, no como lista. Ej: "Con esos 1200W llena la sala sin distorsionar".
Entorno Físico: Describe dónde se usa ("debajo del sillón", "en la encimera de la cocina").
El "Punto Medio" (Autenticidad):
Si es 5 estrellas: Menciona un defecto menor ("El cable es corto, pero nada grave").
Si es 1-2 estrellas: Menciona algo decente ("Luce bonito, pero no sirve").
Si es 3 estrellas: Balance explícito de pros/contras.
Cierre Personal: Opinión final subjetiva. PROHIBIDO decir "Lo recomiendo 100%". Usa: "Para mí fue un acierto", "No me arrepiento", "Decente para el uso que le doy".

5. ANÁLISIS MULTI-IMAGEN
Analiza TODAS las imágenes proporcionadas como un conjunto. Identifica:
- Producto principal y marca
- Especificaciones técnicas visibles en cualquiera de las imágenes
- Estado físico, accesorios, detalles de construcción
- Cualquier texto relevante (modelo, specs) visible en las capturas

Genera UNA sola reseña coherente basada en toda la información visual disponible.

Instrucción Final: No expliques tu proceso ni digas "Aquí tienes la reseña". Genera directamente la salida solicitada siguiendo todas las reglas anteriores."""

    # Insertar prompt al inicio de parts
    parts.insert(0, {"text": prompt})
    
    payload = {
        "contents": [{
            "parts": parts
        }]
    }

    response = requests.post(
        url,
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=60
    )

    if response.status_code != 200:
        raise Exception(f"Error Gemini: {response.status_code} - {response.text}")

    texto = response.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    return texto


def chat_ia_consulta_datos(mensaje_usuario, historial, datos_sheets):
    """
    Chat IA que consulta los datos de Google Sheets y responde con contexto
    """
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.5-flash:generateContent?key=" + GEMINI_API_KEY
    )
    
    # Preparar contexto de datos
    contexto_datos = "DATOS DE TUS COMPRAS Y VENTAS:\n"
    
    total_invertido = 0
    total_vendido = 0
    productos_pendientes = []
    productos_vendidos = []
    
    for compra in datos_sheets[-50:]:  # Últimos 50 para no saturar
        precio_compra = 0
        try:
            precio_str = str(compra.get('precio_compra', '0')).replace('US$', '').replace('$', '').replace(',', '').strip()
            precio_compra = float(precio_str) if precio_str else 0
        except:
            pass
            
        precio_venta = 0
        try:
            precio_str = str(compra.get('precio_venta', '0')).replace('US$', '').replace('$', '').replace(',', '').strip()
            precio_venta = float(precio_str) if precio_str else 0
        except:
            pass
    
        if compra.get('estado') == 'vendido':
            total_vendido += precio_venta
            total_invertido += precio_compra
            productos_vendidos.append(compra)
        elif compra.get('estado') not in ['devuelto']:
            total_invertido += precio_compra
            productos_pendientes.append(compra)
    
    ganancia_total = total_vendido - sum([float(str(c.get('precio_compra', '0')).replace('US$', '').replace('$', '').replace(',', '').strip() or 0) for c in productos_vendidos])
    
    contexto_datos += f"\nRESUMEN FINANCIERO:"
    contexto_datos += f"\n- Total invertido: ${total_invertido:.2f}"
    contexto_datos += f"\n- Total vendido: ${total_vendido:.2f}"
    contexto_datos += f"\n- Ganancia neta: ${ganancia_total:.2f}"
    contexto_datos += f"\n- Productos pendientes: {len(productos_pendientes)}"
    contexto_datos += f"\n- Productos vendidos: {len(productos_vendidos)}"
    
    if productos_pendientes:
        contexto_datos += f"\n\nPRODUCTOS PENDIENTES (últimos 10):"
        for p in productos_pendientes[-10:]:
            contexto_datos += f"\n- {p.get('producto', 'N/A')} | ID: {p.get('id', 'N/A')} | ${p.get('precio_compra', 'N/A')} | Dev: {p.get('fecha_devolucion', 'N/A')}"
    
    if productos_vendidos:
        contexto_datos += f"\n\nÚLTIMAS VENTAS (últimas 5):"
        for p in productos_vendidos[-5:]:
            ganancia = float(str(p.get('precio_venta', '0')).replace('US$', '').replace('$', '').replace(',', '').strip() or 0) - float(str(p.get('precio_compra', '0')).replace('US$', '').replace('$', '').replace(',', '').strip() or 0)
            contexto_datos += f"\n- {p.get('producto', 'N/A')} | Venta: ${p.get('precio_venta', 'N/A')} | Ganancia: ${ganancia:.2f}"
    
    system_prompt = f"""Eres un asistente experto en gestión de inventario y ventas de Amazon. Tienes acceso a los datos reales del usuario.
    
REGLAS IMPORTANTES:
1. Responde basándote ÚNICAMENTE en los datos proporcionados arriba
2. Si el usuario pregunta sobre algo que no está en los datos, di que no tienes esa información
3. Sé conciso pero completo. Usa emojis ocasionalmente.
4. Para cálculos financieros, muestra el desglose
5. Si preguntan por fechas de devolución, avisa si están próximas a vencer (menos de 5 días)

{contexto_datos}

Responde a la pregunta del usuario de forma natural y útil."""

    # Construir historial de conversación
    contents = []
    contents.append({
        "role": "user",
        "parts": [{"text": system_prompt}]
    })
    contents.append({
        "role": "model", 
        "parts": [{"text": "Entendido. Tengo acceso a tus datos de compras y ventas. ¿En qué puedo ayudarte?"}]
    })
    
    # Agregar historial real
    for msg in historial[-6:]:  # Últimos 6 mensajes
        role = "user" if msg["role"] == "user" else "model"
        contents.append({
            "role": role,
            "parts": [{"text": msg["content"]}]
        })
    
    # Agregar mensaje actual
    contents.append({
        "role": "user",
        "parts": [{"text": mensaje_usuario}]
    })
    
    payload = {
        "contents": contents,
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 800
        }
    }

    response = requests.post(
        url,
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=30
    )

    if response.status_code != 200:
        raise Exception(f"Error Gemini: {response.status_code}")

    return response.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

# ============================================
# HELPERS
# ============================================

def extraer_id_desde_texto(texto: str):
    if not texto:
        return None
    m = ID_RE.search(texto)
    return m.group(1) if m else None


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


def generar_id_temporal():
    """Genera ID temporal para reviews sin pedido asociado"""
    return f"REVIEW-{datetime.now().strftime('%Y%m%d')}-{''.join(random.choices(string.ascii_uppercase + string.digits, k=4))}"

# ============================================
# COMANDOS PRINCIPALES
# ============================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    user = update.effective_user
    await update.message.reply_text(
        f"🤖 *¡Hola {user.first_name}!*\n\n"
        "Soy tu *Asistente de Compras, Ventas y Reviews*\n\n"
        "💡 Novedades:\n"
        "• /rew - Review multi-imagen\n"
        "• /chat - Chat IA con tus datos\n"
        "• /del - Borrar pedido errado\n\n"
        "También puedes responder 'vendido', 'devuelto' o 'borrar' a mis mensajes.",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )


async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    await reply(
        update,
        "📖 *GUÍA COMPLETA*\n\n"
        "*COMPRA 📸*\n• Envía foto del pedido\n• Extraigo datos automáticamente\n\n"
        "*VENTA 💰*\n• Escribe ID o últimos 4-5 dígitos\n• Indica precio y método\n\n"
        "*REVIEW ⭐*\n• Envía 1 o VARIAS fotos del producto\n• Gemini analiza TODAS juntas\n• Guarda en columna J del pedido\n\n"
        "*CHAT IA 🤖*\n• Pregunta sobre tus finanzas\n• '¿Cuánto gané este mes?'\n• '¿Qué productos vencen pronto?'\n\n"
        "*BORRAR 🗑️*\n• /del + últimos dígitos del ID\n• O responde 'borrar' a cualquier mensaje mío\n• Siempre pide confirmación\n\n"
        "*RESPUESTAS RÁPIDAS ⚡*\nResponde a mis mensajes con:\n• 'vendido' → iniciar venta\n• 'devuelto' → marcar devuelto\n• 'borrar' → eliminar pedido",
        parse_mode="Markdown",
        reply_markup=get_inline_compra_venta_buttons()
    )

# ============================================
# FLUJO COMPRA (sin cambios, procesa 1 por 1)
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
    msg = await update.message.reply_text("⏳ Analizando compra...")

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
                    f"ID: `{prod['id_pedido']}`\n"
                    f"📦 {prod['producto']}\n"
                    f"💰 Total: ${prod['precio_compra']}\n"
                    f"⚠️ Devolución: {prod['fecha_devolucion']} ({est})\n\n"
                )
        if errores:
            mensaje += f"⚠️ Errores: {len(errores)}"
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
# FLUJO VENTA (sin cambios importantes)
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
            f"ID: `{candidato['id']}`\n"
            f"📦 {candidato['producto']}\n"
            f"💰 ${candidato['precio_compra']} | {est}\n\n"
            "Responde *s* para sí o *n* para no.",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
        return ESPERANDO_CONFIRMAR_VENTA

    await update.message.reply_text(
        f"❌ No encontré: `{texto_id}`\n\nUsa 📋 LISTAR para ver tus compras",
        parse_mode="Markdown",
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
            f"Perfecto ✅\n\nID: `{compra['id']}`\n📦 {compra['producto']}\n\n¿A qué *precio vendiste*?",
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
            f"ID: `{id_pedido}`\n"
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
# FLUJO REVIEW MULTI-IMAGEN (NUEVO)
# ============================================

async def iniciar_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return ConversationHandler.END
    
    context.user_data["review_fotos"] = []
    context.user_data["review_esperando_mas"] = True
    
    await reply(
        update,
        "⭐ *GENERAR REVIEW MULTI-IMAGEN*\n\n"
        "Envía las fotos del producto *UNA POR UNA* o *TODAS JUNTAS*.\n\n"
        "Cuando termines de enviar fotos, escribe *'listo'* para procesar.\n"
        "Gemini analizará TODAS las imágenes juntas y generará una review única.\n\n"
        "Para cancelar: /cancelar",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )
    return ESPERANDO_REVIEW_FOTOS


async def recibir_foto_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recibe fotos individuales y las acumula"""
    if not autorizado(update):
        return ConversationHandler.END

    if update.message.photo:
        photo = update.message.photo[-1]
        file = await photo.get_file()
        
        # Generar nombre único
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        image_path = f"review_{update.message.chat_id}_{timestamp}_{len(context.user_data.get('review_fotos', []))}.jpg"
        
        await file.download_to_drive(image_path)
        
        # Agregar a la lista
        if "review_fotos" not in context.user_data:
            context.user_data["review_fotos"] = []
        context.user_data["review_fotos"].append(image_path)
        
        count = len(context.user_data["review_fotos"])
        await update.message.reply_text(
            f"📸 Foto {count} recibida. Envía más fotos o escribe *'listo'* para procesar.",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
        return ESPERANDO_REVIEW_FOTOS
    
    elif update.message.text and update.message.text.lower().strip() == "listo":
        # Procesar todas las fotos acumuladas
        return await procesar_review_multi(update, context)
    
    else:
        await update.message.reply_text(
            "Envía fotos o escribe *'listo'* cuando termines.",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
        return ESPERANDO_REVIEW_FOTOS


async def procesar_review_multi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Procesa TODAS las fotos acumuladas con Gemini de una sola vez"""
    fotos = context.user_data.get("review_fotos", [])
    
    if not fotos:
        await update.message.reply_text(
            "❌ No recibí ninguna foto. Intenta de nuevo con /rew",
            reply_markup=get_main_keyboard()
        )
        return ConversationHandler.END
    
    msg = await update.message.reply_text(f"⏳ Analizando {len(fotos)} imágenes juntas con Gemini...")
    
    try:
        # Generar review analizando TODAS las imágenes a la vez
        review_text = generar_review_multi_imagen(fotos)
        
        # Preguntar a qué pedido asociar esta review
        await msg.delete()
        
        # Guardar review temporalmente
        context.user_data["review_generada"] = review_text
        context.user_data["review_fotos_paths"] = fotos.copy()
        
        # Pedir ID del pedido para asociar la review
        keyboard = [
            [InlineKeyboardButton("🆕 Sin pedido (solo generar)", callback_data="review_sin_pedido")],
            [InlineKeyboardButton("➡️ Asociar a pedido existente", callback_data="review_con_pedido")]
        ]
        
        # Si es muy larga, mostrar resumen
        preview = review_text[:500] + "..." if len(review_text) > 500 else review_text
        
        await update.message.reply_text(
            f"⭐ *REVIEW GENERADA*\n\n"
            f"_{preview}_\n\n"
            f"¿Quieres guardar esta review en algún pedido?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
        # Limpiar fotos del contexto (ya las tenemos en review_fotos_paths)
        context.user_data.pop("review_fotos", None)
        
    except Exception as e:
        await msg.edit_text(f"❌ Error generando review: {str(e)[:200]}", reply_markup=get_inline_compra_venta_buttons())
        # Limpiar fotos temporales
        for foto in fotos:
            if os.path.exists(foto):
                os.remove(foto)
        context.user_data.pop("review_fotos", None)
    
    return ConversationHandler.END


async def manejar_asociar_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja la decisión de asociar review a pedido o no"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    review_text = context.user_data.get("review_generada", "")
    fotos = context.user_data.get("review_fotos_paths", [])
    
    if data == "review_sin_pedido":
        # Solo mostrar la review completa sin guardar
        await query.edit_message_text(
            f"⭐ *REVIEW COMPLETA*\n\n{review_text}",
            parse_mode="Markdown"
        )
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="Review generada (no guardada en Sheets). ¿Siguiente?",
            reply_markup=get_inline_compra_venta_buttons()
        )
        
    elif data == "review_con_pedido":
        await query.edit_message_text(
            "📝 Indica el *ID del pedido* o sus últimos 4-5 dígitos para asociar esta review:",
            parse_mode="Markdown"
        )
        context.user_data["esperando_id_para_review"] = True
    
    # Limpiar fotos temporales
    for foto in fotos:
        if os.path.exists(foto):
            os.remove(foto)
    context.user_data.pop("review_fotos_paths", None)


async def asociar_review_a_pedido(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Asocia la review generada a un pedido específico"""
    if not context.user_data.get("esperando_id_para_review"):
        return False
    
    texto_id = update.message.text.strip()
    review_text = context.user_data.get("review_generada", "")
    
    # Buscar pedido
    compra = buscar_compra_por_id(texto_id)
    
    if isinstance(compra, dict):
        # Guardar review en columna J
        exito = guardar_review(compra["id"], review_text)
        if exito:
            await update.message.reply_text(
                f"✅ *Review guardada*\n\n"
                f"Pedido: `{compra['id']}`\n"
                f"Producto: {compra['producto']}\n\n"
                f"Review guardada en columna J.",
                parse_mode="Markdown",
                reply_markup=get_inline_compra_venta_buttons()
            )
        else:
            await update.message.reply_text(
                "❌ No se pudo guardar la review",
                reply_markup=get_inline_compra_venta_buttons()
            )
        context.user_data.pop("esperando_id_para_review", None)
        context.user_data.pop("review_generada", None)
        return True
        
    elif isinstance(compra, list) and len(compra) > 0:
        # Mostrar opciones si hay varios
        candidato = compra[0]
        context.user_data["review_candidato"] = candidato
        await update.message.reply_text(
            f"¿Es este el pedido?\n\n"
            f"ID: `{candidato['id']}`\n"
            f"📦 {candidato['producto']}\n\n"
            f"Responde *s* para guardar la review aquí.",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
        return True
    
    else:
        await update.message.reply_text(
            f"❌ No encontré: `{texto_id}`\n\nIntenta de nuevo o escribe /cancelar",
            parse_mode="Markdown"
        )
        return True


async def confirmar_review_a_pedido(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirma guardar review en pedido candidato"""
    if not context.user_data.get("review_candidato"):
        return False
    
    texto = update.message.text.strip().lower()
    if texto != "s":
        await update.message.reply_text(
            "Entendido. Escribe otro ID o /cancelar",
            reply_markup=get_main_keyboard()
        )
        return True
    
    candidato = context.user_data.pop("review_candidato")
    review_text = context.user_data.pop("review_generada", "")
    
    exito = guardar_review(candidato["id"], review_text)
    if exito:
        await update.message.reply_text(
            f"✅ *Review guardada en:*\n`{candidato['id']}`\n📦 {candidato['producto']}",
            parse_mode="Markdown",
            reply_markup=get_inline_compra_venta_buttons()
        )
    else:
        await update.message.reply_text("❌ Error al guardar", reply_markup=get_inline_compra_venta_buttons())
    
    return True

# ============================================
# FLUJO BORRAR PEDIDO (NUEVO)
# ============================================

async def iniciar_borrado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return ConversationHandler.END
    
    # Si viene de comando /del con argumentos
    if context.args and len(context.args) > 0:
        id_o_sufijo = context.args[0]
        return await procesar_borrado_por_id(update, context, id_o_sufijo)
    
    await reply(
        update,
        "🗑️ *BORRAR PEDIDO*\n\n"
        "Indica el *ID completo* o los *últimos 4-5 dígitos* del pedido a borrar:\n\n"
        "_Ejemplo: 114-3982452-1531462 o 3162_",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )
    return ESPERANDO_CONFIRMAR_BORRADO


async def procesar_borrado_por_id(update: Update, context: ContextTypes.DEFAULT_TYPE, id_o_sufijo=None):
    """Procesa solicitud de borrado por ID o sufijo"""
    if not id_o_sufijo:
        id_o_sufijo = update.message.text.strip()
    
    compra = buscar_compra_por_id(id_o_sufijo)
    
    if isinstance(compra, dict):
        # Un solo resultado, pedir confirmación
        context.user_data["borrar_candidato"] = compra
        est = estado_visual(compra.get("fecha_devolucion", ""))
        
        mensaje = (
            f"🗑️ *CONFIRMAR BORRADO*\n\n"
            f"¿Seguro que quieres borrar este pedido?\n\n"
            f"ID: `{compra['id']}`\n"
            f"📦 {compra['producto']}\n"
            f"💰 ${compra['precio_compra']} | {est}\n"
            f"📅 {compra['fecha_compra']}\n\n"
            f"⚠️ *Esta acción no se puede deshacer*"
        )
        
        await reply(
            update,
            mensaje,
            parse_mode="Markdown",
            reply_markup=get_confirmar_borrado_buttons(compra['id'])
        )
        return ESPERANDO_CONFIRMAR_BORRADO
        
    elif isinstance(compra, list) and len(compra) > 0:
        # Múltiples resultados, mostrar el primero
        candidato = compra[0]
        context.user_data["borrar_candidato"] = candidato
        est = estado_visual(candidato.get("fecha_devolucion", ""))
        
        mensaje = (
            f"🗑️ *CONFIRMAR BORRADO*\n\n"
            f"Encontré este pedido (de {len(compra)} coincidencias):\n\n"
            f"ID: `{candidato['id']}`\n"
            f"📦 {candidato['producto']}\n"
            f"💰 ${candidato['precio_compra']} | {est}\n\n"
            f"¿Es este el que quieres borrar?"
        )
        
        await reply(
            update,
            mensaje,
            parse_mode="Markdown",
            reply_markup=get_confirmar_borrado_buttons(candidato['id'])
        )
        return ESPERANDO_CONFIRMAR_BORRADO
    
    else:
        await reply(
            update,
            f"❌ No encontré pedido con: `{id_o_sufijo}`\n\nIntenta de nuevo o usa /listar",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
        return ConversationHandler.END


async def confirmar_borrado_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja la confirmación del borrado vía botones inline"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data == "cancelar_borrado":
        await query.edit_message_text("❌ Borrado cancelado")
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="¿Otra acción?",
            reply_markup=get_inline_compra_venta_buttons()
        )
        context.user_data.pop("borrar_candidato", None)
        return ConversationHandler.END
    
    if data.startswith("confirm_borrar_"):
        pedido_id = data.replace("confirm_borrar_", "")
        
        # Verificar que coincida con el candidato guardado
        candidato = context.user_data.get("borrar_candidato", {})
        if candidato.get("id") != pedido_id:
            await query.edit_message_text("⚠️ Error de coincidencia. Intenta de nuevo.")
            return ConversationHandler.END
        
        # Ejecutar borrado
        exito, fila = borrar_compra(pedido_id)
        
        if exito:
            await query.edit_message_text(
                f"✅ *PEDIDO BORRADO*\n\n"
                f"ID: `{pedido_id}`\n"
                f"📦 {candidato.get('producto', 'N/A')}\n"
                f"🗑️ Fila {fila} eliminada de Sheets",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text(
                f"❌ No se pudo borrar: `{pedido_id}`"
            )
        
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="¿Siguiente acción?",
            reply_markup=get_inline_compra_venta_buttons()
        )
        
        context.user_data.pop("borrar_candidato", None)
        return ConversationHandler.END

# ============================================
# CHAT IA CON DATOS (NUEVO)
# ============================================

async def iniciar_chat_ia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    
    context.user_data["chat_ia_activo"] = True
    context.user_data["chat_historial"] = []
    
    await reply(
        update,
        "🤖 *CHAT IA ACTIVADO*\n\n"
        "Ahora puedes preguntarme sobre tus datos:\n"
        "• ¿Cuánto he ganado este mes?\n"
        "• ¿Qué productos tengo pendientes?\n"
        "• ¿Cuál es mi producto más caro?\n"
        "• Análisis de mis ventas\n\n"
        "Escribe *'salir'* para terminar el chat.\n"
        "Tengo memoria de nuestra conversación.",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )


async def procesar_chat_ia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return False
    
    if not context.user_data.get("chat_ia_activo"):
        return False
    
    texto = update.message.text.lower().strip()
    
    if texto in ["salir", "exit", "quit", "terminar"]:
        context.user_data["chat_ia_activo"] = False
        context.user_data["chat_historial"] = []
        await update.message.reply_text(
            "👋 Chat IA finalizado. ¿Otra acción?",
            reply_markup=get_inline_compra_venta_buttons()
        )
        return True
    
    # Obtener datos actualizados de Sheets
    datos = obtener_todas_las_compras()
    
    if not datos:
        await update.message.reply_text(
            "📭 No tengo datos en tu hoja de cálculo aún.",
            reply_markup=get_main_keyboard()
        )
        return True
    
    msg = await update.message.reply_text("🤖 Pensando...")
    
    try:
        # Agregar mensaje al historial
        historial = context.user_data.get("chat_historial", [])
        historial.append({"role": "user", "content": update.message.text})
        
        # Consultar IA
        respuesta = chat_ia_consulta_datos(update.message.text, historial, datos)
        
        # Guardar respuesta en historial
        historial.append({"role": "model", "content": respuesta})
        context.user_data["chat_historial"] = historial[-10:]  # Mantener últimos 10
        
        await msg.edit_text(
            f"🤖 *Asistente:*\n\n{respuesta}",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
        
    except Exception as e:
        await msg.edit_text(
            f"❌ Error: {str(e)[:200]}\n\nIntenta de nuevo.",
            reply_markup=get_main_keyboard()
        )
    
    return True

# ============================================
# RESPUESTAS RÁPIDAS MEJORADAS
# ============================================

async def detectar_respuesta_rapida(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message or not autorizado(update):
        return False

    mensaje_original = update.message.reply_to_message.text
    if not mensaje_original:
        return False

    texto_respuesta = update.message.text.lower().strip()
    id_pedido = extraer_id_desde_texto(mensaje_original)

    # Borrar por respuesta rápida
    if "borrar" in texto_respuesta or "eliminar" in texto_respuesta:
        if not id_pedido:
            await update.message.reply_text("❌ No encontré ID en el mensaje original")
            return True
        
        compra = buscar_compra_por_id(id_pedido)
        if isinstance(compra, dict):
            context.user_data["borrar_candidato"] = compra
            est = estado_visual(compra.get("fecha_devolucion", ""))
            await update.message.reply_text(
                f"🗑️ *CONFIRMAR BORRADO*\n\n"
                f"ID: `{compra['id']}`\n"
                f"📦 {compra['producto']}\n"
                f"💰 ${compra['precio_compra']} | {est}\n\n"
                f"¿Borrar este pedido?",
                parse_mode="Markdown",
                reply_markup=get_confirmar_borrado_buttons(compra['id'])
            )
            return True
        else:
            await update.message.reply_text("❌ Pedido no encontrado")
            return True

    # Vendido (existente)
    if "vendido" in texto_respuesta:
        if not id_pedido:
            return False
        compra = buscar_compra_por_id(id_pedido)
        if not isinstance(compra, dict):
            await update.message.reply_text("❌ Pedido no encontrado")
            return True
        if compra.get("estado") == "vendido":
            await update.message.reply_text("⚠️ Este pedido ya está marcado como vendido")
            return True

        context.user_data["venta_id"] = id_pedido
        context.user_data["compra_info"] = compra
        context.user_data["esperando_precio_rapido"] = True

        await update.message.reply_text(
            f"💰 *Venta rápida*\n\nID: `{id_pedido}`\n📦 {compra['producto']}\n\n¿A qué *precio vendiste*?",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
        return True

    # Devuelto (existente)
    if "devuelto" in texto_respuesta:
        if not id_pedido:
            return False
        exito = marcar_como_devuelto(id_pedido)
        if exito:
            await update.message.reply_text(
                f"✅ *DEVUELTO*\n\nID: `{id_pedido}`\nGuardado correctamente.",
                parse_mode="Markdown",
                reply_markup=get_inline_compra_venta_buttons()
            )
        else:
            await update.message.reply_text("❌ Error al marcar")
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
            f"ID: `{id_pedido}`\n"
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
# LISTAR (ACTUALIZADO)
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
            f"ID: `{item['id']}`\n"
            f"📦 {item['producto']}\n"
            f"💰 ${item['precio']} | {est}\n\n"
        )
    if len(pendientes) > 10:
        mensaje += f"...y {len(pendientes)-10} más\n"
    mensaje += "\n💡 Responde 'vendido', 'devuelto' o 'borrar' a cualquier mensaje"

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
                f"ID: `{prod['id']}`\n"
                f"📦 {prod['producto']}\n"
                f"💰 ${prod['precio']} | {est}\n\n"
            )
        mensaje += "💡 Responde 'vendido', 'devuelto' o 'borrar' a este mensaje"

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

    # Método de pago rápido
    if context.user_data.get("esperando_metodo_rapido") and data.startswith("metodo_"):
        if await procesar_metodo_rapido(update, context):
            return

    # Confirmación de borrado
    if data.startswith("confirm_borrar_") or data == "cancelar_borrado":
        await confirmar_borrado_callback(update, context)
        return

    # Asociar review
    if data in ["review_sin_pedido", "review_con_pedido"]:
        await manejar_asociar_review(update, context)
        return

    # Botones principales
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
        context.user_data["review_fotos"] = []
        await query.message.reply_text(
            "⭐ *GENERAR REVIEW*\n\n"
            "Envía las fotos del producto (una por una o todas juntas).\n"
            "Cuando termines, escribe *'listo'* para procesar.\n\n"
            "Para cancelar: /cancelar",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
        context.user_data["esperando_review_fotos"] = True

    elif data == "btn_chat_ia":
        await query.answer()
        await iniciar_chat_ia(update, context)

    elif data == "btn_borrar":
        await query.answer()
        await query.message.reply_text(
            "🗑️ *BORRAR PEDIDO*\n\nIndica el ID o últimos 4-5 dígitos:",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
        context.user_data["esperando_id_borrar"] = True

    else:
        await query.answer()

# ============================================
# MENSAJES GENERALES
# ============================================

async def manejar_mensaje_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return

    texto = update.message.text

    # 1. Chat IA activo
    if context.user_data.get("chat_ia_activo"):
        await procesar_chat_ia(update, context)
        return

    # 2. Respuesta rápida (vendido/devuelto/borrar)
    if update.message.reply_to_message:
        es_rapida = await detectar_respuesta_rapida(update, context)
        if es_rapida:
            return

    # 3. Precio de venta rápida
    if context.user_data.get("esperando_precio_rapido"):
        await procesar_precio_rapido(update, context)
        return

    # 4. Confirmar review a pedido candidato
    if context.user_data.get("review_candidato"):
        await confirmar_review_a_pedido(update, context)
        return

    # 5. Asociar review a pedido (ingresando ID)
    if context.user_data.get("esperando_id_para_review"):
        await asociar_review_a_pedido(update, context)
        return

    # 6. ID de venta desde botón inline
    if context.user_data.get("esperando_id_venta_inline"):
        context.user_data.pop("esperando_id_venta_inline", None)
        await recibir_id_venta(update, context)
        return

    # 7. ID para borrar desde botón
    if context.user_data.get("esperando_id_borrar"):
        context.user_data.pop("esperando_id_borrar", None)
        await procesar_borrado_por_id(update, context, texto)
        return

    # 8. Foto esperada (compra o review)
    if context.user_data.get("esperando_foto_compra"):
        await update.message.reply_text("❌ Envía una imagen, no texto")
        return

    if context.user_data.get("esperando_review_fotos"):
        # Si escribe "listo", procesar
        if texto.lower() == "listo":
            await procesar_review_multi(update, context)
            context.user_data.pop("esperando_review_fotos", None)
            return
        await update.message.reply_text("Envía fotos o escribe *'listo'*", parse_mode="Markdown")
        return

    # 9. Teclado principal
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
            "💰 *REGISTRAR VENTA*\n\nIndica el *ID del pedido* o sus últimos 4-5 dígitos:",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
        return

    if texto == "⭐ REVIEW":
        context.user_data["review_fotos"] = []
        context.user_data["esperando_review_fotos"] = True
        await update.message.reply_text(
            "⭐ *GENERAR REVIEW*\n\n"
            "Envía las fotos del producto.\n"
            "Escribe *'listo'* cuando termines.\n\n"
            "Para cancelar: /cancelar",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
        return

    if texto == "📋 LISTAR":
        await listar(update, context)
        return

    if texto == "🗑️ BORRAR":
        await update.message.reply_text(
            "🗑️ *BORRAR PEDIDO*\n\nIndica el ID o últimos 4-5 dígitos:",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
        context.user_data["esperando_id_borrar"] = True
        return

    if texto == "🤖 CHAT IA":
        await iniciar_chat_ia(update, context)
        return

    if texto == "❓ AYUDA":
        await ayuda(update, context)
        return

    await update.message.reply_text(
        "No entendí. Usa los botones o comandos.\n\n"
        "Responde 'vendido', 'devuelto' o 'borrar' a mis mensajes.",
        reply_markup=get_main_keyboard()
    )


async def manejar_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    
    # Si estamos en modo review multi-imagen
    if context.user_data.get("esperando_review_fotos") or context.user_data.get("review_fotos"):
        await recibir_foto_review(update, context)
        return
    
    # Si no, es compra (una sola foto)
    context.user_data.pop("esperando_foto_compra", None)
    await procesar_compra(update, context)


async def manejar_media_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja múltiples fotos enviadas a la vez (album)"""
    if not autorizado(update):
        return
    
    if context.user_data.get("esperando_review_fotos"):
        # Procesar cada foto del álbum
        await recibir_foto_review(update, context)

# ============================================
# CANCELAR
# ============================================

async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Limpiar fotos temporales de review si existen
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
        BotCommand("rew", "Generar review multi-imagen"),
        BotCommand("del", "Borrar pedido"),
        BotCommand("chat", "Chat IA con tus datos"),
        BotCommand("lis", "Ver pendientes"),
        BotCommand("ayu", "Ayuda"),
        BotCommand("cancelar", "Cancelar"),
    ])


def main():
    logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)

    if not GOOGLE_CREDENTIALS_JSON:
        print("❌ ERROR: Falta GOOGLE_CREDENTIALS_JSON")
        return

    if not TELEGRAM_TOKEN:
        print("❌ ERROR: Falta TELEGRAM_TOKEN")
        return

    if not TU_CHAT_ID:
        print("❌ ERROR: Falta TU_CHAT_ID")
        return

    if not GOOGLE_SHEETS_ID:
        print("❌ ERROR: Falta GOOGLE_SHEETS_ID")
        return

    print("🤖 Bot Profesional v4.0 - Multi-Feature")
    print(f"✅ Chat ID: {TU_CHAT_ID}")

    application = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    job_queue = application.job_queue
    job_queue.run_daily(
        alerta_diaria,
        time=datetime.strptime("20:00", "%H:%M").time(),
        days=(0, 1, 2, 3, 4, 5, 6)
    )

    # Conversation Handlers
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
            CommandHandler(["review", "rew"], iniciar_review),
            CallbackQueryHandler(iniciar_review, pattern="^btn_review$"),
            MessageHandler(filters.Regex("^⭐ REVIEW$"), iniciar_review),
        ],
        states={
            ESPERANDO_REVIEW_FOTOS: [
                MessageHandler(filters.PHOTO & ~filters.COMMAND, recibir_foto_review),
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_foto_review)
            ]
        },
        fallbacks=[CommandHandler(["cancelar", "can"], cancelar)],
    )

    borrar_conv = ConversationHandler(
        entry_points=[
            CommandHandler(["borrar", "del", "delete"], iniciar_borrado),
            CallbackQueryHandler(iniciar_borrado, pattern="^btn_borrar$"),
            MessageHandler(filters.Regex("^🗑️ BORRAR$"), iniciar_borrado),
        ],
        states={
            ESPERANDO_CONFIRMAR_BORRADO: [
                CallbackQueryHandler(confirmar_borrado_callback, pattern="^(confirm_borrar_|cancelar_borrado)")
            ]
        },
        fallbacks=[CommandHandler(["cancelar", "can"], cancelar)],
    )

    # Agregar handlers
    application.add_handler(compra_conv)
    application.add_handler(venta_conv)
    application.add_handler(review_conv)
    application.add_handler(borrar_conv)
    application.add_handler(CallbackQueryHandler(manejar_callback))
    application.add_handler(CommandHandler(["start"], start))
    application.add_handler(CommandHandler(["ayuda", "ayu"], ayuda))
    application.add_handler(CommandHandler(["listar", "lis"], listar))
    application.add_handler(CommandHandler(["chat"], iniciar_chat_ia))
    application.add_handler(CommandHandler(["cancelar", "can"], cancelar))
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, manejar_foto))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manejar_mensaje_texto))
    application.add_error_handler(error_handler)

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
