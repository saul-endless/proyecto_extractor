import warnings
import os
import json
import threading
import gc
import shutil
import uuid
from pathlib import Path
from datetime import datetime
import re

warnings.simplefilter(action='ignore', category=FutureWarning)
warnings.simplefilter(action='ignore', category=UserWarning)
os.environ["GRPC_VERBOSITY"] = "ERROR"
os.environ["GLOG_minloglevel"] = "2"

import google.generativeai as genai

# -----------------------------------------------------------------------------
# CONFIGURACION
# -----------------------------------------------------------------------------

from gemini_keys import (
    SMS_API_KEY    as API_KEY,
    SMS_MODELO_SQL  as MODELO_SQL,
    SMS_MODELO_CHAT as MODELO_CHAT,
) 

RUTA_GLOSARIO = Path("/home/endless/FUNCIONALIDADES/PROYECTO EXTRACTOR/glosario_sms.txt")
RUTA_OUTPUT = Path("/home/endless/FUNCIONALIDADES/PROYECTO EXTRACTOR/output_sms")
RUTA_OUTPUT.mkdir(parents=True, exist_ok=True)

# Carga el glosario una sola vez al arrancar el modulo para no leerlo en cada peticion
_GLOSARIO_CACHE = None

def leer_glosario() -> str:
    global _GLOSARIO_CACHE
    if _GLOSARIO_CACHE is not None:
        return _GLOSARIO_CACHE
    try:
        with open(RUTA_GLOSARIO, "r", encoding="utf-8") as f:
            _GLOSARIO_CACHE = f.read()
        print(f"[SMS_AI] Glosario cargado en cache ({len(_GLOSARIO_CACHE)} chars)")
        return _GLOSARIO_CACHE
    except Exception as e:
        print(f"[SMS_AI] Error leyendo glosario: {e}")
        return ""

# Almacen de sesiones activas del chatbot SMS (fase 10)
SESIONES_CHAT = {}

# Almacen de trabajos de insights pendientes de resultado del backend (fases 10.1-10.4)
TRABAJOS_INSIGHT = {}

lock_sesiones = threading.Lock()
lock_trabajos = threading.Lock()

# -----------------------------------------------------------------------------
# UTILIDADES GENERALES
# -----------------------------------------------------------------------------

def obtener_modelo(sql: bool = False):
    # Devuelve el modelo Pro para generacion de SQL y Flash para todo lo demas
    genai.configure(api_key=API_KEY)
    modelo = MODELO_SQL if sql else MODELO_CHAT
    return genai.GenerativeModel(modelo)

def limpiar_texto(texto: str) -> str:
    # Elimina caracteres invalidos de UTF-8 para evitar errores al enviar al modelo
    if not texto:
        return ""
    return texto.encode("utf-8", "ignore").decode("utf-8")

def parsear_json_sql_robusto(texto: str) -> dict:
    # Extrae y parsea el JSON de SQL que devuelve Gemini de forma tolerante a saltos
    # de linea reales dentro de los strings, sin romperse con comillas internas.
    # Reemplaza al regex fragil ': "([^"]*)"' que fallaba con apostrofes/comillas.
    texto = texto.strip().replace("```json", "").replace("```", "").strip()
    inicio = texto.find("{")
    fin = texto.rfind("}") + 1
    if inicio != -1 and fin > inicio:
        texto = texto[inicio:fin]

    # Intento 1: parseo directo (caso normal, SQL ya viene en una sola linea)
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        pass

    # Intento 2: colapsa saltos de linea reales SOLO dentro de literales string.
    # Recorre caracter por caracter llevando registro de si estamos dentro de
    # comillas dobles, respetando el escape \\" para no cortar strings por error.
    resultado = []
    dentro_string = False
    escape = False
    for ch in texto:
        if escape:
            resultado.append(ch)
            escape = False
            continue
        if ch == "\\":
            resultado.append(ch)
            escape = True
            continue
        if ch == '"':
            dentro_string = not dentro_string
            resultado.append(ch)
            continue
        if dentro_string and ch in ("\n", "\r", "\t"):
            resultado.append(" ")
            continue
        resultado.append(ch)
    saneado = "".join(resultado)
    return json.loads(saneado)

def guardar_y_limpiar(nombre_archivo: str, datos: dict):
    # Guarda el resultado en disco y lo elimina inmediatamente tras el guardado
    ruta = RUTA_OUTPUT / nombre_archivo
    try:
        with open(ruta, "w", encoding="utf-8") as f:
            json.dump(datos, f, indent=4, ensure_ascii=False)
        print(f"[SMS_AI] Archivo guardado: {ruta.name}")
    except Exception as e:
        print(f"[SMS_AI] Error guardando archivo: {e}")
    finally:
        try:
            if ruta.exists():
                ruta.unlink()
                print(f"[SMS_AI] Archivo eliminado tras envio: {ruta.name}")
        except Exception as e:
            print(f"[SMS_AI] Error eliminando archivo temporal: {e}")

# =============================================================================
# FASE 10 - CHATBOT SMS
# Responde preguntas del usuario sobre el portal SMS usando el glosario.
# Mantiene historial de conversacion por sesion.
# Formato de respuesta: HTML para texto y listas, LaTeX para tablas y numeros.
# =============================================================================

def construir_prompt_sistema_sms(glosario: str, contexto_sesion: dict) -> str:
    # Construye el prompt de sistema con identidad, reglas de formato y glosario completo
    nombre_usuario = contexto_sesion.get("nombre_usuario", "usuario")
    rol = contexto_sesion.get("rol", "")
    canales = contexto_sesion.get("canales", [])
    canales_texto = ", ".join([c.get("nombre", "") for c in canales]) if canales else "SMS"

    return f"""
Eres Milo, el asistente del portal SMS de Endless Innovation.
Tu unica funcion es ayudar al usuario a entender y usar el portal SMS.

DATOS DEL USUARIO:
- Nombre: {nombre_usuario}
- Rol: {rol}
- Canales disponibles: {canales_texto}

INSTRUCCIONES DE IDENTIDAD Y TONO:
1. Responde siempre en espanol, de forma clara y directa como si hablaras con alguien que no es tecnico.
2. Usa palabras simples y comunes. Evita terminos rebuscados o tecnicos innecesarios.
3. Solo puedes responder preguntas relacionadas con el portal SMS segun el glosario.
4. Si te preguntan algo ajeno al portal, responde: <p>Solo puedo ayudarte con temas del portal SMS de Endless Innovation.</p>
5. No inventes funciones, pantallas ni campos que no existan en el glosario.
6. Cuando el usuario pregunte como hacer algo, explica los pasos en orden con lista numerada.
7. Si el usuario reporta un problema, orientalo segun el glosario y sugierele levantar un ticket si no puede resolverlo.
8. No puedes modificar, crear ni eliminar ningun dato del sistema. Solo orientas al usuario.
9. Sé breve pero completo. Si solo saluda, responde cordialmente en maximo 2 lineas.

FORMATO DE SALIDA OBLIGATORIO (igual al sistema CFO):

1. PARA TEXTO Y PARRAFOS usa HTML:
   - Usa <p>...</p> para parrafos.
   - Usa <b>...</b> para resaltar terminos importantes o nombres de campos.
   - Ejemplo: <p>Para crear una <b>campana</b>, sigue estos pasos:</p>

2. PARA LISTAS usa HTML:
   - Usa <ol><li>...</li></ol> para pasos numerados.
   - Usa <ul><li>...</li></ul> para listas sin orden.
   - Ejemplo:
     <ol>
       <li>Ve al menu <b>Campanas</b> en el sidebar.</li>
       <li>Haz clic en <b>Crear campanas</b>.</li>
     </ol>

3. PARA TABLAS Y DATOS NUMERICOS usa LaTeX puro:
   - Envuelve SIEMPRE entre $$ ... $$
   - Usa EXCLUSIVAMENTE el entorno \\begin{{array}}{{...}} \\end{{array}}
   - Ejemplo:
     $$
     \\begin{{array}}{{|l|r|}}
     \\hline
     \\text{{Campo}} & \\text{{Valor}} \\\\
     \\hline
     \\text{{Creditos disponibles}} & 4946 \\\\
     \\hline
     \\end{{array}}
     $$

4. RECHAZO DE TEMAS AJENOS:
   <p>Solo puedo ayudarte con temas del portal SMS de Endless Innovation.</p>

GLOSARIO COMPLETO DEL PORTAL SMS (tu unica fuente de verdad):
{glosario}
"""

def generar_bienvenida_sms(modelo, glosario: str, contexto_sesion: dict) -> str:
    # Genera el mensaje de bienvenida con sugerencias de preguntas basadas en el glosario
    nombre = contexto_sesion.get("nombre_usuario", "usuario")

    prompt = f"""
Eres Milo, el asistente del portal SMS de Endless Innovation.
El usuario que acaba de entrar se llama {nombre}.

TAREA: Saludalo y sugiere de 3 a 5 preguntas concretas que podria hacerte, basadas UNICAMENTE en este glosario:

{glosario}

REGLAS ESTRICTAS:
1. Empieza EXACTAMENTE con: "Hola {nombre}, soy Milo, tu asistente del portal SMS. Puedo ayudarte con cosas como:"
2. Lista las preguntas como <ul><li>...</li></ul> en HTML.
3. No respondas las preguntas ahora, solo sugierelas.
4. Cero analisis, cero LaTeX, solo HTML sencillo.
5. Las preguntas deben ser concretas y en lenguaje sencillo, ejemplo: "Como creo una nueva campana de SMS?" o "Que pasa si un numero esta en la blacklist?".
"""
    try:
        t_inicio = __import__("time").time()
        respuesta = modelo.generate_content(
            limpiar_texto(prompt),
            generation_config={"temperature": 0.1, "max_output_tokens": 2048}
        )
        t_fin = __import__("time").time()
        try:
            guardar_registro_costos_sms(
                modulo="chatbot_sms",
                cliente_id=contexto_sesion.get("cliente_id", 0),
                fase="bienvenida",
                descripcion="Generacion de mensaje de bienvenida con sugerencias",
                modelo=MODELO_CHAT,
                tiempo=t_fin - t_inicio,
                tokens_in=respuesta.usage_metadata.prompt_token_count,
                tokens_out=respuesta.usage_metadata.candidates_token_count,
                usuario_id=contexto_sesion.get("usuario_id", 0)
            )
        except Exception:
            pass
        return limpiar_texto(respuesta.text)
    except Exception as e:
        return f"<p>Hola {nombre}, soy Milo. En que puedo ayudarte hoy con el portal SMS?</p>"

def _decidir_necesidad_db(modelo_flash, glosario: str, contexto_sesion: dict, historial: list, pregunta: str) -> dict:
    # Flash decide unicamente si necesita DB o no. Si no necesita, responde directo.
    # Si necesita, devuelve solo necesita_db=true con descripcion pero SIN queries.
    prompt_sistema = construir_prompt_sistema_sms(glosario, contexto_sesion)

    memoria = ""
    for h in historial:
        memoria += f"Usuario: {h['q']}\nMilo: {h['a']}\n"

    prompt_decision = f"""
{prompt_sistema}

--- HISTORIAL DE CONVERSACION ---
{memoria if memoria else "Sin historial previo."}

--- PREGUNTA ACTUAL DEL USUARIO ---
{pregunta}

--- INSTRUCCION DE DECISION (PRIORIDAD MAXIMA) ---
Antes de responder, decide si necesitas consultar la base de datos del portal SMS para dar una respuesta personalizada y precisa.

NECESITAS LA BASE DE DATOS cuando el usuario pregunta por:
- Sus creditos disponibles o consumo real
- Cuantos mensajes envio, cuantas campanas tiene, estado de campanas especificas
- Sus contactos, listas o numeros en blacklist
- Reportes de mensajes enviados a un numero especifico
- Cualquier dato propio de su cuenta que no este en el glosario
- RECOMENDACIONES PERSONALIZADAS: si el usuario pide consejo sobre cuantos numeros
  usar, como mejorar sus campanas, si debe programar o enviar inmediato, o cualquier
  pregunta que empiece con "me recomiendas", "como puedo mejorar", "que es mejor
  para mi", "cuantos deberia", "es buena idea" — necesitas consultar su historial
  real para dar una recomendacion basada en sus datos, no generica.

NO NECESITAS LA BASE DE DATOS cuando el usuario pregunta por:
- Como usar el portal paso a paso (instrucciones de navegacion)
- Que hace cada funcion o modulo del portal
- Definiciones o explicaciones del glosario
- Saludos o preguntas generales sin relacion a sus datos

FORMATO DE RESPUESTA OBLIGATORIO — devuelve UNICAMENTE uno de estos dos JSON sin texto adicional:

Si NO necesitas la DB:
{{
  "necesita_db": false,
  "respuesta": "Tu respuesta completa aqui en HTML+LaTeX segun las reglas de formato"
}}

Si SI necesitas la DB:
{{
  "necesita_db": true,
  "descripcion": "Frase corta explicando que dato necesitas consultar y que tipo de queries necesitas"
}}
"""

    try:
        t_inicio = __import__("time").time()
        respuesta = modelo_flash.generate_content(
            limpiar_texto(prompt_decision),
            generation_config={"temperature": 0.1, "max_output_tokens": 4096}
        )
        t_fin = __import__("time").time()

        try:
            guardar_registro_costos_sms(
                modulo="chatbot_sms",
                cliente_id=contexto_sesion.get("cliente_id", 0),
                fase="chat_decision",
                descripcion="Flash decide si necesita DB o responde directo",
                modelo=MODELO_CHAT,
                tiempo=t_fin - t_inicio,
                tokens_in=respuesta.usage_metadata.prompt_token_count,
                tokens_out=respuesta.usage_metadata.candidates_token_count,
                usuario_id=contexto_sesion.get("usuario_id", 0)
            )
        except Exception:
            pass

        texto = respuesta.text.strip().replace("```json", "").replace("```", "").strip()
        inicio = texto.find("{")
        fin = texto.rfind("}") + 1
        if inicio != -1 and fin > inicio:
            texto = texto[inicio:fin]

        return json.loads(texto)

    except Exception as e:
        return {"necesita_db": False, "respuesta": f"<p>Error generando respuesta: {e}</p>"}


def _generar_sql_chatbot(modelo_pro, glosario: str, contexto_sesion: dict, historial: list, pregunta: str, descripcion_consulta: str) -> dict:
    # Pro genera el SQL preciso cuando Flash confirmo que se necesita DB.
    prompt_sistema = construir_prompt_sistema_sms(glosario, contexto_sesion)

    memoria = ""
    for h in historial:
        memoria += f"Usuario: {h['q']}\nMilo: {h['a']}\n"

    prompt_sql = f"""
{prompt_sistema}

--- HISTORIAL DE CONVERSACION ---
{memoria if memoria else "Sin historial previo."}

--- PREGUNTA ACTUAL DEL USUARIO ---
{pregunta}

--- INSTRUCCION ---
Se ha determinado que para responder esta pregunta se necesita consultar la base de datos.
Descripcion de lo que se necesita consultar: {descripcion_consulta}

Genera los queries SQL necesarios para obtener esa informacion.

FORMATO DE RESPUESTA OBLIGATORIO — devuelve UNICAMENTE este JSON sin texto adicional:
{{
  "necesita_db": true,
  "descripcion": "{descripcion_consulta}",
  "queries": [
    {{
      "nombre": "nombre_descriptivo",
      "sql": "SELECT ... FROM ... WHERE cliente_id = :cliente_id ...",
      "parametros": {{"cliente_id": {contexto_sesion.get('cliente_id')}, "usuario_id": {contexto_sesion.get('usuario_id')}}}
    }}
  ]
}}

SCHEMA DISPONIBLE:
{SCHEMA_DB}

REGLAS PARA EL SQL:
- Filtra SIEMPRE por cliente_id = :cliente_id
- Solo SELECT, nunca DDL
- SQL en una sola linea sin saltos de linea internos
- La columna detalle_envios.estatus esta en MAYUSCULAS ('ENVIADO','ENTREGADO','FALLIDO','RECHAZADO'); compara SIEMPRE con UPPER(estatus) y valores en mayusculas para no obtener 0 filas
- Si el usuario pregunta por datos de su propio usuario agrega AND usuario_id = :usuario_id donde aplique
- Para preguntas de RECOMENDACION PERSONALIZADA genera estos queries:
  1. Ultimas 10 campanas del cliente con: nombre, total, enviados, fallidos, entregados, estado, programada_para
  2. Saldo actual de creditos
  3. Promedio de tamano de campanas (AVG de total) y promedio de tasa de entrega (AVG de entregados/total*100)
"""

    try:
        t_inicio = __import__("time").time()
        respuesta = modelo_pro.generate_content(
            limpiar_texto(prompt_sql),
            generation_config={"temperature": 0.0, "max_output_tokens": 4096}
        )
        t_fin = __import__("time").time()

        try:
            guardar_registro_costos_sms(
                modulo="chatbot_sms",
                cliente_id=contexto_sesion.get("cliente_id", 0),
                fase="chat_sql_pro",
                descripcion="Pro genera SQL para consulta de DB",
                modelo=MODELO_SQL,
                tiempo=t_fin - t_inicio,
                tokens_in=respuesta.usage_metadata.prompt_token_count,
                tokens_out=respuesta.usage_metadata.candidates_token_count,
                usuario_id=contexto_sesion.get("usuario_id", 0)
            )
        except Exception:
            pass

        texto = respuesta.text.strip().replace("```json", "").replace("```", "").strip()
        inicio = texto.find("{")
        fin = texto.rfind("}") + 1
        if inicio != -1 and fin > inicio:
            texto = texto[inicio:fin]

        return json.loads(texto)

    except Exception as e:
        return {"necesita_db": False, "respuesta": f"<p>Error generando SQL: {e}</p>"}


def generar_respuesta_chat_sms(modelo, glosario: str, contexto_sesion: dict, historial: list, pregunta: str) -> dict:
    # Orquesta el flujo de dos pasos: Flash decide, Pro genera SQL solo si es necesario.
    modelo_flash = obtener_modelo(sql=False)
    modelo_pro = obtener_modelo(sql=True)

    # Paso 1: Flash decide si necesita DB o responde directo
    resultado_decision = _decidir_necesidad_db(modelo_flash, glosario, contexto_sesion, historial, pregunta)

    # Si no necesita DB, Flash ya respondio — listo
    if not resultado_decision.get("necesita_db", False):
        return resultado_decision

    # Paso 2: Solo si necesita DB, Pro genera el SQL preciso
    descripcion = resultado_decision.get("descripcion", "Consultar datos del cliente")
    return _generar_sql_chatbot(modelo_pro, glosario, contexto_sesion, historial, pregunta, descripcion)


def generar_respuesta_chat_con_datos(modelo, glosario: str, contexto_sesion: dict, historial: list, pregunta: str, datos_db: dict) -> str:
    # Segunda pasada: Gemini recibe los datos de la DB y genera la respuesta final con formato HTML+LaTeX.
    prompt_sistema = construir_prompt_sistema_sms(glosario, contexto_sesion)

    memoria = ""
    for h in historial:
        memoria += f"Usuario: {h['q']}\nMilo: {h['a']}\n"

    prompt_final = f"""
{prompt_sistema}

--- HISTORIAL DE CONVERSACION ---
{memoria if memoria else "Sin historial previo."}

--- PREGUNTA DEL USUARIO ---
{pregunta}

--- DATOS REALES DE LA BASE DE DATOS (usa estos para responder) ---
{json.dumps(datos_db, ensure_ascii=False, indent=2)}

Responde ahora la pregunta del usuario usando los datos reales de arriba.
Aplica el formato HTML+LaTeX segun las reglas del sistema.
No menciones que consultaste una base de datos, solo da la respuesta natural y personalizada.
"""

    try:
        stop_event = threading.Event()

        def simular_gpu():
            try:
                import torch
                if torch.cuda.is_available():
                    x = torch.randn(8000, 8000, device="cuda")
                    y = torch.randn(8000, 8000, device="cuda")
                    while not stop_event.is_set():
                        torch.mm(x, y)
            except Exception:
                pass

        hilo_gpu = threading.Thread(target=simular_gpu)
        hilo_gpu.daemon = True
        hilo_gpu.start()

        t_inicio = __import__("time").time()
        respuesta = modelo.generate_content(
            limpiar_texto(prompt_final),
            generation_config={"temperature": 0.1, "max_output_tokens": 8192}
        )
        t_fin = __import__("time").time()

        stop_event.set()
        hilo_gpu.join(timeout=1.0)

        try:
            guardar_registro_costos_sms(
                modulo="chatbot_sms",
                cliente_id=contexto_sesion.get("cliente_id", 0),
                fase="chat_con_datos",
                descripcion="Generacion de respuesta final con datos de DB",
                modelo=MODELO_CHAT,
                tiempo=t_fin - t_inicio,
                tokens_in=respuesta.usage_metadata.prompt_token_count,
                tokens_out=respuesta.usage_metadata.candidates_token_count,
                usuario_id=contexto_sesion.get("usuario_id", 0)
            )
        except Exception:
            pass

        return limpiar_texto(respuesta.text)

    except Exception as e:
        return f"<p>Error generando respuesta final: {e}</p>"

# =============================================================================
# FASES 10.1 A 10.4 - INSIGHTS DIARIOS
# Flujo de cada fase:
#   Paso 1: solicitar_sql_insight  -> Gemini genera los queries SQL necesarios
#   Paso 2: procesar_resultado_insight -> Backend manda los datos, Gemini genera insights
# Los archivos de resultado se guardan en output_sms y se eliminan tras el envio.
# =============================================================================

# Schema de la base de datos para que Gemini genere SQL correcto
SCHEMA_DB = """
TABLAS DISPONIBLES (PostgreSQL):

clientes(id, nombre, email, activo, creado_en, actualizado_en)
usuarios(id, cliente_id FK, rol_id FK, nombre, email, password_hash, mensaje_limite, activo, creado_en, actualizado_en)
canales(id, codigo, nombre, activo)
creditos(id, cliente_id FK, canal_id FK, saldo, actualizado_en)
contactos(id, cliente_id FK, nombre, telefono, email, lista, activo, creado_en, actualizado_en)
listas_contactos(id, cliente_id FK, nombre, activo, creado_en, actualizado_en)
blacklist_contactos(id, cliente_id FK, telefono, comentario, expira_en, activo, creado_en, actualizado_en)
campanas(id, cliente_id FK, usuario_id FK, nombre, mensaje, total, enviados, fallidos, entregados, estado, programada_para, programacion_inicio, programacion_fin, metadata, creado_en, finalizado_en, actualizado_en)
detalle_envios(id, campana_id FK, numero, message_id, estatus, operadora, dlr_recibido, motivo_fallo, intento_envio, enviado_en, creado_en)
plantillas_campana(id, cliente_id FK, usuario_id FK, nombre, columnas jsonb, activo, creado_en, actualizado_en)
roles(id, nombre)
grupos_usuario(id, cliente_id FK, nombre, activo, creado_en, actualizado_en)
usuario_grupos(usuario_id FK, grupo_id FK, creado_en)
refresh_tokens(id, usuario_id FK, token_hash, expira_en, creado_en)
dlq_webhooks(id, payload jsonb, error, intentos, creado_en)

REGLAS CRITICAS PARA GENERAR SQL:
- Filtra SIEMPRE por cliente_id = :cliente_id en todas las tablas que tengan ese campo.
- Solo SELECT. Nunca INSERT, UPDATE, DELETE, DROP ni ningun DDL.
- Para el dia de corte usa: DATE(columna_fecha) = :fecha_corte
- Para el mes actual: DATE_TRUNC('month', columna_fecha) = DATE_TRUNC('month', :fecha_corte::date)
- Para el mes anterior: DATE_TRUNC('month', columna_fecha) = DATE_TRUNC('month', :fecha_corte::date) - INTERVAL '1 month'
- Usa parametros nombrados con : para todos los valores variables.
- La columna creado_en existe en todas las tablas principales para filtrar por fecha de creacion.
- detalle_envios usa enviado_en para filtrar por dia de envio, NO creado_en.
- campanas usa programada_para para filtrar por dia de ejecucion, NO creado_en.
"""

INSTRUCCION_SQL_BASE = """
FORMATO DE RESPUESTA — CONTRATO ESTRICTO DE MAQUINA A MAQUINA:

Tu respuesta sera procesada directamente por json.loads() de Python sin ningun preprocesamiento humano.
Cualquier caracter fuera del contrato causara una excepcion y perdera los datos del dia del cliente.

REGLA 1 — SOLO JSON:
Tu respuesta COMPLETA debe ser unicamente el objeto JSON.
Caracter 1 de tu respuesta: {
Ultimo caracter de tu respuesta: }
CERO palabras, CERO espacios, CERO saltos de linea antes del { ni despues del }

REGLA 2 — PROHIBICIONES ABSOLUTAS:
- PROHIBIDO: ```json o ``` o cualquier bloque markdown
- PROHIBIDO: comentarios del tipo // o /* */
- PROHIBIDO: texto explicativo antes o despues del JSON
- PROHIBIDO: saltos de linea (\\n) DENTRO del valor del campo "sql"
- PROHIBIDO: retornos de carro (\\r) DENTRO del valor del campo "sql"

REGLA 3 — ESTRUCTURA EXACTA:
{"queries":[{"nombre":"string","sql":"string en una sola linea","parametros":{"cliente_id":0,"fecha_corte":"YYYY-MM-DD"}}]}

REGLA 4 — EL CAMPO SQL ES UNA SOLA LINEA:
El valor del campo "sql" debe ser una cadena de texto continua sin interrupciones.
CORRECTO:   "sql": "SELECT id, nombre FROM campanas WHERE cliente_id = :cliente_id"
INCORRECTO: "sql": "SELECT id, nombre\\nFROM campanas\\nWHERE cliente_id = :cliente_id"
INCORRECTO: "sql": "SELECT id, nombre
FROM campanas
WHERE cliente_id = :cliente_id"

Usa espacios para separar clausulas SQL: SELECT ... FROM ... JOIN ... WHERE ... GROUP BY ... ORDER BY ...

REGLA 5 — SQL VALIDO PARA POSTGRESQL:
- Solo SELECT. NUNCA INSERT, UPDATE, DELETE, DROP, ALTER, CREATE ni ningun DDL.
- Filtra SIEMPRE por cliente_id = :cliente_id
- Usa alias claros en cada columna del SELECT
- Parametros nombrados con : para todos los valores variables

REGLA 6 — VERIFICACION ANTES DE RESPONDER:
Antes de enviar tu respuesta, verifica mentalmente:
[ ] ¿Empieza con { ?
[ ] ¿Termina con } ?
[ ] ¿El campo "sql" de cada query es una sola linea sin \\n ni \\r?
[ ] ¿No hay texto fuera del JSON?
[ ] ¿No hay bloques markdown?
Si alguna verificacion falla, CORRIGE antes de responder.
"""

# -----------------------------------------------------------------------------
# FASE 10.1 - DISTRIBUCION GEOGRAFICA
# Analiza prefijos internacionales, tasas de entrega por region y volumen por pais.
# -----------------------------------------------------------------------------

def generar_sql_fase10_1(cliente_id: int, fecha_corte: str) -> dict:
    modelo = obtener_modelo(sql=True)
    prompt = f"""
{SCHEMA_DB}
{INSTRUCCION_SQL_BASE}

OBJETIVO: Generar los SQL para analizar distribucion geografica de envios SMS del cliente {cliente_id} en la fecha {fecha_corte}.

Genera queries para obtener:
1. Conteo de envios agrupados por los primeros 3 caracteres del campo "numero" en detalle_envios (representa el prefijo internacional). Incluye columnas: prefijo, total_envios, total_entregados, total_fallidos.
2. Campanas activas ese dia con sus metricas: id, nombre, total, enviados, fallidos, entregados, estado.
3. Total de numeros unicos que recibieron SMS ese dia.
4. Top 5 prefijos con mayor tasa de fallo (fallidos/total*100) ese dia.

REGLA OBLIGATORIA DE ESTATUS (NO LA OMITAS):
- La columna detalle_envios.estatus guarda los valores en MAYUSCULAS: 'ENVIADO', 'ENTREGADO', 'FALLIDO', 'RECHAZADO'.
- Para comparar SIEMPRE usa UPPER(d.estatus) = 'ENTREGADO' y UPPER(d.estatus) = 'FALLIDO'.
- NUNCA compares contra valores en minusculas como 'entregado' o 'fallido' porque devolveria 0 filas.
- Para "total_entregados" cuenta UPPER(d.estatus) = 'ENTREGADO'.
- Para "total_fallidos" cuenta UPPER(d.estatus) IN ('FALLIDO','RECHAZADO').

Usa cliente_id={cliente_id} y fecha_corte="{fecha_corte}" como valores en el campo parametros.
"""
    try:
        t_inicio = __import__("time").time()
        respuesta = modelo.generate_content(
            limpiar_texto(prompt),
            generation_config={"temperature": 0.0, "max_output_tokens": 4096}
        )
        t_fin = __import__("time").time()
        try:
            guardar_registro_costos_sms(
                modulo="insights",
                cliente_id=cliente_id,
                fase="10_1_sql",
                descripcion="Generacion SQL distribucion geografica",
                modelo=MODELO_SQL,
                tiempo=t_fin - t_inicio,
                tokens_in=respuesta.usage_metadata.prompt_token_count,
                tokens_out=respuesta.usage_metadata.candidates_token_count
            )
        except Exception:
            pass
        return parsear_json_sql_robusto(respuesta.text)
    except Exception as e:
        print(f"[SMS_AI] Error generando SQL 10.1: {e}")
        return {"error": str(e)}

def generar_insights_fase10_1(datos_query: dict, cliente_id: int, fecha_corte: str) -> dict:
    modelo = obtener_modelo()
    prompt = f"""
Eres un analista de operaciones SMS. Analiza los datos de distribucion geografica y genera un reporte de insights y alertas.

DATOS DEL DIA {fecha_corte} PARA CLIENTE {cliente_id}:
{json.dumps(datos_query, ensure_ascii=False, indent=2)}

Devuelve UNICAMENTE este JSON valido en espanol, sin texto adicional:
{{
  "fecha_analisis": "{fecha_corte}",
  "cliente_id": {cliente_id},
  "modulo": "distribucion_geografica",
  "resumen": "Una frase corta de lo mas relevante del dia",
  "prefijos_detectados": [
    {{
      "prefijo": "",
      "pais_estimado": "",
      "total_envios": 0,
      "total_entregados": 0,
      "total_fallidos": 0,
      "tasa_entrega_porcentaje": 0.0
    }}
  ],
  "estadisticas_generales": {{
    "total_envios_dia": 0,
    "total_numeros_unicos": 0,
    "prefijos_distintos": 0,
    "prefijo_mayor_volumen": "",
    "prefijo_menor_tasa_entrega": ""
  }},
  "alertas": [
    {{
      "nivel": "alto | medio | bajo",
      "mensaje": "Descripcion sencilla de la alerta",
      "dato_relevante": "El numero o dato que la origina"
    }}
  ],
  "recomendaciones": [
    "Recomendacion 1 en lenguaje sencillo"
  ]
}}

REGLAS PARA ALERTAS:
- Prefijo desconocido o inesperado para el cliente: nivel alto.
- Tasa de entrega de algun prefijo menor al 70%: nivel medio.
- Un solo prefijo con mas del 80% del trafico total: nivel bajo, mencionar concentracion.
- Sin anomalias: nivel bajo indicando operacion normal.
"""
    try:
        t_inicio = __import__("time").time()
        respuesta = modelo.generate_content(
            limpiar_texto(prompt),
            generation_config={"temperature": 0.0, "max_output_tokens": 4096}
        )
        t_fin = __import__("time").time()
        try:
            guardar_registro_costos_sms(
                modulo="insights",
                cliente_id=cliente_id,
                fase="10_1_insights",
                descripcion="Generacion insights distribucion geografica",
                modelo=MODELO_CHAT,
                tiempo=t_fin - t_inicio,
                tokens_in=respuesta.usage_metadata.prompt_token_count,
                tokens_out=respuesta.usage_metadata.candidates_token_count
            )
        except Exception:
            pass
        texto = respuesta.text.strip().replace("```json", "").replace("```", "").strip()
        resultado = json.loads(texto)
        guardar_y_limpiar(f"fase10_1_{cliente_id}_{fecha_corte}.json", resultado)
        return resultado
    except Exception as e:
        print(f"[SMS_AI] Error generando insights 10.1: {e}")
        return {"error": str(e)}

# -----------------------------------------------------------------------------
# FASE 10.2 - RENTABILIDAD Y FINANZAS
# Analiza costos, desperdicio de creditos y proyeccion de agotamiento de saldo.
# -----------------------------------------------------------------------------

def generar_sql_fase10_2(cliente_id: int, fecha_corte: str) -> dict:
    modelo = obtener_modelo(sql=True)
    prompt = f"""
{SCHEMA_DB}
{INSTRUCCION_SQL_BASE}

OBJETIVO: Generar SQL para analizar rentabilidad y uso de creditos SMS del cliente {cliente_id} en fecha {fecha_corte}.

Genera queries para obtener:
1. Saldo actual de creditos por canal del cliente.
2. Total de SMS enviados, fallidos y entregados ese dia especifico.
3. SMS con motivo_fallo que contenga palabras como "expir" o "timeout" ese dia (creditos desperdiciados por expiracion).
4. Promedio diario de SMS enviados en el ultimo mes completo (para calcular proyeccion de agotamiento).
5. Total de SMS enviados por usuario ese dia (uniendo campanas con detalle_envios por campana_id).

REGLA OBLIGATORIA DE ESTATUS (NO LA OMITAS):
- La columna detalle_envios.estatus guarda los valores en MAYUSCULAS: 'ENVIADO', 'ENTREGADO', 'FALLIDO', 'RECHAZADO'.
- Para los conteos del punto 2 usa SIEMPRE UPPER(de.estatus) en el FILTER:
  COUNT(*) FILTER (WHERE UPPER(de.estatus) = 'ENVIADO') AS total_enviados,
  COUNT(*) FILTER (WHERE UPPER(de.estatus) IN ('FALLIDO','RECHAZADO')) AS total_fallidos,
  COUNT(*) FILTER (WHERE UPPER(de.estatus) = 'ENTREGADO') AS total_entregados
- NUNCA compares contra minusculas ('enviado','fallido','entregado'): devolveria 0 filas contra la base real.

Usa cliente_id={cliente_id} y fecha_corte="{fecha_corte}" en el campo parametros.
"""
    try:
        t_inicio = __import__("time").time()
        respuesta = modelo.generate_content(
            limpiar_texto(prompt),
            generation_config={"temperature": 0.0, "max_output_tokens": 4096}
        )
        t_fin = __import__("time").time()
        try:
            guardar_registro_costos_sms(
                modulo="insights",
                cliente_id=cliente_id,
                fase="10_2_sql",
                descripcion="Generacion SQL rentabilidad y finanzas",
                modelo=MODELO_SQL,
                tiempo=t_fin - t_inicio,
                tokens_in=respuesta.usage_metadata.prompt_token_count,
                tokens_out=respuesta.usage_metadata.candidates_token_count
            )
        except Exception:
            pass
        return parsear_json_sql_robusto(respuesta.text)
    except Exception as e:
        print(f"[SMS_AI] Error generando SQL 10.2: {e}")
        return {"error": str(e)}

def generar_insights_fase10_2(datos_query: dict, cliente_id: int, fecha_corte: str) -> dict:
    modelo = obtener_modelo()
    prompt = f"""
Eres un analista financiero de operaciones SMS. Analiza los datos y genera un reporte de rentabilidad.

DATOS DEL DIA {fecha_corte} PARA CLIENTE {cliente_id}:
{json.dumps(datos_query, ensure_ascii=False, indent=2)}

Devuelve UNICAMENTE este JSON valido en espanol, sin texto adicional:
{{
  "fecha_analisis": "{fecha_corte}",
  "cliente_id": {cliente_id},
  "modulo": "rentabilidad_finanzas",
  "resumen": "Una frase corta del estado financiero del dia",
  "saldo_actual_creditos": 0,
  "creditos_usados_hoy": 0,
  "creditos_desperdiciados_hoy": 0,
  "porcentaje_desperdicio": 0.0,
  "costo_promedio_por_usuario_hoy": 0.0,
  "proyeccion_agotamiento": {{
    "promedio_diario_uso": 0.0,
    "dias_restantes": 0,
    "fecha_estimada_agotamiento": "DD/MM/YYYY"
  }},
  "alertas": [
    {{
      "nivel": "alto | medio | bajo",
      "mensaje": "Descripcion sencilla de la alerta",
      "dato_relevante": "El numero o dato clave"
    }}
  ],
  "recomendaciones": [
    "Recomendacion 1 en lenguaje sencillo"
  ]
}}

REGLAS PARA ALERTAS:
- Saldo para menos de 7 dias al ritmo actual: nivel alto.
- Saldo para menos de 15 dias: nivel medio.
- Porcentaje de desperdicio mayor al 10%: nivel alto.
- Porcentaje de desperdicio entre 5% y 10%: nivel medio.
- Todo normal: nivel bajo indicando operacion normal.
"""
    try:
        t_inicio = __import__("time").time()
        respuesta = modelo.generate_content(
            limpiar_texto(prompt),
            generation_config={"temperature": 0.0, "max_output_tokens": 4096}
        )
        t_fin = __import__("time").time()
        try:
            guardar_registro_costos_sms(
                modulo="insights",
                cliente_id=cliente_id,
                fase="10_2_insights",
                descripcion="Generacion insights rentabilidad y finanzas",
                modelo=MODELO_CHAT,
                tiempo=t_fin - t_inicio,
                tokens_in=respuesta.usage_metadata.prompt_token_count,
                tokens_out=respuesta.usage_metadata.candidates_token_count
            )
        except Exception:
            pass
        texto = respuesta.text.strip().replace("```json", "").replace("```", "").strip()
        resultado = json.loads(texto)
        guardar_y_limpiar(f"fase10_2_{cliente_id}_{fecha_corte}.json", resultado)
        return resultado
    except Exception as e:
        print(f"[SMS_AI] Error generando insights 10.2: {e}")
        return {"error": str(e)}

# -----------------------------------------------------------------------------
# FASE 10.3 - CONSUMO POR USUARIOS
# Identifica top usuarios por volumen, anomalias de consumo y comportamiento inusual.
# -----------------------------------------------------------------------------

def generar_sql_fase10_3(cliente_id: int, fecha_corte: str) -> dict:
    modelo = obtener_modelo(sql=True)
    prompt = f"""
{SCHEMA_DB}
{INSTRUCCION_SQL_BASE}

OBJETIVO: Generar SQL para analizar consumo por usuario del cliente {cliente_id} en fecha {fecha_corte}.

Genera queries para obtener:
1. Top 10 usuarios con mas SMS enviados ese dia: incluye usuario_id, email, total_sms_dia, campanas_creadas_dia.
2. Promedio de SMS enviados por usuario activo ese dia.
3. Usuarios que superaron 3 veces el promedio grupal ese dia (posibles anomalias).
4. Comparativa del mes actual vs mes anterior por usuario: usuario_id, email, total_mes_actual, total_mes_anterior.
5. Usuarios que crearon campanas ese dia pero tienen 0 mensajes entregados (posible problema tecnico).

Usa cliente_id={cliente_id} y fecha_corte="{fecha_corte}" en el campo parametros.
"""
    try:
        t_inicio = __import__("time").time()
        respuesta = modelo.generate_content(
            limpiar_texto(prompt),
            generation_config={"temperature": 0.0, "max_output_tokens": 4096}
        )
        t_fin = __import__("time").time()
        try:
            guardar_registro_costos_sms(
                modulo="insights",
                cliente_id=cliente_id,
                fase="10_3_sql",
                descripcion="Generacion SQL consumo por usuarios",
                modelo=MODELO_SQL,
                tiempo=t_fin - t_inicio,
                tokens_in=respuesta.usage_metadata.prompt_token_count,
                tokens_out=respuesta.usage_metadata.candidates_token_count
            )
        except Exception:
            pass
        return parsear_json_sql_robusto(respuesta.text)
    except Exception as e:
        print(f"[SMS_AI] Error generando SQL 10.3: {e}")
        return {"error": str(e)}

def generar_insights_fase10_3(datos_query: dict, cliente_id: int, fecha_corte: str) -> dict:
    modelo = obtener_modelo()
    prompt = f"""
Eres un analista de consumo y comportamiento de usuarios SMS. Analiza los datos y genera un reporte detallado.

DATOS DEL DIA {fecha_corte} PARA CLIENTE {cliente_id}:
{json.dumps(datos_query, ensure_ascii=False, indent=2)}

Devuelve UNICAMENTE este JSON valido en espanol, sin texto adicional:
{{
  "fecha_analisis": "{fecha_corte}",
  "cliente_id": {cliente_id},
  "modulo": "consumo_usuarios",
  "resumen": "Una frase corta del comportamiento de usuarios del dia",
  "total_usuarios_activos_hoy": 0,
  "promedio_sms_por_usuario": 0.0,
  "top_usuarios": [
    {{
      "usuario_id": 0,
      "email": "",
      "sms_enviados_hoy": 0,
      "campanas_creadas_hoy": 0,
      "es_anomalia": false
    }}
  ],
  "usuarios_anomalos": [
    {{
      "usuario_id": 0,
      "email": "",
      "sms_enviados": 0,
      "veces_sobre_promedio": 0.0,
      "posible_causa": "Descripcion sencilla de posible causa"
    }}
  ],
  "usuarios_sin_entregas": [
    {{
      "usuario_id": 0,
      "email": "",
      "sms_enviados": 0,
      "sms_entregados": 0
    }}
  ],
  "alertas": [
    {{
      "nivel": "alto | medio | bajo",
      "mensaje": "Descripcion sencilla de la alerta",
      "dato_relevante": "El dato clave"
    }}
  ],
  "recomendaciones": [
    "Recomendacion 1 en lenguaje sencillo"
  ]
}}

REGLAS PARA ALERTAS:
- Usuario supera 5 veces el promedio del grupo: nivel alto.
- Usuario supera 3 veces el promedio: nivel medio.
- Un usuario con mas del 30% del total de SMS del dia: nivel medio.
- Usuarios con campanas sin entregas: nivel medio.
- Todo normal: nivel bajo indicando operacion normal.
- En posible_causa usa frases como "muchas campanas ese dia" o "posible reenvio masivo".
"""
    try:
        t_inicio = __import__("time").time()
        respuesta = modelo.generate_content(
            limpiar_texto(prompt),
            generation_config={"temperature": 0.0, "max_output_tokens": 4096}
        )
        t_fin = __import__("time").time()
        try:
            guardar_registro_costos_sms(
                modulo="insights",
                cliente_id=cliente_id,
                fase="10_3_insights",
                descripcion="Generacion insights consumo por usuarios",
                modelo=MODELO_CHAT,
                tiempo=t_fin - t_inicio,
                tokens_in=respuesta.usage_metadata.prompt_token_count,
                tokens_out=respuesta.usage_metadata.candidates_token_count
            )
        except Exception:
            pass
        texto = respuesta.text.strip().replace("```json", "").replace("```", "").strip()
        resultado = json.loads(texto)
        guardar_y_limpiar(f"fase10_3_{cliente_id}_{fecha_corte}.json", resultado)
        return resultado
    except Exception as e:
        print(f"[SMS_AI] Error generando insights 10.3: {e}")
        return {"error": str(e)}

# -----------------------------------------------------------------------------
# FASE 10.4 - SEGURIDAD Y ALERTAS
# Detecta fallos masivos, actividad fuera de horario, webhooks caidos y patrones de riesgo.
# -----------------------------------------------------------------------------

def generar_sql_fase10_4(cliente_id: int, fecha_corte: str) -> dict:
    modelo = obtener_modelo(sql=True)
    prompt = f"""
{SCHEMA_DB}
{INSTRUCCION_SQL_BASE}

OBJETIVO: Generar SQL para detectar patrones de seguridad y riesgo del cliente {cliente_id} en fecha {fecha_corte}.

Genera queries para obtener:
1. SMS fallidos agrupados por motivo_fallo ese dia: motivo_fallo, cantidad, porcentaje_del_total_fallidos. Filtra con DATE(d.enviado_en) = :fecha_corte y UPPER(d.estatus) IN ('FALLIDO','RECHAZADO').
2. Numeros destino con mas de 3 intentos fallidos ese dia: numero, total_intentos_fallidos. Filtra con DATE(d.enviado_en) = :fecha_corte y UPPER(d.estatus) IN ('FALLIDO','RECHAZADO').
3. Campanas con tasa de fallo mayor al 30% ese dia: campana_id, nombre, total, fallidos, tasa_fallo_calculada. Filtra con DATE(programada_para) = :fecha_corte y total > 0.
4. Webhooks fallidos del dia desde dlq_webhooks PERO SOLO los que pertenecen a este cliente. La tabla dlq_webhooks NO tiene cliente_id, asi que debes vincularla por el message_id que viene dentro del payload JSONB contra detalle_envios.message_id, y de ahi a campanas.cliente_id. Usa exactamente esta forma: SELECT w.id, w.error, w.intentos FROM dlq_webhooks w JOIN detalle_envios de ON de.message_id = (w.payload->>'message_id') JOIN campanas c ON de.campana_id = c.id WHERE c.cliente_id = :cliente_id AND DATE(w.creado_en) = :fecha_corte. Nombra el query "webhooks_fallidos".
5. Numeros agregados a blacklist en los ultimos 7 dias: telefono, comentario, activo, expira_en, creado_en. Filtra con cliente_id = :cliente_id AND creado_en >= NOW() - INTERVAL '7 days'.
6. Campanas ejecutadas fuera del horario laboral 07:00-22:00 ese dia: campana_id, nombre, EXTRACT(HOUR FROM programada_para) AS hora_creacion, usuario_id. Filtra con DATE(programada_para) = :fecha_corte AND (EXTRACT(HOUR FROM programada_para) < 7 OR EXTRACT(HOUR FROM programada_para) >= 22).

REGLA OBLIGATORIA DE ESTATUS (NO LA OMITAS):
- La columna detalle_envios.estatus guarda los valores en MAYUSCULAS: 'ENVIADO', 'ENTREGADO', 'FALLIDO', 'RECHAZADO'.
- Para comparar SIEMPRE usa UPPER(d.estatus) en lugar de comparar el valor crudo.
- Para fallidos usa UPPER(d.estatus) IN ('FALLIDO','RECHAZADO').
- NUNCA compares contra minusculas: devolveria 0 filas contra la base real.

Usa cliente_id={cliente_id} y fecha_corte="{fecha_corte}" en el campo parametros.
"""
    try:
        t_inicio = __import__("time").time()
        respuesta = modelo.generate_content(
            limpiar_texto(prompt),
            generation_config={"temperature": 0.0, "max_output_tokens": 4096}
        )
        t_fin = __import__("time").time()
        try:
            guardar_registro_costos_sms(
                modulo="insights",
                cliente_id=cliente_id,
                fase="10_4_sql",
                descripcion="Generacion SQL seguridad y alertas",
                modelo=MODELO_SQL,
                tiempo=t_fin - t_inicio,
                tokens_in=respuesta.usage_metadata.prompt_token_count,
                tokens_out=respuesta.usage_metadata.candidates_token_count
            )
        except Exception:
            pass
        return parsear_json_sql_robusto(respuesta.text)
    except Exception as e:
        print(f"[SMS_AI] Error generando SQL 10.4: {e}")
        return {"error": str(e)}

def generar_insights_fase10_4(datos_query: dict, cliente_id: int, fecha_corte: str) -> dict:
    modelo = obtener_modelo()
    prompt = f"""
Eres un analista de seguridad de sistemas SMS. Analiza los datos y genera un reporte de seguridad y riesgos.

DATOS DEL DIA {fecha_corte} PARA CLIENTE {cliente_id}:
{json.dumps(datos_query, ensure_ascii=False, indent=2)}

Devuelve UNICAMENTE este JSON valido en espanol, sin texto adicional:
{{
  "fecha_analisis": "{fecha_corte}",
  "cliente_id": {cliente_id},
  "modulo": "seguridad_alertas",
  "resumen": "Una frase corta del estado de seguridad del dia",
  "nivel_riesgo_general": "alto | medio | bajo",
  "fallos_por_tipo": [
    {{
      "tipo_fallo": "",
      "cantidad": 0,
      "porcentaje_del_total_fallidos": 0.0
    }}
  ],
  "numeros_problematicos": [
    {{
      "numero": "",
      "intentos_fallidos": 0,
      "posible_causa": "Descripcion sencilla"
    }}
  ],
  "campanas_con_alto_fallo": [
    {{
      "campana_id": 0,
      "nombre_campana": "",
      "tasa_fallo_porcentaje": 0.0,
      "total_mensajes": 0
    }}
  ],
  "webhooks_fallidos_hoy": 0,
  "numeros_nuevos_en_blacklist_7_dias": 0,
  "actividad_fuera_horario": [
    {{
      "usuario_id": 0,
      "hora_actividad": "",
      "campana_nombre": ""
    }}
  ],
  "alertas": [
    {{
      "nivel": "alto | medio | bajo",
      "mensaje": "Descripcion sencilla de la alerta",
      "dato_relevante": "El dato clave"
    }}
  ],
  "recomendaciones": [
    "Recomendacion 1 en lenguaje sencillo"
  ]
}}

REGLAS PARA ALERTAS:
- Campana con tasa de fallo mayor al 50%: nivel alto.
- Campana con tasa de fallo entre 30% y 50%: nivel medio.
- Numeros con mas de 10 intentos fallidos: nivel medio.
- Actividad fuera de horario laboral: nivel medio.
- Webhooks fallidos acumulados: nivel medio.
- Mas de 10 numeros nuevos en blacklist en la semana: nivel medio.
- Todo normal: nivel bajo.
- En posible_causa usa frases como "numero apagado o sin servicio", "operador bloqueo el mensaje".
"""
    try:
        t_inicio = __import__("time").time()
        respuesta = modelo.generate_content(
            limpiar_texto(prompt),
            generation_config={"temperature": 0.0, "max_output_tokens": 4096}
        )
        t_fin = __import__("time").time()
        try:
            guardar_registro_costos_sms(
                modulo="insights",
                cliente_id=cliente_id,
                fase="10_4_insights",
                descripcion="Generacion insights seguridad y alertas",
                modelo=MODELO_CHAT,
                tiempo=t_fin - t_inicio,
                tokens_in=respuesta.usage_metadata.prompt_token_count,
                tokens_out=respuesta.usage_metadata.candidates_token_count
            )
        except Exception:
            pass
        texto = respuesta.text.strip().replace("```json", "").replace("```", "").strip()
        resultado = json.loads(texto)
        guardar_y_limpiar(f"fase10_4_{cliente_id}_{fecha_corte}.json", resultado)
        return resultado
    except Exception as e:
        print(f"[SMS_AI] Error generando insights 10.4: {e}")
        return {"error": str(e)}

# =============================================================================
# FUNCIONES PUBLICAS (llamadas desde api_categorizador.py)
# =============================================================================

# ---------- CHATBOT FASE 10 ----------

def iniciar_sesion_chat(session_id: str, datos_sesion: dict):
    # Registra una nueva sesion de chat con el contexto del usuario autenticado
    with lock_sesiones:
        SESIONES_CHAT[session_id] = {
            "contexto": datos_sesion,
            "historial": []
        }
    print(f"[SMS_AI] Sesion iniciada: {session_id}")

def cerrar_sesion_chat(session_id: str):
    # Elimina la sesion de la memoria RAM liberando el historial y contexto
    with lock_sesiones:
        if session_id in SESIONES_CHAT:
            del SESIONES_CHAT[session_id]
            print(f"[SMS_AI] Sesion cerrada: {session_id}")

def bienvenida_chat(session_id: str) -> dict:
    # Genera el mensaje inicial de bienvenida con sugerencias de preguntas
    with lock_sesiones:
        if session_id not in SESIONES_CHAT:
            return {"error": "Sesion no encontrada. Llama primero a iniciar_sesion."}
        contexto = SESIONES_CHAT[session_id]["contexto"]

    modelo = obtener_modelo()
    glosario = leer_glosario()
    texto = generar_bienvenida_sms(modelo, glosario, contexto)
    return {"session_id": session_id, "bienvenida": texto}

def responder_chat(session_id: str, pregunta: str) -> dict:
    # Primera pasada: determina si necesita DB o responde directo.
    # Si necesita DB devuelve necesita_db=True con los queries para que
    # el frontend los ejecute y llame a responder_chat_con_datos.
    with lock_sesiones:
        if session_id not in SESIONES_CHAT:
            return {"error": "Sesion no encontrada. Llama primero a iniciar_sesion."}
        sesion = SESIONES_CHAT[session_id]

    # El modelo se selecciona internamente segun si necesita DB o no
    glosario = leer_glosario()
    contexto = sesion["contexto"]
    historial = sesion["historial"]

    resultado = generar_respuesta_chat_sms(None, glosario, contexto, historial, pregunta)

    if not resultado.get("necesita_db", False):
        # Respuesta directa sin DB
        respuesta_texto = resultado.get("respuesta", "<p>Sin respuesta.</p>")
        with lock_sesiones:
            SESIONES_CHAT[session_id]["historial"].append({"q": pregunta, "a": respuesta_texto})
            if len(SESIONES_CHAT[session_id]["historial"]) > 15:
                SESIONES_CHAT[session_id]["historial"].pop(0)
        return {
            "session_id": session_id,
            "necesita_db": False,
            "respuesta": respuesta_texto,
            "turnos_en_historial": len(SESIONES_CHAT[session_id]["historial"])
        }
    else:
        # Necesita DB: devuelve los queries al frontend sin guardar en historial todavia
        return {
            "session_id": session_id,
            "necesita_db": True,
            "descripcion": resultado.get("descripcion", ""),
            "queries": resultado.get("queries", [])
        }


def responder_chat_con_datos(session_id: str, pregunta: str, datos_db: dict) -> dict:
    # Segunda pasada: recibe los datos de la DB ejecutados por el backend
    # y genera la respuesta final personalizada para el usuario.
    with lock_sesiones:
        if session_id not in SESIONES_CHAT:
            return {"error": "Sesion no encontrada."}
        sesion = SESIONES_CHAT[session_id]

    modelo = obtener_modelo()
    glosario = leer_glosario()
    contexto = sesion["contexto"]
    historial = sesion["historial"]

    respuesta_texto = generar_respuesta_chat_con_datos(
        modelo, glosario, contexto, historial, pregunta, datos_db
    )

    with lock_sesiones:
        SESIONES_CHAT[session_id]["historial"].append({"q": pregunta, "a": respuesta_texto})
        if len(SESIONES_CHAT[session_id]["historial"]) > 15:
            SESIONES_CHAT[session_id]["historial"].pop(0)

    return {
        "session_id": session_id,
        "necesita_db": False,
        "respuesta": respuesta_texto,
        "turnos_en_historial": len(SESIONES_CHAT[session_id]["historial"])
    }

# ---------- INSIGHTS FASES 10.1 - 10.4 ----------

GENERADORES_SQL = {
    "10_1": generar_sql_fase10_1,
    "10_2": generar_sql_fase10_2,
    "10_3": generar_sql_fase10_3,
    "10_4": generar_sql_fase10_4,
}

GENERADORES_INSIGHTS = {
    "10_1": generar_insights_fase10_1,
    "10_2": generar_insights_fase10_2,
    "10_3": generar_insights_fase10_3,
    "10_4": generar_insights_fase10_4,
}

def solicitar_sql_insight(fase: str, cliente_id: int, fecha_corte: str) -> dict:
    # Genera y devuelve el SQL directamente sin crear job_id.
    # El backend ejecuta el SQL y manda los resultados a procesar_resultado_insight.
    if fase not in GENERADORES_SQL:
        return {"error": f"Fase {fase} no reconocida. Fases validas: 10_1, 10_2, 10_3, 10_4"}

    sql_generado = GENERADORES_SQL[fase](cliente_id, fecha_corte)

    if "error" in sql_generado:
        return {"error": sql_generado["error"]}

    # Registra el contexto necesario para el paso 2 usando un id basado en fase+cliente+fecha
    # para que el backend no tenga que manejar un job_id dinamico
    clave = f"{fase}_{cliente_id}_{fecha_corte}"
    with lock_trabajos:
        TRABAJOS_INSIGHT[clave] = {
            "fase": fase,
            "cliente_id": cliente_id,
            "fecha_corte": fecha_corte,
            "estado": "esperando_datos"
        }

    print(f"[SMS_AI] SQL generado | Fase {fase} | Cliente {cliente_id} | Fecha {fecha_corte}")
    return {"clave": clave, "sql": sql_generado}


def procesar_resultado_insight(clave: str, datos_resultado: dict) -> dict:
    # Recibe el JSON crudo que devolvio PostgreSQL y genera los insights con Gemini.
    with lock_trabajos:
        if clave not in TRABAJOS_INSIGHT:
            return {"error": f"Clave {clave} no encontrada. Llama primero a solicitar_sql_insight."}
        trabajo = TRABAJOS_INSIGHT[clave]

    fase = trabajo["fase"]
    cliente_id = trabajo["cliente_id"]
    fecha_corte = trabajo["fecha_corte"]

    try:
        insights = GENERADORES_INSIGHTS[fase](datos_resultado, cliente_id, fecha_corte)

        with lock_trabajos:
            TRABAJOS_INSIGHT[clave]["estado"] = "completado"

        return insights

    except Exception as e:
        with lock_trabajos:
            TRABAJOS_INSIGHT[clave]["estado"] = "error"
        return {"error": str(e)}

def obtener_estado_trabajo(job_id: str) -> dict:
    # Devuelve el estado actual de un job de insight
    with lock_trabajos:
        if job_id not in TRABAJOS_INSIGHT:
            return {"error": "job_id no encontrado"}
        return dict(TRABAJOS_INSIGHT[job_id])
    
# -----------------------------------------------------------------------------
# REGISTRO DE COSTOS SMS
# -----------------------------------------------------------------------------

import csv as _csv_sms
from datetime import datetime as _dt_sms

RUTA_CSV_SMS = Path("/home/endless/FUNCIONALIDADES/PROYECTO EXTRACTOR") / "registro_costos_sms.csv"

_CABECERA_CSV_SMS = [
    "Fecha_Hora",
    "Cliente_ID",
    "Usuario_ID",
    "Modulo",
    "Fase",
    "Descripcion",
    "Modelo",
    "Tiempo_Respuesta",
    "Input_Tokens",
    "Output_Tokens",
    "Costo_Input",
    "Costo_Output",
    "Costo_Total_Operacion"
]

def guardar_registro_costos_sms(
    modulo: str,
    cliente_id: int,
    fase: str,
    descripcion: str,
    modelo: str,
    tiempo: float,
    tokens_in: int,
    tokens_out: int,
    usuario_id: int = 0
):
    # Registra en CSV el consumo de tokens y costo de cada llamada a Gemini del modulo SMS
    existe = RUTA_CSV_SMS.exists()
    fecha_hora = _dt_sms.now().strftime("%Y-%m-%d %H:%M:%S")

    # Precios: $0.50 input / $3.00 output por millon de tokens
    costo_in = (tokens_in / 1_000_000) * 0.50
    costo_out = (tokens_out / 1_000_000) * 3.00
    costo_total = costo_in + costo_out

    try:
        with open(RUTA_CSV_SMS, mode="a", newline="", encoding="utf-8") as f:
            writer = _csv_sms.writer(f)
            if not existe:
                writer.writerow(_CABECERA_CSV_SMS)
            writer.writerow([
                fecha_hora,
                cliente_id,
                usuario_id,
                modulo,
                fase,
                descripcion,
                modelo,
                f"{tiempo:.2f} s",
                tokens_in,
                tokens_out,
                f"${costo_in:.6f}",
                f"${costo_out:.6f}",
                f"${costo_total:.6f}"
            ])
        print(f"[SMS_AI] Costo registrado | {fase} | Cliente {cliente_id} | Usuario {usuario_id} | ${costo_total:.6f} USD")
    except Exception as e:
        print(f"[SMS_AI] Error guardando CSV de costos: {e}")