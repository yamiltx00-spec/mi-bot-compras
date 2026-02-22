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
    CallbackQueryHandler,
)
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ============================================
# CONFIGURACIÓN
# ============================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TU_CHAT_ID = os.getenv("TU_CHAT_ID")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

# Estados simples
ESTADO_NORMAL = "normal"
ESTADO_ESPERANDO_FOTOS_REVIEW = "review_fotos"

# Variables globales simples
user_states = {}
user_data_cache = {}

METODOS_PAGO = {
    "paypal": "💳 PayPal",
    "amazon": "📦 Amazon", 
    "zelle": "💰 Zelle",
    "efectivo": "💵 Efectivo",
    "deposito": "🏦 Depósito",
    "otro": "📝 Otro",
}

ID_RE = re.compile(r"([0-9]{3}-[0-9]{7}-[0-9]{7}|TEMP-\d{8}-\d{4}|NO_ID_\d+)")
ID_COMPLETO_RE = re.compile(r"^\d{3}-\d{7}-\d{7}$")

# ============================================
# TECLADOS
# ============================================

def get_main_keyboard():
    keyboard = [
        [KeyboardButton("📸 COMPRA"), KeyboardButton("💰 VENTA"), KeyboardButton("⭐ REVIEW")],
        [KeyboardButton("📋 LISTAR"), KeyboardButton("🗑️ BORRAR"), KeyboardButton("📊 ESTADÍSTICAS")],
        [KeyboardButton("❓ AYUDA")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)


def get_inline_buttons():
    keyboard = [
        [
            InlineKeyboardButton("📸 Compra", callback_data="btn_compra"),
            InlineKeyboardButton("💰 Venta", callback_data="btn_venta"),
            InlineKeyboardButton("⭐ Review", callback_data="btn_review"),
        ],
        [
            InlineKeyboardButton("📋 Listar", callback_data="btn_listar"),
            InlineKeyboardButton("🗑️ Borrar", callback_data="btn_borrar"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_confirmar_borrar_buttons(pedido_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Sí, borrar", callback_data=f"borrar_{pedido_id}"),
        InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")
    ]])


def get_metodo_pago_buttons():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("PayPal", callback_data="mp_paypal"),
            InlineKeyboardButton("Zelle", callback_data="mp_zelle"),
            InlineKeyboardButton("Efectivo", callback_data="mp_efectivo"),
        ],
        [
            InlineKeyboardButton("Amazon", callback_data="mp_amazon"),
            InlineKeyboardButton("Depósito", callback_data="mp_deposito"),
            InlineKeyboardButton("Otro", callback_data="mp_otro"),
        ]
    ])

# ============================================
# GOOGLE SHEETS
# ============================================

def get_sheets_service():
    info = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return build("sheets", "v4", credentials=creds)


def obtener_datos():
    """Obtiene todos los datos de Sheets"""
    try:
        service = get_sheets_service()
        result = service.spreadsheets().values().get(
            spreadsheetId=GOOGLE_SHEETS_ID, range="A:J"
        ).execute()
        values = result.get("values", [])
        
        datos = []
        for i, row in enumerate(values[1:], 1):
            if not row:
                continue
            
            pedido_id = row[0] if len(row) > 0 and row[0] else f"NO_ID_{i}"
            
            datos.append({
                "fila": i + 1,
                "id": pedido_id,
                "fecha_compra": row[1] if len(row) > 1 else "",
                "producto": row[2] if len(row) > 2 else "Sin nombre",
                "precio_compra": row[3] if len(row) > 3 else "0",
                "fecha_devolucion": row[4] if len(row) > 4 else "",
                "fecha_venta": row[5] if len(row) > 5 else "",
                "precio_venta": row[6] if len(row) > 6 else "",
                "metodo_pago": row[7] if len(row) > 7 else "",
                "estado": row[8] if len(row) > 8 and row[8] else "pendiente",
                "review": row[9] if len(row) > 9 else "",
            })
        return datos
    except Exception as e:
        logging.error(f"Error obtener datos: {e}")
        return []


def buscar_producto(busqueda, datos=None):
    """Busca por ID o nombre de producto"""
    if datos is None:
        datos = obtener_datos()
    
    busqueda = busqueda.lower().strip()
    
    # Buscar por ID exacto
    for d in datos:
        if d["id"].lower() == busqueda:
            return d
    
    # Buscar por sufijo de ID
    for d in datos:
        if d["id"].endswith(busqueda):
            return d
    
    # Buscar por nombre (contiene)
    coincidencias = []
    for d in datos:
        if busqueda in d["producto"].lower():
            coincidencias.append(d)
    
    if len(coincidencias) == 1:
        return coincidencias[0]
    elif len(coincidencias) > 1:
        return coincidencias[:3]
    
    return None


def agregar_compra(datos_compra):
    try:
        service = get_sheets_service()
        
        # Fecha devolución
        fecha_dev = datos_compra.get("fecha_devolucion", "")
        if not fecha_dev or fecha_dev in ["NO_ENCONTRADO", ""]:
            try:
                fecha_comp = datetime.strptime(datos_compra["fecha_compra"], "%d/%m/%Y")
                fecha_dev = (fecha_comp + timedelta(days=30)).strftime("%d/%m/%Y")
            except:
                fecha_dev = (datetime.now() + timedelta(days=30)).strftime("%d/%m/%Y")
        
        # Generar ID si no hay
        pedido_id = datos_compra.get("id_pedido", "")
        if not pedido_id or pedido_id in ["NO_ENCONTRADO", "NO_DISPONIBLE", ""]:
            pedido_id = f"TEMP-{datetime.now().strftime('%Y%m%d')}-{random.randint(1000,9999)}"
        
        values = [[
            pedido_id,
            datos_compra.get("fecha_compra", datetime.now().strftime("%d/%m/%Y")),
            datos_compra.get("producto", "Sin nombre"),
            datos_compra.get("precio_compra", "0"),
            fecha_dev,
            "", "", "", "pendiente", "",
        ]]
        
        service.spreadsheets().values().append(
            spreadsheetId=GOOGLE_SHEETS_ID,
            range="A:J",
            valueInputOption="USER_ENTERED",
            body={"values": values},
        ).execute()
        
        return True, pedido_id
    except Exception as e:
        logging.error(f"Error agregar: {e}")
        return False, None


def registrar_venta(id_pedido, precio_venta, metodo_pago):
    try:
        service = get_sheets_service()
        datos = obtener_datos()
        
        for d in datos:
            if d["id"] == id_pedido:
                fila = d["fila"]
                fecha_venta = datetime.now().strftime("%d/%m/%Y")
                
                service.spreadsheets().values().update(
                    spreadsheetId=GOOGLE_SHEETS_ID,
                    range=f"F{fila}:I{fila}",
                    valueInputOption="USER_ENTERED",
                    body={"values": [[fecha_venta, str(precio_venta), metodo_pago, "vendido"]]},
                ).execute()
                
                try:
                    precio_compra = float(str(d["precio_compra"]).replace("US$", "").replace("$", "").replace(",", "").strip() or 0)
                except:
                    precio_compra = 0
                
                return True, precio_compra
        
        return False, 0
    except Exception as e:
        logging.error(f"Error venta: {e}")
        return False, 0


def marcar_devuelto(id_pedido):
    try:
        service = get_sheets_service()
        datos = obtener_datos()
        
        for d in datos:
            if d["id"] == id_pedido:
                fila = d["fila"]
                service.spreadsheets().values().update(
                    spreadsheetId=GOOGLE_SHEETS_ID,
                    range=f"F{fila}:I{fila}",
                    valueInputOption="USER_ENTERED",
                    body={"values": [[datetime.now().strftime("%d/%m/%Y"), "0", "", "devuelto"]]},
                ).execute()
                return True
        return False
    except Exception as e:
        logging.error(f"Error devolución: {e}")
        return False


def borrar_producto(id_pedido):
    try:
        service = get_sheets_service()
        datos = obtener_datos()
        
        for d in datos:
            if d["id"] == id_pedido:
                fila = d["fila"]
                service.spreadsheets().values().clear(
                    spreadsheetId=GOOGLE_SHEETS_ID,
                    range=f"A{fila}:J{fila}",
                ).execute()
                return True, d
        return False, None
    except Exception as e:
        logging.error(f"Error borrar: {e}")
        return False, None


def guardar_review(id_pedido, review_text):
    try:
        service = get_sheets_service()
        datos = obtener_datos()
        
        for d in datos:
            if d["id"] == id_pedido:
                fila = d["fila"]
                service.spreadsheets().values().update(
                    spreadsheetId=GOOGLE_SHEETS_ID,
                    range=f"J{fila}",
                    valueInputOption="USER_ENTERED",
                    body={"values": [[review_text[:5000]]]},
                ).execute()
                return True
        return False
    except Exception as e:
        logging.error(f"Error review: {e}")
        return False

# ============================================
# GEMINI - SOLO PARA REVIEWS Y ANÁLISIS DE IMÁGENES
# ============================================

def extraer_datos_compra_imagen(image_path):
    """Extrae datos de compra de imagen"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    
    with open(image_path, "rb") as f:
        img_base64 = base64.b64encode(f.read()).decode()
    
    prompt = """Analiza esta captura de pantalla de compra de Amazon.
Extrae en JSON:
{
    "id_pedido": "número de orden o NO_DISPONIBLE",
    "fecha_compra": "DD/MM/YYYY",
    "producto": "nombre del producto",
    "precio_compra": "precio total",
    "fecha_devolucion": "DD/MM/YYYY o vacío"
}

Responde SOLO el JSON."""
    
    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "image/jpeg", "data": img_base64}}
            ]
        }]
    }
    
    try:
        response = requests.post(url, headers={"Content-Type": "application/json"}, json=payload, timeout=30)
        texto = response.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        
        if "```" in texto:
            texto = texto.split("```")[1].replace("json", "").strip()
        
        datos = json.loads(texto)
        return {
            "id_pedido": datos.get("id_pedido", "NO_DISPONIBLE"),
            "fecha_compra": datos.get("fecha_compra", datetime.now().strftime("%d/%m/%Y")),
            "producto": datos.get("producto", "Producto sin nombre"),
            "precio_compra": datos.get("precio_compra", "0"),
            "fecha_devolucion": datos.get("fecha_devolucion", ""),
        }
    except Exception as e:
        logging.error(f"Error extracción: {e}")
        return None


def generar_review_imagenes(image_paths):
    """Genera review de múltiples imágenes"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    
    parts = []
    prompt = """Genera reseñas realistas para Amazon en español e inglés basándote en estas fotos.

REGLAS:
- NO menciones envío, precio, ni atención al cliente
- Incluye 1-2 errores ortográficos menores naturales
- Sé específico con detalles visibles en las fotos
- 4-5 estrellas aleatorias
- Si 5 estrellas, menciona un defecto menor
- 80-150 palabras cada una

Formato:
[ESPAÑOL]
⭐ X estrellas
Título: ...
Reseña: ...

[ENGLISH]
⭐ X stars
Title: ...
Review: ..."""
    
    parts.append({"text": prompt})
    
    for path in image_paths:
        with open(path, "rb") as f:
            img_base64 = base64.b64encode(f.read()).decode()
            parts.append({"inline_data": {"mime_type": "image/jpeg", "data": img_base64}})
    
    payload = {"contents": [{"parts": parts}]}
    
    try:
        response = requests.post(url, headers={"Content-Type": "application/json"}, json=payload, timeout=60)
        return response.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        logging.error(f"Error generar review: {e}")
        return None

# ============================================
# HELPERS
# ============================================

def autorizado(update: Update) -> bool:
    uid = str(update.effective_user.id) if update.effective_user else ""
    return uid == TU_CHAT_ID


def extraer_id_de_texto(texto):
    """Extrae cualquier ID de un texto"""
    if not texto:
        return None
    match = ID_RE.search(texto)
    return match.group(1) if match else None


def extraer_precio(texto):
    """Extrae el primer número que parezca precio"""
    # Buscar patrones como $150, 150$, 150.00, 150,00
    patrones = [
        r'\$\s*(\d+(?:[.,]\d{2})?)',
        r'(\d+(?:[.,]\d{2})?)\s*\$',
        r'(\d+(?:[.,]\d{2})?)\s*(?:usd|dólares|pesos)',
        r'\b(\d{3,}(?:[.,]\d{2})?)\b',  # Números grandes (probable precio)
    ]
    
    for patron in patrones:
        match = re.search(patron, texto.lower())
        if match:
            try:
                return float(match.group(1).replace(",", "."))
            except:
                continue
    return None


def detectar_metodo_pago(texto):
    """Detecta método de pago en texto"""
    texto_lower = texto.lower()
    for key in METODOS_PAGO.keys():
        if key in texto_lower:
            return key
    return None


def estado_visual(fecha_str):
    try:
        fecha = datetime.strptime(fecha_str, "%d/%m/%Y")
        dias = (fecha - datetime.now()).days
        if dias < 0:
            return "🔴 VENCIDO"
        elif dias <= 3:
            return f"⚠️ {dias}d"
        else:
            return f"✅ {dias}d"
    except:
        return "⚠️"

# ============================================
# COMANDOS
# ============================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    
    user_id = str(update.effective_user.id)
    user_states[user_id] = ESTADO_NORMAL
    user_data_cache[user_id] = {}
    
    await update.message.reply_text(
        "🤖 *¡Hola! Asistente de Compras Listo*\n\n"
        "Puedes usar los botones o simplemente escribirme:\n"
        "• *Vendí [producto] en [precio] por [método]*\n"
        "• *Devuelto* (respondiendo a un mensaje)\n"
        "• *Borrar* (respondiendo a un mensaje)\n"
        "• *Cuánto he ganado?*\n\n"
        "También funciona con los botones de abajo 👇",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )


async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    
    await update.message.reply_text(
        "📖 *CÓMO USAR*\n\n"
        "*COMPRA:*\n"
        "📸 Botón o envía foto del pedido\n\n"
        "*VENTA (3 formas):*\n"
        "1️⃣ Botón 💰 VENTA → ID → Precio → Método\n"
        "2️⃣ Escribe: *Vendí la silla en 150 por paypal*\n"
        "3️⃣ Responde 'vendido' a cualquier mensaje mío\n\n"
        "*DEVOLUCIÓN:*\n"
        "Responde *'devuelto'* a cualquier mensaje\n\n"
        "*BORRAR:*\n"
        "1️⃣ Botón 🗑️ BORRAR → ID o nombre\n"
        "2️⃣ Responde *'borrar'* a cualquier mensaje\n"
        "3️⃣ Escribe: *Borra el que no tiene ID*\n\n"
        "*REVIEW:*\n"
        "⭐ Botón → Envía fotos → Escribe *'listo'* → Selecciona producto\n\n"
        "*CONSULTAS:*\n"
        "• *Cuánto he invertido?*\n"
        "• *Cuánto he ganado?*\n"
        "• *Qué productos tengo?*",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )


async def cmd_listar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    
    datos = obtener_datos()
    pendientes = [d for d in datos if d["estado"] == "pendiente"]
    
    if not pendientes:
        await update.message.reply_text("📭 No tienes productos pendientes")
        return
    
    texto = f"📋 *PENDIENTES: {len(pendientes)}*\n\n"
    for p in pendientes[:15]:
        est = estado_visual(p["fecha_devolucion"])
        id_corto = p["id"][-6:] if len(p["id"]) > 6 else p["id"]
        nombre = p["producto"][:25] + "..." if len(p["producto"]) > 25 else p["producto"]
        texto += f"`{id_corto}` | {nombre}\n💰 {p['precio_compra']} | {est}\n\n"
    
    if len(pendientes) > 15:
        texto += f"...y {len(pendientes)-15} más\n"
    
    texto += "\n_Responde 'vendido', 'devuelto' o 'borrar' a cualquier mensaje_"
    
    await update.message.reply_text(texto, parse_mode="Markdown")


async def cmd_estadisticas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    
    datos = obtener_datos()
    
    total = len(datos)
    pendientes = len([d for d in datos if d["estado"] == "pendiente"])
    vendidos = len([d for d in datos if d["estado"] == "vendido"])
    devueltos = len([d for d in datos if d["estado"] == "devuelto"])
    
    # Calcular totales
    total_invertido = 0
    total_vendido = 0
    ganancia_total = 0
    
    for d in datos:
        try:
            precio_c = float(str(d["precio_compra"]).replace("US$", "").replace("$", "").replace(",", "").strip() or 0)
            total_invertido += precio_c
            
            if d["estado"] == "vendido":
                precio_v = float(str(d["precio_venta"]).replace("US$", "").replace("$", "").replace(",", "").strip() or 0)
                total_vendido += precio_v
                ganancia_total += (precio_v - precio_c)
        except:
            pass
    
    texto = (
        f"📊 *ESTADÍSTICAS*\n\n"
        f"📦 Total productos: *{total}*\n"
        f"⏳ Pendientes: *{pendientes}*\n"
        f"✅ Vendidos: *{vendidos}*\n"
        f"🔄 Devueltos: *{devueltos}*\n\n"
        f"💰 Total invertido: *${total_invertido:.2f}*\n"
        f"💵 Total vendido: *${total_vendido:.2f}*\n"
        f"📈 Ganancia neta: *${ganancia_total:.2f}*"
    )
    
    await update.message.reply_text(texto, parse_mode="Markdown")

# ============================================
# PROCESAMIENTO PRINCIPAL - DETECCIÓN LOCAL RÁPIDA
# ============================================

async def procesar_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Procesa todo texto con detección local rápida"""
    if not autorizado(update):
        return
    
    user_id = str(update.effective_user.id)
    mensaje = update.message.text.strip()
    mensaje_lower = mensaje.lower()
    es_respuesta = update.message.reply_to_message is not None
    msg_original = update.message.reply_to_message.text if es_respuesta else None
    
    # Verificar estado actual del usuario
    estado = user_states.get(user_id, ESTADO_NORMAL)
    
    # === ESTADO: ESPERANDO FOTOS PARA REVIEW ===
    if estado == ESTADO_ESPERANDO_FOTOS_REVIEW:
        if mensaje_lower in ["listo", "ya", "terminé", "ok"]:
            return await procesar_review_final(update, context, user_id)
        elif mensaje_lower in ["cancelar", "cancela", "salir"]:
            return await cancelar_operacion(update, context, user_id)
        else:
            await update.message.reply_text(
                "📸 Estoy esperando las fotos.\n"
                "Envíalas y escribe *'listo'* cuando termines,\n"
                "o *'cancelar'* para salir.",
                parse_mode="Markdown"
            )
            return
    
    # === COMANDOS RÁPIDOS DE RESPUESTA ===
    if es_respuesta and msg_original:
        id_encontrado = extraer_id_de_texto(msg_original)
        
        # DEVUELTO
        if any(p in mensaje_lower for p in ["devuelto", "devolví", "return", "regresé"]):
            if id_encontrado:
                exito = marcar_devuelto(id_encontrado)
                if exito:
                    await update.message.reply_text(f"✅ Marcado como devuelto: `{id_encontrado}`", parse_mode="Markdown")
                else:
                    await update.message.reply_text("❌ Error al marcar")
            else:
                await update.message.reply_text("❌ No encontré ID en el mensaje original")
            return
        
        # BORRAR
        if any(p in mensaje_lower for p in ["borrar", "eliminar", "borra", "quitar"]):
            if id_encontrado:
                producto = buscar_producto(id_encontrado)
                if producto and not isinstance(producto, list):
                    user_data_cache[user_id] = {"borrar_id": id_encontrado, "producto": producto}
                    await update.message.reply_text(
                        f"🗑️ *¿Borrar este producto?*\n\n"
                        f"📦 {producto['producto']}\n"
                        f"🆔 `{id_encontrado}`\n"
                        f"💰 {producto['precio_compra']}",
                        parse_mode="Markdown",
                        reply_markup=get_confirmar_borrar_buttons(id_encontrado)
                    )
                    return
            
            # Si no hay ID, buscar en el texto del mensaje original
            nombre_prod = msg_original.split("\n")[0][:30] if msg_original else None
            if nombre_prod:
                productos = buscar_producto(nombre_prod)
                if productos and not isinstance(productos, list):
                    user_data_cache[user_id] = {"borrar_id": productos["id"], "producto": productos}
                    await update.message.reply_text(
                        f"🗑️ *¿Borrar este producto?*\n\n"
                        f"📦 {productos['producto']}\n"
                        f"🆔 `{productos['id']}`",
                        parse_mode="Markdown",
                        reply_markup=get_confirmar_borrar_buttons(productos["id"])
                    )
                    return
            
            await update.message.reply_text("❌ No pude identificar el producto. Usa el botón 🗑️ BORRAR")
            return
        
        # VENDIDO (inicio rápido)
        if any(p in mensaje_lower for p in ["vendido", "vendí", "lo vendí", "se vendió"]):
            if id_encontrado:
                producto = buscar_producto(id_encontrado)
                if producto and not isinstance(producto, list):
                    # Extraer precio y método del mensaje
                    precio = extraer_precio(mensaje)
                    metodo = detectar_metodo_pago(mensaje)
                    
                    if precio and metodo:
                        # Completar venta inmediatamente
                        exito, precio_compra = registrar_venta(id_encontrado, precio, METODOS_PAGO[metodo])
                        if exito:
                            ganancia = precio - precio_compra
                            emoji = "🎉" if ganancia > 0 else "⚠️"
                            await update.message.reply_text(
                                f"✅ *VENTA REGISTRADA*\n\n"
                                f"📦 {producto['producto']}\n"
                                f"💵 ${precio:.2f}\n"
                                f"💳 {METODOS_PAGO[metodo]}\n"
                                f"{emoji} Ganancia: ${ganancia:.2f}",
                                parse_mode="Markdown"
                            )
                        else:
                            await update.message.reply_text("❌ Error al registrar")
                    else:
                        # Guardar para completar después
                        user_data_cache[user_id] = {
                            "accion": "venta",
                            "producto_id": id_encontrado,
                            "producto": producto,
                            "precio": precio,
                            "metodo": metodo
                        }
                        
                        faltantes = []
                        if not precio:
                            faltantes.append("¿A qué precio lo vendiste?")
                        if not metodo:
                            faltantes.append("¿Por qué método? (paypal, zelle, efectivo, etc.)")
                        
                        await update.message.reply_text(
                            f"💰 *Venta de:* {producto['producto']}\n\n" +
                            "\n".join(f"• {f}" for f in faltantes),
                            parse_mode="Markdown"
                        )
                    return
            
            await update.message.reply_text("❌ No encontré el producto. Intenta con el ID completo.")
            return
    
    # === COMPLETAR VENTA PENDIENTE ===
    cache = user_data_cache.get(user_id, {})
    if cache.get("accion") == "venta":
        producto = cache["producto"]
        
        # Actualizar datos faltantes
        if not cache.get("precio"):
            cache["precio"] = extraer_precio(mensaje)
        if not cache.get("metodo"):
            cache["metodo"] = detectar_metodo_pago(mensaje)
        
        if cache.get("precio") and cache.get("metodo"):
            # Completar venta
            exito, precio_compra = registrar_venta(
                cache["producto_id"], 
                cache["precio"], 
                METODOS_PAGO[cache["metodo"]]
            )
            
            user_data_cache[user_id] = {}  # Limpiar cache
            
            if exito:
                ganancia = cache["precio"] - precio_compra
                emoji = "🎉" if ganancia > 0 else "⚠️"
                await update.message.reply_text(
                    f"✅ *VENTA COMPLETADA*\n\n"
                    f"📦 {producto['producto']}\n"
                    f"💵 ${cache['precio']:.2f}\n"
                    f"💳 {METODOS_PAGO[cache['metodo']]}\n"
                    f"{emoji} Ganancia: ${ganancia:.2f}",
                    parse_mode="Markdown"
                )
            else:
                await update.message.reply_text("❌ Error al completar venta")
        else:
            # Aún faltan datos
            faltantes = []
            if not cache.get("precio"):
                faltantes.append("¿Precio?")
            if not cache.get("metodo"):
                faltantes.append("¿Método de pago?")
            
            await update.message.reply_text(
                "Aún necesito:\n" + "\n".join(f"• {f}" for f in faltantes)
            )
        
        return
    
    # === VENTA NUEVA POR TEXTO ===
    if any(p in mensaje_lower for p in ["vendí", "vendi", "vender", "vendido"]):
        # Buscar producto mencionado
        palabras = mensaje.split()
        posibles_nombres = []
        
        for i, palabra in enumerate(palabras):
            if palabra.lower() in ["vendí", "vendi", "vendido", "el", "la", "los", "las"]:
                continue
            if any(char.isdigit() for char in palabra) and ("$" in palabra or len(palabra) < 10):
                continue  # Probable precio
            if len(palabra) > 3:
                posibles_nombres.append(palabra)
        
        # Buscar con las palabras encontradas
        producto = None
        for nombre in posibles_nombres[:3]:
            resultado = buscar_producto(nombre)
            if resultado and not isinstance(resultado, list):
                producto = resultado
                break
            elif resultado and isinstance(resultado, list):
                producto = resultado[0]
                break
        
        if not producto:
            await update.message.reply_text(
                "❌ No encontré el producto que mencionas.\n"
                "Intenta con: *Vendí [nombre del producto] en [precio] por [método]*\n"
                "O usa el botón 💰 VENTA",
                parse_mode="Markdown"
            )
            return
        
        # Extraer datos
        precio = extraer_precio(mensaje)
        metodo = detectar_metodo_pago(mensaje)
        
        if precio and metodo:
            exito, precio_compra = registrar_venta(producto["id"], precio, METODOS_PAGO[metodo])
            if exito:
                ganancia = precio - precio_compra
                emoji = "🎉" if ganancia > 0 else "⚠️"
                await update.message.reply_text(
                    f"✅ *VENTA REGISTRADA*\n\n"
                    f"📦 {producto['producto']}\n"
                    f"💵 ${precio:.2f}\n"
                    f"💳 {METODOS_PAGO[metodo]}\n"
                    f"{emoji} Ganancia: ${ganancia:.2f}",
                    parse_mode="Markdown"
                )
            else:
                await update.message.reply_text("❌ Error al registrar")
        else:
            # Guardar para completar
            user_data_cache[user_id] = {
                "accion": "venta",
                "producto_id": producto["id"],
                "producto": producto,
                "precio": precio,
                "metodo": metodo
            }
            
            faltantes = []
            if not precio:
                faltantes.append("¿A qué precio?")
            if not metodo:
                faltantes.append("¿Por qué método? (paypal, zelle, efectivo, amazon, depósito)")
            
            await update.message.reply_text(
                f"💰 *Venta de:* {producto['producto']}\n\n" +
                "\n".join(f"• {f}" for f in faltantes),
                parse_mode="Markdown"
            )
        
        return
    
    # === BORRAR POR TEXTO ===
    if any(p in mensaje_lower for p in ["borra", "borrar", "elimina", "quita"]):
        # Extraer posible ID o nombre
        palabras = mensaje.split()
        busqueda = None
        
        for i, palabra in enumerate(palabras):
            if palabra.lower() in ["borra", "borrar", "elimina", "quita", "el", "la", "los", "las", "que", "no", "tiene"]:
                continue
            if len(palabra) > 2:
                busqueda = palabra
                break
        
        if not busqueda:
            await update.message.reply_text(
                "🗑️ Indica qué quieres borrar:\n"
                "• ID del pedido (completo o últimos dígitos)\n"
                "• Nombre del producto\n"
                "• *'el que no tiene ID'* para borrar temporales",
                parse_mode="Markdown"
            )
            return
        
        # Buscar
        if "id" in mensaje_lower and ("no" in mensaje_lower or "sin" in mensaje_lower):
            # Buscar productos sin ID real
            datos = obtener_datos()
            sin_id = [d for d in datos if d["id"].startswith("TEMP-") or d["id"].startswith("NO_ID")]
            if sin_id:
                producto = sin_id[-1]  # El último agregado
                user_data_cache[user_id] = {"borrar_id": producto["id"], "producto": producto}
                await update.message.reply_text(
                    f"🗑️ *¿Borrar este producto sin ID?*\n\n"
                    f"📦 {producto['producto']}\n"
                    f"🆔 `{producto['id']}`\n"
                    f"💰 {producto['precio_compra']}",
                    parse_mode="Markdown",
                    reply_markup=get_confirmar_borrar_buttons(producto["id"])
                )
                return
        
        resultado = buscar_producto(busqueda)
        
        if resultado and not isinstance(resultado, list):
            user_data_cache[user_id] = {"borrar_id": resultado["id"], "producto": resultado}
            await update.message.reply_text(
                f"🗑️ *¿Borrar este producto?*\n\n"
                f"📦 {resultado['producto']}\n"
                f"🆔 `{resultado['id']}`\n"
                f"💰 {resultado['precio_compra']}",
                parse_mode="Markdown",
                reply_markup=get_confirmar_borrar_buttons(resultado["id"])
            )
        elif resultado and isinstance(resultado, list):
            texto = "🗑️ Encontré varios. ¿Cuál?\n\n"
            for i, p in enumerate(resultado[:3], 1):
                id_corto = p["id"][-6:] if len(p["id"]) > 6 else p["id"]
                texto += f"{i}. `{id_corto}` - {p['producto'][:30]}\n"
            texto += "\nResponde con el número o ID completo"
            await update.message.reply_text(texto, parse_mode="Markdown")
        else:
            await update.message.reply_text("❌ No encontré ese producto")
        
        return
    
    # === CONSULTAS ===
    if any(p in mensaje_lower for p in ["cuánto", "cuantos", "cuantas", "estadísticas", "stats", "ganancia", "invertido"]):
        datos = obtener_datos()
        
        total_inv = 0
        total_ventas = 0
        ganancia = 0
        
        for d in datos:
            try:
                pc = float(str(d["precio_compra"]).replace("US$", "").replace("$", "").replace(",", "").strip() or 0)
                total_inv += pc
                if d["estado"] == "vendido":
                    pv = float(str(d["precio_venta"]).replace("US$", "").replace("$", "").replace(",", "").strip() or 0)
                    total_ventas += pv
                    ganancia += (pv - pc)
            except:
                pass
        
        pendientes = len([d for d in datos if d["estado"] == "pendiente"])
        vendidos = len([d for d in datos if d["estado"] == "vendido"])
        
        await update.message.reply_text(
            f"📊 *TUS NÚMEROS*\n\n"
            f"💰 Invertido: *${total_inv:.2f}*\n"
            f"💵 Vendido: *${total_ventas:.2f}*\n"
            f"📈 Ganancia: *${ganancia:.2f}*\n\n"
            f"📦 Pendientes: *{pendientes}*\n"
            f"✅ Vendidos: *{vendidos}*",
            parse_mode="Markdown"
        )
        return
    
    # === BOTONES Y COMANDOS ===
    if mensaje == "📸 COMPRA":
        await update.message.reply_text(
            "📸 Envía la foto del pedido de Amazon",
            reply_markup=get_main_keyboard()
        )
        return
    
    if mensaje == "💰 VENTA":
        user_data_cache[user_id] = {"accion": "venta_buscar"}
        await update.message.reply_text(
            "💰 Indica el producto que vendiste:\n"
            "• ID del pedido\n"
            "• Nombre del producto\n"
            "• O escribe: *Vendí [producto] en [precio]*",
            parse_mode="Markdown"
        )
        return
    
    if mensaje == "⭐ REVIEW":
        user_states[user_id] = ESTADO_ESPERANDO_FOTOS_REVIEW
        user_data_cache[user_id] = {"fotos_review": [], "producto_review": None}
        await update.message.reply_text(
            "⭐ *MODO REVIEW ACTIVADO*\n\n"
            "Envía las fotos del producto (varias si quieres).\n"
            "Cuando termines, escribe *'listo'*.\n"
            "Para cancelar, escribe *'cancelar'*.",
            parse_mode="Markdown"
        )
        return
    
    if mensaje == "📋 LISTAR":
        await cmd_listar(update, context)
        return
    
    if mensaje == "🗑️ BORRAR":
        await update.message.reply_text(
            "🗑️ Indica qué quieres borrar:\n"
            "• ID del pedido\n"
            "• Nombre del producto\n"
            "• *'el último'* o *'sin ID'*",
            parse_mode="Markdown"
        )
        return
    
    if mensaje == "📊 ESTADÍSTICAS":
        await cmd_estadisticas(update, context)
        return
    
    if mensaje == "❓ AYUDA":
        await ayuda(update, context)
        return
    
    # === VENTA EN CURSO (BÚSQUEDA DE PRODUCTO) ===
    if cache.get("accion") == "venta_buscar":
        resultado = buscar_producto(mensaje)
        
        if resultado and not isinstance(resultado, list):
            user_data_cache[user_id] = {
                "accion": "venta",
                "producto_id": resultado["id"],
                "producto": resultado,
                "precio": None,
                "metodo": None
            }
            await update.message.reply_text(
                f"💰 *Producto:* {resultado['producto']}\n"
                f"¿A qué precio lo vendiste y por qué método?",
                parse_mode="Markdown"
            )
        elif resultado and isinstance(resultado, list):
            texto = "💰 Encontré varios:\n\n"
            for i, p in enumerate(resultado[:3], 1):
                id_corto = p["id"][-6:] if len(p["id"]) > 6 else p["id"]
                texto += f"{i}. `{id_corto}` - {p['producto'][:30]}\n"
            texto += "\nResponde con el número o nombre más específico"
            await update.message.reply_text(texto, parse_mode="Markdown")
        else:
            await update.message.reply_text("❌ No encontré ese producto. Intenta con el ID.")
        
        return
    
    # === NO ENTENDIDO ===
    await update.message.reply_text(
        "🤔 No entendí bien. Prueba con:\n"
        "• *Vendí [producto] en [precio]*\n"
        "• *Borrar [ID o nombre]*\n"
        "• *Cuánto he ganado?*\n"
        "• Usa los botones de abajo 👇\n\n"
        "O escribe /ayuda para ver ejemplos",
        reply_markup=get_main_keyboard()
    )


async def procesar_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Procesa fotos según el contexto"""
    if not autorizado(update):
        return
    
    user_id = str(update.effective_user.id)
    estado = user_states.get(user_id, ESTADO_NORMAL)
    
    # === FOTO PARA REVIEW ===
    if estado == ESTADO_ESPERANDO_FOTOS_REVIEW:
        photo = update.message.photo[-1]
        file = await photo.get_file()
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        image_path = f"review_{user_id}_{timestamp}_{len(user_data_cache.get(user_id, {}).get('fotos_review', []))}.jpg"
        
        await file.download_to_drive(image_path)
        
        if user_id not in user_data_cache:
            user_data_cache[user_id] = {}
        if "fotos_review" not in user_data_cache[user_id]:
            user_data_cache[user_id]["fotos_review"] = []
        
        user_data_cache[user_id]["fotos_review"].append(image_path)
        
        count = len(user_data_cache[user_id]["fotos_review"])
        await update.message.reply_text(
            f"📸 Foto {count} recibida. Envía más o escribe *'listo'*",
            parse_mode="Markdown"
        )
        return
    
    # === FOTO PARA COMPRA ===
    photo = update.message.photo[-1]
    file = await photo.get_file()
    
    image_path = f"compra_{user_id}_{update.message.message_id}.jpg"
    await file.download_to_drive(image_path)
    
    msg = await update.message.reply_text("⏳ Analizando compra...")
    
    try:
        datos = extraer_datos_compra_imagen(image_path)
        
        if not datos:
            await msg.edit_text("❌ No pude leer la imagen")
            return
        
        exito, pedido_id = agregar_compra(datos)
        
        if exito:
            est = estado_visual(datos.get("fecha_devolucion", ""))
            await msg.edit_text(
                f"✅ *COMPRA REGISTRADA*\n\n"
                f"🆔 `{pedido_id}`\n"
                f"📦 {datos['producto']}\n"
                f"💰 {datos['precio_compra']}\n"
                f"📅 Devolución: {datos.get('fecha_devolucion', 'No disponible')} {est}\n\n"
                f"_Responde 'vendido' o 'devuelto' a este mensaje_",
                parse_mode="Markdown",
                reply_markup=get_inline_buttons()
            )
        else:
            await msg.edit_text("❌ Error al guardar")
            
    except Exception as e:
        await msg.edit_text(f"❌ Error: {str(e)[:200]}")
    finally:
        if os.path.exists(image_path):
            os.remove(image_path)


async def procesar_review_final(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id):
    """Procesa fotos acumuladas y genera review"""
    cache = user_data_cache.get(user_id, {})
    fotos = cache.get("fotos_review", [])
    
    if not fotos:
        await update.message.reply_text("❌ No hay fotos. Cancelando.")
        user_states[user_id] = ESTADO_NORMAL
        return
    
    msg = await update.message.reply_text(f"⏳ Generando review con {len(fotos)} imágenes...")
    
    try:
        review = generar_review_imagenes(fotos)
        
        # Limpiar fotos
        for f in fotos:
            if os.path.exists(f):
                os.remove(f)
        
        user_states[user_id] = ESTADO_NORMAL
        user_data_cache[user_id]["fotos_review"] = []
        user_data_cache[user_id]["review_generada"] = review
        
        if not review:
            await msg.edit_text("❌ Error generando review")
            return
        
        # Preguntar a qué producto asociar
        datos = obtener_datos()
        pendientes = [d for d in datos if d["estado"] == "pendiente"][-5:]  # Últimos 5
        
        if pendientes:
            texto = "⭐ *REVIEW GENERADA*\n\n¿A qué producto la asociamos?\n\n"
            for i, p in enumerate(pendientes, 1):
                id_corto = p["id"][-6:] if len(p["id"]) > 6 else p["id"]
                texto += f"{i}. `{id_corto}` - {p['producto'][:25]}\n"
            texto += "\nResponde con el número, *'ninguno'* para no guardar, o *'otro'* para buscar"
            
            user_data_cache[user_id]["review_opciones"] = pendientes
            
            await msg.edit_text(texto, parse_mode="Markdown")
        else:
            await msg.edit_text(
                f"⭐ *REVIEW GENERADA*\n\n{review}\n\n💾 No guardada (sin productos pendientes)",
                parse_mode="Markdown"
            )
            
    except Exception as e:
        await msg.edit_text(f"❌ Error: {str(e)[:200]}")
        for f in fotos:
            if os.path.exists(f):
                os.remove(f)
        user_states[user_id] = ESTADO_NORMAL


async def asociar_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Asocia review generada a un producto"""
    user_id = str(update.effective_user.id)
    mensaje = update.message.text.strip()
    cache = user_data_cache.get(user_id, {})
    
    review = cache.get("review_generada", "")
    opciones = cache.get("review_opciones", [])
    
    if not review:
        return False  # No hay review pendiente
    
    # No guardar
    if mensaje.lower() in ["ninguno", "no", "cancelar"]:
        await update.message.reply_text(
            "⭐ Review generada (no guardada en Sheets)",
            reply_markup=get_inline_buttons()
        )
        user_data_cache[user_id]["review_generada"] = None
        user_data_cache[user_id]["review_opciones"] = None
        return True
    
    # Buscar por número
    try:
        num = int(mensaje)
        if 1 <= num <= len(opciones):
            producto = opciones[num-1]
            guardar_review(producto["id"], review)
            await update.message.reply_text(
                f"✅ Review guardada en:\n📦 {producto['producto']}\n🆔 `{producto['id']}`",
                parse_mode="Markdown",
                reply_markup=get_inline_buttons()
            )
            user_data_cache[user_id]["review_generada"] = None
            user_data_cache[user_id]["review_opciones"] = None
            return True
    except ValueError:
        pass
    
    # Buscar por ID o nombre
    resultado = buscar_producto(mensaje)
    if resultado and not isinstance(resultado, list):
        guardar_review(resultado["id"], review)
        await update.message.reply_text(
            f"✅ Review guardada en:\n📦 {resultado['producto']}",
            parse_mode="Markdown"
        )
        user_data_cache[user_id]["review_generada"] = None
        return True
    
    await update.message.reply_text("❌ No encontré ese producto. Intenta de nuevo o escribe 'ninguno'")
    return True


async def cancelar_operacion(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id):
    """Cancela operación actual"""
    # Limpiar fotos si hay
    cache = user_data_cache.get(user_id, {})
    fotos = cache.get("fotos_review", [])
    for f in fotos:
        if os.path.exists(f):
            os.remove(f)
    
    user_states[user_id] = ESTADO_NORMAL
    user_data_cache[user_id] = {}
    
    await update.message.reply_text("❌ Cancelado", reply_markup=get_main_keyboard())


async def manejar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja botones inline"""
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = str(query.from_user.id)
    
    # Borrar confirmado
    if data.startswith("borrar_"):
        pedido_id = data.replace("borrar_", "")
        exito, producto = borrar_producto(pedido_id)
        
        if exito:
            await query.edit_message_text(
                f"🗑️ *BORRADO*\n\n"
                f"📦 {producto['producto']}\n"
                f"🆔 `{pedido_id}`",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text("❌ Error al borrar")
        return
    
    if data == "cancelar":
        await query.edit_message_text("❌ Cancelado")
        return
    
    # Botones de menú
    if data == "btn_compra":
        await query.message.reply_text("📸 Envía la foto del pedido")
    elif data == "btn_venta":
        user_data_cache[user_id] = {"accion": "venta_buscar"}
        await query.message.reply_text("💰 Indica el producto que vendiste (ID o nombre)")
    elif data == "btn_review":
        user_states[user_id] = ESTADO_ESPERANDO_FOTOS_REVIEW
        user_data_cache[user_id] = {"fotos_review": []}
        await query.message.reply_text(
            "⭐ Envía fotos del producto. Escribe 'listo' cuando termines."
        )
    elif data == "btn_listar":
        await cmd_listar(update, context)
    elif data == "btn_borrar":
        await query.message.reply_text("🗑️ Indica qué quieres borrar (ID o nombre)")
    
    # Método de pago (para ventas iniciadas por botón)
    elif data.startswith("mp_"):
        metodo = data.replace("mp_", "")
        cache = user_data_cache.get(user_id, {})
        
        if cache.get("accion") == "esperando_metodo":
            producto = cache.get("producto")
            precio = cache.get("precio")
            
            exito, precio_compra = registrar_venta(producto["id"], precio, METODOS_PAGO[metodo])
            
            if exito:
                ganancia = precio - precio_compra
                emoji = "🎉" if ganancia > 0 else "⚠️"
                await query.edit_message_text(
                    f"✅ *VENTA REGISTRADA*\n\n"
                    f"📦 {producto['producto']}\n"
                    f"💵 ${precio:.2f}\n"
                    f"💳 {METODOS_PAGO[metodo]}\n"
                    f"{emoji} Ganancia: ${ganancia:.2f}"
                )
            else:
                await query.edit_message_text("❌ Error al registrar")
            
            user_data_cache[user_id] = {}

# ============================================
# MAIN
# ============================================

async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("start", "Iniciar bot"),
        BusCommand("ayuda", "Ver ayuda y ejemplos"),
        BotCommand("listar", "Listar pendientes"),
        BotCommand("stats", "Ver estadísticas"),
    ])


def main():
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO
    )
    
    if not all([TELEGRAM_TOKEN, GOOGLE_SHEETS_ID, GEMINI_API_KEY, TU_CHAT_ID, GOOGLE_CREDENTIALS_JSON]):
        print("❌ Faltan variables de entorno")
        return
    
    print("🤖 Bot IA v6.0 - Simplificado y Estable")
    print(f"✅ Chat ID: {TU_CHAT_ID}")
    
    application = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    
    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("ayuda", ayuda))
    application.add_handler(CommandHandler("listar", cmd_listar))
    application.add_handler(CommandHandler("stats", cmd_estadisticas))
    
    application.add_handler(CallbackQueryHandler(manejar_callback))
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, procesar_foto))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, procesar_texto))
    
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
