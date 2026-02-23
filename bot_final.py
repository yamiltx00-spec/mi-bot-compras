import os
import json
import base64
import requests
import logging
import re
import random
import asyncio
from datetime import datetime, timedelta
from collections import defaultdict
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
from telegram.error import NetworkError, TimedOut
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

# Sistema de memoria y cache
class SistemaMemoria:
    def __init__(self):
        self.cache_datos = None
        self.ultima_actualizacion = None
        self.historial_chat = defaultdict(list)
        self.contexto_usuario = {}
        self.pendientes = {}
    
    def obtener_datos(self, force=False):
        """Cache de 60 segundos para velocidad"""
        ahora = datetime.now()
        if (force or self.cache_datos is None or 
            self.ultima_actualizacion is None or
            (ahora - self.ultima_actualizacion).seconds > 60):
            self.cache_datos = self._cargar_desde_sheets()
            self.ultima_actualizacion = ahora
        return self.cache_datos
    
    def _cargar_desde_sheets(self):
        try:
            info = json.loads(GOOGLE_CREDENTIALS_JSON)
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )
            service = build("sheets", "v4", credentials=creds)
            
            result = service.spreadsheets().values().get(
                spreadsheetId=GOOGLE_SHEETS_ID, range="A:J"
            ).execute()
            
            values = result.get("values", [])
            datos = []
            
            for i, row in enumerate(values[1:], 1):
                if not row:
                    continue
                
                pedido_id = row[0] if len(row) > 0 and row[0] else f"NO_ID_{i}"
                
                dias_vencimiento = None
                try:
                    if len(row) > 4 and row[4]:
                        fecha_dev = datetime.strptime(row[4], "%d/%m/%Y")
                        dias_vencimiento = (fecha_dev - datetime.now()).days
                except:
                    pass
                
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
                    "dias_vencimiento": dias_vencimiento,
                })
            
            return datos
        except Exception as e:
            logging.error(f"Error cargando datos: {e}")
            return self.cache_datos or []
    
    def agregar_a_historial(self, user_id, rol, contenido):
        self.historial_chat[user_id].append({
            "rol": rol,
            "contenido": contenido,
            "timestamp": datetime.now()
        })
        self.historial_chat[user_id] = self.historial_chat[user_id][-10:]
    
    def obtener_contexto(self, user_id):
        historial = self.historial_chat.get(user_id, [])
        return "\n".join([f"{h['rol']}: {h['contenido']}" for h in historial[-5:]])

memoria = SistemaMemoria()

METODOS_PAGO = {
    "paypal": "💳 PayPal",
    "zelle": "💰 Zelle",
    "efectivo": "💵 Efectivo",
    "amazon": "📦 Amazon",
    "deposito": "🏦 Depósito",
    "otro": "📝 Otro",
}

# ============================================
# GEMINI
# ============================================

def llamar_gemini(prompt, max_tokens=800):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": max_tokens,
        }
    }
    
    try:
        response = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=15
        )
        
        if response.status_code == 200:
            return response.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        return None
    except Exception as e:
        logging.error(f"Error Gemini: {e}")
        return None


def extraer_datos_compra_imagen(image_path):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    
    with open(image_path, "rb") as f:
        img_base64 = base64.b64encode(f.read()).decode()
    
    prompt = """Analiza esta captura de pantalla de compra de Amazon.
Extrae en JSON válido:
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
        response = requests.post(url, headers={"Content-Type": "application/json"}, json=payload, timeout=20)
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
        logging.error(f"Error analizando imagen: {e}")
        return None


def generar_review_imagenes(image_paths):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    
    parts = [{"text": """Genera reseñas realistas para Amazon en español e inglés.

REGLAS:
- NO menciones envío, precio, ni atención al cliente
- Incluye 1-2 errores ortográficos menores naturales
- Sé específico con detalles visibles en las fotos
- 4-5 estrellas aleatorias
- 80-150 palabras cada una

Formato:
[ESPAÑOL]
⭐ X estrellas
Título: ...
Reseña: ...

[ENGLISH]
⭐ X stars
Title: ...
Review: ..."""}]
    
    for path in image_paths:
        with open(path, "rb") as f:
            img_base64 = base64.b64encode(f.read()).decode()
            parts.append({"inline_data": {"mime_type": "image/jpeg", "data": img_base64}})
    
    payload = {"contents": [{"parts": parts}]}
    
    try:
        response = requests.post(url, headers={"Content-Type": "application/json"}, json=payload, timeout=45)
        return response.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        logging.error(f"Error generando review: {e}")
        return None

# ============================================
# LÓGICA DE NEGOCIO
# ============================================

def clasificar_intencion(mensaje):
    m = mensaje.lower()
    
    if any(p in m for p in ["vendí", "vendi", "vendido", "se vendió", "lo vendí", "ya lo vendí"]):
        return "VENTA", extraer_datos_venta(m)
    
    if any(p in m for p in ["devolví", "devuelto", "lo devolví", "return", "regresé"]):
        return "DEVOLUCION", None
    
    if any(p in m for p in ["borra", "borrar", "elimina", "quita", "borralo", "eliminar"]):
        return "BORRAR", extraer_datos_busqueda(m)
    
    if any(p in m for p in ["cuántos tengo", "qué productos", "lista de", "muéstrame", "ver los", "dime los", "cuales tengo", "cuántos son", "cuántos productos"]):
        return "CONSULTA_INVENTARIO", extraer_filtros_consulta(m)
    
    if any(p in m for p in ["cuánto he ganado", "cuánto he invertido", "ganancia", "pérdida", "rentabilidad", "margen", "finanzas", "plata", "dinero", "cuánto dinero"]):
        return "CONSULTA_FINANCIERA", None
    
    if any(p in m for p in ["dónde está", "busca el", "encuentra", "el producto", "el item", "cuál es el", "cuál es mi", "buscar", "encontrar"]):
        return "CONSULTA_PRODUCTO", extraer_datos_busqueda(m)
    
    if any(p in m for p in ["review", "reseña", "opinión", "reseñar"]):
        return "REVIEW", None
    
    if any(p in m for p in ["hola", "ayuda", "help", "qué puedes hacer", "cómo funciona", "qué haces"]):
        return "AYUDA", None
    
    return "CONVERSACION", None


def extraer_datos_venta(mensaje):
    precio = None
    patrones_precio = [
        r'en\s+(\d+(?:\.\d+)?)\s*(?:usd|\$)?',
        r'por\s+(\d+(?:\.\d+)?)',
        r'a\s+(\d+(?:\.\d+)?)\s*(?:usd|\$)?',
        r'(\d{2,}(?:\.\d+)?)\s*(?:usd|\$)?',
    ]
    for patron in patrones_precio:
        match = re.search(patron, mensaje.lower())
        if match:
            try:
                val = float(match.group(1))
                if val > 10:
                    precio = val
                    break
            except:
                pass
    
    metodo = None
    for key in METODOS_PAGO.keys():
        if key in mensaje.lower():
            metodo = key
            break
    
    producto = None
    match = re.search(r'vend[ií]\s+(?:el|la|los|las)?\s+(.+?)(?:\s+(?:en|por|a)\s+\d|$)', mensaje.lower())
    if match:
        producto = match.group(1).strip()
    
    return {"precio": precio, "metodo": metodo, "producto_nombre": producto}


def extraer_datos_busqueda(mensaje):
    palabras_filtrar = ["borra", "borrar", "elimina", "quita", "el", "la", "los", "las", 
                       "que", "tiene", "con", "sin", "id", "identificador", "producto",
                       "busca", "encuentra", "dime", "cuál", "es", "mi", "buscar", "encontrar"]
    
    palabras = mensaje.lower().split()
    candidatos = [p for p in palabras if p not in palabras_filtrar and len(p) > 2]
    
    if candidatos:
        return " ".join(candidatos[:3])
    return None


def extraer_filtros_consulta(mensaje):
    m = mensaje.lower()
    filtros = {}
    
    if any(p in m for p in ["pendiente", "por vender", "no vendido"]):
        filtros["estado"] = "pendiente"
    if any(p in m for p in ["vendido", "ya vendí"]):
        filtros["estado"] = "vendido"
    if any(p in m for p in ["por vencer", "vence pronto", "urgente", "vencer"]):
        filtros["por_vencer"] = 7
    if any(p in m for p in ["vencido", "ya venció"]):
        filtros["vencido"] = True
    if any(p in m for p in ["caro", "costoso", "mayor precio", "más caro"]):
        filtros["orden"] = "precio_desc"
    if any(p in m for p in ["barato", "económico", "menor precio", "más barato"]):
        filtros["orden"] = "precio_asc"
    
    return filtros


def buscar_producto(criterio, datos=None):
    if datos is None:
        datos = memoria.obtener_datos()
    
    if not criterio:
        return None
    
    criterio_lower = criterio.lower().strip()
    
    for d in datos:
        if d["id"].lower() == criterio_lower:
            return d
    
    for d in datos:
        if d["id"].endswith(criterio_lower):
            return d
    
    coincidencias = []
    for d in datos:
        if criterio_lower in d["producto"].lower():
            coincidencias.append(d)
    
    if len(coincidencias) == 1:
        return coincidencias[0]
    elif len(coincidencias) > 1:
        return coincidencias[:5]
    
    palabras = criterio_lower.split()
    if len(palabras) > 1:
        for d in datos:
            coincidencias_palabras = sum(1 for p in palabras if p in d["producto"].lower())
            if coincidencias_palabras >= len(palabras) / 2:
                return d
    
    return None


def calcular_estadisticas(datos):
    total_inv = 0
    total_ventas = 0
    ganancia_total = 0
    
    for d in datos:
        try:
            pc = float(str(d["precio_compra"]).replace("US$", "").replace("$", "").replace(",", "").strip() or 0)
            total_inv += pc
            
            if d["estado"] == "vendido":
                pv = float(str(d["precio_venta"]).replace("US$", "").replace("$", "").replace(",", "").strip() or 0)
                total_ventas += pv
                ganancia_total += (pv - pc)
        except:
            pass
    
    return {
        "total_invertido": total_inv,
        "total_ventas": total_ventas,
        "ganancia_neta": ganancia_total,
        "por_recuperar": total_inv - total_ventas
    }


def obtener_productos_por_vencer(dias=7, datos=None):
    if datos is None:
        datos = memoria.obtener_datos()
    
    resultado = []
    for d in datos:
        if d["estado"] != "pendiente":
            continue
        if d["dias_vencimiento"] is not None:
            if 0 <= d["dias_vencimiento"] <= dias:
                resultado.append(d)
            elif d["dias_vencimiento"] < 0:
                resultado.append(d)
    
    return sorted(resultado, key=lambda x: x["dias_vencimiento"] if x["dias_vencimiento"] is not None else 999)

# ============================================
# OPERACIONES SHEETS
# ============================================

def get_sheets_service():
    info = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return build("sheets", "v4", credentials=creds)


def agregar_compra(datos_compra):
    try:
        service = get_sheets_service()
        
        fecha_dev = datos_compra.get("fecha_devolucion", "")
        if not fecha_dev:
            try:
                fecha_comp = datetime.strptime(datos_compra["fecha_compra"], "%d/%m/%Y")
                fecha_dev = (fecha_comp + timedelta(days=30)).strftime("%d/%m/%Y")
            except:
                fecha_dev = (datetime.now() + timedelta(days=30)).strftime("%d/%m/%Y")
        
        pedido_id = datos_compra.get("id_pedido", "")
        if not pedido_id or pedido_id in ["NO_DISPONIBLE", "NO_ENCONTRADO", ""]:
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
        
        memoria.obtener_datos(force=True)
        return True, pedido_id
        
    except Exception as e:
        logging.error(f"Error agregando compra: {e}")
        return False, None


def registrar_venta(id_pedido, precio_venta, metodo_pago):
    try:
        service = get_sheets_service()
        datos = memoria.obtener_datos()
        
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
                    pc = float(str(d["precio_compra"]).replace("US$", "").replace("$", "").replace(",", "").strip() or 0)
                except:
                    pc = 0
                
                memoria.obtener_datos(force=True)
                return True, pc
        
        return False, 0
        
    except Exception as e:
        logging.error(f"Error registrando venta: {e}")
        return False, 0


def marcar_devuelto(id_pedido):
    try:
        service = get_sheets_service()
        datos = memoria.obtener_datos()
        
        for d in datos:
            if d["id"] == id_pedido:
                fila = d["fila"]
                service.spreadsheets().values().update(
                    spreadsheetId=GOOGLE_SHEETS_ID,
                    range=f"F{fila}:I{fila}",
                    valueInputOption="USER_ENTERED",
                    body={"values": [[datetime.now().strftime("%d/%m/%Y"), "0", "", "devuelto"]]},
                ).execute()
                
                memoria.obtener_datos(force=True)
                return True
        
        return False
        
    except Exception as e:
        logging.error(f"Error marcando devuelto: {e}")
        return False


def borrar_producto(id_pedido):
    try:
        service = get_sheets_service()
        datos = memoria.obtener_datos()
        
        for d in datos:
            if d["id"] == id_pedido:
                fila = d["fila"]
                producto_info = d
                
                service.spreadsheets().values().clear(
                    spreadsheetId=GOOGLE_SHEETS_ID,
                    range=f"A{fila}:J{fila}",
                ).execute()
                
                memoria.obtener_datos(force=True)
                return True, producto_info
        
        return False, None
        
    except Exception as e:
        logging.error(f"Error borrando: {e}")
        return False, None


def guardar_review(id_pedido, review_text):
    try:
        service = get_sheets_service()
        datos = memoria.obtener_datos()
        
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
        logging.error(f"Error guardando review: {e}")
        return False

# ============================================
# UI
# ============================================

def get_main_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📸 Registrar compra"), KeyboardButton("💰 Registrar venta")],
        [KeyboardButton("📋 Ver inventario"), KeyboardButton("📊 Mis finanzas")],
        [KeyboardButton("⭐ Generar review"), KeyboardButton("🗑️ Borrar producto")],
        [KeyboardButton("❓ Ayuda")]
    ], resize_keyboard=True, one_time_keyboard=False)


def get_inline_confirmar(accion, item_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ Sí, {accion}", callback_data=f"{accion}_{item_id}"),
        InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")
    ]])


# ============================================
# HANDLERS
# ============================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != TU_CHAT_ID:
        return
    
    user_id = str(update.effective_user.id)
    memoria.contexto_usuario[user_id] = "normal"
    
    await update.message.reply_text(
        "🤖 *¡Hola Omar! Soy tu Asistente de Inventario*\n\n"
        "Puedes hablarme naturalmente:\n"
        "• *Cuántos productos tengo por vencer?*\n"
        "• *El carrito ya lo vendí en 45 por zelle*\n"
        "• *Borra el que no tiene ID*\n"
        "• *Muéstrame los más caros*\n\n"
        "También puedes usar los botones 👇\n"
        "Te avisaré proactivamente de urgencias.",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )


async def mostrar_ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *EJEMPLOS DE USO*\n\n"
        "*VENTAS:*\n"
        "• _Vendí el carrito en 45 por zelle_\n"
        "• _El producto 1234 ya lo vendí_\n"
        "• _Lo vendí por paypal_ (respondiendo)\n\n"
        "*DEVOLUCIONES:*\n"
        "• _Devolví la silla_\n"
        "• _Lo devolví_ (respondiendo)\n\n"
        "*BORRAR:*\n"
        "• _Borra el que no tiene ID_\n"
        "• _Elimina el 1234_\n"
        "• _Borrar_ (respondiendo)\n\n"
        "*CONSULTAS:*\n"
        "• _Cuánto he ganado?_\n"
        "• _Qué productos tengo?_\n"
        "• _Cuál es el más caro?_\n\n"
        "*REVIEWS:*\n"
        "• _Genera review_ → envía fotos → _listo_",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )


async def procesar_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != TU_CHAT_ID:
        return
    
    user_id = str(update.effective_user.id)
    mensaje = update.message.text.strip()
    
    # Verificar pendientes primero
    pendiente = memoria.pendientes.get(user_id, {})
    
    # MODO REVIEW
    if pendiente.get("modo") == "esperando_fotos_review":
        if mensaje.lower() in ["listo", "ya", "terminé"]:
            return await finalizar_review(update, context, user_id)
        elif mensaje.lower() in ["cancelar", "salir"]:
            return await cancelar_review(update, context, user_id)
        else:
            await update.message.reply_text("📸 Envía fotos o escribe *'listo'* / *'cancelar'*", parse_mode="Markdown")
            return
    
    # MODO VENTA PENDIENTE
    if pendiente.get("modo") == "esperando_datos_venta":
        return await completar_venta_pendiente(update, context, user_id, mensaje)
    
    # MODO ASOCIAR REVIEW
    if pendiente.get("modo") == "esperando_asociar_review":
        return await asociar_review_a_producto(update, context, user_id, mensaje)
    
    # CLASIFICAR INTENCIÓN
    intencion, datos_extra = clasificar_intencion(mensaje)
    memoria.agregar_a_historial(user_id, "Usuario", mensaje)
    
    # PROCESAR SEGÚN INTENCIÓN
    if intencion == "VENTA":
        await procesar_venta(update, context, user_id, mensaje, datos_extra)
    elif intencion == "DEVOLUCION":
        await procesar_devolucion(update, context, user_id, mensaje)
    elif intencion == "BORRAR":
        await procesar_borrar(update, context, user_id, mensaje, datos_extra)
    elif intencion == "CONSULTA_INVENTARIO":
        await procesar_consulta_inventario(update, context, user_id, mensaje, datos_extra)
    elif intencion == "CONSULTA_FINANCIERA":
        await procesar_consulta_financiera(update, context, user_id)
    elif intencion == "CONSULTA_PRODUCTO":
        await procesar_consulta_producto(update, context, user_id, mensaje, datos_extra)
    elif intencion == "REVIEW":
        await iniciar_review(update, context, user_id)
    elif intencion == "AYUDA":
        await mostrar_ayuda(update, context)
    else:
        await conversacion_general(update, context, user_id, mensaje)


async def procesar_venta(update, context, user_id, mensaje, datos_extra):
    datos = memoria.obtener_datos()
    producto = None
    
    # Buscar por nombre en datos_extra
    if datos_extra and datos_extra.get("producto_nombre"):
        producto = buscar_producto(datos_extra["producto_nombre"], datos)
    
    # Buscar en mensaje respondido
    if not producto and update.message.reply_to_message:
        msg_original = update.message.reply_to_message.text
        match = re.search(r'[0-9]{3}-[0-9]{7}-[0-9]{7}|TEMP-\d{8}-\d{4}|NO_ID_\d+', msg_original)
        if match:
            producto = buscar_producto(match.group(0), datos)
    
    # Buscar en historial reciente
    if not producto:
        for msg in reversed(memoria.historial_chat.get(user_id, [])[-5:]):
            match = re.search(r'[0-9]{3}-[0-9]{7}-[0-9]{7}|TEMP-\d{8}-\d{4}|NO_ID_\d+', msg["contenido"])
            if match:
                producto = buscar_producto(match.group(0), datos)
                if producto:
                    break
    
    if not producto:
        await update.message.reply_text("❌ No encontré el producto. Indica el ID o nombre, o responde al mensaje del producto.")
        return
    
    if isinstance(producto, list):
        producto = producto[0]
    
    if producto["estado"] == "vendido":
        await update.message.reply_text(f"⚠️ Ya está vendido: {producto['producto']}")
        return
    
    precio = datos_extra.get("precio") if datos_extra else None
    metodo = datos_extra.get("metodo") if datos_extra else None
    
    if precio and metodo:
        exito, precio_compra = registrar_venta(producto["id"], precio, METODOS_PAGO[metodo])
        if exito:
            ganancia = precio - precio_compra
            emoji = "🎉" if ganancia > 0 else "⚠️"
            await update.message.reply_text(
                f"✅ *Venta registrada*\n\n"
                f"📦 {producto['producto']}\n"
                f"💵 ${precio:.2f}\n"
                f"💳 {METODOS_PAGO[metodo]}\n"
                f"{emoji} Ganancia: ${ganancia:.2f}",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text("❌ Error al registrar")
    else:
        memoria.pendientes[user_id] = {
            "modo": "esperando_datos_venta",
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
            f"💰 *Venta: {producto['producto']}*\n\n" + "\n".join(f"• {f}" for f in faltantes),
            parse_mode="Markdown"
        )


async def completar_venta_pendiente(update, context, user_id, mensaje):
    pendiente = memoria.pendientes.get(user_id, {})
    producto = pendiente.get("producto")
    
    if not producto:
        await update.message.reply_text("❌ Error: no tengo registro de la venta.")
        memoria.pendientes[user_id] = {}
        return
    
    # Extraer precio
    if not pendiente.get("precio"):
        match = re.search(r'(\d+(?:\.\d+)?)', mensaje.replace(",", "."))
        if match:
            try:
                val = float(match.group(1))
                if val > 10:
                    pendiente["precio"] = val
            except:
                pass
    
    # Extraer método
    if not pendiente.get("metodo"):
        for key in METODOS_PAGO.keys():
            if key in mensaje.lower():
                pendiente["metodo"] = key
                break
    
    if pendiente.get("precio") and pendiente.get("metodo"):
        exito, precio_compra = registrar_venta(
            producto["id"],
            pendiente["precio"],
            METODOS_PAGO[pendiente["metodo"]]
        )
        
        memoria.pendientes[user_id] = {}
        
        if exito:
            ganancia = pendiente["precio"] - precio_compra
            emoji = "🎉" if ganancia > 0 else "⚠️"
            await update.message.reply_text(
                f"✅ *Venta completada*\n\n"
                f"📦 {producto['producto']}\n"
                f"💵 ${pendiente['precio']:.2f}\n"
                f"💳 {METODOS_PAGO[pendiente['metodo']]}\n"
                f"{emoji} Ganancia: ${ganancia:.2f}",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text("❌ Error al registrar")
    else:
        faltantes = []
        if not pendiente.get("precio"):
            faltantes.append("¿Precio? (solo el número)")
        if not pendiente.get("metodo"):
            faltantes.append("¿Método? (paypal, zelle, efectivo, amazon, depósito)")
        
        await update.message.reply_text("Aún necesito:\n" + "\n".join(f"• {f}" for f in faltantes))


async def procesar_devolucion(update, context, user_id, mensaje):
    datos = memoria.obtener_datos()
    producto = None
    
    if update.message.reply_to_message:
        msg_original = update.message.reply_to_message.text
        match = re.search(r'[0-9]{3}-[0-9]{7}-[0-9]{7}|TEMP-\d{8}-\d{4}|NO_ID_\d+', msg_original)
        if match:
            producto = buscar_producto(match.group(0), datos)
    
    if not producto:
        for msg in reversed(memoria.historial_chat.get(user_id, [])[-5:]):
            match = re.search(r'[0-9]{3}-[0-9]{7}-[0-9]{7}|TEMP-\d{8}-\d{4}|NO_ID_\d+', msg["contenido"])
            if match:
                producto = buscar_producto(match.group(0), datos)
                if producto:
                    break
    
    if not producto:
        await update.message.reply_text("❌ No encontré qué producto devolver. Responde al mensaje o indica el ID.")
        return
    
    if isinstance(producto, list):
        producto = producto[0]
    
    exito = marcar_devuelto(producto["id"])
    
    if exito:
        await update.message.reply_text(
            f"✅ *Devuelto*\n\n"
            f"📦 {producto['producto']}\n"
            f"🆔 `{producto['id']}`",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("❌ Error al procesar")


async def procesar_borrar(update, context, user_id, mensaje, datos_extra):
    datos = memoria.obtener_datos()
    busqueda = datos_extra if datos_extra else mensaje
    
    # Caso especial: sin ID
    if any(p in mensaje.lower() for p in ["sin id", "no tiene id", "no id", "temporal", "el último", "ultimo"]):
        sin_id = [d for d in datos if d["id"].startswith("TEMP-") or d["id"].startswith("NO_ID")]
        if sin_id:
            producto = sin_id[-1]  # El más reciente
            
            memoria.pendientes[user_id] = {
                "modo": "esperando_confirmacion_borrar",
                "producto": producto
            }
            
            await update.message.reply_text(
                f"🗑️ *¿Borrar este producto?*\n\n"
                f"📦 {producto['producto']}\n"
                f"🆔 `{producto['id']}`\n"
                f"💰 {producto['precio_compra']}\n\n"
                f"⚠️ No se puede deshacer",
                parse_mode="Markdown",
                reply_markup=get_inline_confirmar("borrar", producto["id"])
            )
            return
    
    # Buscar normal
    resultado = buscar_producto(busqueda, datos)
    
    if resultado and not isinstance(resultado, list):
        memoria.pendientes[user_id] = {
            "modo": "esperando_confirmacion_borrar",
            "producto": resultado
        }
        
        await update.message.reply_text(
            f"🗑️ *¿Borrar este producto?*\n\n"
            f"📦 {resultado['producto']}\n"
            f"🆔 `{resultado['id']}`\n"
            f"💰 {resultado['precio_compra']}\n\n"
            f"⚠️ No se puede deshacer",
            parse_mode="Markdown",
            reply_markup=get_inline_confirmar("borrar", resultado["id"])
        )
    elif resultado and isinstance(resultado, list):
        texto = "🗑️ Encontré varios:\n\n"
        for i, p in enumerate(resultado[:3], 1):
            id_corto = p["id"][-8:] if len(p["id"]) > 8 else p["id"]
            texto += f"{i}. `{id_corto}` - {p['producto'][:35]}\n"
        texto += "\nResponde con el número o sé más específico."
        
        await update.message.reply_text(texto, parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ No encontré ese producto. Intenta con el ID o 'el último'.")


async def procesar_consulta_inventario(update, context, user_id, mensaje, filtros):
    datos = memoria.obtener_datos()
    resultado = datos
    
    if filtros:
        if filtros.get("estado"):
            resultado = [d for d in resultado if d["estado"] == filtros["estado"]]
        if filtros.get("por_vencer"):
            dias = filtros["por_vencer"]
            resultado = [d for d in resultado if d["dias_vencimiento"] is not None and 0 <= d["dias_vencimiento"] <= dias]
        if filtros.get("vencido"):
            resultado = [d for d in resultado if d["dias_vencimiento"] is not None and d["dias_vencimiento"] < 0]
        if filtros.get("orden") == "precio_desc":
            resultado = sorted(resultado, key=lambda x: float(str(x["precio_compra"]).replace("US$", "").replace("$", "").replace(",", "") or 0), reverse=True)
        elif filtros.get("orden") == "precio_asc":
            resultado = sorted(resultado, key=lambda x: float(str(x["precio_compra"]).replace("US$", "").replace("$", "").replace(",", "") or 0))
    
    if not resultado:
        await update.message.reply_text("📭 No encontré productos con esos criterios.")
        return
    
    # Generar respuesta
    total = len(resultado)
    texto = f"📋 *INVENTARIO: {total} productos*\n\n"
    
    for p in resultado[:15]:
        id_corto = p["id"][-6:] if len(p["id"]) > 6 else p["id"]
        estado_emoji = "⏳" if p["estado"] == "pendiente" else "✅" if p["estado"] == "vendido" else "🔄"
        
        vencimiento = ""
        if p["dias_vencimiento"] is not None:
            if p["dias_vencimiento"] < 0:
                vencimiento = " 🔴 VENCIDO"
            elif p["dias_vencimiento"] <= 3:
                vencimiento = f" ⚠️ {p['dias_vencimiento']}d"
        
        nombre = p["producto"][:30] + "..." if len(p["producto"]) > 30 else p["producto"]
        texto += f"{estado_emoji} `{id_corto}` {nombre}{vencimiento}\n"
    
    if len(resultado) > 15:
        texto += f"\n_Y {len(resultado)-15} más..._"
    
    await update.message.reply_text(texto, parse_mode="Markdown")


async def procesar_consulta_financiera(update, context, user_id):
    datos = memoria.obtener_datos()
    stats = calcular_estadisticas(datos)
    
    await update.message.reply_text(
        f"📊 *TUS FINANZAS*\n\n"
        f"💰 Invertido: *${stats['total_invertido']:.2f}*\n"
        f"💵 Vendido: *${stats['total_ventas']:.2f}*\n"
        f"📈 Ganancia: *${stats['ganancia_neta']:.2f}*\n"
        f"⏳ Por recuperar: *${stats['por_recuperar']:.2f}*\n\n"
        f"📦 Total productos: {len(datos)}",
        parse_mode="Markdown"
    )


async def procesar_consulta_producto(update, context, user_id, mensaje, busqueda):
    datos = memoria.obtener_datos()
    producto = None
    
    if busqueda:
        producto = buscar_producto(busqueda, datos)
    else:
        palabras = mensaje.split()
        for p in palabras:
            if len(p) > 3:
                producto = buscar_producto(p, datos)
                if producto and not isinstance(producto, list):
                    break
    
    if isinstance(producto, list):
        producto = producto[0] if producto else None
    
    if producto:
        ganancia_texto = ""
        if producto["estado"] == "vendido":
            try:
                pc = float(str(producto["precio_compra"]).replace("US$", "").replace("$", "").replace(",", "") or 0)
                pv = float(str(producto["precio_venta"]).replace("US$", "").replace("$", "").replace(",", "") or 0)
                ganancia = pv - pc
                ganancia_texto = f"\n💵 Vendido: ${pv:.2f}\n📈 Ganancia: ${ganancia:.2f}"
            except:
                pass
        else:
            ganancia_texto = "\n⏳ Pendiente de venta"
        
        vencimiento_texto = ""
        if producto["dias_vencimiento"] is not None:
            if producto["dias_vencimiento"] < 0:
                vencimiento_texto = "\n🔴 *VENCIDO*"
            elif producto["dias_vencimiento"] == 0:
                vencimiento_texto = "\n🔴 *VENCE HOY*"
            elif producto["dias_vencimiento"] <= 3:
                vencimiento_texto = f"\n⚠️ Vence en {producto['dias_vencimiento']} días"
        
        await update.message.reply_text(
            f"📦 *{producto['producto']}*\n\n"
            f"🆔 `{producto['id']}`\n"
            f"💰 Compra: {producto['precio_compra']}{ganancia_texto}{vencimiento_texto}\n"
            f"📊 Estado: {producto['estado'].upper()}",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("❌ No encontré ese producto.")


async def iniciar_review(update, context, user_id):
    memoria.pendientes[user_id] = {
        "modo": "esperando_fotos_review",
        "fotos": []
    }
    
    await update.message.reply_text(
        "⭐ *MODO REVIEW*\n\n"
        "Envía las fotos del producto.\n"
        "Escribe *'listo'* cuando termines\n"
        "o *'cancelar'* para salir.",
        parse_mode="Markdown"
    )


async def finalizar_review(update, context, user_id):
    pendiente = memoria.pendientes.get(user_id, {})
    fotos = pendiente.get("fotos", [])
    
    if not fotos:
        await update.message.reply_text("❌ No hay fotos. Cancelando.")
        memoria.pendientes[user_id] = {}
        return
    
    msg = await update.message.reply_text(f"⏳ Generando review con {len(fotos)} fotos...")
    
    try:
        review = generar_review_imagenes(fotos)
        
        for f in fotos:
            try:
                if os.path.exists(f):
                    os.remove(f)
            except:
                pass
        
        if not review:
            await msg.edit_text("❌ Error generando review")
            memoria.pendientes[user_id] = {}
            return
        
        memoria.pendientes[user_id] = {
            "modo": "esperando_asociar_review",
            "review": review
        }
        
        datos = memoria.obtener_datos()
        pendientes = [d for d in datos if d["estado"] == "pendiente"][-5:]
        
        if pendientes:
            texto = "⭐ *Review lista*\n\n¿A qué producto?\n\n"
            for i, p in enumerate(pendientes, 1):
                id_corto = p["id"][-6:] if len(p["id"]) > 6 else p["id"]
                texto += f"{i}. `{id_corto}` {p['producto'][:30]}\n"
            texto += "\nResponde número, *'ninguno'* o *'otro'*"
            
            await msg.edit_text(texto, parse_mode="Markdown")
        else:
            await msg.edit_text(
                f"⭐ *Review generada*\n\n{review[:3000]}",
                parse_mode="Markdown"
            )
            memoria.pendientes[user_id] = {}
            
    except Exception as e:
        logging.error(f"Error review: {e}")
        await msg.edit_text(f"❌ Error: {str(e)[:200]}")
        for f in fotos:
            try:
                if os.path.exists(f):
                    os.remove(f)
            except:
                pass
        memoria.pendientes[user_id] = {}


async def asociar_review_a_producto(update, context, user_id, mensaje):
    pendiente = memoria.pendientes.get(user_id, {})
    review = pendiente.get("review", "")
    
    if mensaje.lower() in ["ninguno", "no", "cancelar"]:
        await update.message.reply_text("⭐ Review generada (no guardada en Sheets)")
        memoria.pendientes[user_id] = {}
        return
    
    # Buscar por número
    try:
        num = int(mensaje)
        datos = memoria.obtener_datos()
        pendientes = [d for d in datos if d["estado"] == "pendiente"][-5:]
        
        if 1 <= num <= len(pendientes):
            producto = pendientes[num-1]
            guardar_review(producto["id"], review)
            await update.message.reply_text(
                f"✅ Review guardada en:\n📦 {producto['producto']}",
                parse_mode="Markdown"
            )
            memoria.pendientes[user_id] = {}
            return
    except ValueError:
        pass
    
    # Buscar por ID/nombre
    resultado = buscar_producto(mensaje)
    if resultado and not isinstance(resultado, list):
        guardar_review(resultado["id"], review)
        await update.message.reply_text(f"✅ Review guardada en {resultado['producto']}")
        memoria.pendientes[user_id] = {}
    else:
        await update.message.reply_text("❌ No encontrado. Intenta de nuevo o 'ninguno'")


async def cancelar_review(update, context, user_id):
    pendiente = memoria.pendientes.get(user_id, {})
    fotos = pendiente.get("fotos", [])
    
    for f in fotos:
        try:
            if os.path.exists(f):
                os.remove(f)
        except:
            pass
    
    memoria.pendientes[user_id] = {}
    await update.message.reply_text("❌ Cancelado", reply_markup=get_main_keyboard())


async def conversacion_general(update, context, user_id, mensaje):
    datos = memoria.obtener_datos()
    urgentes = obtener_productos_por_vencer(3, datos)
    
    # Respuesta simple sin IA para velocidad
    respuestas_comunes = {
        "hola": "¡Hola Omar! ¿En qué puedo ayudarte con tu inventario?",
        "gracias": "¡De nada! Estoy aquí para lo que necesites.",
        "ok": "👍",
        "bien": "¡Perfecto! ¿Necesitas revisar algo del inventario?",
        "adios": "¡Hasta luego! Te avisaré si hay urgencias.",
    }
    
    for clave, respuesta in respuestas_comunes.items():
        if clave in mensaje.lower():
            await update.message.reply_text(respuesta)
            return
    
    # Si hay urgentes, mencionarlos
    if urgentes:
        texto = "🤔 No entendí bien. ¿Quieres que te muestre los productos urgentes?\n\n"
        texto += f"Tienes {len(urgentes)} productos por vencer pronto.\n\n"
        texto += "Prueba con:\n• *Cuántos productos tengo?*\n• *Qué productos por vencer?*\n• *Cuánto he ganado?*"
    else:
        texto = "🤔 No entendí bien. Prueba con:\n• *Cuántos productos tengo?*\n• *Vendí X en Y por Z*\n• *Borra el que no tiene ID*\n\nO usa los botones de abajo 👇"
    
    await update.message.reply_text(texto, parse_mode="Markdown")


async def procesar_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != TU_CHAT_ID:
        return
    
    user_id = str(update.effective_user.id)
    pendiente = memoria.pendientes.get(user_id, {})
    
    # MODO REVIEW
    if pendiente.get("modo") == "esperando_fotos_review":
        photo = update.message.photo[-1]
        file = await photo.get_file()
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        image_path = f"review_{user_id}_{timestamp}_{len(pendiente.get('fotos', []))}.jpg"
        
        await file.download_to_drive(image_path)
        
        if "fotos" not in pendiente:
            pendiente["fotos"] = []
        pendiente["fotos"].append(image_path)
        
        count = len(pendiente["fotos"])
        await update.message.reply_text(f"📸 Foto {count} recibida. Envía más o escribe 'listo'")
        return
    
    # COMPRA NUEVA
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
            dias_venc = None
            try:
                if datos.get("fecha_devolucion"):
                    fecha_dev = datetime.strptime(datos["fecha_devolucion"], "%d/%m/%Y")
                    dias_venc = (fecha_dev - datetime.now()).days
            except:
                pass
            
            vencimiento = ""
            if dias_venc is not None:
                if dias_venc < 0:
                    vencimiento = " 🔴 VENCIDO"
                elif dias_venc == 0:
                    vencimiento = " 🔴 HOY"
                elif dias_venc <= 3:
                    vencimiento = f" ⚠️ {dias_venc}d"
            
            await msg.edit_text(
                f"✅ *Compra registrada*\n\n"
                f"📦 {datos['producto']}\n"
                f"🆔 `{pedido_id}`\n"
                f"💰 {datos['precio_compra']}{vencimiento}\n\n"
                f"_Responde 'vendido' o 'devuelto'_",
                parse_mode="Markdown"
            )
        else:
            await msg.edit_text("❌ Error al guardar")
            
    except Exception as e:
        await msg.edit_text(f"❌ Error: {str(e)[:200]}")
    finally:
        try:
            if os.path.exists(image_path):
                os.remove(image_path)
        except:
            pass


async def manejar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = str(query.from_user.id)
    
    if data.startswith("borrar_"):
        pedido_id = data.replace("borrar_", "")
        exito, producto = borrar_producto(pedido_id)
        
        if exito:
            await query.edit_message_text(
                f"🗑️ *Borrado*\n\n"
                f"📦 {producto['producto']}\n"
                f"🆔 `{pedido_id}`",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text("❌ Error al borrar")
        
        memoria.pendientes[user_id] = {}
        return
    
    if data == "cancelar":
        await query.edit_message_text("❌ Cancelado")
        memoria.pendientes[user_id] = {}
        return
    
    # Botones menú
    if data == "btn_compra":
        await query.message.reply_text("📸 Envía foto del pedido")
    elif data == "btn_venta":
        await query.message.reply_text("💰 Ejemplo: *Vendí el carrito en 45 por zelle*", parse_mode="Markdown")
    elif data == "btn_review":
        await iniciar_review(update, context, user_id)
    elif data == "btn_inventario":
        datos = memoria.obtener_datos()
        pendientes = [d for d in datos if d["estado"] == "pendiente"]
        por_vencer = obtener_productos_por_vencer(7, datos)
        
        texto = f"📋 Inventario: {len(datos)} total, {len(pendientes)} pendientes, {len(por_vencer)} por vencer"
        await query.message.reply_text(texto)
    elif data == "btn_finanzas":
        await procesar_consulta_financiera(update, context, user_id)


async def alerta_proactiva(context: ContextTypes.DEFAULT_TYPE):
    try:
        datos = memoria.obtener_datos(force=True)
        
        urgentes = obtener_productos_por_vencer(3, datos)
        vencidos = [u for u in urgentes if u["dias_vencimiento"] < 0]
        por_vencer = [u for u in urgentes if u["dias_vencimiento"] >= 0]
        
        if not urgentes:
            return
        
        mensaje = "🚨 *ALERTA DE INVENTARIO*\n\n"
        
        if vencidos:
            mensaje += f"🔴 *{len(vencidos)} VENCIDOS:*\n"
            for v in vencidos[:3]:
                mensaje += f"• {v['producto'][:40]}\n"
            mensaje += "\n"
        
        if por_vencer:
            mensaje += f"⚠️ *{len(por_vencer)} por vencer:*\n"
            for p in por_vencer[:5]:
                mensaje += f"• {p['producto'][:35]} ({p['dias_vencimiento']}d)\n"
        
        mensaje += "\n_Responde para ver opciones_"
        
        await context.bot.send_message(
            chat_id=TU_CHAT_ID,
            text=mensaje,
            parse_mode="Markdown"
        )
        
    except Exception as e:
        logging.error(f"Error alerta: {e}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logging.error(f"Error: {context.error}")
    
    if isinstance(context.error, (NetworkError, TimedOut)):
        logging.info("Error de red de Telegram, reintentando...")
        return
    
    if update and isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ Tuve un problema. ¿Puedes repetir?")
        except:
            pass


async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("start", "Iniciar asistente"),
        BotCommand("ayuda", "Ver ejemplos"),
    ])
    
    job_queue = application.job_queue
    job_queue.run_daily(alerta_proactiva, time=datetime.strptime("09:00", "%H:%M").time())
    job_queue.run_daily(alerta_proactiva, time=datetime.strptime("20:00", "%H:%M").time())


def main():
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO
    )
    
    if not all([TELEGRAM_TOKEN, GOOGLE_SHEETS_ID, GEMINI_API_KEY, TU_CHAT_ID, GOOGLE_CREDENTIALS_JSON]):
        print("❌ Faltan variables de entorno")
        return
    
    print("🤖 Omar AI v7.0 - Estable")
    print(f"✅ Chat ID: {TU_CHAT_ID}")
    
    application = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("ayuda", mostrar_ayuda))
    
    application.add_handler(CallbackQueryHandler(manejar_callback))
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, procesar_foto))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, procesar_mensaje))
    
    application.add_error_handler(error_handler)
    
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
