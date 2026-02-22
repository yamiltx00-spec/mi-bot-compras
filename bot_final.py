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
# CONFIGURACIÓN
# ============================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TU_CHAT_ID = os.getenv("TU_CHAT_ID")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

# Cache de datos
cache_datos = {"datos": None, "ultima_actualizacion": None, "mensajes_pendientes": {}}

# Estados solo para flujos complejos que requieren pasos
ESPERANDO_FOTOS_REVIEW = 1

METODOS_PAGO = {
    "paypal": "💳 PayPal",
    "amazon": "📦 Amazon", 
    "zelle": "💰 Zelle",
    "efectivo": "💵 Efectivo",
    "deposito": "🏦 Depósito",
    "otro": "📝 Otro",
}

ID_RE = re.compile(r"ID:\s*([0-9]{3}-[0-9]{7}-[0-9]{7})")
ID_COMPLETO_RE = re.compile(r"^\d{3}-\d{7}-\d{7}$")

# ============================================
# TECLADOS
# ============================================

def get_main_keyboard():
    keyboard = [
        [KeyboardButton("📸 COMPRA"), KeyboardButton("💰 VENTA"), KeyboardButton("⭐ REVIEW")],
        [KeyboardButton("📋 LISTAR"), KeyboardButton("🗑️ BORRAR"), KeyboardButton("🤖 MODO IA")],
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
            InlineKeyboardButton("🤖 Modo IA", callback_data="btn_modo_ia"),
            InlineKeyboardButton("🗑️ Borrar", callback_data="btn_borrar"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_confirmar_buttons(accion, item_id):
    keyboard = [
        [
            InlineKeyboardButton(f"✅ Sí, {accion}", callback_data=f"confirm_{accion}_{item_id}"),
            InlineKeyboardButton("❌ Cancelar", callback_data="cancelar_accion"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

# ============================================
# GOOGLE SHEETS
# ============================================

def get_sheets_service():
    try:
        info = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        return build("sheets", "v4", credentials=creds)
    except Exception as e:
        logging.error(f"Error Sheets: {e}")
        raise


def obtener_todas_las_compras(force_refresh=False):
    """Obtiene datos con cache de 2 minutos"""
    global cache_datos
    ahora = datetime.now()
    
    if (not force_refresh and cache_datos["datos"] is not None and 
        cache_datos["ultima_actualizacion"] is not None and
        (ahora - cache_datos["ultima_actualizacion"]).seconds < 120):
        return cache_datos["datos"]
    
    try:
        service = get_sheets_service()
        result = service.spreadsheets().values().get(
            spreadsheetId=GOOGLE_SHEETS_ID, range="A:J"
        ).execute()
        values = result.get("values", [])
        
        compras = []
        for i, row in enumerate(values[1:], 1):
            if not row:
                continue
            
            compra = {
                "fila": i + 1,
                "id": row[0] if len(row) > 0 else f"NO_ID_{i}",
                "fecha_compra": row[1] if len(row) > 1 else "",
                "producto": row[2] if len(row) > 2 else "Sin nombre",
                "precio_compra": row[3] if len(row) > 3 else "0",
                "fecha_devolucion": row[4] if len(row) > 4 else "",
                "fecha_venta": row[5] if len(row) > 5 else "",
                "precio_venta": row[6] if len(row) > 6 else "",
                "metodo_pago": row[7] if len(row) > 7 else "",
                "estado": row[8] if len(row) > 8 and row[8] else "pendiente",
                "review": row[9] if len(row) > 9 else "",
            }
            compras.append(compra)
        
        cache_datos["datos"] = compras
        cache_datos["ultima_actualizacion"] = ahora
        return compras
        
    except Exception as e:
        logging.error(f"Error obtener datos: {e}")
        return cache_datos.get("datos", [])


def buscar_por_id_o_producto(busqueda):
    """Busca por ID completo, sufijo o nombre de producto"""
    datos = obtener_todas_las_compras()
    busqueda_lower = busqueda.lower().strip()
    
    # Buscar por ID completo
    if ID_COMPLETO_RE.match(busqueda):
        for d in datos:
            if d["id"] == busqueda:
                return d
    
    # Buscar por sufijo de ID
    for d in datos:
        if d["id"].endswith(busqueda):
            return d
    
    # Buscar por nombre de producto (contiene)
    coincidencias = []
    for d in datos:
        if busqueda_lower in d["producto"].lower():
            coincidencias.append(d)
    
    if len(coincidencias) == 1:
        return coincidencias[0]
    elif len(coincidencias) > 1:
        return coincidencias[:3]  # Devolver top 3
    
    return None


def agregar_compra(datos):
    try:
        service = get_sheets_service()
        
        # Calcular fecha devolución si no existe
        fecha_dev = datos.get("fecha_devolucion", "")
        if not fecha_dev or fecha_dev == "NO_ENCONTRADO":
            try:
                fecha_compra = datetime.strptime(datos["fecha_compra"], "%d/%m/%Y")
                fecha_dev = (fecha_compra + timedelta(days=30)).strftime("%d/%m/%Y")
            except:
                fecha_dev = (datetime.now() + timedelta(days=30)).strftime("%d/%m/%Y")
        
        # Generar ID temporal si no hay
        pedido_id = datos.get("id_pedido", "")
        if not pedido_id or pedido_id == "NO_ENCONTRADO":
            pedido_id = f"TEMP-{datetime.now().strftime('%Y%m%d')}-{random.randint(1000,9999)}"
        
        values = [[
            pedido_id,
            datos.get("fecha_compra", datetime.now().strftime("%d/%m/%Y")),
            datos.get("producto", "Sin nombre"),
            datos.get("precio_compra", "0"),
            fecha_dev,
            "", "", "", "pendiente", "",
        ]]
        
        service.spreadsheets().values().append(
            spreadsheetId=GOOGLE_SHEETS_ID,
            range="A:J",
            valueInputOption="USER_ENTERED",
            body={"values": values},
        ).execute()
        
        # Refrescar cache
        obtener_todas_las_compras(force_refresh=True)
        return True, pedido_id
        
    except Exception as e:
        logging.error(f"Error agregar compra: {e}")
        return False, None


def registrar_venta(id_pedido, precio_venta, metodo_pago):
    try:
        service = get_sheets_service()
        datos = obtener_todas_las_compras(force_refresh=True)
        
        for compra in datos:
            if compra["id"] == id_pedido:
                fila = compra["fila"]
                fecha_venta = datetime.now().strftime("%d/%m/%Y")
                
                service.spreadsheets().values().update(
                    spreadsheetId=GOOGLE_SHEETS_ID,
                    range=f"F{fila}:I{fila}",
                    valueInputOption="USER_ENTERED",
                    body={"values": [[fecha_venta, str(precio_venta), metodo_pago, "vendido"]]},
                ).execute()
                
                # Calcular ganancia
                try:
                    precio_compra = float(str(compra["precio_compra"]).replace("US$", "").replace("$", "").replace(",", "").strip() or 0)
                except:
                    precio_compra = 0
                
                obtener_todas_las_compras(force_refresh=True)
                return True, precio_compra
        
        return False, 0
        
    except Exception as e:
        logging.error(f"Error registrar venta: {e}")
        return False, 0


def marcar_devuelto(id_pedido):
    try:
        service = get_sheets_service()
        datos = obtener_todas_las_compras(force_refresh=True)
        
        for compra in datos:
            if compra["id"] == id_pedido:
                fila = compra["fila"]
                fecha_hoy = datetime.now().strftime("%d/%m/%Y")
                
                service.spreadsheets().values().update(
                    spreadsheetId=GOOGLE_SHEETS_ID,
                    range=f"F{fila}:I{fila}",
                    valueInputOption="USER_ENTERED",
                    body={"values": [[fecha_hoy, "0", "", "devuelto"]]},
                ).execute()
                
                obtener_todas_las_compras(force_refresh=True)
                return True
        
        return False
        
    except Exception as e:
        logging.error(f"Error marcar devuelto: {e}")
        return False


def borrar_compra(id_pedido):
    try:
        service = get_sheets_service()
        datos = obtener_todas_las_compras(force_refresh=True)
        
        for compra in datos:
            if compra["id"] == id_pedido:
                fila = compra["fila"]
                service.spreadsheets().values().clear(
                    spreadsheetId=GOOGLE_SHEETS_ID,
                    range=f"A{fila}:J{fila}",
                ).execute()
                
                obtener_todas_las_compras(force_refresh=True)
                return True, compra
        
        return False, None
        
    except Exception as e:
        logging.error(f"Error borrar: {e}")
        return False, None


def guardar_review(id_pedido, review_text):
    try:
        service = get_sheets_service()
        datos = obtener_todas_las_compras(force_refresh=True)
        
        for compra in datos:
            if compra["id"] == id_pedido:
                fila = compra["fila"]
                service.spreadsheets().values().update(
                    spreadsheetId=GOOGLE_SHEETS_ID,
                    range=f"J{fila}",
                    valueInputOption="USER_ENTERED",
                    body={"values": [[review_text[:5000]]]},  # Limitar tamaño
                ).execute()
                return True
        
        return False
        
    except Exception as e:
        logging.error(f"Error guardar review: {e}")
        return False

# ============================================
# GEMINI - FUNCIONES INTELIGENTES
# ============================================

def llamar_gemini(prompt, temperature=0.7, max_tokens=1000):
    """Llamada base a Gemini"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        }
    }
    
    try:
        response = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=30
        )
        
        if response.status_code != 200:
            logging.error(f"Error Gemini HTTP {response.status_code}")
            return None
            
        return response.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        
    except Exception as e:
        logging.error(f"Error llamar Gemini: {e}")
        return None


def extraer_datos_compra_imagen(image_path):
    """Extrae datos de compra de una imagen"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    
    with open(image_path, "rb") as f:
        img_base64 = base64.b64encode(f.read()).decode()
    
    prompt = """Analiza esta captura de pantalla de compra de Amazon.
Extrae en JSON válido:
{
    "id_pedido": "número de orden completo",
    "fecha_compra": "DD/MM/YYYY",
    "producto": "nombre corto del producto",
    "precio_compra": "precio total con símbolo $",
    "fecha_devolucion": "DD/MM/YYYY o vacío"
}

Si no hay número de orden visible, usa "NO_DISPONIBLE".
Responde SOLO con el JSON, sin texto adicional."""
    
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
        
        # Limpiar markdown
        if "```" in texto:
            texto = texto.split("```")[1].replace("json", "").strip()
        
        datos = json.loads(texto)
        
        # Validar campos
        return {
            "id_pedido": datos.get("id_pedido", "NO_DISPONIBLE"),
            "fecha_compra": datos.get("fecha_compra", datetime.now().strftime("%d/%m/%Y")),
            "producto": datos.get("producto", "Producto sin nombre"),
            "precio_compra": datos.get("precio_compra", "0"),
            "fecha_devolucion": datos.get("fecha_devolucion", ""),
        }
        
    except Exception as e:
        logging.error(f"Error extraer datos imagen: {e}")
        return None


def generar_review_imagenes(image_paths):
    """Genera review analizando múltiples imágenes"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    
    parts = []
    
    # Prompt inicial
    prompt = """Actúa como experto en reseñas de Amazon. Genera UNA reseña realista en español e inglés.

REGLAS ESTRICTAS:
- NO menciones envío, empaquetado, precio ni atención al cliente
- NO uses frases genéricas como "muy buen producto"
- Incluye 1-3 errores ortográficos menores naturales
- Menciona detalles específicos del producto visibles en las fotos
- Estructura: Título corto (4-8 palabras) + Cuerpo (80-150 palabras)
- Si es 5 estrellas, menciona UN defecto menor realista
- Si es 1-2 estrellas, menciona algo positivo antes de la crítica

Formato de salida:
[ESPAÑOL]
⭐ X estrellas
Título: ...
Reseña: ...

[ENGLISH]
⭐ X stars  
Title: ...
Review: ..."""
    
    parts.append({"text": prompt})
    
    # Agregar imágenes
    for path in image_paths:
        with open(path, "rb") as f:
            img_base64 = base64.b64encode(f.read()).decode()
            parts.append({
                "inline_data": {"mime_type": "image/jpeg", "data": img_base64}
            })
    
    payload = {"contents": [{"parts": parts}]}
    
    try:
        response = requests.post(url, headers={"Content-Type": "application/json"}, json=payload, timeout=60)
        return response.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        logging.error(f"Error generar review: {e}")
        return None


def interpretar_mensaje_ia(mensaje_usuario, contexto_chat, datos_disponibles):
    """
    IA principal que interpreta cualquier mensaje y decide qué acción tomar.
    Devuelve un dict con la acción a ejecutar.
    """
    
    prompt = f"""Eres el cerebro de un bot de gestión de inventario. Analiza el mensaje del usuario y determina la ACCIÓN a ejecutar.

CONTEXTO DEL CHAT:
{contexto_chat}

DATOS DISPONIBLES DEL USUARIO:
- Total productos: {len(datos_disponibles)}
- Productos pendientes: {len([d for d in datos_disponibles if d['estado'] == 'pendiente'])}
- Productos vendidos: {len([d for d in datos_disponibles if d['estado'] == 'vendido'])}
- Productos devueltos: {len([d for d in datos_disponibles if d['estado'] == 'devuelto'])}

MENSAJE DEL USUARIO: "{mensaje_usuario}"

INSTRUCCIONES:
Determina la intención y extrae parámetros. Responde ÚNICAMENTE en este formato JSON:

{{
    "accion": "VENTA|DEVOLUCION|BORRAR|CONSULTA|REVIEW|COMPRA|AYUDA|DESCONOCIDO",
    "confianza": 0.0-1.0,
    "parametros": {{
        "id_producto": "ID extraído o null",
        "nombre_producto": "nombre mencionado o null", 
        "precio": número o null,
        "metodo_pago": "paypal|zelle|efectivo|amazon|deposito|otro o null",
        "pregunta_faltante": "qué información falta para completar la acción o null"
    }},
    "respuesta_natural": "mensaje amigable para el usuario explicando qué harás o qué necesitas"
}}

REGLAS DE ACCIÓN:
- VENTA: Si menciona "vendí", "vendido", "lo vendí", "se vendió"
- DEVOLUCION: Si menciona "devolví", "devuelto", "lo devolví", "return"
- BORRAR: Si menciona "borra", "elimina", "quita", "borrar", "eliminar"
- CONSULTA: Si pregunta por datos ("cuántos", "cuánto", "lista", "pendientes", "ganancia")
- REVIEW: Si menciona "review", "reseña", "opinión"
- COMPRA: Si envía foto o menciona "nueva compra", "registrar compra"
- AYUDA: Si pide ayuda o no entiende

EJEMPLOS:
"vendí la silla gamer en 150 por paypal" → {{"accion": "VENTA", "parametros": {{"nombre_producto": "silla gamer", "precio": 150, "metodo_pago": "paypal"}}}}
"borra el que no tiene id" → {{"accion": "BORRAR", "parametros": {{"id_producto": null, "nombre_producto": "sin id"}}}}
"cuánto he ganado?" → {{"accion": "CONSULTA", "parametros": {{"tipo": "ganancia_total"}}}}
"listo" (esperando fotos) → {{"accion": "REVIEW", "parametros": {{"completar": true}}}}

Responde SOLO el JSON válido, sin markdown ni explicaciones."""

    respuesta = llamar_gemini(prompt, temperature=0.3, max_tokens=800)
    
    if not respuesta:
        return {"accion": "ERROR", "respuesta_natural": "Lo siento, tuve un problema procesando tu mensaje. ¿Puedes intentar de nuevo?"}
    
    try:
        # Limpiar posible markdown
        if "```" in respuesta:
            respuesta = respuesta.split("```")[1].replace("json", "").strip()
        
        resultado = json.loads(respuesta)
        return resultado
        
    except json.JSONDecodeError:
        # Fallback: intentar extraer acción manualmente
        mensaje_lower = mensaje_usuario.lower()
        
        if any(p in mensaje_lower for p in ["vendí", "vendido", "lo vendí", "vendes"]):
            return {"accion": "VENTA", "confianza": 0.8, "parametros": {}, "respuesta_natural": "Veo que quieres registrar una venta. Déjame procesarlo..."}
        elif any(p in mensaje_lower for p in ["devolví", "devuelto", "return"]):
            return {"accion": "DEVOLUCION", "confianza": 0.8, "parametros": {}, "respuesta_natural": "Procesando devolución..."}
        elif any(p in mensaje_lower for p in ["borra", "elimina", "quita"]):
            return {"accion": "BORRAR", "confianza": 0.8, "parametros": {}, "respuesta_natural": "Entendido, quieres borrar algo..."}
        elif any(p in mensaje_lower for p in ["cuánto", "cuántos", "lista", "pendientes"]):
            return {"accion": "CONSULTA", "confianza": 0.8, "parametros": {}, "respuesta_natural": "Consultando tus datos..."}
        
        return {"accion": "DESCONOCIDO", "confianza": 0.5, "parametros": {}, "respuesta_natural": "No estoy seguro de qué quieres hacer. ¿Puedes ser más específico? Puedes decirme cosas como 'vendí X en Y' o 'borra el producto Z'."}


def generar_respuesta_consulta(tipo_consulta, datos):
    """Genera respuesta natural para consultas de datos"""
    
    prompt = f"""Genera una respuesta natural y útil para una consulta de inventario.

TIPO DE CONSULTA: {tipo_consulta}

DATOS:
- Total productos: {len(datos)}
- Pendientes: {len([d for d in datos if d['estado'] == 'pendiente'])}
- Vendidos: {len([d for d in datos if d['estado'] == 'vendido'])}
- Devueltos: {len([d for d in datos if d['estado'] == 'devuelto'])}

PRODUCTOS PENDIENTES (máx 5):
{chr(10).join([f"- {d['producto']} (${d['precio_compra']})" for d in datos if d['estado'] == 'pendiente'][:5])}

Responde de forma conversacional, incluyendo números específicos. Sé breve pero completo. Usa emojis ocasionales."""

    respuesta = llamar_gemini(prompt, temperature=0.7, max_tokens=500)
    return respuesta or "Aquí tienes tus datos..."

# ============================================
# HELPERS
# ============================================

def autorizado(update: Update) -> bool:
    uid = str(update.effective_user.id) if update.effective_user else ""
    return uid == TU_CHAT_ID


def extraer_id_de_texto(texto):
    """Extrae ID de pedido de cualquier texto"""
    if not texto:
        return None
    match = ID_RE.search(texto)
    return match.group(1) if match else None


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


async def enviar_mensaje(update: Update, texto: str, **kwargs):
    """Helper para enviar mensajes desde cualquier contexto"""
    if update.callback_query:
        await update.callback_query.message.reply_text(texto, **kwargs)
    elif update.message:
        await update.message.reply_text(texto, **kwargs)

# ============================================
# COMANDOS
# ============================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    
    await update.message.reply_text(
        f"🤖 *¡Hola! Soy tu Asistente Inteligente*\n\n"
        f"Puedo entender mensajes naturales:\n"
        f"• _Vendí la silla gamer en 150 por paypal_\n"
        f"• _Borra el que no tiene ID_\n"
        f"• _Cuánto he ganado este mes?_\n"
        f"• _Genera review de este producto_\n\n"
        f"También puedes usar los botones de abajo 👇\n"
        f"Escribe /ayuda para más información.",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )


async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update):
        return
    
    await update.message.reply_text(
        "📖 *MODO INTELIGENTE ACTIVADO*\n\n"
        "*Solo dímelo naturalmente:*\n\n"
        "💰 *Ventas:*\n"
        "• _Vendí [producto] en [precio] por [método]_\n"
        "• _Lo vendí por 200 en zelle_\n"
        "• _Marcar como vendido_\n\n"
        "🔄 *Devoluciones:*\n"
        "• _Lo devolví_\n"
        "• _Marcar como devuelto_\n\n"
        "🗑️ *Borrar:*\n"
        "• _Borra el que no tiene ID_\n"
        "• _Elimina el último_\n"
        "• Responde _'borrar'_ a cualquier mensaje mío\n\n"
        "📊 *Consultas:*\n"
        "• _Cuánto he invertido?_\n"
        "• _Qué productos tengo pendientes?_\n"
        "• _Cuál es mi ganancia?_\n\n"
        "⭐ *Reviews:*\n"
        "• _Genera review de este producto_ (envía fotos después)\n"
        "• Usa el botón ⭐ REVIEW\n\n"
        "Los botones también funcionan como respaldo.",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )


async def modo_ia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Activa el modo IA conversacional"""
    if not autorizado(update):
        return
    
    context.user_data["modo_ia"] = True
    context.user_data["contexto_chat"] = []
    
    await update.message.reply_text(
        "🤖 *MODO IA ACTIVADO*\n\n"
        "Ahora entiendo mensajes naturales. Ejemplos:\n"
        "• _Vendí la silla en 150 por paypal_\n"
        "• _Borra el producto que no tiene ID_\n"
        "• _Cuánto he ganado?_\n\n"
        "Simplemente escríbeme lo que quieres hacer.\n"
        "Escribe _'salir'_ para desactivar modo IA.",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )

# ============================================
# PROCESADOR PRINCIPAL DE IA
# ============================================

async def procesar_mensaje_inteligente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Procesa cualquier mensaje de texto usando IA para entender la intención
    """
    if not autorizado(update):
        return
    
    mensaje = update.message.text
    es_respuesta = update.message.reply_to_message is not None
    mensaje_original = update.message.reply_to_message.text if es_respuesta else None
    
    # Verificar si estamos esperando fotos para review
    if context.user_data.get("esperando_fotos_review"):
        if mensaje.lower() in ["listo", "ya", "terminé", "listo"]:
            return await procesar_review_completo(update, context)
        else:
            await update.message.reply_text(
                "📸 Estoy esperando las fotos del producto.\n"
                "Envíalas y luego escribe *'listo'* cuando termines.",
                parse_mode="Markdown"
            )
            return
    
    # Obtener contexto del chat
    contexto = context.user_data.get("contexto_chat", [])
    datos = obtener_todas_las_compras()
    
    # Construir contexto enriquecido si es respuesta
    contexto_enriquecido = ""
    if es_respuesta and mensaje_original:
        # Extraer ID del mensaje original
        id_encontrado = extraer_id_de_texto(mensaje_original)
        producto_encontrado = None
        
        if id_encontrado:
            for d in datos:
                if d["id"] == id_encontrado:
                    producto_encontrado = d
                    break
        
        contexto_enriquecido = f"MENSAJE AL QUE RESPONDE: {mensaje_original}\n"
        if producto_encontrado:
            contexto_enriquecido += f"PRODUCTO REFERENCIADO: ID={producto_encontrado['id']}, Nombre={producto_encontrado['producto']}, Estado={producto_encontrado['estado']}, Precio=${producto_encontrado['precio_compra']}\n"
        elif id_encontrado:
            contexto_enriquecido += f"ID ENCONTRADO EN MENSAJE: {id_encontrado}\n"
    
    # Interpretar intención con IA
    msg_procesando = await update.message.reply_text("🤔 Entendiendo...")
    
    intencion = interpretar_mensaje_ia(
        mensaje_usuario=mensaje,
        contexto_chat=contexto_enriquecido + "\n".join([f"Usuario: {c['usuario']}\nBot: {c['bot']}" for c in contexto[-3:]]),
        datos_disponibles=datos
    )
    
    # Guardar en contexto
    contexto.append({"usuario": mensaje, "bot": intencion.get("respuesta_natural", "")})
    context.user_data["contexto_chat"] = contexto[-10:]  # Mantener últimos 10
    
    # Ejecutar acción según intención
    accion = intencion.get("accion", "DESCONOCIDO")
    params = intencion.get("parametros", {})
    respuesta_ia = intencion.get("respuesta_natural", "Procesando...")
    
    if accion == "ERROR":
        await msg_procesando.edit_text("❌ Lo siento, tuve un error. Intenta de nuevo.")
        return
    
    # === VENTA ===
    if accion == "VENTA":
        await msg_procesando.edit_text(respuesta_ia)
        
        # Buscar producto
        producto = None
        if params.get("id_producto"):
            producto = buscar_por_id_o_producto(params["id_producto"])
        elif params.get("nombre_producto"):
            producto = buscar_por_id_o_producto(params["nombre_producto"])
        elif es_respuesta and mensaje_original:
            # Buscar en el mensaje al que se respondió
            id_ref = extraer_id_de_texto(mensaje_original)
            if id_ref:
                producto = buscar_por_id_o_producto(id_ref)
        
        if isinstance(producto, list):
            producto = producto[0] if producto else None
        
        if not producto:
            await update.message.reply_text(
                "❌ No encontré el producto que mencionas.\n"
                "¿Puedes ser más específico o usar el ID?",
                reply_markup=get_main_keyboard()
            )
            return
        
        # Verificar si ya está vendido
        if producto["estado"] == "vendido":
            await update.message.reply_text(
                f"⚠️ Este producto ya está marcado como vendido:\n"
                f"📦 {producto['producto']}\n"
                f"💰 Vendido en: ${producto['precio_venta']}",
                reply_markup=get_main_keyboard()
            )
            return
        
        # Extraer precio y método del mensaje si no están en params
        precio = params.get("precio")
        metodo = params.get("metodo_pago")
        
        # Si no tenemos precio o método, preguntar de forma natural
        if not precio or not metodo:
            faltantes = []
            if not precio:
                faltantes.append("¿A qué precio lo vendiste?")
            if not metodo:
                faltantes.append("¿Por qué método te pagaron? (paypal, zelle, efectivo, etc.)")
            
            context.user_data["pendiente_venta"] = {
                "producto": producto,
                "precio": precio,
                "metodo": metodo
            }
            
            await update.message.reply_text(
                f"💰 Entendido, quieres vender: *{producto['producto']}*\n\n"
                f"Necesito que me indiques:\n" + "\n".join(f"• {f}" for f in faltantes),
                parse_mode="Markdown",
                reply_markup=get_main_keyboard()
            )
            return
        
        # Registrar venta completa
        exito, precio_compra = registrar_venta(producto["id"], precio, METODOS_PAGO.get(metodo, metodo))
        
        if exito:
            ganancia = precio - precio_compra
            emoji = "🎉" if ganancia > 0 else "⚠️"
            await update.message.reply_text(
                f"✅ *VENTA REGISTRADA*\n\n"
                f"📦 {producto['producto']}\n"
                f"💵 Venta: ${precio:.2f}\n"
                f"💰 Compra: ${precio_compra:.2f}\n"
                f"💳 {METODOS_PAGO.get(metodo, metodo)}\n"
                f"{emoji} Ganancia: ${ganancia:.2f}",
                parse_mode="Markdown",
                reply_markup=get_inline_buttons()
            )
        else:
            await update.message.reply_text("❌ Error al registrar la venta")
        
        return
    
    # === DEVOLUCION ===
    if accion == "DEVOLUCION":
        await msg_procesando.edit_text(respuesta_ia)
        
        # Buscar producto similar a venta
        producto = None
        if params.get("id_producto"):
            producto = buscar_por_id_o_producto(params["id_producto"])
        elif es_respuesta and mensaje_original:
            id_ref = extraer_id_de_texto(mensaje_original)
            if id_ref:
                producto = buscar_por_id_o_producto(id_ref)
        
        if isinstance(producto, list):
            producto = producto[0] if producto else None
        
        if not producto:
            await update.message.reply_text("❌ ¿Qué producto quieres marcar como devuelto?")
            return
        
        exito = marcar_devuelto(producto["id"])
        if exito:
            await update.message.reply_text(
                f"✅ *DEVUELTO*\n\n"
                f"📦 {producto['producto']}\n"
                f"ID: `{producto['id']}`",
                parse_mode="Markdown",
                reply_markup=get_inline_buttons()
            )
        else:
            await update.message.reply_text("❌ Error al marcar como devuelto")
        
        return
    
    # === BORRAR ===
    if accion == "BORRAR":
        await msg_procesando.edit_text(respuesta_ia)
        
        # Buscar producto a borrar
        candidatos = []
        
        if params.get("id_producto"):
            candidatos = [buscar_por_id_o_producto(params["id_producto"])]
        elif params.get("nombre_producto"):
            resultado = buscar_por_id_o_producto(params["nombre_producto"])
            if isinstance(resultado, list):
                candidatos = resultado
            else:
                candidatos = [resultado]
        elif es_respuesta and mensaje_original:
            id_ref = extraer_id_de_texto(mensaje_original)
            if id_ref:
                candidatos = [buscar_por_id_o_producto(id_ref)]
        
        # Filtrar None
        candidatos = [c for c in candidatos if c]
        
        if not candidatos:
            # Buscar productos sin ID como fallback
            sin_id = [d for d in datos if d["id"].startswith("TEMP-") or d["id"].startswith("NO_")]
            if sin_id and ("sin id" in mensaje.lower() or "no tiene id" in mensaje.lower()):
                candidatos = sin_id[:3]
        
        if not candidatos:
            await update.message.reply_text("❌ No encontré el producto que quieres borrar.")
            return
        
        if len(candidatos) == 1:
            producto = candidatos[0]
            context.user_data["borrar_confirmar"] = producto
            
            await update.message.reply_text(
                f"🗑️ *CONFIRMAR BORRADO*\n\n"
                f"¿Seguro que quieres borrar?\n\n"
                f"📦 {producto['producto']}\n"
                f"🆔 ID: `{producto['id']}`\n"
                f"💰 ${producto['precio_compra']}\n\n"
                f"⚠️ Esta acción no se puede deshacer",
                parse_mode="Markdown",
                reply_markup=get_confirmar_buttons("borrar", producto['id'])
            )
        else:
            # Mostrar opciones
            texto = "🗑️ Encontré varios productos. ¿Cuál quieres borrar?\n\n"
            for i, c in enumerate(candidatos[:3], 1):
                texto += f"{i}. {c['producto']} (ID: `{c['id']}`)\n"
            
            context.user_data["borrar_opciones"] = candidatos[:3]
            await update.message.reply_text(texto, parse_mode="Markdown")
        
        return
    
    # === CONSULTA ===
    if accion == "CONSULTA":
        await msg_procesando.edit_text(respuesta_ia)
        
        # Generar respuesta detallada
        tipo = params.get("tipo", "general")
        respuesta = generar_respuesta_consulta(tipo, datos)
        
        await update.message.reply_text(
            respuesta or "Aquí tienes la información solicitada 📊",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
        return
    
    # === REVIEW ===
    if accion == "REVIEW":
        await msg_procesando.edit_text(respuesta_ia)
        
        # Buscar producto para asociar review
        producto = None
        if params.get("id_producto"):
            producto = buscar_por_id_o_producto(params["id_producto"])
        elif es_respuesta and mensaje_original:
            id_ref = extraer_id_de_texto(mensaje_original)
            if id_ref:
                producto = buscar_por_id_o_producto(id_ref)
        
        if isinstance(producto, list):
            producto = producto[0] if producto else None
        
        context.user_data["review_producto"] = producto
        context.user_data["esperando_fotos_review"] = True
        context.user_data["fotos_review"] = []
        
        nombre_prod = producto['producto'] if producto else "el producto"
        
        await update.message.reply_text(
            f"⭐ *GENERAR REVIEW*\n\n"
            f"Producto: *{nombre_prod}*\n\n"
            f"Envíame las fotos del producto (pueden ser varias).\n"
            f"Cuando termines, escribe *'listo'* y generaré la review.",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
        return
    
    # === DESCONOCIDO / AYUDA ===
    await msg_procesando.edit_text(
        respuesta_ia + "\n\n¿Necesitas ayuda? Escribe /ayuda para ver ejemplos de lo que puedo hacer.",
        reply_markup=get_main_keyboard()
    )


async def procesar_pendiente_venta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Completa una venta que faltaban datos"""
    pendiente = context.user_data.get("pendiente_venta")
    if not pendiente:
        return False  # No hay venta pendiente, procesar normalmente
    
    mensaje = update.message.text.lower()
    producto = pendiente["producto"]
    
    # Extraer precio si no lo tenemos
    if not pendiente["precio"]:
        # Buscar número en el mensaje
        numeros = re.findall(r'\d+(?:\.\d+)?', mensaje.replace(",", "."))
        if numeros:
            try:
                pendiente["precio"] = float(numeros[0])
            except:
                pass
    
    # Extraer método si no lo tenemos
    if not pendiente["metodo"]:
        for key in METODOS_PAGO.keys():
            if key in mensaje:
                pendiente["metodo"] = key
                break
    
    # Verificar si ya tenemos todo
    if pendiente["precio"] and pendiente["metodo"]:
        # Registrar
        exito, precio_compra = registrar_venta(
            producto["id"], 
            pendiente["precio"], 
            METODOS_PAGO[pendiente["metodo"]]
        )
        
        context.user_data.pop("pendiente_venta", None)
        
        if exito:
            ganancia = pendiente["precio"] - precio_compra
            emoji = "🎉" if ganancia > 0 else "⚠️"
            await update.message.reply_text(
                f"✅ *VENTA COMPLETADA*\n\n"
                f"📦 {producto['producto']}\n"
                f"💵 ${pendiente['precio']:.2f}\n"
                f"💳 {METODOS_PAGO[pendiente['metodo']]}\n"
                f"{emoji} Ganancia: ${ganancia:.2f}",
                parse_mode="Markdown",
                reply_markup=get_inline_buttons()
            )
        else:
            await update.message.reply_text("❌ Error al completar la venta")
        
        return True
    
    # Si aún falta algo, preguntar
    faltantes = []
    if not pendiente["precio"]:
        faltantes.append("¿A qué precio?")
    if not pendiente["metodo"]:
        faltantes.append("¿Qué método de pago?")
    
    await update.message.reply_text(
        "Aún necesito saber:\n" + "\n".join(f"• {f}" for f in faltantes)
    )
    return True

# ============================================
# FLUJOS ESPECÍFICOS (FOTOS, CALLBACKS)
# ============================================

async def procesar_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Procesa fotos según el contexto"""
    if not autorizado(update):
        return
    
    # Si estamos esperando fotos para review
    if context.user_data.get("esperando_fotos_review"):
        photo = update.message.photo[-1]
        file = await photo.get_file()
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        image_path = f"review_{update.message.chat_id}_{timestamp}_{len(context.user_data.get('fotos_review', []))}.jpg"
        
        await file.download_to_drive(image_path)
        
        if "fotos_review" not in context.user_data:
            context.user_data["fotos_review"] = []
        context.user_data["fotos_review"].append(image_path)
        
        count = len(context.user_data["fotos_review"])
        await update.message.reply_text(
            f"📸 Foto {count} recibida. Envía más o escribe *'listo'*",
            parse_mode="Markdown"
        )
        return
    
    # Si no, es una compra nueva
    await procesar_compra_nueva(update, context)


async def procesar_compra_nueva(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Procesa foto de compra nueva"""
    photo = update.message.photo[-1]
    file = await photo.get_file()
    
    image_path = f"compra_{update.message.chat_id}_{update.message.message_id}.jpg"
    await file.download_to_drive(image_path)
    
    msg = await update.message.reply_text("⏳ Analizando compra...")
    
    try:
        datos = extraer_datos_compra_imagen(image_path)
        
        if not datos:
            await msg.edit_text("❌ No pude leer la imagen. Intenta de nuevo.")
            return
        
        exito, pedido_id = agregar_compra(datos)
        
        if exito:
            est = estado_visual(datos.get("fecha_devolucion", ""))
            await msg.edit_text(
                f"✅ *COMPRA REGISTRADA*\n\n"
                f"🆔 ID: `{pedido_id}`\n"
                f"📦 {datos['producto']}\n"
                f"💰 {datos['precio_compra']}\n"
                f"📅 Devolución: {datos.get('fecha_devolucion', 'No disponible')} {est}\n\n"
                f"Responde a este mensaje con *'vendido'* o *'devuelto'* para actualizar",
                parse_mode="Markdown",
                reply_markup=get_inline_buttons()
            )
        else:
            await msg.edit_text("❌ Error al guardar en Sheets")
            
    except Exception as e:
        await msg.edit_text(f"❌ Error: {str(e)[:200]}")
    finally:
        if os.path.exists(image_path):
            os.remove(image_path)


async def procesar_review_completo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Procesa todas las fotos acumuladas y genera review"""
    fotos = context.user_data.get("fotos_review", [])
    producto = context.user_data.get("review_producto")
    
    if not fotos:
        await update.message.reply_text("❌ No recibí fotos. Cancelando.")
        context.user_data.pop("esperando_fotos_review", None)
        return
    
    msg = await update.message.reply_text(f"⏳ Generando review con {len(fotos)} imágenes...")
    
    try:
        review = generar_review_imagenes(fotos)
        
        if not review:
            await msg.edit_text("❌ Error generando review")
            return
        
        # Guardar en Sheets si hay producto asociado
        if producto:
            guardar_review(producto["id"], review)
            guardado = f"\n\n💾 Guardada en: {producto['producto']}"
        else:
            guardado = "\n\n💾 Review no guardada (sin producto asociado)"
        
        # Limpiar fotos
        for f in fotos:
            if os.path.exists(f):
                os.remove(f)
        
        context.user_data.pop("esperando_fotos_review", None)
        context.user_data.pop("fotos_review", None)
        context.user_data.pop("review_producto", None)
        
        # Enviar review
        if len(review) > 4000:
            partes = [review[i:i+4000] for i in range(0, len(review), 4000)]
            await msg.edit_text(f"⭐ *REVIEW GENERADA*{guardado}\n\n(Parte 1/{len(partes)})")
            await update.message.reply_text(partes[0])
            for parte in partes[1:]:
                await update.message.reply_text(parte)
        else:
            await msg.edit_text(
                f"⭐ *REVIEW GENERADA*{guardado}\n\n{review}",
                parse_mode="Markdown",
                reply_markup=get_inline_buttons()
            )
            
    except Exception as e:
        await msg.edit_text(f"❌ Error: {str(e)[:200]}")
        for f in fotos:
            if os.path.exists(f):
                os.remove(f)


async def manejar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja botones inline"""
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data.startswith("confirm_borrar_"):
        pedido_id = data.replace("confirm_borrar_", "")
        exito, producto = borrar_compra(pedido_id)
        
        if exito:
            await query.edit_message_text(
                f"🗑️ *BORRADO*\n\n"
                f"📦 {producto['producto']}\n"
                f"🆔 `{pedido_id}`\n\n"
                f"Eliminado correctamente.",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text("❌ Error al borrar")
        return
    
    if data == "cancelar_accion":
        await query.edit_message_text("❌ Cancelado")
        return
    
    # Botones principales
    if data == "btn_compra":
        await query.message.reply_text(
            "📸 Envía la foto del pedido de Amazon",
            reply_markup=get_main_keyboard()
        )
    elif data == "btn_venta":
        context.user_data["pendiente_venta"] = {"producto": None, "precio": None, "metodo": None}
        await query.message.reply_text(
            "💰 Indica qué vendiste, a qué precio y por qué método.\n"
            "Ejemplo: _Vendí la silla en 150 por paypal_",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
    elif data == "btn_review":
        context.user_data["esperando_fotos_review"] = True
        context.user_data["fotos_review"] = []
        await query.message.reply_text(
            "⭐ Envía las fotos del producto. Escribe 'listo' cuando termines.",
            reply_markup=get_main_keyboard()
        )
    elif data == "btn_modo_ia":
        await modo_ia(update, context)
    elif data == "btn_borrar":
        await query.message.reply_text(
            "🗑️ Indica qué quieres borrar (ID o nombre)",
            reply_markup=get_main_keyboard()
        )


async def listar_pendientes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lista productos pendientes"""
    if not autorizado(update):
        return
    
    datos = obtener_todas_las_compras()
    pendientes = [d for d in datos if d["estado"] == "pendiente"]
    
    if not pendientes:
        await update.message.reply_text("📭 No tienes productos pendientes", reply_markup=get_inline_buttons())
        return
    
    texto = f"📋 *PENDIENTES ({len(pendientes)})*\n\n"
    for p in pendientes[:10]:
        est = estado_visual(p["fecha_devolucion"])
        id_corto = p["id"][-7:] if len(p["id"]) > 7 else p["id"]
        texto += f"🆔 `{id_corto}` | {p['producto'][:30]}...\n💰 ${p['precio_compra']} | {est}\n\n"
    
    if len(pendientes) > 10:
        texto += f"...y {len(pendientes)-10} más\n"
    
    texto += "\n_Responde 'vendido' o 'devuelto' a cualquier mensaje_"
    
    await update.message.reply_text(texto, parse_mode="Markdown", reply_markup=get_main_keyboard())


async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancela cualquier operación pendiente"""
    # Limpiar fotos si hay
    fotos = context.user_data.get("fotos_review", [])
    for f in fotos:
        if os.path.exists(f):
            os.remove(f)
    
    context.user_data.clear()
    await update.message.reply_text("❌ Cancelado", reply_markup=get_inline_buttons())

# ============================================
# MAIN
# ============================================

async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("start", "Iniciar bot"),
        BotCommand("ayuda", "Ver ejemplos de comandos"),
        BotCommand("listar", "Ver productos pendientes"),
        BotCommand("modoia", "Activar modo IA conversacional"),
        BotCommand("cancelar", "Cancelar operación actual"),
    ])


def main():
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO
    )
    
    if not all([TELEGRAM_TOKEN, GOOGLE_SHEETS_ID, GEMINI_API_KEY, TU_CHAT_ID, GOOGLE_CREDENTIALS_JSON]):
        print("❌ Faltan variables de entorno")
        return
    
    print("🤖 Bot IA v5.0 - Sistema Inteligente")
    print(f"✅ Chat ID: {TU_CHAT_ID}")
    
    application = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    
    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("ayuda", ayuda))
    application.add_handler(CommandHandler("listar", listar_pendientes))
    application.add_handler(CommandHandler("modoia", modo_ia))
    application.add_handler(CommandHandler("cancelar", cancelar))
    
    application.add_handler(CallbackQueryHandler(manejar_callback))
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, procesar_foto))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, procesar_mensaje_inteligente))
    
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
