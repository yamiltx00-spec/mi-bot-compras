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
        self.historial_chat = defaultdict(list)  # user_id -> lista de mensajes
        self.contexto_usuario = {}  # user_id -> estado actual
        self.pendientes = {}  # user_id -> acciones pendientes
    
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
                
                # Calcular días para vencimiento
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
        """Agrega mensaje al historial (máx 10)"""
        self.historial_chat[user_id].append({
            "rol": rol,
            "contenido": contenido,
            "timestamp": datetime.now()
        })
        # Mantener solo últimos 10
        self.historial_chat[user_id] = self.historial_chat[user_id][-10:]
    
    def obtener_contexto(self, user_id):
        """Obtiene los últimos mensajes formateados"""
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
# GEMINI - SOLO PARA RESPUESTAS NATURALES
# ============================================

def llamar_gemini(prompt, max_tokens=800):
    """Llamada simple y rápida a Gemini"""
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
            timeout=15  # Timeout corto para rapidez
        )
        
        if response.status_code == 200:
            return response.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        return None
    except Exception as e:
        logging.error(f"Error Gemini: {e}")
        return None


def generar_respuesta_conversacional(intencion, datos, contexto, mensaje_usuario):
    """Genera respuesta natural usando Gemini"""
    
    prompt = f"""Eres OmarAI, un asistente experto en gestión de inventario de Amazon. 
Hablas de forma amigable, profesional y directa. Usas emojis ocasionalmente.

CONTEXTO DE LA CONVERSACIÓN:
{contexto}

INTENCIÓN DETECTADA: {intencion}

DATOS DEL INVENTARIO:
- Total productos: {len(datos)}
- Pendientes: {len([d for d in datos if d['estado'] == 'pendiente'])}
- Vendidos: {len([d for d in datos if d['estado'] == 'vendido'])}
- Por vencer (7 días): {len([d for d in datos if d['dias_vencimiento'] is not None and 0 <= d['dias_vencimiento'] <= 7])}
- Vencidos: {len([d for d in datos if d['dias_vencimiento'] is not None and d['dias_vencimiento'] < 0])}

MENSAJE DEL USUARIO: "{mensaje_usuario}"

INSTRUCCIONES:
1. Responde de forma natural y conversacional
2. Si es una consulta específica, da el dato exacto
3. Si requiere acción, confirma lo que harás
4. Si hay urgencias (productos por vencer), menciónalas
5. Sé breve pero completo (máx 150 palabras)

Responde directamente:"""

    return llamar_gemini(prompt)


def analizar_imagen_compra(image_path):
    """Extrae datos de imagen de compra"""
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

Responde SOLO el JSON, sin markdown ni explicaciones."""
    
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
        
        # Limpiar markdown si existe
        if "```" in texto:
            texto = texto.split("```")[1].replace("json", "").strip()
        
        return json.loads(texto)
    except Exception as e:
        logging.error(f"Error analizando imagen: {e}")
        return None


def generar_review_multi_imagen(image_paths):
    """Genera review de múltiples imágenes"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    
    parts = [{"text": """Genera reseñas realistas para Amazon en español e inglés.

REGLAS ESTRICTAS:
- NO menciones envío, precio, ni atención al cliente
- Incluye 1-2 errores ortográficos menores naturales
- Sé específico con detalles visibles en las fotos
- 4-5 estrellas aleatorias
- Si 5 estrellas, menciona un defecto menor realista
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
# LÓGICA DE NEGOCIO - RÁPIDA Y LOCAL
# ============================================

def clasificar_intencion(mensaje):
    """Clasifica la intención del mensaje (rápido, sin IA)"""
    m = mensaje.lower()
    
    # VENTA
    if any(p in m for p in ["vendí", "vendi", "vendido", "se vendió", "lo vendí", "ya lo vendí"]):
        return "VENTA", extraer_datos_venta(m)
    
    # DEVOLUCIÓN
    if any(p in m for p in ["devolví", "devuelto", "lo devolví", "return", "regresé"]):
        return "DEVOLUCION", None
    
    # BORRAR
    if any(p in m for p in ["borra", "borrar", "elimina", "quita", "borralo", "eliminar"]):
        return "BORRAR", extraer_datos_busqueda(m)
    
    # CONSULTA INVENTARIO
    if any(p in m for p in ["cuántos tengo", "qué productos", "lista de", "muéstrame", "ver los", "dime los", "cuales tengo"]):
        return "CONSULTA_INVENTARIO", extraer_filtros_consulta(m)
    
    # CONSULTA FINANCIERA
    if any(p in m for p in ["cuánto he ganado", "cuánto he invertido", "ganancia", "pérdida", "rentabilidad", "margen", "finanzas", "plata", "dinero"]):
        return "CONSULTA_FINANCIERA", None
    
    # CONSULTA ESPECÍFICA PRODUCTO
    if any(p in m for p in ["dónde está", "busca el", "encuentra", "el producto", "el item", "cuál es el", "cuál es mi"]):
        return "CONSULTA_PRODUCTO", extraer_datos_busqueda(m)
    
    # REVIEW
    if any(p in m for p in ["review", "reseña", "opinión", "reseñar"]):
        return "REVIEW", None
    
    # AYUDA/SALUDO
    if any(p in m for p in ["hola", "ayuda", "help", "qué puedes hacer", "cómo funciona"]):
        return "AYUDA", None
    
    # CONVERSACIÓN GENERAL (default)
    return "CONVERSACION", None


def extraer_datos_venta(mensaje):
    """Extrae precio y método de mensaje de venta"""
    # Precio
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
                precio = float(match.group(1))
                if precio > 10:  # Evitar números pequeños que no sean precios
                    break
            except:
                pass
    
    # Método de pago
    metodo = None
    for key in METODOS_PAGO.keys():
        if key in mensaje.lower():
            metodo = key
            break
    
    # Producto (palabras entre "vendí" y "en/por/a")
    producto = None
    match = re.search(r'vend[ií]\s+(?:el|la|los|las)?\s+(.+?)(?:\s+(?:en|por|a)\s+\d|$)', mensaje.lower())
    if match:
        producto = match.group(1).strip()
    
    return {"precio": precio, "metodo": metodo, "producto_nombre": producto}


def extraer_datos_busqueda(mensaje):
    """Extrae término de búsqueda de producto"""
    # Eliminar palabras comunes
    palabras_filtrar = ["borra", "borrar", "elimina", "quita", "el", "la", "los", "las", 
                       "que", "tiene", "con", "sin", "id", "identificador", "producto",
                       "busca", "encuentra", "dime", "cuál", "es", "mi"]
    
    palabras = mensaje.lower().split()
    candidatos = [p for p in palabras if p not in palabras_filtrar and len(p) > 2]
    
    if candidatos:
        return " ".join(candidatos[:3])  # Máx 3 palabras
    return None


def extraer_filtros_consulta(mensaje):
    """Extrae filtros de consulta"""
    m = mensaje.lower()
    filtros = {}
    
    if any(p in m for p in ["pendiente", "por vender", "no vendido"]):
        filtros["estado"] = "pendiente"
    if any(p in m for p in ["vendido", "ya vendí"]):
        filtros["estado"] = "vendido"
    if any(p in m for p in ["por vencer", "vence pronto", "urgente"]):
        filtros["por_vencer"] = 7
    if any(p in m for p in ["vencido", "ya venció"]):
        filtros["vencido"] = True
    if any(p in m for p in ["caro", "costoso", "mayor precio"]):
        filtros["orden"] = "precio_desc"
    if any(p in m for p in ["barato", "económico", "menor precio"]):
        filtros["orden"] = "precio_asc"
    
    return filtros


def buscar_producto(criterio, datos=None):
    """Búsqueda flexible de productos"""
    if datos is None:
        datos = memoria.obtener_datos()
    
    if not criterio:
        return None
    
    criterio_lower = criterio.lower().strip()
    
    # 1. Búsqueda exacta por ID
    for d in datos:
        if d["id"].lower() == criterio_lower:
            return d
    
    # 2. Búsqueda por sufijo de ID (últimos dígitos)
    for d in datos:
        if d["id"].endswith(criterio_lower):
            return d
    
    # 3. Búsqueda por nombre (contiene)
    coincidencias = []
    for d in datos:
        if criterio_lower in d["producto"].lower():
            coincidencias.append(d)
    
    if len(coincidencias) == 1:
        return coincidencias[0]
    elif len(coincidencias) > 1:
        return coincidencias[:5]  # Top 5
    
    # 4. Búsqueda por palabras individuales
    palabras = criterio_lower.split()
    if len(palabras) > 1:
        for d in datos:
            coincidencias_palabras = sum(1 for p in palabras if p in d["producto"].lower())
            if coincidencias_palabras >= len(palabras) / 2:  # Al menos la mitad de las palabras
                return d
    
    return None


def calcular_estadisticas(datos):
    """Calcula estadísticas financieras"""
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
    """Obtiene productos que vencen en X días"""
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
                resultado.append(d)  # Ya vencidos al inicio
    
    return sorted(resultado, key=lambda x: x["dias_vencimiento"] if x["dias_vencimiento"] is not None else 999)

# ============================================
# OPERACIONES CON SHEETS
# ============================================

def agregar_compra(datos_compra):
    try:
        info = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        service = build("sheets", "v4", credentials=creds)
        
        # Fecha devolución
        fecha_dev = datos_compra.get("fecha_devolucion", "")
        if not fecha_dev:
            try:
                fecha_comp = datetime.strptime(datos_compra["fecha_compra"], "%d/%m/%Y")
                fecha_dev = (fecha_comp + timedelta(days=30)).strftime("%d/%m/%Y")
            except:
                fecha_dev = (datetime.now() + timedelta(days=30)).strftime("%d/%m/%Y")
        
        # ID
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
        info = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        service = build("sheets", "v4", credentials=creds)
        
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
        info = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        service = build("sheets", "v4", credentials=creds)
        
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
        info = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        service = build("sheets", "v4", credentials=creds)
        
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
        info = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        service = build("sheets", "v4", credentials=creds)
        
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
# TECLADOS Y UI
# ============================================

def get_main_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📸 Registrar compra"), KeyboardButton("💰 Registrar venta")],
        [KeyboardButton("📋 Ver inventario"), KeyboardButton("📊 Mis finanzas")],
        [KeyboardButton("⭐ Generar review"), KeyboardButton("🗑️ Borrar producto")],
        [KeyboardButton("❓ Ayuda"), KeyboardButton("🔔 Alertas")]
    ], resize_keyboard=True, one_time_keyboard=False)


def get_inline_confirmar(accion, item_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ Sí, {accion}", callback_data=f"{accion}_{item_id}"),
        InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")
    ]])


def get_inline_metodos_pago():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("PayPal", callback_data="mp_paypal"),
         InlineKeyboardButton("Zelle", callback_data="mp_zelle")],
        [InlineKeyboardButton("Efectivo", callback_data="mp_efectivo"),
         InlineKeyboardButton("Amazon", callback_data="mp_amazon")]
    ])

# ============================================
# MANEJADORES PRINCIPALES
# ============================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != TU_CHAT_ID:
        return
    
    user_id = str(update.effective_user.id)
    memoria.contexto_usuario[user_id] = "normal"
    
    await update.message.reply_text(
        "🤖 *¡Hola Omar! Soy tu Asistente de Inventario*\n\n"
        "Puedes hablarme naturalmente. Algunos ejemplos:\n\n"
        "💬 *\"Cuántos productos tengo por vencer?\"*\n"
        "💬 *\"El carrito de muebles ya lo vendí en 45 por zelle\"*\n"
        "💬 *\"Borra el producto que no tiene ID\"*\n"
        "💬 *\"Muéstrame los más caros que tengo\"*\n\n"
        "También puedes usar los botones de abajo 👇\n"
        "Te avisaré proactivamente cuando haya urgencias.",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )


async def procesar_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Procesador principal de mensajes"""
    user_id = str(update.effective_user.id)
    if user_id != TU_CHAT_ID:
        return
    
    mensaje = update.message.text.strip()
    
    # Verificar si hay acción pendiente
    pendiente = memoria.pendientes.get(user_id, {})
    
    # MODO REVIEW ACTIVO
    if pendiente.get("modo") == "esperando_fotos_review":
        if mensaje.lower() in ["listo", "ya", "terminé", "ok"]:
            return await finalizar_review(update, context, user_id)
        elif mensaje.lower() in ["cancelar", "salir", "no"]:
            return await cancelar_review(update, context, user_id)
        else:
            await update.message.reply_text(
                "📸 Estoy esperando las fotos del producto.\n"
                "Envíalas y escribe *'listo'* cuando termines,\n"
                "o *'cancelar'* para salir.",
                parse_mode="Markdown"
            )
            return
    
    # MODO ESPERANDO DATOS VENTA
    if pendiente.get("modo") == "esperando_datos_venta":
        return await completar_venta_pendiente(update, context, user_id, mensaje)
    
    # MODO ESPERANDO CONFIRMACIÓN BORRADO
    if pendiente.get("modo") == "esperando_confirmacion_borrar":
        # Se maneja por callback, aquí ignoramos o reiteramos
        await update.message.reply_text("Por favor usa los botones de confirmación ↑")
        return
    
    # CLASIFICAR INTENCIÓN Y PROCESAR
    intencion, datos_extra = clasificar_intencion(mensaje)
    
    # Agregar a historial
    memoria.agregar_a_historial(user_id, "Usuario", mensaje)
    
    # Procesar según intención
    if intencion == "VENTA":
        await procesar_intencion_venta(update, context, user_id, mensaje, datos_extra)
    elif intencion == "DEVOLUCION":
        await procesar_intencion_devolucion(update, context, user_id, mensaje)
    elif intencion == "BORRAR":
        await procesar_intencion_borrar(update, context, user_id, mensaje, datos_extra)
    elif intencion == "CONSULTA_INVENTARIO":
        await procesar_consulta_inventario(update, context, user_id, mensaje, datos_extra)
    elif intencion == "CONSULTA_FINANCIERA":
        await procesar_consulta_financiera(update, context, user_id, mensaje)
    elif intencion == "CONSULTA_PRODUCTO":
        await procesar_consulta_producto(update, context, user_id, mensaje, datos_extra)
    elif intencion == "REVIEW":
        await iniciar_modo_review(update, context, user_id)
    elif intencion == "AYUDA":
        await mostrar_ayuda(update, context)
    else:
        # Conversación general - usar IA
        await procesar_conversacion_general(update, context, user_id, mensaje)


async def procesar_intencion_venta(update, context, user_id, mensaje, datos_extra):
    """Procesa venta con datos extraídos o pide lo que falta"""
    datos = memoria.obtener_datos()
    
    # Buscar producto
    producto = None
    if datos_extra and datos_extra.get("producto_nombre"):
        producto = buscar_producto(datos_extra["producto_nombre"], datos)
    
    # Si no encontró por nombre, buscar en mensaje original si es respuesta
    if not producto and update.message.reply_to_message:
        msg_original = update.message.reply_to_message.text
        id_encontrado = None
        match = re.search(r'[0-9]{3}-[0-9]{7}-[0-9]{7}|TEMP-\d{8}-\d{4}|NO_ID_\d+', msg_original)
        if match:
            id_encontrado = match.group(0)
            producto = buscar_producto(id_encontrado, datos)
    
    # Si aún no, buscar el último producto mencionado en conversación
    if not producto:
        # Buscar IDs en historial reciente
        for msg in reversed(memoria.historial_chat.get(user_id, [])[-5:]):
            match = re.search(r'[0-9]{3}-[0-9]{7}-[0-9]{7}|TEMP-\d{8}-\d{4}|NO_ID_\d+', msg["contenido"])
            if match:
                producto = buscar_producto(match.group(0), datos)
                if producto:
                    break
    
    if not producto:
        await update.message.reply_text(
            "❌ No encontré el producto que vendiste.\n"
            "¿Puedes indicarme el ID o nombre del producto?\n"
            "O responde al mensaje donde aparece el producto."
        )
        return
    
    if producto["estado"] == "vendido":
        await update.message.reply_text(
            f"⚠️ Este producto ya está marcado como vendido:\n"
            f"📦 {producto['producto']}\n"
            f"💰 Vendido en: ${producto['precio_venta']}"
        )
        return
    
    precio = datos_extra.get("precio") if datos_extra else None
    metodo = datos_extra.get("metodo") if datos_extra else None
    
    if precio and metodo:
        # Completar venta inmediatamente
        exito, precio_compra = registrar_venta(producto["id"], precio, METODOS_PAGO[metodo])
        
        if exito:
            ganancia = precio - precio_compra
            emoji = "🎉" if ganancia > 0 else "⚠️"
            respuesta = (
                f"✅ *¡Venta registrada!*\n\n"
                f"📦 {producto['producto']}\n"
                f"💵 ${precio:.2f}\n"
                f"💳 {METODOS_PAGO[metodo]}\n"
                f"{emoji} Ganancia: ${ganancia:.2f}\n\n"
                f"¡Buena venta Omar! 🚀"
            )
            await update.message.reply_text(respuesta, parse_mode="Markdown")
            memoria.agregar_a_historial(user_id, "Asistente", f"Venta registrada: {producto['producto']} por ${precio}")
        else:
            await update.message.reply_text("❌ Error al registrar la venta")
    else:
        # Guardar pendiente y preguntar lo que falta
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
            faltantes.append("¿Por qué método te pagaron? (PayPal, Zelle, Efectivo, etc.)")
        
        await update.message.reply_text(
            f"💰 Entendido, vendiste: *{producto['producto']}*\n\n"
            f"Necesito que me indiques:\n" +
            "\n".join(f"• {f}" for f in faltantes),
            parse_mode="Markdown"
        )


async def completar_venta_pendiente(update, context, user_id, mensaje):
    """Completa una venta que estaba esperando datos"""
    pendiente = memoria.pendientes.get(user_id, {})
    producto = pendiente.get("producto")
    
    if not producto:
        await update.message.reply_text("❌ Error: no tengo registro de qué producto vendías.")
        memoria.pendientes[user_id] = {}
        return
    
    # Extraer datos del mensaje
    if not pendiente.get("precio"):
        # Buscar número en mensaje
        match = re.search(r'(\d+(?:\.\d+)?)', mensaje.replace(",", "."))
        if match:
            try:
                val = float(match.group(1))
                if val > 10:
                    pendiente["precio"] = val
            except:
                pass
    
    if not pendiente.get("metodo"):
        for key in METODOS_PAGO.keys():
            if key in mensaje.lower():
                pendiente["metodo"] = key
                break
    
    # Verificar si ya tenemos todo
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
                f"✅ *¡Venta completada!*\n\n"
                f"📦 {producto['producto']}\n"
                f"💵 ${pendiente['precio']:.2f}\n"
                f"💳 {METODOS_PAGO[pendiente['metodo']]}\n"
                f"{emoji} Ganancia: ${ganancia:.2f}",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text("❌ Error al registrar la venta")
    else:
        # Seguir preguntando
        faltantes = []
        if not pendiente.get("precio"):
            faltantes.append("¿A qué precio? (solo el número)")
        if not pendiente.get("metodo"):
            faltantes.append("¿Por qué método? (PayPal, Zelle, Efectivo, Amazon, Depósito)")
        
        await update.message.reply_text(
            "Aún necesito saber:\n" + "\n".join(f"• {f}" for f in faltantes)
        )


async def procesar_intencion_devolucion(update, context, user_id, mensaje):
    """Procesa devolución"""
    datos = memoria.obtener_datos()
    producto = None
    
    # Buscar en mensaje respondido
    if update.message.reply_to_message:
        msg_original = update.message.reply_to_message.text
        match = re.search(r'[0-9]{3}-[0-9]{7}-[0-9]{7}|TEMP-\d{8}-\d{4}|NO_ID_\d+', msg_original)
        if match:
            producto = buscar_producto(match.group(0), datos)
    
    # Si no, buscar en historial
    if not producto:
        for msg in reversed(memoria.historial_chat.get(user_id, [])[-5:]):
            match = re.search(r'[0-9]{3}-[0-9]{7}-[0-9]{7}|TEMP-\d{8}-\d{4}|NO_ID_\d+', msg["contenido"])
            if match:
                producto = buscar_producto(match.group(0), datos)
                if producto:
                    break
    
    if not producto:
        await update.message.reply_text(
            "❌ No encontré qué producto devolviste.\n"
            "Responde al mensaje del producto o indícame el ID/nombre."
        )
        return
    
    exito = marcar_devuelto(producto["id"])
    
    if exito:
        await update.message.reply_text(
            f"✅ *Producto marcado como devuelto*\n\n"
            f"📦 {producto['producto']}\n"
            f"🆔 `{producto['id']}`\n"
            f"📅 Fecha: {datetime.now().strftime('%d/%m/%Y')}",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("❌ Error al procesar la devolución")


async def procesar_intencion_borrar(update, context, user_id, mensaje, datos_extra):
    """Procesa borrado de producto"""
    datos = memoria.obtener_datos()
    busqueda = datos_extra if datos_extra else mensaje
    
    # Caso especial: "el que no tiene ID" o "sin ID"
    if any(p in mensaje.lower() for p in ["sin id", "no tiene id", "no id", "temporal"]):
        sin_id = [d for d in datos if d["id"].startswith("TEMP-") or d["id"].startswith("NO_ID")]
        if sin_id:
            # Ordenar por fecha (más reciente primero) y tomar el último
            producto = sin_id[-1]
            
            memoria.pendientes[user_id] = {
                "modo": "esperando_confirmacion_borrar",
                "producto": producto
            }
            
            await update.message.reply_text(
                f"🗑️ *¿Borrar este producto sin ID?*\n\n"
                f"📦 {producto['producto']}\n"
                f"🆔 `{producto['id']}`\n"
                f"💰 {producto['precio_compra']}\n"
                f"📅 {producto['fecha_compra']}\n\n"
                f"⚠️ Esta acción no se puede deshacer",
                parse_mode="Markdown",
                reply_markup=get_inline_confirmar("borrar", producto["id"])
            )
            return
    
    # Buscar por criterio
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
            f"💰 {resultado['precio_compra']}\n"
            f"Estado: {resultado['estado']}\n\n"
            f"⚠️ Esta acción no se puede deshacer",
            parse_mode="Markdown",
            reply_markup=get_inline_confirmar("borrar", resultado["id"])
        )
        
    elif resultado and isinstance(resultado, list):
        texto = "🗑️ Encontré varios productos. ¿Cuál quieres borrar?\n\n"
        for i, p in enumerate(resultado[:3], 1):
            id_corto = p["id"][-8:] if len(p["id"]) > 8 else p["id"]
            texto += f"{i}. `{id_corto}` - {p['producto'][:35]}\n"
        texto += "\nResponde con el número o sé más específico."
        
        memoria.pendientes[user_id] = {
            "modo": "esperando_seleccion_borrar",
            "opciones": resultado[:3]
        }
        
        await update.message.reply_text(texto, parse_mode="Markdown")
        
    else:
        await update.message.reply_text(
            "❌ No encontré ese producto.\n"
            "Intenta con:\n"
            "• El ID completo o los últimos dígitos\n"
            "• El nombre del producto\n"
            "• 'El que no tiene ID' para borrar temporales"
        )


async def procesar_consulta_inventario(update, context, user_id, mensaje, filtros):
    """Procesa consultas sobre inventario"""
    datos = memoria.obtener_datos()
    
    # Aplicar filtros
    resultado = datos
    
    if filtros:
        if filtros.get("estado"):
            resultado = [d for d in resultado if d["estado"] == filtros["estado"]]
        
        if filtros.get("por_vencer"):
            dias = filtros["por_vencer"]
            resultado = [d for d in resultado 
                        if d["dias_vencimiento"] is not None 
                        and 0 <= d["dias_vencimiento"] <= dias]
        
        if filtros.get("vencido"):
            resultado = [d for d in resultado 
                        if d["dias_vencimiento"] is not None 
                        and d["dias_vencimiento"] < 0]
        
        if filtros.get("orden") == "precio_desc":
            resultado = sorted(resultado, 
                             key=lambda x: float(str(x["precio_compra"]).replace("US$", "").replace("$", "").replace(",", "") or 0), 
                             reverse=True)
        elif filtros.get("orden") == "precio_asc":
            resultado = sorted(resultado, 
                             key=lambda x: float(str(x["precio_compra"]).replace("US$", "").replace("$", "").replace(",", "") or 0))
    
    # Generar respuesta con contexto
    contexto = memoria.obtener_contexto(user_id)
    respuesta_ia = generar_respuesta_conversacional(
        "CONSULTA_INVENTARIO", 
        resultado[:10], 
        contexto, 
        mensaje
    )
    
    # Formatear lista de productos
    if len(resultado) > 0:
        texto_productos = "\n\n*Productos:*\n"
        for p in resultado[:10]:
            id_corto = p["id"][-8:] if len(p["id"]) > 8 else p["id"]
            estado_emoji = "⏳" if p["estado"] == "pendiente" else "✅" if p["estado"] == "vendido" else "🔄"
            vencimiento = ""
            if p["dias_vencimiento"] is not None:
                if p["dias_vencimiento"] < 0:
                    vencimiento = " 🔴 VENCIDO"
                elif p["dias_vencimiento"] <= 3:
                    vencimiento = f" ⚠️ {p['dias_vencimiento']}d"
            
            texto_productos += f"{estado_emoji} `{id_corto}` {p['producto'][:30]}{vencimiento}\n"
        
        if len(resultado) > 10:
            texto_productos += f"\n_Y {len(resultado)-10} más..._"
        
        respuesta_final = (respuesta_ia or f"Encontré *{len(resultado)}* productos:") + texto_productos
    else:
        respuesta_final = respuesta_ia or "No encontré productos con esos criterios."
    
    await update.message.reply_text(respuesta_final, parse_mode="Markdown")


async def procesar_consulta_financiera(update, context, user_id, mensaje):
    """Procesa consultas financieras"""
    datos = memoria.obtener_datos()
    stats = calcular_estadisticas(datos)
    
    contexto = memoria.obtener_contexto(user_id)
    respuesta_ia = generar_respuesta_conversacional(
        "CONSULTA_FINANCIERA",
        datos,
        contexto,
        mensaje
    )
    
    respuesta_final = respuesta_ia or (
        f"📊 *Tus Finanzas*\n\n"
        f"💰 Invertido: ${stats['total_invertido']:.2f}\n"
        f"💵 Vendido: ${stats['total_ventas']:.2f}\n"
        f"📈 Ganancia: ${stats['ganancia_neta']:.2f}\n"
        f"⏳ Por recuperar: ${stats['por_recuperar']:.2f}\n\n"
        f"📦 Productos: {len(datos)} total"
    )
    
    await update.message.reply_text(respuesta_final, parse_mode="Markdown")


async def procesar_consulta_producto(update, context, user_id, mensaje, busqueda):
    """Busca información específica de un producto"""
    datos = memoria.obtener_datos()
    producto = buscar_producto(busqueda, datos) if busqueda else None
    
    # Si no hay búsqueda específica, interpretar el mensaje
    if not producto:
        # Buscar cualquier ID o nombre en el mensaje
        palabras = mensaje.split()
        for p in palabras:
            if len(p) > 3:
                producto = buscar_producto(p, datos)
                if producto and not isinstance(producto, list):
                    break
    
    if producto and not isinstance(producto, list):
        # Calcular ganancia potencial o real
        ganancia_texto = ""
        if producto["estado"] == "vendido":
            try:
                pc = float(str(producto["precio_compra"]).replace("US$", "").replace("$", "").replace(",", "") or 0)
                pv = float(str(producto["precio_venta"]).replace("US$", "").replace("$", "").replace(",", "") or 0)
                ganancia = pv - pc
                ganancia_texto = f"\n💵 Vendido en: ${pv:.2f}\n📈 Ganancia: ${ganancia:.2f}"
            except:
                pass
        else:
            ganancia_texto = "\n⏳ *Pendiente de venta*"
        
        vencimiento_texto = ""
        if producto["dias_vencimiento"] is not None:
            if producto["dias_vencimiento"] < 0:
                vencimiento_texto = f"\n🔴 *VENCIDO* hace {abs(producto['dias_vencimiento'])} días"
            elif producto["dias_vencimiento"] == 0:
                vencimiento_texto = "\n🔴 *VENCE HOY*"
            elif producto["dias_vencimiento"] <= 3:
                vencimiento_texto = f"\n⚠️ Vence en {producto['dias_vencimiento']} días"
            else:
                vencimiento_texto = f"\n✅ Vence en {producto['dias_vencimiento']} días"
        
        await update.message.reply_text(
            f"📦 *{producto['producto']}*\n\n"
            f"🆔 `{producto['id']}`\n"
            f"💰 Compra: {producto['precio_compra']}\n"
            f"📅 Comprado: {producto['fecha_compra']}{ganancia_texto}{vencimiento_texto}\n"
            f"📝 Estado: {producto['estado'].upper()}",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("❌ No encontré ese producto. ¿Tienes el ID o nombre correcto?")


async def procesar_conversacion_general(update, context, user_id, mensaje):
    """Procesa conversación general usando IA"""
    datos = memoria.obtener_datos()
    contexto = memoria.obtener_contexto(user_id)
    
    # Verificar si hay productos urgentes para mencionar
    urgentes = obtener_productos_por_vencer(3, datos)
    
    prompt_extra = ""
    if urgentes:
        prompt_extra = f"\n\nPRODUCTOS URGENTES (menciona si es relevante):\n" + "\n".join([
            f"- {u['producto']} (vence en {u['dias_vencimiento']} días)" 
            for u in urgentes[:3]
        ])
    
    respuesta = generar_respuesta_conversacional(
        "CONVERSACION_GENERAL",
        datos,
        contexto + prompt_extra,
        mensaje
    )
    
    await update.message.reply_text(
        respuesta or "🤔 Estoy aquí para ayudarte con tu inventario. ¿Qué necesitas saber?",
        parse_mode="Markdown"
    )


async def iniciar_modo_review(update, context, user_id):
    """Inicia modo de captura de fotos para review"""
    memoria.pendientes[user_id] = {
        "modo": "esperando_fotos_review",
        "fotos": []
    }
    
    await update.message.reply_text(
        "⭐ *MODO REVIEW ACTIVADO*\n\n"
        "Envíame las fotos del producto (pueden ser varias).\n"
        "Cuando termines, escribe *'listo'* y generaré la reseña.\n"
        "Escribe *'cancelar'* para salir sin guardar.",
        parse_mode="Markdown"
    )


async def finalizar_review(update, context, user_id):
    """Procesa fotos y genera review"""
    pendiente = memoria.pendientes.get(user_id, {})
    fotos = pendiente.get("fotos", [])
    
    if not fotos:
        await update.message.reply_text("❌ No recibí fotos. Cancelando.")
        memoria.pendientes[user_id] = {}
        return
    
    msg = await update.message.reply_text(f"⏳ Analizando {len(fotos)} fotos y generando review...")
    
    try:
        review = generar_review_multi_imagen(fotos)
        
        # Limpiar fotos temporales
        for f in fotos:
            try:
                if os.path.exists(f):
                    os.remove(f)
            except:
                pass
        
        if not review:
            await msg.edit_text("❌ Error generando la review. Intenta de nuevo.")
            memoria.pendientes[user_id] = {}
            return
        
        # Guardar review en memoria temporal
        memoria.pendientes[user_id] = {
            "modo": "esperando_asociar_review",
            "review": review
        }
        
        # Buscar productos pendientes para asociar
        datos = memoria.obtener_datos()
        pendientes = [d for d in datos if d["estado"] == "pendiente"][-5:]
        
        if pendientes:
            texto = "⭐ *Review generada*\n\n¿A qué producto la asociamos?\n\n"
            for i, p in enumerate(pendientes, 1):
                id_corto = p["id"][-8:] if len(p["id"]) > 8 else p["id"]
                texto += f"{i}. `{id_corto}` - {p['producto'][:35]}\n"
            texto += "\nResponde con el número, *'ninguno'* para no guardar, o *'otro'* para buscar otro producto"
            
            await msg.edit_text(texto, parse_mode="Markdown")
        else:
            await msg.edit_text(
                f"⭐ *Review generada*\n\n{review}\n\n"
                f"💾 No se guardó en Sheets (no hay productos pendientes para asociar).\n"
                f"Puedes copiarla manualmente.",
                parse_mode="Markdown"
            )
            memoria.pendientes[user_id] = {}
            
    except Exception as e:
        logging.error(f"Error en review: {e}")
        await msg.edit_text(f"❌ Error: {str(e)[:200]}")
        for f in fotos:
            try:
                if os.path.exists(f):
                    os.remove(f)
            except:
                pass
        memoria.pendientes[user_id] = {}


async def cancelar_review(update, context, user_id):
    """Cancela modo review"""
    pendiente = memoria.pendientes.get(user_id, {})
    fotos = pendiente.get("fotos", [])
    
    for f in fotos:
        try:
            if os.path.exists(f):
                os.remove(f)
        except:
            pass
    
    memoria.pendientes[user_id] = {}
    await update.message.reply_text("❌ Modo review cancelado", reply_markup=get_main_keyboard())


async def procesar_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Procesa fotos según el modo actual"""
    user_id = str(update.effective_user.id)
    if user_id != TU_CHAT_ID:
        return
    
    pendiente = memoria.pendientes.get(user_id, {})
    
    # MODO REVIEW - Acumular fotos
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
    
    # MODO COMPRA - Procesar imagen de pedido
    photo = update.message.photo[-1]
    file = await photo.get_file()
    
    image_path = f"compra_{user_id}_{update.message.message_id}.jpg"
    await file.download_to_drive(image_path)
    
    msg = await update.message.reply_text("⏳ Analizando imagen de compra...")
    
    try:
        datos = analizar_imagen_compra(image_path)
        
        if not datos:
            await msg.edit_text("❌ No pude leer la información de la imagen. Intenta con otra foto o ingresa los datos manualmente.")
            return
        
        exito, pedido_id = agregar_compra(datos)
        
        if exito:
            # Calcular días para vencimiento
            dias_venc = None
            try:
                if datos.get("fecha_devolucion"):
                    fecha_dev = datetime.strptime(datos["fecha_devolucion"], "%d/%m/%Y")
                    dias_venc = (fecha_dev - datetime.now()).days
            except:
                pass
            
            vencimiento_texto = ""
            if dias_venc is not None:
                if dias_venc < 0:
                    vencimiento_texto = "🔴 Ya venció"
                elif dias_venc == 0:
                    vencimiento_texto = "🔴 Vence hoy"
                elif dias_venc <= 3:
                    vencimiento_texto = f"⚠️ Vence en {dias_venc} días"
                else:
                    vencimiento_texto = f"✅ Vence en {dias_venc} días"
            
            await msg.edit_text(
                f"✅ *¡Compra registrada!*\n\n"
                f"📦 {datos['producto']}\n"
                f"🆔 `{pedido_id}`\n"
                f"💰 {datos['precio_compra']}\n"
                f"📅 {vencimiento_texto}\n\n"
                f"_Responde 'vendido' o 'devuelto' a este mensaje para actualizar_",
                parse_mode="Markdown",
                reply_markup=get_main_keyboard()
            )
            
            memoria.agregar_a_historial(user_id, "Sistema", f"Compra registrada: {datos['producto']}")
        else:
            await msg.edit_text("❌ Error al guardar en Google Sheets")
            
    except Exception as e:
        logging.error(f"Error procesando compra: {e}")
        await msg.edit_text(f"❌ Error: {str(e)[:200]}")
    finally:
        try:
            if os.path.exists(image_path):
                os.remove(image_path)
        except:
            pass


async def manejar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja botones inline"""
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = str(query.from_user.id)
    
    if data.startswith("borrar_"):
        pedido_id = data.replace("borrar_", "")
        exito, producto = borrar_producto(pedido_id)
        
        if exito:
            await query.edit_message_text(
                f"🗑️ *Producto eliminado*\n\n"
                f"📦 {producto['producto']}\n"
                f"🆔 `{pedido_id}`",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text("❌ Error al borrar el producto")
        
        memoria.pendientes[user_id] = {}
        return
    
    if data == "cancelar":
        await query.edit_message_text("❌ Cancelado")
        memoria.pendientes[user_id] = {}
        return
    
    if data.startswith("mp_"):
        metodo = data.replace("mp_", "")
        # Esto se maneja en el flujo de venta pendiente
        return
    
    # Botones de menú principal
    if data == "btn_compra":
        await query.message.reply_text(
            "📸 Envía una foto del pedido de Amazon o indica los datos manualmente.",
            reply_markup=get_main_keyboard()
        )
    elif data == "btn_venta":
        await query.message.reply_text(
            "💰 Indica qué vendiste. Ejemplo: *Vendí el carrito en 45 por zelle*",
            parse_mode="Markdown"
        )
    elif data == "btn_review":
        await iniciar_modo_review(update, context, user_id)
    elif data == "btn_inventario":
        await mostrar_inventario_resumen(update, context)
    elif data == "btn_finanzas":
        await mostrar_finanzas_resumen(update, context)


async def mostrar_inventario_resumen(update, context):
    """Muestra resumen de inventario"""
    datos = memoria.obtener_datos()
    pendientes = [d for d in datos if d["estado"] == "pendiente"]
    por_vencer = obtener_productos_por_vencer(7, datos)
    
    texto = (
        f"📋 *INVENTARIO*\n\n"
        f"📦 Total: {len(datos)}\n"
        f"⏳ Pendientes: {len(pendientes)}\n"
        f"⚠️ Por vencer (7 días): {len(por_vencer)}\n\n"
    )
    
    if por_vencer:
        texto += "*Urgentes:*\n"
        for p in por_vencer[:5]:
            emoji = "🔴" if p["dias_vencimiento"] < 0 else "⚠️"
            texto += f"{emoji} {p['producto'][:30]} ({p['dias_vencimiento']}d)\n"
    
    await update.message.reply_text(texto, parse_mode="Markdown")


async def mostrar_finanzas_resumen(update, context):
    """Muestra resumen financiero"""
    datos = memoria.obtener_datos()
    stats = calcular_estadisticas(datos)
    
    await update.message.reply_text(
        f"📊 *RESUMEN FINANCIERO*\n\n"
        f"💰 Invertido: ${stats['total_invertido']:.2f}\n"
        f"💵 Vendido: ${stats['total_ventas']:.2f}\n"
        f"📈 Ganancia neta: ${stats['ganancia_neta']:.2f}\n"
        f"⏳ Por recuperar: ${stats['por_recuperar']:.2f}\n\n"
        f"Rentabilidad: {(stats['ganancia_neta']/stats['total_invertido']*100):.1f}%" if stats['total_invertido'] > 0 else "N/A",
        parse_mode="Markdown"
    )


async def mostrar_ayuda(update, context):
    """Muestra ayuda completa"""
    await update.message.reply_text(
        "🤖 *OMAR AI - AYUDA*\n\n"
        "*CONVERSACIÓN NATURAL:*\n"
        "Solo háblame como lo harías con una persona:\n\n"
        "💰 *Ventas:*\n"
        "• _Vendí el carrito de muebles en 45 por zelle_\n"
        "• _El producto 1234 ya lo vendí en 60_\n"
        "• _Lo vendí por paypal (respondiendo a un mensaje)_\n\n"
        "🔄 *Devoluciones:*\n"
        "• _Devolví la silla gamer_\n"
        "• _Lo devolví ayer (respondiendo a mensaje)_\n\n"
        "🗑️ *Borrar:*\n"
        "• _Borra el que no tiene ID_\n"
        "• _Elimina el producto 1234_\n"
        "• _Borrar (respondiendo a mensaje)_\n\n"
        "📊 *Consultas:*\n"
        "• _Cuánto he ganado este mes?_\n"
        "• _Qué productos tengo por vencer?_\n"
        "• _Cuál es el más caro?_\n"
        "• _Muéstrame los pendientes_\n"
        "• _Dame un resumen financiero_\n\n"
        "⭐ *Reviews:*\n"
        "• _Genera review de este producto_\n"
        "• Botón ⭐ Generar review\n\n"
        "📸 *Compras:*\n"
        "• Envía foto del pedido\n"
        "• Botón 📸 Registrar compra\n\n"
        "El bot te avisará proactivamente de urgencias.",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )


async def alerta_proactiva(context: ContextTypes.DEFAULT_TYPE):
    """Envía alertas proactivas al usuario"""
    try:
        datos = memoria.obtener_datos(force=True)
        
        # Productos vencidos o por vencer
        urgentes = obtener_productos_por_vencer(3, datos)
        vencidos = [u for u in urgentes if u["dias_vencimiento"] < 0]
        por_vencer = [u for u in urgentes if u["dias_vencimiento"] >= 0]
        
        if not urgentes:
            return
        
        mensaje = "🚨 *ALERTA DE INVENTARIO*\n\n"
        
        if vencidos:
            mensaje += f"🔴 *{len(vencidos)} productos VENCIDOS:*\n"
            for v in vencidos[:3]:
                mensaje += f"• {v['producto'][:40]}\n"
            mensaje += "\n"
        
        if por_vencer:
            mensaje += f"⚠️ *{len(por_vencer)} por vencer:*\n"
            for p in por_vencer[:5]:
                mensaje += f"• {p['producto'][:35]} ({p['dias_vencimiento']} días)\n"
        
        mensaje += "\n_Responde para ver opciones o ignorar_"
        
        await context.bot.send_message(
            chat_id=TU_CHAT_ID,
            text=mensaje,
            parse_mode="Markdown"
        )
        
    except Exception as e:
        logging.error(f"Error en alerta proactiva: {e}")

# ============================================
# MAIN
# ============================================

async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("start", "Iniciar asistente"),
        BotCommand("ayuda", "Ver ejemplos de uso"),
        BotCommand("inventario", "Ver resumen de inventario"),
        BotCommand("finanzas", "Ver resumen financiero"),
    ])
    
    # Configurar alertas proactivas
    job_queue = application.job_queue
    
    # Alerta diaria a las 9:00 AM
    job_queue.run_daily(
        alerta_proactiva,
        time=datetime.strptime("09:00", "%H:%M").time(),
        days=(0, 1, 2, 3, 4, 5, 6)
    )
    
    # Recordatorio a las 8:00 PM si hay urgencias
    job_queue.run_daily(
        alerta_proactiva,
        time=datetime.strptime("20:00", "%H:%M").time(),
        days=(0, 1, 2, 3, 4, 5, 6)
    )


def main():
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO
    )
    
    if not all([TELEGRAM_TOKEN, GOOGLE_SHEETS_ID, GEMINI_API_KEY, TU_CHAT_ID, GOOGLE_CREDENTIALS_JSON]):
        print("❌ Faltan variables de entorno")
        return
    
    print("🤖 Omar AI v7.0 - Asistente Conversacional")
    print(f"✅ Chat ID: {TU_CHAT_ID}")
    
    application = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    
    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("ayuda", mostrar_ayuda))
    application.add_handler(CommandHandler("inventario", mostrar_inventario_resumen))
    application.add_handler(CommandHandler("finanzas", mostrar_finanzas_resumen))
    
    application.add_handler(CallbackQueryHandler(manejar_callback))
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, procesar_foto))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, procesar_mensaje))
    
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
