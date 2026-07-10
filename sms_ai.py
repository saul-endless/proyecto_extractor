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
    genai.configure(api_key=API_KEY)
    modelo = MODELO_SQL if sql else MODELO_CHAT
    return genai.GenerativeModel(
        modelo,
        generation_config=genai.types.GenerationConfig(
            candidate_count=1,
        )
    )

def limpiar_texto(texto: str) -> str:
    # Elimina caracteres invalidos de UTF-8 para evitar errores al enviar al modelo
    if not texto:
        return ""
    return texto.encode("utf-8", "ignore").decode("utf-8")

def parsear_json_sql_robusto(texto: str) -> dict:
    texto = texto.strip().replace("```json", "").replace("```", "").strip()
    inicio = texto.find("{")
    fin = texto.rfind("}") + 1
    if inicio != -1 and fin > inicio:
        texto = texto[inicio:fin]

    # Intento 1: parseo directo (caso normal)
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        pass

    # Intento 2: colapsa saltos de linea reales SOLO dentro de literales string
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
    try:
        return json.loads(saneado)
    except json.JSONDecodeError:
        pass

    # Intento 3: extrae cada valor del campo "sql" con regex y lo sanea individualmente
    # Cubre el caso donde Gemini metio saltos de linea Y comillas mixtas que confunden el parser
    import re
    def limpiar_valor_sql(match):
        contenido = match.group(1)
        # Reemplaza saltos de linea reales por espacio
        contenido = contenido.replace('\n', ' ').replace('\r', ' ').replace('\t', ' ')
        # Escapa comillas dobles no escapadas dentro del valor
        contenido = re.sub(r'(?<!\\)"', '\\"', contenido)
        return f'"sql": "{contenido}"'

    texto_regex = re.sub(
        r'"sql"\s*:\s*"(.*?)"(?=\s*[,}])',
        limpiar_valor_sql,
        texto,
        flags=re.DOTALL
    )
    try:
        return json.loads(texto_regex)
    except json.JSONDecodeError:
        pass

    # Intento 4: sanea TODO el JSON colapsando saltos de linea dentro de strings,
    # luego reintenta json.loads. Mas robusto que la extraccion posicional para CTEs.
    def _colapsar_saltos_en_strings(s: str) -> str:
        out = []
        dentro = False
        esc = False
        for ch in s:
            if esc:
                out.append(ch)
                esc = False
                continue
            if ch == "\\":
                out.append(ch)
                esc = True
                continue
            if ch == '"':
                dentro = not dentro
                out.append(ch)
                continue
            if dentro and ch in ("\n", "\r", "\t"):
                out.append(" ")
                continue
            out.append(ch)
        return "".join(out)

    try:
        return json.loads(_colapsar_saltos_en_strings(texto))
    except json.JSONDecodeError:
        pass

    # Intento 5: extraccion posicional tolerante a CTEs, comillas internas y SQL multilinea.
    queries = []
    array_match = re.search(r'"queries"\s*:\s*\[', texto)
    if array_match:
        i = array_match.end()
        n = len(texto)
        while i < n:
            # Avanza hasta el siguiente objeto o el cierre del array
            while i < n and texto[i] not in ("{", "]"):
                i += 1
            if i >= n or texto[i] == "]":
                break
            # Recorre el objeto contando llaves SOLO fuera de strings
            obj_start = i
            depth_obj = 0
            j = i
            dentro_str = False
            esc = False
            while j < n:
                ch = texto[j]
                if esc:
                    esc = False
                    j += 1
                    continue
                if ch == "\\":
                    esc = True
                    j += 1
                    continue
                if ch == '"':
                    dentro_str = not dentro_str
                    j += 1
                    continue
                if not dentro_str:
                    if ch == "{":
                        depth_obj += 1
                    elif ch == "}":
                        depth_obj -= 1
                        if depth_obj == 0:
                            j += 1
                            break
                j += 1
            obj_str = texto[obj_start:j]

            nombre_m = re.search(r'"nombre"\s*:\s*"([^"]*)"', obj_str)
            # SQL: desde "sql":" hasta la comilla de cierre seguida de , o } (no escapada)
            sql_m = re.search(r'"sql"\s*:\s*"(.*?)(?<!\\)"(?=\s*[,}])', obj_str, re.DOTALL)
            params_m = re.search(r'"parametros"\s*:\s*(\{.*?\})', obj_str, re.DOTALL)
            if sql_m:
                sql_raw = sql_m.group(1).replace('\n', ' ').replace('\r', ' ').replace('\t', ' ')
                sql_raw = re.sub(r'\s+', ' ', sql_raw).strip()
                try:
                    params_txt = params_m.group(1).replace('\n', ' ').replace('\r', ' ') if params_m else "{}"
                    params = json.loads(params_txt)
                except Exception:
                    params = {}
                queries.append({
                    "nombre": nombre_m.group(1) if nombre_m else f"consulta_{len(queries)+1}",
                    "sql": sql_raw,
                    "parametros": params
                })
            i = j

    if queries:
        print(f"[SMS_AI] Parser intento 5: recuperados {len(queries)} queries via extraccion posicional tolerante")
        return {"queries": queries}

    raise ValueError(f"No se pudo parsear el JSON de Gemini tras 5 intentos. Primeros 200 chars: {texto[:200]}")

def guardar_y_limpiar(nombre_archivo: str, datos: dict):
    # Guarda el resultado en disco y lo elimina solo si el guardado fue exitoso
    ruta = RUTA_OUTPUT / nombre_archivo
    guardado_ok = False
    try:
        with open(ruta, "w", encoding="utf-8") as f:
            json.dump(datos, f, indent=4, ensure_ascii=False)
        guardado_ok = True
        print(f"[SMS_AI] Archivo guardado: {ruta.name}")
    except Exception as e:
        print(f"[SMS_AI] Error guardando archivo: {e}")
    if guardado_ok:
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
    nombre_usuario = contexto_sesion.get("nombre_usuario", "usuario")
    rol = contexto_sesion.get("rol", "")
    canales = contexto_sesion.get("canales", [])
    canales_texto = ", ".join([c.get("nombre", "") for c in canales]) if canales else "SMS"

    regla_habilitaciones = """
REGLA CRITICA — HABILITACIONES:
Si al consultar la base de datos notas que el módulo o la métrica solicitada NO está habilitada para el cliente (por ejemplo, `modulo_fortigate_activo` es false, o en `graficas_activas` la métrica aparece como false):
- NO consultes más datos ni digas "sin datos".
- Responde directamente con un mensaje amigable: explica que esa función no está activada en su cuenta actualmente y sugiere levantar un ticket.
- Ejemplo: <p>La métrica de <b>[Nombre de la Métrica]</b> no está habilitada en tu cuenta en este momento. Si deseas activarla, puedes <b>levantar un ticket</b> desde el menú de soporte y el equipo de Endless Innovation la configurará para ti.</p>
"""

    return f"""
Eres Milo Analytics, el asistente del portal de Endless Innovation.
Tu funcion es ayudar al usuario a entender y usar el portal, que incluye DOS modulos principales:
1. MODULO SMS: campanas, mensajes, contactos, creditos, blacklist, reportes de mensajes.
2. MODULO FORTINET: monitoreo de red, metricas de CPU, RAM, sesiones, ancho de banda, estado del enlace, alertas de seguridad, blacklist de sitios web.

DATOS DEL USUARIO:
- Nombre: {nombre_usuario}
- Rol: {rol}
- Canales disponibles: {canales_texto}

{regla_habilitaciones}

INSTRUCCIONES DE IDENTIDAD Y TONO:
1. Responde siempre en espanol, de forma clara y directa como si hablaras con alguien que no es tecnico.
2. Usa palabras simples y comunes. Evita terminos rebuscados o tecnicos innecesarios.
3. Puedes responder preguntas relacionadas con el portal SMS Y con el modulo Fortinet segun el glosario.
4. Si te preguntan algo completamente ajeno al portal (clima, noticias, temas personales), responde:
   <p>Solo puedo ayudarte con temas del portal de Endless Innovation.</p>
5. No inventes funciones, pantallas ni campos que no existan en el glosario.
6. Cuando el usuario pregunte como hacer algo, explica los pasos en orden con lista numerada.
7. Si el usuario reporta un problema, orientalo segun el glosario y sugierele levantar un ticket si no puede resolverlo.
8. No puedes modificar, crear ni eliminar ningun dato del sistema. Solo orientas al usuario.
9. Se breve pero completo. Si solo saluda, responde cordialmente en maximo 2 lineas. Cuando respondas con datos de la base de datos, SIEMPRE empieza con un parrafo <p> que responda directamente la pregunta antes de mostrar cualquier tabla o lista.
10. Preguntas sobre CPU, RAM, sesiones, ancho de banda, alertas, enlace de red o trafico
    SON parte del portal — corresponden al modulo Fortinet. Consulta la DB para responderlas.

FORMATO DE SALIDA OBLIGATORIO (igual al sistema CFO):

1. PARA TEXTO Y PARRAFOS usa HTML:
   - Usa <p>...</p> para parrafos.
   - Usa <b>...</b> para resaltar terminos importantes o nombres de campos.
   - Ejemplo: <p>Para crear una <b>campana</b>, sigue estos pasos:</p>

2. PARA LISTAS usa HTML:
   - Usa <ol><li>...</li></ol> para pasos numerados.
   - Usa <ul><li>...</li></ul> para listas sin orden.

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
    # Genera el mensaje de bienvenida con sugerencias variadas (SMS y Red/Internet)
    nombre = contexto_sesion.get("nombre_usuario", "usuario")

    prompt = f"""
Eres Milo Analytics, el asistente integral del portal de Endless Innovation (Módulos SMS y Fortinet).
El usuario que acaba de entrar se llama {nombre}.

TAREA: Saludalo y sugiere de 3 a 5 preguntas concretas y muy variadas que podria hacerte sobre el uso del sistema y el estado de sus servicios.

GLOSARIO DE APOYO:
{glosario}

REGLAS ESTRICTAS:
1. Empieza EXACTAMENTE con: "Hola {nombre}, soy Milo Analytics, tu asistente del Portal de Cliente. Puedo ayudarte con cosas como:"
2. Lista las preguntas como <ul><li>...</li></ul> en HTML.
3. No respondas las preguntas ahora, solo sugierelas.
4. Cero analisis, cero LaTeX, solo HTML sencillo.
5. DIVERSIDAD OBLIGATORIA: Debes incluir una mezcla de temas. Incluye al menos una pregunta sobre envíos SMS (ej. "¿Cómo creo una campaña de mensajes?") y OBLIGATORIAMENTE incluye preguntas sobre el estado de su red o internet (ej. "¿Cómo está mi conexión a internet hoy?", "¿Cuál es el consumo de mi ancho de banda?", "¿Tengo alertas de seguridad en mi red?").
"""
    try:
        t_inicio = __import__("time").time()
        respuesta = modelo.generate_content(
            limpiar_texto(prompt),
            generation_config={"temperature": 0.1, "max_output_tokens": 2048},
            request_options={"timeout": 240}
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
        return f"<p>Hola {nombre}, soy Milo Analytics. En que puedo ayudarte hoy con el portal SMS?</p>"

def _decidir_necesidad_db(modelo_flash, glosario: str, contexto_sesion: dict, historial: list, pregunta: str) -> dict:
    # Flash decide unicamente si necesita DB o no. Si no necesita, responde directo.
    # Si necesita, devuelve solo necesita_db=true con descripcion pero SIN queries.
    prompt_sistema = construir_prompt_sistema_sms(glosario, contexto_sesion)

    memoria = ""
    for h in historial:
        memoria += f"Usuario: {h['q']}\nMilo Analytics: {h['a']}\n"

    prompt_decision = f"""
{prompt_sistema}

--- HISTORIAL DE CONVERSACION ---
{memoria if memoria else "Sin historial previo."}

--- PREGUNTA ACTUAL DEL USUARIO ---
{pregunta}

--- INSTRUCCION DE DECISION (PRIORIDAD MAXIMA) ---
Antes de responder, decide si necesitas consultar la base de datos del portal SMS para dar una respuesta personalizada y precisa.

NECESITAS LA BASE DE DATOS cuando el usuario pregunta por:
- Metricas de su red Fortinet: CPU, RAM, sesiones, ancho de banda, alertas, estado del enlace,
  trafico, caidas, picos — SIEMPRE requieren consultar la DB aunque no mencionen "Fortinet" explicitamente.
- Preguntas sobre el "Estado de la Red", "estado del enlace", "como esta mi red", "hay problemas
  de conexion", "el internet esta bien" — SIEMPRE son preguntas de Fortinet, SIEMPRE necesitan DB.
- Cualquier pregunta que incluya palabras como: red, internet, conexion, enlace, router, firewall,
  FortiGate, Fortinet, ancho de banda, latencia, paquetes — SIEMPRE necesitan DB.
- Sus creditos disponibles o consumo real
- Rentabilidad, finanzas, gasto o desperdicio de creditos, costo por usuario, o
  proyeccion de cuando se le acabara el saldo — SIEMPRE necesitan DB. Este es un
  tema PROPIO del modulo SMS (creditos/consumo), NUNCA lo consideres ajeno al
  portal aunque la palabra "rentabilidad" o "finanzas" no aparezca en el glosario.
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
 - Preguntas sobre CUALQUIER métrica de red o servicio Fortinet (estado de conexion, ancho de banda, latencia, uptime, CPU, RAM, sesiones, alertas, eventos, blacklist de sitios, consumo por usuario, consumo por aplicacion): SIEMPRE necesitan DB. El sistema DEBE consultar primero la tabla dashboard_configs para saber si esa metrica especifica esta activada (campo graficas_activas) antes de responder, aunque el modulo global este activo. Nunca respondas una metrica de red sin pasar por la validacion de habilitaciones.

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

    import time as _time
    ultimo_error = None
    respuesta = None
    for _intento in range(2):
        try:
            t_inicio = _time.time()
            respuesta = modelo_flash.generate_content(
                limpiar_texto(prompt_decision),
                generation_config={
                    "temperature": 0.1,
                    "max_output_tokens": 8192,
                    "response_mime_type": "application/json"
                },
                request_options={"timeout": 240}
            )
            t_fin = _time.time()

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
            ultimo_error = e
            print(f"[SMS_AI] ERROR intento {_intento + 1} en _decidir_necesidad_db: {type(e).__name__}: {e}")
            try:
                print(f"[SMS_AI] Texto raw de Gemini que fallo el parse:\n{respuesta.text[:500]}")
            except Exception:
                pass
            if _intento == 0:
                _time.sleep(5)

    return {"necesita_db": False, "respuesta": f"<p>Error generando respuesta tras 2 intentos: {ultimo_error}</p>"}


SCHEMA_RESPUESTA_SQL_CHAT = {
    "type": "object",
    "properties": {
        "necesita_db": {"type": "boolean"},
        "descripcion": {"type": "string"},
        "queries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "nombre": {"type": "string"},
                    "sql": {"type": "string"},
                    "parametros": {
                        "type": "object",
                        "properties": {
                            "cliente_id": {"type": "integer"},
                            "usuario_id": {"type": "integer"}
                        }
                    }
                },
                "required": ["nombre", "sql"]
            }
        }
    },
    "required": ["queries"]
}


def _generar_sql_chatbot(modelo_pro, glosario: str, contexto_sesion: dict, historial: list, pregunta: str, descripcion_consulta: str) -> dict:
    # Pro genera el SQL preciso cuando Flash confirmo que se necesita DB.
    prompt_sistema = construir_prompt_sistema_sms(glosario, contexto_sesion)

    memoria = ""
    for h in historial:
        memoria += f"Usuario: {h['q']}\nMilo Analytics: {h['a']}\n"

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
- REGLA CRITICA ORDER BY (PostgreSQL): NUNCA uses alias de columnas dentro de operaciones o expresiones en el ORDER BY. Ejemplo INVALIDO que rompe la consulta: ORDER BY (megabytes_descargados + megabytes_subidos) DESC. Opciones VALIDAS: usar la posicion de la columna (ORDER BY 2 DESC), usar un alias solo sin operaciones (ORDER BY megabytes_descargados DESC), o repetir la expresion completa del SELECT dentro del ORDER BY.
- La columna detalle_envios.estatus esta en MAYUSCULAS ('ENVIADO','ENTREGADO','FALLIDO','RECHAZADO','EXPIRED'); compara SIEMPRE con UPPER(estatus) y valores en mayusculas para no obtener 0 filas
- BÚSQUEDAS DE TEXTO (NOMBRES): Al buscar nombres de sitios Fortinet, campañas o usuarios, NUNCA uses coincidencia exacta (= 'Nombre'). Usa SIEMPRE "ILIKE '%palabra%'" para ignorar mayúsculas/minúsculas y permitir búsquedas parciales. Si hay varias palabras, sepáralas: "nombre ILIKE '%Santa Fe%' AND nombre ILIKE '%Lifestyle%'".
- Si el usuario pregunta por datos de su propio usuario agrega AND usuario_id = :usuario_id donde aplique
- Para preguntas de RECOMENDACION PERSONALIZADA genera estos queries:
  1. Ultimas 10 campanas del cliente con: nombre, total, enviados, fallidos, entregados, estado, programada_para
  2. Saldo actual de creditos
  3. Promedio de tamano de campanas (AVG de total) y promedio de tasa de entrega (AVG de entregados/total*100)
- Para preguntas de RENTABILIDAD Y FINANZAS (ej. "rentabilidad y finanzas de hoy") genera estos queries:
  1. Saldo actual de creditos por canal: "SELECT cr.saldo, ca.nombre AS canal FROM creditos cr JOIN canales ca ON ca.id = cr.canal_id WHERE cr.cliente_id = :cliente_id"
  2. Totales del dia (usa DATE(de.enviado_en) = CURRENT_DATE si dice "hoy", o ajusta el rango si el usuario pide otro periodo): "SELECT COUNT(*) FILTER (WHERE UPPER(de.estatus) = 'ENVIADO') AS enviados, COUNT(*) FILTER (WHERE UPPER(de.estatus) = 'ENTREGADO') AS entregados, COUNT(*) FILTER (WHERE UPPER(de.estatus) IN ('FALLIDO','RECHAZADO','EXPIRED')) AS fallidos FROM detalle_envios de JOIN campanas c ON de.campana_id = c.id WHERE c.cliente_id = :cliente_id AND DATE(de.enviado_en) = CURRENT_DATE"
  3. Creditos consumidos hoy agrupados por campana: nombre, total, enviados, fallidos, entregados, filtrando DATE(de.enviado_en) = CURRENT_DATE
- PARA VALIDAR HABILITACIONES: OBLIGATORIAMENTE, si el usuario pregunta por métricas de Fortinet o servicios (estado de conexion, ancho de banda, latencia, uptime, CPU, alertas, eventos, blacklist, consumo por usuario, consumo por aplicacion), incluye SIEMPRE este query EXACTO con el nombre "validacion_habilitaciones". Agrega como columna booleana cada metrica usando OR sobre TODAS las configuraciones del cliente (scope cliente y scope site), de modo que una metrica cuenta como ACTIVA si esta en true en al menos una configuracion:
  "sql": "SELECT c.modulo_fortigate_activo, bool_or(COALESCE((d.graficas_activas->>'conexion_status')::boolean, false)) AS conexion_status, bool_or(COALESCE((d.graficas_activas->>'bandwidth')::boolean, false)) AS bandwidth, bool_or(COALESCE((d.graficas_activas->>'latency')::boolean, false)) AS latency, bool_or(COALESCE((d.graficas_activas->>'uptime')::boolean, false)) AS uptime, bool_or(COALESCE((d.graficas_activas->>'cpu')::boolean, false)) AS cpu, bool_or(COALESCE((d.graficas_activas->>'alertas')::boolean, false)) AS alertas, bool_or(COALESCE((d.graficas_activas->>'eventos')::boolean, false)) AS eventos, bool_or(COALESCE((d.graficas_activas->>'blacklist')::boolean, false)) AS blacklist, bool_or(COALESCE((d.graficas_activas->>'consumo_usuario')::boolean, false)) AS consumo_usuario, bool_or(COALESCE((d.graficas_activas->>'consumo_aplicacion')::boolean, false)) AS consumo_aplicacion, COUNT(d.id) AS total_configs FROM clientes c LEFT JOIN dashboard_configs d ON d.cliente_id = c.id AND d.activo = true WHERE c.id = :cliente_id GROUP BY c.modulo_fortigate_activo"
- MAPEO de lo que pregunta el usuario a la columna de validacion: estado de conexion/enlace -> conexion_status; ancho de banda/trafico/mbps -> bandwidth; latencia -> latency; uptime/disponibilidad -> uptime; cpu/procesador -> cpu; alertas -> alertas; eventos -> eventos; sitios bloqueados/blacklist fortinet -> blacklist; consumo por usuario -> consumo_usuario; consumo por aplicacion -> consumo_aplicacion.
- REGLA DE PROTECCIÓN ANCHO DE BANDA / TRAFICO / MBPS: Si el usuario pregunta por el ancho de banda general, tráfico de red o consumo en Mbps de la red/enlace (como en la gráfica de snapshots), el modelo DEBE consultar la tabla 'fortigate_status_snapshots' y NO las vistas de usuarios. Como 'rx_bytes' y 'tx_bytes' son contadores acumulativos dentro de 'payload->'_portal_metrics'->'interface'', para obtener el consumo real del periodo sin causar errores de división por cero, selecciona el valor máximo menos el mínimo agrupado por sitio con la siguiente sintaxis exacta: SELECT s.nombre, (MAX((snap.payload->'_portal_metrics'->'interface'->>'rx_bytes')::bigint) - MIN((snap.payload->'_portal_metrics'->'interface'->>'rx_bytes')::bigint)) / 1048576.0 AS megabytes_descargados, (MAX((snap.payload->'_portal_metrics'->'interface'->>'tx_bytes')::bigint) - MIN((snap.payload->'_portal_metrics'->'interface'->>'tx_bytes')::bigint)) / 1048576.0 AS megabytes_subidos FROM fortigate_status_snapshots snap JOIN fortigate_sites s ON snap.site_id = s.id WHERE s.cliente_id = :cliente_id AND snap.tomado_en >= NOW() - INTERVAL '3 hours' GROUP BY s.nombre; Usa las tablas o vistas de 'consumo_usuario' SÓLO si el usuario menciona explícitamente palabras como 'usuarios', 'dispositivos conectados' o 'consumo por persona'.
"""

    import time as _time
    ultimo_error = None
    respuesta = None
    for _intento in range(2):
        try:
            t_inicio = _time.time()
            respuesta = modelo_pro.generate_content(
                limpiar_texto(prompt_sql),
                generation_config={
                    "temperature": 0.0,
                    "max_output_tokens": 8192,
                    "response_mime_type": "application/json",
                    "response_schema": SCHEMA_RESPUESTA_SQL_CHAT
                },
                request_options={"timeout": 240}
            )
            t_fin = _time.time()

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

            resultado_parseado = parsear_json_sql_robusto(respuesta.text)

            # El schema no obliga a necesita_db; lo forzamos aqui para que
            # responder_chat siempre reconozca que hay queries que ejecutar.
            if isinstance(resultado_parseado, dict) and "queries" in resultado_parseado:
                resultado_parseado["necesita_db"] = True
                if not resultado_parseado.get("descripcion"):
                    resultado_parseado["descripcion"] = descripcion_consulta

            if isinstance(resultado_parseado, dict) and "queries" in resultado_parseado:
                cliente_id_contexto = contexto_sesion.get("cliente_id", 0)
                for query in resultado_parseado["queries"]:
                    if "parametros" not in query or not isinstance(query["parametros"], dict):
                        query["parametros"] = {}
                    query["parametros"]["cliente_id"] = cliente_id_contexto
            
            return resultado_parseado

        except Exception as e:
            ultimo_error = e
            print(f"[SMS_AI] ERROR intento {_intento + 1} en _generar_sql_chatbot: {type(e).__name__}: {e}")
            try:
                print(f"[SMS_AI] Texto raw de Gemini que fallo el parse:\n{respuesta.text[:500]}")
            except Exception:
                pass
            if _intento == 0:
                _time.sleep(5)

    return {"necesita_db": False, "respuesta": f"<p>Error generando SQL tras 2 intentos: {ultimo_error}</p>"}


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
        memoria += f"Usuario: {h['q']}\nMilo Analytics: {h['a']}\n"

    prompt_final = f"""
{prompt_sistema}

--- HISTORIAL DE CONVERSACION ---
{memoria if memoria else "Sin historial previo."}

--- PREGUNTA DEL USUARIO ---
{pregunta}

--- DATOS REALES DE LA BASE DE DATOS (usa estos para responder) ---
{json.dumps(datos_db, ensure_ascii=False, indent=2)}

=== REGLA DE VALIDACIÓN DE HABILITACIONES (PRIORIDAD MÁXIMA) ===
Trabaja con la consulta llamada "validacion_habilitaciones" si existe en los datos.

PASO 1 — MODULO GLOBAL:
Busca el campo `modulo_fortigate_activo`.
- Si es FALSE o no existe: el cliente NO contrató el módulo de red.
  Responde EXCLUSIVAMENTE con:
  "<p>El módulo de <b>Red y Seguridad</b> no está habilitado en tu cuenta en este momento. Si deseas activarlo, puedes <b>levantar un ticket</b> desde el menú de soporte y el equipo de Endless Innovation lo configurará para ti.</p>"
  No agregues nada más.

PASO 2 — MÉTRICA ESPECÍFICA (solo si modulo_fortigate_activo es TRUE):
Identifica qué métrica preguntó el usuario y revisa su columna booleana en "validacion_habilitaciones":
  estado de conexion -> conexion_status | ancho de banda -> bandwidth | latencia -> latency |
  uptime -> uptime | cpu -> cpu | alertas -> alertas | eventos -> eventos |
  blacklist fortinet -> blacklist | consumo por usuario -> consumo_usuario |
  consumo por aplicacion -> consumo_aplicacion

- Si total_configs es 0 (el cliente no tiene ninguna configuración de dashboard):
  Trata TODAS las métricas como NO activas. Responde:
  "<p>La métrica de <b>[Nombre de la Métrica]</b> aún no está configurada en tu cuenta. Para activarla puedes <b>levantar un ticket</b> desde el menú de soporte y el equipo de Endless Innovation la habilitará para ti.</p>"

- Si la columna de esa métrica es FALSE: la métrica está apagada (en gris) para este cliente.
  Responde EXCLUSIVAMENTE con (NO muestres datos aunque vengan en otras consultas):
  "<p>La métrica de <b>[Nombre de la Métrica]</b> no está activada en tu cuenta en este momento. Si deseas activarla, puedes <b>levantar un ticket</b> desde el menú de soporte y el equipo de Endless Innovation la configurará para ti.</p>"

- Si la columna de esa métrica es TRUE: la métrica está activa. Continúa al PASO 3.

PASO 3 — DATOS (solo si la métrica está activa):
- Si las consultas devolvieron datos válidos: preséntalos directamente, sin ninguna advertencia de habilitación.
- Si las consultas devolvieron filas vacías o sin valores numéricos:
  Di que aún no hay registros suficientes en el sistema para esa métrica y sugiere esperar a que se acumulen más datos o levantar un ticket para revisión.
  NUNCA digas que "no está activada" en este caso: la razón es falta de datos, no falta de permiso.
===============================================================

Responde ahora la pregunta del usuario siguiendo las reglas.
Aplica el formato HTML+LaTeX segun las reglas del sistema.
No menciones que consultaste una base de datos ni validaste permisos, solo da la respuesta natural y personalizada.

=== REGLA ABSOLUTA DE ESTRUCTURA (NO NEGOCIABLE) ===

Tu respuesta DEBE comenzar OBLIGATORIAMENTE con un parrafo <p>...</p> que responda
de forma directa, concreta y en una sola oracion lo que el usuario pregunto.

ESTE PARRAFO ES OBLIGATORIO INCLUSO CUANDO LA RESPUESTA INCLUYE TABLAS.
Si tu respuesta va a tener una tabla, el parrafo va ANTES que la tabla, siempre.
Si tu respuesta es solo texto, el parrafo es la respuesta completa.

COMO DEBE SER EL PARRAFO (ejemplos):
- Si preguntaron por el mejor horario de subida: "<p>El mejor horario para subir archivos es a las <b>2:00 AM</b>, cuando el uso de CPU es solo del <b>0.32%</b> y el ancho de banda esta practicamente libre.</p>"
- Si preguntaron cuantos mensajes se enviaron: "<p>En el periodo analizado se enviaron <b>12,450 mensajes</b>, de los cuales el <b>87%</b> fueron entregados exitosamente.</p>"
- Si preguntaron quien consume mas: "<p>El usuario con mayor consumo es <b>Carlos Lopez</b>, con <b>3,200 SMS</b> enviados en el periodo.</p>"

PROHIBIDO comenzar la respuesta con una tabla ($$), una lista (<ul>, <ol>) o un encabezado.
El primer caracter de tu respuesta debe ser el simbolo menor que < de una etiqueta <p>.

ESTRUCTURA EN ORDEN EXACTO:
1. <p> Parrafo con la respuesta directa y concreta (SIEMPRE PRIMERO, con el dato clave en <b></b>) </p>
2. Tablas en LaTeX ($$...$$) solo si hay datos numericos complementarios que mostrar
3. Listas HTML (<ul> o <ol>) solo si hay puntos adicionales relevantes

=== FIN DE REGLA ABSOLUTA ===
"""

    try:
        t_inicio = __import__("time").time()
        respuesta = modelo.generate_content(
            limpiar_texto(prompt_final),
            generation_config={"temperature": 0.1, "max_output_tokens": 8192},
            request_options={"timeout": 240}
        )
        t_fin = __import__("time").time()

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

        texto_final = limpiar_texto(respuesta.text)
        
        # LOG: imprime en consola el texto generado para depuracion
        print(f"\n[SMS_AI][LOG RESPUESTA FINAL] ----- INICIO -----\n{texto_final}\n[SMS_AI][LOG RESPUESTA FINAL] ----- FIN -----\n")
        
        return texto_final

    except Exception as e:
        return f"<p>Error generando respuesta final: {e}</p>"

# =============================================================================
# FASES 10.1 A 10.4 - INSIGHTS DIARIOS
# Flujo de cada fase:
#   Paso 1: solicitar_sql_insight  -> Gemini genera los queries SQL necesarios
#   Paso 2: procesar_resultado_insight -> Backend manda los datos, Gemini genera insights
# Los archivos de resultado se guardan en output_sms y se eliminan tras el envio.
# =============================================================================

SCHEMA_RESPUESTA_SQL = {
    "type": "object",
    "properties": {
        "queries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "nombre": {"type": "string"},
                    "sql": {"type": "string"},
                    "parametros": {
                        "type": "object",
                        "properties": {
                            "cliente_id": {"type": "integer"},
                            "fecha_corte": {"type": "string"},
                            "fecha_inicio": {"type": "string"},
                            "usuario_id": {"type": "integer"}
                        }
                    }
                },
                "required": ["nombre", "sql"]
            }
        }
    },
    "required": ["queries"]
}

# Dias por defecto que analiza cada fase hacia atras desde fecha_corte.
# El Node ajusta este rango al registro mas antiguo si el cliente tiene menos historial.
DIAS_RANGO_DEFECTO = 30

def _rango_parametros(cliente_id: int, fecha_corte: str) -> dict:
    # Devuelve los parametros de rango que usaran los queries de cada fase.
    # fecha_inicio se calcula como fecha_corte - DIAS_RANGO_DEFECTO dias.
    # NOTA: el ajuste final al registro mas antiguo lo aplica el Node antes de ejecutar.
    return {
        "cliente_id": cliente_id,
        "fecha_corte": fecha_corte,
        "dias_rango": DIAS_RANGO_DEFECTO
    }

# Schema de la base de datos para que Gemini genere SQL correcto
SCHEMA_DB = """
TABLAS DISPONIBLES (PostgreSQL):

--- MODULO SMS ---

clientes(id, nombre, rfc, email, telefono, activo, modulo_fortigate_activo, creado_en, actualizado_en)
  -- modulo_fortigate_activo: boolean indica si el cliente tiene Fortinet habilitado

usuarios(id, cliente_id FK->clientes.id, rol_id FK->roles.id, nombre, email, password_hash, mensaje_limite bigint, activo, creado_en, actualizado_en)
  -- mensaje_limite: maximo de SMS que puede enviar este usuario (NULL = sin limite)

usuario_limites(usuario_id FK->usuarios.id, mensaje_limite bigint, actualizado_en)
  -- tabla auxiliar de limites, usar usuarios.mensaje_limite como fuente principal

roles(id, nombre varchar(40))
  -- valores tipicos: 'admin', 'operador'

grupos_usuario(id, cliente_id FK->clientes.id, nombre varchar(80), activo, creado_en, actualizado_en)

usuario_grupos(usuario_id FK->usuarios.id, grupo_id FK->grupos_usuario.id, creado_en)
  -- relacion muchos a muchos usuarios-grupos

canales(id, codigo varchar(20), nombre varchar(60), activo)
  -- ejemplo: codigo='SMS', nombre='Mensajes SMS'

creditos(id, cliente_id FK->clientes.id, canal_id FK->canales.id, saldo bigint, actualizado_en)
  -- saldo: creditos SMS disponibles para ese cliente en ese canal

contactos(id, cliente_id FK->clientes.id, nombre varchar(120), telefono varchar(25), email varchar(120), lista varchar(80), activo, creado_en, actualizado_en)
  -- lista: nombre de la lista a la que pertenece el contacto (texto libre)

listas_contactos(id, cliente_id FK->clientes.id, nombre varchar(80), activo, creado_en, actualizado_en)

blacklist_contactos(id, cliente_id FK->clientes.id, telefono varchar(25), comentario text, expira_en date, activo, creado_en, actualizado_en)
  -- expira_en NULL = bloqueo permanente

campanas(id, cliente_id FK->clientes.id, usuario_id FK->usuarios.id, nombre varchar(120), mensaje text, total int, enviados int, fallidos int, entregados int, estado varchar(20), programada_para timestamptz, programacion_inicio timestamptz, programacion_fin timestamptz, metadata jsonb, creado_en timestamptz, finalizado_en timestamptz, actualizado_en timestamptz)
  -- estado valores: 'PENDIENTE', 'EN_PROCESO', 'COMPLETADA', 'FALLIDA', 'PROGRAMADA'
  -- pendientes (no es columna): se calcula como total - enviados - fallidos
  -- programada_para: fecha/hora de ejecucion de la campana

detalle_envios(id bigint, campana_id FK->campanas.id, numero varchar(20), message_id varchar(80), estatus varchar(20), operadora varchar(40), dlr_recibido timestamptz, variables jsonb, mensaje_renderizado text, motivo_fallo text, intento_envio int, enviado_en timestamptz, creado_en timestamptz)
  -- CRITICO: estatus siempre en MAYUSCULAS. Valores REALES en esta base:
  -- 'ENVIADO': mensaje enviado al operador, pendiente de confirmacion
  -- 'ENTREGADO': confirmado como recibido por el dispositivo (DLR positivo)
  -- 'FALLIDO': error en el envio
  -- 'RECHAZADO': rechazado por el operador
  -- 'EXPIRED': el mensaje expiro sin confirmacion de entrega (muy comun)
  -- Para contar mensajes NO entregados usar: UPPER(estatus) IN ('FALLIDO','RECHAZADO','EXPIRED')
  -- Para contar mensajes exitosos usar: UPPER(estatus) IN ('ENVIADO','ENTREGADO')
  -- usar SIEMPRE UPPER(estatus) al comparar para evitar 0 filas
  -- enviado_en: timestamp real del envio (usar para filtrar por fecha, NO creado_en)
  -- variables: datos personalizados de campanas con plantilla (jsonb)
  -- mensaje_renderizado: texto final del SMS despues de reemplazar variables

plantillas_campana(id bigint, cliente_id FK->clientes.id, usuario_id FK->usuarios.id, nombre varchar(120), columnas jsonb, activo, creado_en, actualizado_en)
  -- columnas: array JSON con los nombres de las columnas de la plantilla

refresh_tokens(id, usuario_id FK->usuarios.id, token_hash varchar(255), expira_en timestamptz, creado_en)

dlq_webhooks(id bigint, payload jsonb, error text, intentos int, creado_en timestamptz)
  -- payload contiene message_id para vincular con detalle_envios
  -- NO tiene cliente_id directo; vincular via: dlq_webhooks -> detalle_envios.message_id -> campanas.cliente_id

--- MODULO FORTINET ---

fortigate_sites(id bigint, cliente_id FK->clientes.id, nombre varchar(120), base_url varchar(255), serial varchar(80), modelo varchar(80), api_username varchar(80), modulo_fortigate_activo bool, activo bool, metadata jsonb, last_status_ok bool, last_status_http_code int, last_status_at timestamptz, last_error text, creado_en timestamptz, actualizado_en timestamptz)
  -- cada site es un dispositivo FortiGate fisico del cliente
  -- last_status_ok: true si el ultimo polling fue exitoso

fortigate_status_snapshots(id bigint, site_id FK->fortigate_sites.id, http_code int, payload jsonb, fuente varchar(20), tomado_en timestamptz)
  -- TABLA PRINCIPAL DE METRICAS: CPU, RAM, ancho de banda, uptime, latencia, etc.
  -- fuente: 'live' o 'historico'
  -- TODAS las metricas estan dentro del campo payload (JSONB), no como columnas directas
  -- ESTRUCTURA REAL DEL PAYLOAD (results SIEMPRE esta vacio, ignorarlo):
  -- Las metricas estan en _portal_metrics y resource_usage dentro del payload
  --
  -- CPU actual (%):
  --   (payload->'_portal_metrics'->'resource_usage'->'cpu'->0->>'current')::int
  --   RUTA ALTERNATIVA si la anterior devuelve NULL:
  --   (payload->'resource_usage'->'cpu'->0->>'current')::int
  --   USAR COALESCE para cubrir ambas rutas:
  --   COALESCE(
  --     (payload->'_portal_metrics'->'resource_usage'->'cpu'->0->>'current')::int,
  --     (payload->'resource_usage'->'cpu'->0->>'current')::int
  --   ) AS cpu_actual
  --
  -- RAM actual (%):
  --   COALESCE(
  --     (payload->'_portal_metrics'->'resource_usage'->'mem'->0->>'current')::int,
  --     (payload->'resource_usage'->'mem'->0->>'current')::int
  --   ) AS ram_actual
  --
  -- Sesiones activas:
  --   COALESCE(
  --     (payload->'_portal_metrics'->'resource_usage'->'session'->0->>'current')::int,
  --     (payload->'resource_usage'->'session'->0->>'current')::int
  --   ) AS sesiones_activas
  --
  -- Estado del enlace WAN (true=arriba, false=caido):
  --   (payload->'_portal_metrics'->'interface'->>'link')::boolean
  --
  -- Ancho de banda - bytes recibidos (bajada):
  --   (payload->'_portal_metrics'->'interface'->>'rx_bytes')::bigint
  --
  -- Ancho de banda - bytes enviados (subida):
  --   (payload->'_portal_metrics'->'interface'->>'tx_bytes')::bigint
  --
  -- IP de la interfaz WAN:
  --   payload->'_portal_metrics'->'interface'->>'ip'
  --
  -- NOTA: latencia y perdida de paquetes NO estan en el payload actual.
  --   health_check y link_monitor son null en todos los registros actuales.
  --   Para latencia usar diferencias de tx/rx_bytes entre snapshots consecutivos.
  --
  -- Historial de CPU (array de [timestamp_ms, valor]):
  --   payload->'resource_usage'->'cpu'->0->'historical'->'1-min'->'values'
  --   payload->'resource_usage'->'cpu'->0->'historical'->'1-hour'->'values'
  --   payload->'resource_usage'->'cpu'->0->'historical'->'24-hour'->'values'
  --
  -- Para filtrar por site del cliente:
  --   WHERE site_id IN (SELECT id FROM fortigate_sites WHERE cliente_id = :cliente_id)
  -- tomado_en: timestamp del snapshot almacenado en UTC (usar para filtrar por fecha/hora)
  -- CRITICO ZONA HORARIA: la DB guarda tomado_en en UTC. El cliente esta en America/Mexico_City (UTC-6).
  -- Para filtrar "ultima hora" usar: tomado_en >= NOW() - INTERVAL '1 hour'
  -- Para mostrar horas al usuario SIEMPRE convertir: tomado_en AT TIME ZONE 'America/Mexico_City'
  -- NUNCA mostrar el timestamp UTC crudo al usuario, siempre convertir antes de mostrar.
  -- Ejemplo correcto: SELECT (tomado_en AT TIME ZONE 'America/Mexico_City') AS hora_local ...

fortigate_alert_events(id bigint, site_id FK->fortigate_sites.id, payload jsonb, event_time timestamptz, creado_en timestamptz, event_key varchar(128))
  -- alertas y eventos registrados por el FortiGate
  -- CAMPOS REALES DEL PAYLOAD (verificados en DB):
  --   payload->>'action'       : accion del evento (ej: 'forward')
  --   payload->>'status'       : estado del evento (ej: 'error', 'success')
  --   payload->>'http_status'  : codigo HTTP de la respuesta (ej: 404, 200)
  --   payload->>'name'         : nombre del tipo de evento (ej: 'traffic')
  --   payload->>'path'         : modulo del FortiGate (ej: 'disk', 'system')
  --   payload->>'vdom'         : dominio virtual (ej: 'root')
  --   payload->>'serial'       : serial del dispositivo
  --   payload->>'_isBlacklist' : boolean, true si es evento de blacklist
  --   payload->>'_eventKey'    : clave unica del evento
  -- NOTA: los campos srcip, dstip y app NO existen en los registros actuales
  -- Para consultar alertas usar: action, status, http_status, name, path
  -- event_key: identificador unico del tipo de evento

fortigate_alert_ack(id bigint, user_id FK->usuarios.id, site_id FK->fortigate_sites.id, event_key varchar(128), acknowledged_at timestamptz)
  -- registro de alertas que el usuario ya confirmo/leyo

fortigate_blacklist_rules(id bigint, cliente_id FK->clientes.id, tipo varchar(20), valor varchar(255), categoria varchar(120), scope varchar(20), rule_hash varchar(128), comentario text, metadata jsonb, creado_por FK->usuarios.id, activo bool, creado_en timestamptz, actualizado_en timestamptz)
  -- reglas de bloqueo de URLs/IPs en FortiGate
  -- tipo: 'domain' o 'category' (valores exactos segun constraint v8)
  -- scope: 'all_sites' o 'by_site' (OJO: era 'specific', v8 lo cambio a 'by_site')
  -- valor: la URL, dominio o categoria bloqueada

fortigate_blacklist_jobs(id bigint, cliente_id FK->clientes.id, rule_id FK->fortigate_blacklist_rules.id, scope varchar(20), site_ids jsonb, estado varchar(20), requested_by FK->usuarios.id, error_text text, creado_en timestamptz, iniciado_en timestamptz, finalizado_en timestamptz)
  -- jobs de aplicacion de reglas blacklist a los dispositivos fisicos
  -- estado: 'pending', 'running', 'done', 'error'

fortigate_blacklist_job_results(id bigint, job_id FK->fortigate_blacklist_jobs.id, site_id FK->fortigate_sites.id, estado varchar(20), detalle_error text, request_path text, request_payload jsonb, response_code int, response_payload jsonb, aplicado_en timestamptz, creado_en timestamptz, actualizado_en timestamptz)
  -- resultado por sitio de cada job de blacklist

fortigate_site_tokens(id bigint, site_id FK->fortigate_sites.id, token_ciphertext text, token_fingerprint varchar(128), version int, activo bool, creado_en timestamptz, actualizado_en timestamptz)
  -- tokens de autenticacion cifrados para cada sitio FortiGate (NO usar en queries de negocio)

REGLAS CRITICAS PARA GENERAR SQL:
- Filtra SIEMPRE por cliente_id = :cliente_id en tablas SMS.
- Para Fortinet filtra por site_id IN (SELECT id FROM fortigate_sites WHERE cliente_id = :cliente_id AND activo = true).
- Solo SELECT. Nunca INSERT, UPDATE, DELETE, DROP ni ningun DDL.
- Para el dia de corte usa: DATE(columna_fecha) = :fecha_corte
- Para el mes actual: DATE_TRUNC('month', columna_fecha) = DATE_TRUNC('month', :fecha_corte::date)
- Para el mes anterior: DATE_TRUNC('month', columna_fecha) = DATE_TRUNC('month', :fecha_corte::date) - INTERVAL '1 month'
- Usa parametros nombrados con : para todos los valores variables.
- detalle_envios: usar enviado_en para filtrar por fecha de envio, NO creado_en.
- campanas: usar programada_para para filtrar por fecha de ejecucion, NO creado_en.
- fortigate_status_snapshots: usar tomado_en para filtrar por fecha/hora.
- CRITICO estatus SMS: SIEMPRE UPPER(estatus) al comparar. Valores reales en DB: 'ENVIADO','ENTREGADO','FALLIDO','RECHAZADO','EXPIRED'. Para fallidos/no entregados usar IN ('FALLIDO','RECHAZADO','EXPIRED'). Para exitosos usar IN ('ENVIADO','ENTREGADO').
- pendientes en campanas NO es columna: calcular como (total - enviados - fallidos).
- Para metricas Fortinet del payload usar operadores JSONB (->>, ->, #>>).

--- TABLAS NUEVAS v8 ---

clientes ahora tiene columnas adicionales:
  parent_id INT (FK->clientes.id, NULL si es raiz)
  nivel INT (0=platform, 1=top_level, 2+=subempresa)
  tipo_cliente VARCHAR(40): 'platform' | 'top_level' | 'subempresa' | 'standard'

roles ahora incluye: 'superadmin_global', 'superadmin_grupo' ademas de 'admin','operador','solo_lectura'

permisos(id, codigo varchar(80), descripcion, creado_en)
  -- permisos granulares del sistema. Ejemplos de codigo: 'view:own_cliente', 'manage:usuarios', 'view:all_campanas'

rol_permisos(rol_id FK->roles.id, permiso_id FK->permisos.id)
  -- tabla pivot que asigna permisos a roles

tickets(id bigint, cliente_id FK->clientes.id, usuario_id FK->usuarios.id,
        nombre_reportador varchar(255), email_reportador varchar(255), telefono_reportador varchar(25),
        categoria varchar(120), subcategoria varchar(120), descripcion text,
        estado_local varchar(40): 'abierto'|'en_revision'|'resuelto'|'cerrado',
        odoo_ticket_id int, odoo_url varchar(500), sincronizado_en timestamptz, odoo_error text,
        cantidad_evidencias int, creado_en timestamptz, actualizado_en timestamptz)
  -- tickets de soporte levantados desde el portal

ticket_evidencias(id bigint, ticket_id FK->tickets.id, nombre_archivo varchar(255),
                  tipo_archivo varchar(80), tamaño_bytes bigint, ruta_almacenamiento varchar(500),
                  url_odoo varchar(500), creado_en timestamptz)
  -- archivos adjuntos de cada ticket

modulo_fortigate_consumo_usuario(id bigint, cliente_id FK->clientes.id, usuario_id FK->usuarios.id,
  dispositivo_ip varchar(45), sesion_inicio timestamptz, sesion_fin timestamptz,
  estado varchar(20): 'activo'|'cerrada'|'suspendida',
  bytes_subidos bigint, bytes_descargados bigint,
  paquetes_enviados bigint, paquetes_recibidos bigint,
  latencia_promedio_ms int, perdida_paquetes_promedio decimal(5,2),
  dispositivo_id varchar(100), dispositivo_tipo varchar(50), ubicacion varchar(255),
  creado_en timestamptz, actualizado_en timestamptz)
  -- bytes_subidos = trafico de subida de ese usuario en esa sesion
  -- bytes_descargados = trafico de bajada de ese usuario en esa sesion
  -- latencia_promedio_ms: columna existente en DB pero NO se usa en el portal, ignorar para consultas
  -- Filtrar siempre por cliente_id. Para "hoy" usar DATE(sesion_inicio) = CURRENT_DATE AT TIME ZONE 'America/Mexico_City'

modulo_fortigate_eventos_usuario(id bigint, consumo_id FK->modulo_fortigate_consumo_usuario.id,
  tipo_evento varchar(50): 'login'|'logout'|'limite_alcanzado'|'error',
  descripcion text, datos_adicionales jsonb, creado_en timestamptz)
  -- eventos asociados a cada sesion de consumo de usuario Fortinet

VISTAS DISPONIBLES (solo lectura, usar en SELECT directamente):
  v_consumo_hoy       : mb_descargados, mb_subidos, latencia_ms, dispositivos_conectados por usuario HOY
  v_consumo_mes       : gb_descargados, gb_subidos, sesiones_totales por usuario en el MES actual
  v_fortigate_consumo_usuario    : site_id, user_name, src_ip, event_time, bytes_up, bytes_down, duration_seconds — NO tiene cliente_id, filtrar con: site_id IN (SELECT id FROM fortigate_sites WHERE cliente_id = :cliente_id AND activo = true)
  v_fortigate_consumo_aplicacion : site_id, app_name, event_time, bytes_up, bytes_down — NO tiene cliente_id, filtrar con: site_id IN (SELECT id FROM fortigate_sites WHERE cliente_id = :cliente_id AND activo = true)

NOTA IMPORTANTE SOBRE LATENCIA:
  - La latencia en tiempo real NO existe como columna directa en fortigate_status_snapshots.
  - Si el usuario pregunta por latencia Y modulo_fortigate_activo=true, consulta la tabla
    modulo_fortigate_consumo_usuario usando la columna latencia_promedio_ms para obtener
    promedios historicos por sesion de usuario.
  - Si esa tabla no tiene registros, informa que no hay datos suficientes aun y sugiere
    esperar a que el sistema acumule mas sesiones o levantar un ticket.
  - NUNCA digas que la latencia "no esta habilitada" ni que "no existe" si modulo_fortigate_activo=true.
    Solo di que no hay registros suficientes si la consulta devuelve vacio.
"""

# -----------------------------------------------------------------------------
# UTILIDADES PARA DETECCION DE BASE VACIA
# -----------------------------------------------------------------------------

def _datos_tienen_registros(datos_query: dict) -> bool:
    """Detecta si los datos de la consulta contienen filas reales o estan todos vacios."""
    if datos_query.get("sin_datos"):
        return False
    consultas = datos_query.get("consultas", [])
    if not consultas:
        return False
    total_filas = sum(c.get("rowCount", 0) for c in consultas if isinstance(c, dict))
    return total_filas > 0


def _respuesta_sin_datos(fase: str, cliente_id: int, fecha_corte: str, mensaje: str = None) -> dict:
    """Genera una respuesta estandar de sin datos para mostrar al usuario."""
    nombres_fase = {
        "10_1": "Distribucion Geografica",
        "10_2": "Rentabilidad y Finanzas",
        "10_3": "Consumo por Usuarios",
        "10_4": "Seguridad y Alertas",
    }
    nombre = nombres_fase.get(fase, "Analisis")
    return {
        "fecha_analisis": fecha_corte,
        "cliente_id": cliente_id,
        "sin_datos": True,
        "mensaje_usuario": mensaje or (
            f"No se encontraron registros en la base de datos para el periodo {fecha_corte}. "
            f"Verifica que existan envios SMS o actividad de red en esa fecha."
        ),
        "modulo": nombre,
        "alertas": [],
        "recomendaciones": [
            f"Selecciona una fecha en la que hayas realizado envios para ver el analisis de {nombre}.",
            "Si acabas de empezar a usar el portal, los analisis estaran disponibles una vez que se registre actividad."
        ]
    }


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

REGLA 4 — EL CAMPO SQL ES UNA SOLA LINEA SIN EXCEPCION:
El valor del campo "sql" DEBE ser una cadena de texto continua en una sola linea.
ESTO ES CRITICO: cualquier salto de linea dentro del valor "sql" rompe el parser y pierde los datos del cliente.
CORRECTO:   "sql": "SELECT id, nombre FROM campanas WHERE cliente_id = :cliente_id AND DATE(enviado_en) = :fecha_corte"
INCORRECTO: "sql": "SELECT id, nombre\\nFROM campanas\\nWHERE cliente_id = :cliente_id"
INCORRECTO: "sql": "SELECT id, nombre
FROM campanas
WHERE cliente_id = :cliente_id"

Usa ESPACIOS para separar clausulas: SELECT col1, col2 FROM tabla JOIN tabla2 ON ... WHERE ... GROUP BY ... ORDER BY ... LIMIT ...
NUNCA uses saltos de linea, tabulaciones ni retornos de carro dentro del valor del campo "sql".
Si el SQL es largo, simplemente continua en la misma linea con espacios.

REGLA 5 — SQL VALIDO PARA POSTGRESQL:
- Solo SELECT. NUNCA INSERT, UPDATE, DELETE, DROP, ALTER, CREATE ni ningun DDL.
- Filtra SIEMPRE por cliente_id = :cliente_id
- Usa alias claros en cada columna del SELECT
- Parametros nombrados con : para todos los valores variables

REGLA 6 — VERIFICACION OBLIGATORIA ANTES DE RESPONDER:
Antes de enviar tu respuesta, verifica mentalmente cada campo "sql":
[ ] ¿Empieza con { ?
[ ] ¿Termina con } ?
[ ] ¿Cada campo "sql" es UNA SOLA linea continua sin ningun \\n, \\r ni tabulacion?
[ ] ¿No hay texto fuera del JSON?
[ ] ¿No hay bloques markdown ni comentarios?
[ ] ¿Cada parametro nombrado con : tiene su correspondiente entrada en "parametros"?
Si alguna verificacion falla, REESCRIBE el campo que fallo antes de responder.
RECUERDA: un solo salto de linea dentro de "sql" hace que el sistema pierda todos los datos del dia.
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

OBJETIVO: Generar los SQL para analizar la distribución geográfica de envíos SMS del cliente {cliente_id} en el periodo completo (desde :fecha_inicio hasta :fecha_corte).

Genera queries para obtener:
1. Conteo de envios agrupados por los primeros 3 caracteres del campo "numero" en detalle_envios en todo el periodo. Filtra con DATE(d.enviado_en) BETWEEN :fecha_inicio AND :fecha_corte. Incluye columnas: prefijo, total_envios, total_entregados, total_fallidos, total_sin_confirmacion.
2. Campanas que tuvieron envios en el periodo (JOIN con detalle_envios): c.nombre AS nombre_campana, c.total, c.enviados, c.fallidos, c.entregados. Filtra con DATE(de.enviado_en) BETWEEN :fecha_inicio AND :fecha_corte.
3. Total de numeros unicos que recibieron SMS en el periodo. Filtra con DATE(enviado_en) BETWEEN :fecha_inicio AND :fecha_corte.
4. Top 5 prefijos con mayor tasa de fallo en el periodo.
5. Tendencia diaria: total de envios agrupados por fecha en el periodo. Filtra con DATE(enviado_en) BETWEEN :fecha_inicio AND :fecha_corte.

REGLA OBLIGATORIA DE ESTATUS:
- La columna detalle_envios.estatus guarda los valores en MAYUSCULAS.
- Para "total_entregados" cuenta UPPER(d.estatus) = 'ENTREGADO'.
- Para "total_fallidos" cuenta UPPER(d.estatus) IN ('FALLIDO','RECHAZADO','EXPIRED').
- Para "total_sin_confirmacion" cuenta UPPER(d.estatus) = 'ENVIADO'.

RANGO DE FECHAS (CRITICO): TODOS los queries deben usar el rango completo con: DATE(columna_fecha) BETWEEN :fecha_inicio AND :fecha_corte.
Incluye SIEMPRE en parametros los tres: cliente_id, fecha_corte y fecha_inicio.

Usa cliente_id={cliente_id}, fecha_corte="{fecha_corte}" y fecha_inicio (lo inyecta el sistema) como valores en el campo parametros.
"""
    try:
        t_inicio = __import__("time").time()
        respuesta = modelo.generate_content(
            limpiar_texto(prompt),
            generation_config={
                "temperature": 0.0,
                "max_output_tokens": 8192,
                "response_mime_type": "application/json",
                "response_schema": SCHEMA_RESPUESTA_SQL
            },
            request_options={"timeout": 240}
        )
        t_fin = __import__("time").time()
        try:
            guardar_registro_costos_sms(modulo="insights", cliente_id=cliente_id, fase="10_1_sql", descripcion="Generacion SQL distribucion geografica", modelo=MODELO_SQL, tiempo=t_fin - t_inicio, tokens_in=respuesta.usage_metadata.prompt_token_count, tokens_out=respuesta.usage_metadata.candidates_token_count)
        except Exception:
            pass
        return parsear_json_sql_robusto(respuesta.text)
    except Exception as e:
        print(f"[SMS_AI] Error generando SQL 10.1: {e}")
        return {"error": str(e)}

def generar_insights_fase10_1(datos_query: dict, cliente_id: int, fecha_corte: str) -> dict:
    if not _datos_tienen_registros(datos_query):
        resultado = _respuesta_sin_datos("10_1", cliente_id, fecha_corte, datos_query.get("mensaje"))
        guardar_y_limpiar(f"fase10_1_{cliente_id}_{fecha_corte}.json", resultado)
        return resultado

    modelo = obtener_modelo()
    prompt = f"""
Eres un analista de operaciones SMS orientado a negocio. Analiza los datos de distribucion geografica del periodo y genera un reporte facil de entender para el usuario final. NO uses IDs de base de datos.
Extrae la fecha de inicio y fin desde los parametros de las consultas SQL incluidas en los datos para armar el 'periodo_analizado'.

DATOS DEL PERIODO PARA CLIENTE {cliente_id}:
{json.dumps(datos_query, ensure_ascii=False, indent=2)}

Devuelve UNICAMENTE este JSON valido en espanol:
{{
  "periodo_analizado": "DD/MM/YYYY al DD/MM/YYYY",
  "cliente_id": {cliente_id},
  "modulo": "distribucion_geografica",
  "resumen": "Resumen ejecutivo del comportamiento en el periodo, destacando si hay muchos mensajes atrapados sin confirmacion",
  "prefijos_detectados": [
    {{
      "prefijo": "",
      "pais_estimado": "",
      "total_envios_periodo": 0,
      "total_entregados": 0,
      "total_fallidos": 0,
      "total_sin_confirmacion": 0,
      "tasa_entrega_porcentaje": 0.0
    }}
  ],
  "estadisticas_generales": {{
    "total_envios_periodo": 0,
    "total_numeros_unicos": 0,
    "prefijos_distintos": 0,
    "prefijo_mayor_volumen": "",
    "prefijo_menor_tasa_entrega": ""
  }},
  "alertas": [
    {{
      "nivel": "alto | medio | bajo",
      "mensaje": "Alerta explicada de forma sencilla (ej. gran cantidad de mensajes en transito sin DLR)",
      "dato_relevante": "Dato clave sin jerga tecnica"
    }}
  ],
  "recomendaciones": [
    "Recomendacion de negocio clara y directa"
  ]
}}
"""
    try:
        t_inicio = __import__("time").time()
        respuesta = modelo.generate_content(
            limpiar_texto(prompt),
            generation_config={"temperature": 0.0, "max_output_tokens": 4096},
            request_options={"timeout": 240}
        )
        t_fin = __import__("time").time()
        try:
            guardar_registro_costos_sms(modulo="insights", cliente_id=cliente_id, fase="10_1_insights", descripcion="Generacion insights distribucion geografica", modelo=MODELO_CHAT, tiempo=t_fin - t_inicio, tokens_in=respuesta.usage_metadata.prompt_token_count, tokens_out=respuesta.usage_metadata.candidates_token_count)
        except Exception:
            pass
        
        texto = respuesta.text.strip().replace("```json", "").replace("```", "").strip()
        inicio = texto.find("{")
        fin = texto.rfind("}") + 1
        if inicio != -1 and fin > inicio:
            texto = texto[inicio:fin]

        try:
            resultado = json.loads(texto)
        except json.JSONDecodeError:
            saneado = []
            dentro = False
            esc = False
            for ch in texto:
                if esc:
                    saneado.append(ch); esc = False; continue
                if ch == "\\":
                    saneado.append(ch); esc = True; continue
                if ch == '"':
                    dentro = not dentro; saneado.append(ch); continue
                if dentro and ch in ("\n", "\r", "\t"):
                    saneado.append(" "); continue
                saneado.append(ch)
            resultado = json.loads("".join(saneado))

        guardar_y_limpiar(f"fase10_1_{cliente_id}_{fecha_corte}.json", resultado)
        return resultado
    except Exception as e:
        print(f"[SMS_AI] Error generando insights 10.1: {e}")
        try:
            print(f"[SMS_AI] Texto raw 10.1 (primeros 300 chars): {respuesta.text[:300]}")
        except Exception:
            pass
        resultado = _respuesta_sin_datos("10_1", cliente_id, fecha_corte)
        resultado["_error_tecnico"] = str(e)
        return resultado

# -----------------------------------------------------------------------------
# FASE 10.2 - RENTABILIDAD Y FINANZAS
# Analiza costos, desperdicio de creditos y proyeccion de agotamiento de saldo.
# -----------------------------------------------------------------------------

def generar_sql_fase10_2(cliente_id: int, fecha_corte: str) -> dict:
    modelo = obtener_modelo(sql=True)
    prompt = f"""
{SCHEMA_DB}
{INSTRUCCION_SQL_BASE}

OBJETIVO: Generar SQL para analizar rentabilidad y uso de creditos SMS del cliente {cliente_id} en el periodo (desde :fecha_inicio hasta :fecha_corte).

Genera queries para obtener:
1. Saldo actual de creditos por canal del cliente.
2. Total de SMS enviados, fallidos y entregados en todo el periodo. Filtra con DATE(de.enviado_en) BETWEEN :fecha_inicio AND :fecha_corte.
3. SMS con motivo_fallo que contenga palabras como "expir" o "timeout" en el periodo.
4. Promedio diario real de SMS enviados en el periodo (excluye dias en 0). Filtra con DATE(enviado_en) BETWEEN :fecha_inicio AND :fecha_corte.
5. Total de SMS enviados por usuario en el periodo. Haz JOIN con usuarios para obtener u.nombre y u.email. Filtra con DATE(de.enviado_en) BETWEEN :fecha_inicio AND :fecha_corte.
6. Total de creditos consumidos agrupados por fecha en el periodo para ver tendencia.

REGLA OBLIGATORIA DE ESTATUS:
- Usa COUNT(*) FILTER (WHERE UPPER(de.estatus) = 'ENVIADO') AS total_enviados
- Para fallidos incluye 'EXPIRED': COUNT(*) FILTER (WHERE UPPER(de.estatus) IN ('FALLIDO','RECHAZADO','EXPIRED'))
- Usa UPPER(de.estatus) SIEMPRE en mayusculas.

RANGO DE FECHAS (CRITICO): Usa SIEMPRE DATE(columna_fecha) BETWEEN :fecha_inicio AND :fecha_corte. No filtres solo por fecha_corte.
Incluye SIEMPRE en parametros los tres: cliente_id, fecha_corte y fecha_inicio.

Usa cliente_id={cliente_id}, fecha_corte="{fecha_corte}" y fecha_inicio (lo inyecta el sistema) en parametros.
"""
    try:
        t_inicio = __import__("time").time()
        respuesta = modelo.generate_content(
            limpiar_texto(prompt),
            generation_config={
                "temperature": 0.0,
                "max_output_tokens": 8192,
                "response_mime_type": "application/json",
                "response_schema": SCHEMA_RESPUESTA_SQL
            },
            request_options={"timeout": 240}
        )
        t_fin = __import__("time").time()
        try:
            guardar_registro_costos_sms(modulo="insights", cliente_id=cliente_id, fase="10_2_sql", descripcion="Generacion SQL rentabilidad y finanzas", modelo=MODELO_SQL, tiempo=t_fin - t_inicio, tokens_in=respuesta.usage_metadata.prompt_token_count, tokens_out=respuesta.usage_metadata.candidates_token_count)
        except Exception:
            pass
        return parsear_json_sql_robusto(respuesta.text)
    except Exception as e:
        print(f"[SMS_AI] Error generando SQL 10.2: {e}")
        return {"error": str(e)}

def generar_insights_fase10_2(datos_query: dict, cliente_id: int, fecha_corte: str) -> dict:
    if not _datos_tienen_registros(datos_query):
        resultado = _respuesta_sin_datos("10_2", cliente_id, fecha_corte, datos_query.get("mensaje"))
        guardar_y_limpiar(f"fase10_2_{cliente_id}_{fecha_corte}.json", resultado)
        return resultado

    modelo = obtener_modelo()
    prompt = f"""
Eres un analista financiero de operaciones SMS. Analiza los datos del periodo y genera un reporte en lenguaje amigable para el usuario.
Extrae la fecha de inicio y fin desde los parametros de las consultas SQL incluidas en los datos para armar el 'periodo_analizado'.

DATOS DEL PERIODO PARA CLIENTE {cliente_id}:
{json.dumps(datos_query, ensure_ascii=False, indent=2)}

Devuelve UNICAMENTE este JSON valido en espanol:
{{
  "periodo_analizado": "DD/MM/YYYY al DD/MM/YYYY",
  "cliente_id": {cliente_id},
  "modulo": "rentabilidad_finanzas",
  "resumen": "Frase corta del estado financiero en el periodo analizado",
  "saldo_actual_creditos": 0,
  "creditos_usados_periodo": 0,
  "creditos_desperdiciados_periodo": 0,
  "porcentaje_desperdicio": 0.0,
  "costo_promedio_por_usuario_periodo": 0.0,
  "proyeccion_agotamiento": {{
    "promedio_diario_uso": 0.0,
    "dias_restantes": 0,
    "fecha_estimada_agotamiento": "DD/MM/YYYY"
  }},
  "alertas": [
    {{
      "nivel": "alto | medio | bajo",
      "mensaje": "Descripcion sencilla",
      "dato_relevante": "Dato clave"
    }}
  ],
  "recomendaciones": [
    "Recomendacion clara para optimizar el saldo"
  ]
}}
"""
    try:
        t_inicio = __import__("time").time()
        respuesta = modelo.generate_content(
            limpiar_texto(prompt),
            generation_config={"temperature": 0.0, "max_output_tokens": 4096},
            request_options={"timeout": 240}
        )
        t_fin = __import__("time").time()
        try:
            guardar_registro_costos_sms(modulo="insights", cliente_id=cliente_id, fase="10_2_insights", descripcion="Generacion insights rentabilidad y finanzas", modelo=MODELO_CHAT, tiempo=t_fin - t_inicio, tokens_in=respuesta.usage_metadata.prompt_token_count, tokens_out=respuesta.usage_metadata.candidates_token_count)
        except Exception:
            pass
        texto = respuesta.text.strip().replace("```json", "").replace("```", "").strip()
        inicio = texto.find("{")
        fin = texto.rfind("}") + 1
        if inicio != -1 and fin > inicio:
            texto = texto[inicio:fin]

        try:
            resultado = json.loads(texto)
        except json.JSONDecodeError:
            saneado = []
            dentro = False
            esc = False
            for ch in texto:
                if esc:
                    saneado.append(ch); esc = False; continue
                if ch == "\\":
                    saneado.append(ch); esc = True; continue
                if ch == '"':
                    dentro = not dentro; saneado.append(ch); continue
                if dentro and ch in ("\n", "\r", "\t"):
                    saneado.append(" "); continue
                saneado.append(ch)
            resultado = json.loads("".join(saneado))

        guardar_y_limpiar(f"fase10_2_{cliente_id}_{fecha_corte}.json", resultado)
        return resultado
    except Exception as e:
        print(f"[SMS_AI] Error generando insights 10.2: {e}")
        try:
            print(f"[SMS_AI] Texto raw 10.2 (primeros 300 chars): {respuesta.text[:300]}")
        except Exception:
            pass
        resultado = _respuesta_sin_datos("10_2", cliente_id, fecha_corte)
        resultado["_error_tecnico"] = str(e)
        return resultado

# -----------------------------------------------------------------------------
# FASE 10.3 - CONSUMO POR USUARIOS
# Identifica top usuarios por volumen, anomalias de consumo y comportamiento inusual.
# -----------------------------------------------------------------------------

def generar_sql_fase10_3(cliente_id: int, fecha_corte: str) -> dict:
    modelo = obtener_modelo(sql=True)
    prompt = f"""
{SCHEMA_DB}
{INSTRUCCION_SQL_BASE}

OBJETIVO: Generar SQL para analizar consumo por usuario del cliente {cliente_id} en el periodo completo (desde :fecha_inicio hasta :fecha_corte).

Genera queries para obtener:
1. Top 10 usuarios con mas SMS enviados en el periodo: u.nombre AS nombre_usuario, u.email, total_sms_periodo, campanas_creadas. Filtra por DATE(de.enviado_en) BETWEEN :fecha_inicio AND :fecha_corte. JOIN detalle_envios -> campanas -> usuarios.
2. Promedio de SMS enviados por usuario activo en todo el periodo.
3. Usuarios que superaron 3 veces el promedio grupal en el periodo.
4. Comparativa de primera mitad del periodo vs segunda mitad por usuario: u.nombre, u.email, total_mitad_1, total_mitad_2. (Usa condicionales simples de fechas).
5. Usuarios con campanas creadas en el periodo que tienen tasa de entrega menor al 50%.

REGLA DE SIMPLICIDAD SQL (OBLIGATORIA):
- NO uses CTEs (clausula WITH). Consultas simples y planas.
- NUNCA uses logica de 'fallback'. Simplemente usa BETWEEN :fecha_inicio AND :fecha_corte.

RANGO DE FECHAS (CRITICO): Usa SIEMPRE DATE(columna_fecha) BETWEEN :fecha_inicio AND :fecha_corte.
Incluye SIEMPRE en parametros los tres: cliente_id, fecha_corte y fecha_inicio.

Usa cliente_id={cliente_id}, fecha_corte="{fecha_corte}" y fecha_inicio (lo inyecta el sistema) en parametros.
"""
    try:
        t_inicio = __import__("time").time()
        respuesta = modelo.generate_content(
            limpiar_texto(prompt),
            generation_config={
                "temperature": 0.0,
                "max_output_tokens": 8192,
                "response_mime_type": "application/json",
                "response_schema": SCHEMA_RESPUESTA_SQL
            },
            request_options={"timeout": 240}
        )
        t_fin = __import__("time").time()
        try:
            guardar_registro_costos_sms(modulo="insights", cliente_id=cliente_id, fase="10_3_sql", descripcion="Generacion SQL consumo por usuarios", modelo=MODELO_SQL, tiempo=t_fin - t_inicio, tokens_in=respuesta.usage_metadata.prompt_token_count, tokens_out=respuesta.usage_metadata.candidates_token_count)
        except Exception:
            pass
        return parsear_json_sql_robusto(respuesta.text)
    except Exception as e:
        print(f"[SMS_AI] Error generando SQL 10.3: {e}")
        return {"error": str(e)}

def generar_insights_fase10_3(datos_query: dict, cliente_id: int, fecha_corte: str) -> dict:
    if not _datos_tienen_registros(datos_query):
        resultado = _respuesta_sin_datos("10_3", cliente_id, fecha_corte, datos_query.get("mensaje"))
        guardar_y_limpiar(f"fase10_3_{cliente_id}_{fecha_corte}.json", resultado)
        return resultado

    modelo = obtener_modelo()
    prompt = f"""
Eres un analista de consumo de usuarios SMS. Genera un reporte detallado del periodo. Usa EXCLUSIVAMENTE nombres reales o correos, NUNCA expongas "usuario_id".
Extrae la fecha de inicio y fin desde los parametros de las consultas SQL incluidas en los datos para armar el 'periodo_analizado'.

DATOS DEL PERIODO PARA CLIENTE {cliente_id}:
{json.dumps(datos_query, ensure_ascii=False, indent=2)}

Devuelve UNICAMENTE este JSON valido en espanol:
{{
  "periodo_analizado": "DD/MM/YYYY al DD/MM/YYYY",
  "cliente_id": {cliente_id},
  "modulo": "consumo_usuarios",
  "resumen": "Frase del comportamiento de usuarios del periodo",
  "total_usuarios_activos_periodo": 0,
  "promedio_sms_por_usuario": 0.0,
  "top_usuarios": [
    {{
      "nombre_usuario": "Nombre o Email",
      "sms_enviados_periodo": 0,
      "campanas_creadas_periodo": 0,
      "es_anomalia": false
    }}
  ],
  "usuarios_anomalos": [
    {{
      "nombre_usuario": "Nombre o Email",
      "sms_enviados": 0,
      "veces_sobre_promedio": 0.0,
      "posible_causa": "Descripcion sencilla (ej. envio masivo)"
    }}
  ],
  "usuarios_sin_entregas": [
    {{
      "nombre_usuario": "Nombre o Email",
      "sms_enviados": 0,
      "sms_entregados": 0
    }}
  ],
  "alertas": [
    {{
      "nivel": "alto | medio | bajo",
      "mensaje": "Descripcion sencilla",
      "dato_relevante": "Dato clave"
    }}
  ],
  "recomendaciones": [
    "Recomendacion facil de interpretar"
  ]
}}
"""
    try:
        t_inicio = __import__("time").time()
        respuesta = modelo.generate_content(
            limpiar_texto(prompt),
            generation_config={"temperature": 0.0, "max_output_tokens": 4096},
            request_options={"timeout": 240}
        )
        t_fin = __import__("time").time()
        try:
            guardar_registro_costos_sms(modulo="insights", cliente_id=cliente_id, fase="10_3_insights", descripcion="Generacion insights consumo por usuarios", modelo=MODELO_CHAT, tiempo=t_fin - t_inicio, tokens_in=respuesta.usage_metadata.prompt_token_count, tokens_out=respuesta.usage_metadata.candidates_token_count)
        except Exception:
            pass
        texto = respuesta.text.strip().replace("```json", "").replace("```", "").strip()
        inicio = texto.find("{")
        fin = texto.rfind("}") + 1
        if inicio != -1 and fin > inicio:
            texto = texto[inicio:fin]

        try:
            resultado = json.loads(texto)
        except json.JSONDecodeError:
            saneado = []
            dentro = False
            esc = False
            for ch in texto:
                if esc:
                    saneado.append(ch); esc = False; continue
                if ch == "\\":
                    saneado.append(ch); esc = True; continue
                if ch == '"':
                    dentro = not dentro; saneado.append(ch); continue
                if dentro and ch in ("\n", "\r", "\t"):
                    saneado.append(" "); continue
                saneado.append(ch)
            resultado = json.loads("".join(saneado))

        guardar_y_limpiar(f"fase10_3_{cliente_id}_{fecha_corte}.json", resultado)
        return resultado
    except Exception as e:
        print(f"[SMS_AI] Error generando insights 10.3: {e}")
        try:
            print(f"[SMS_AI] Texto raw 10.3 (primeros 300 chars): {respuesta.text[:300]}")
        except Exception:
            pass
        resultado = _respuesta_sin_datos("10_3", cliente_id, fecha_corte)
        resultado["_error_tecnico"] = str(e)
        return resultado

# -----------------------------------------------------------------------------
# FASE 10.4 - SEGURIDAD Y ALERTAS
# Detecta fallos masivos, actividad fuera de horario, webhooks caidos y patrones de riesgo.
# -----------------------------------------------------------------------------

def generar_sql_fase10_4(cliente_id: int, fecha_corte: str) -> dict:
    modelo = obtener_modelo(sql=True)
    prompt = f"""
{SCHEMA_DB}
{INSTRUCCION_SQL_BASE}

OBJETIVO: Generar SQL para detectar patrones de seguridad y riesgo del cliente {cliente_id} en el periodo completo (desde :fecha_inicio hasta :fecha_corte).

Genera queries para obtener:
1. SMS fallidos agrupados por motivo_fallo en el periodo (cuenta también los NULL). Filtra con DATE(d.enviado_en) BETWEEN :fecha_inicio AND :fecha_corte y UPPER(d.estatus) IN ('FALLIDO','RECHAZADO','EXPIRED').
2. Numeros destino con mas de 3 intentos fallidos en el periodo. Filtra igual que el punto 1.
3. Campanas con tasa de fallo mayor al 30% en el periodo: c.nombre AS nombre_campana, c.total, c.fallidos. Filtra con DATE(de.enviado_en) BETWEEN :fecha_inicio AND :fecha_corte.
4. Webhooks fallidos del periodo pertenecientes a este cliente: SELECT w.id, w.error, w.intentos FROM dlq_webhooks w JOIN detalle_envios de ON de.message_id = (w.payload->>'message_id') JOIN campanas c ON de.campana_id = c.id WHERE c.cliente_id = :cliente_id AND DATE(w.creado_en) BETWEEN :fecha_inicio AND :fecha_corte.
5. Numeros agregados a blacklist en el periodo (creado_en BETWEEN :fecha_inicio AND :fecha_corte).
6. Campanas ejecutadas fuera del horario laboral 07:00-22:00 en el periodo: c.nombre AS nombre_campana, u.nombre AS creador, EXTRACT(HOUR FROM c.programada_para). Filtra con DATE(c.programada_para) BETWEEN :fecha_inicio AND :fecha_corte.

REGLA OBLIGATORIA DE ESTATUS:
- Para comparar SIEMPRE usa UPPER(d.estatus). Para fallidos usa UPPER(d.estatus) IN ('FALLIDO','RECHAZADO','EXPIRED').

RANGO DE FECHAS (CRITICO): Usa SIEMPRE DATE(columna_fecha) BETWEEN :fecha_inicio AND :fecha_corte.
Incluye SIEMPRE en parametros los tres: cliente_id, fecha_corte y fecha_inicio.

Usa cliente_id={cliente_id}, fecha_corte="{fecha_corte}" y fecha_inicio (lo inyecta el sistema) en parametros.
"""
    try:
        t_inicio = __import__("time").time()
        respuesta = modelo.generate_content(
            limpiar_texto(prompt),
            generation_config={
                "temperature": 0.0,
                "max_output_tokens": 8192,
                "response_mime_type": "application/json",
                "response_schema": SCHEMA_RESPUESTA_SQL
            },
            request_options={"timeout": 240}
        )
        t_fin = __import__("time").time()
        try:
            guardar_registro_costos_sms(modulo="insights", cliente_id=cliente_id, fase="10_4_sql", descripcion="Generacion SQL seguridad y alertas", modelo=MODELO_SQL, tiempo=t_fin - t_inicio, tokens_in=respuesta.usage_metadata.prompt_token_count, tokens_out=respuesta.usage_metadata.candidates_token_count)
        except Exception:
            pass
        return parsear_json_sql_robusto(respuesta.text)
    except Exception as e:
        print(f"[SMS_AI] Error generando SQL 10.4: {e}")
        return {"error": str(e)}

def generar_insights_fase10_4(datos_query: dict, cliente_id: int, fecha_corte: str) -> dict:
    if not _datos_tienen_registros(datos_query):
        resultado = _respuesta_sin_datos("10_4", cliente_id, fecha_corte, datos_query.get("mensaje"))
        guardar_y_limpiar(f"fase10_4_{cliente_id}_{fecha_corte}.json", resultado)
        return resultado

    modelo = obtener_modelo()
    prompt = f"""
Eres un analista de ciberseguridad y riesgos SMS. Genera un reporte comprensible para un usuario final. 
REGLAS CRÍTICAS:
1. Extrae los textos EXACTOS de la base de datos para "tipo_fallo" (por ejemplo, si dice "timeout of 15000ms exceeded", ponlo así). No inventes categorías genéricas.
2. Extrae los nombres reales de las campañas y los usuarios. NO uses IDs.
3. Extrae la fecha de inicio y fin desde los parametros de las consultas SQL incluidas en los datos para armar el 'periodo_analizado'.
4. En los números problemáticos, ofusca los últimos 4 dígitos por privacidad (ej. 551234****).

DATOS DEL PERIODO PARA CLIENTE {cliente_id}:
{json.dumps(datos_query, ensure_ascii=False, indent=2)}

Devuelve UNICAMENTE este JSON valido en espanol:
{{
  "periodo_analizado": "DD/MM/YYYY al DD/MM/YYYY",
  "cliente_id": {cliente_id},
  "modulo": "seguridad_alertas",
  "resumen": "Frase contundente sobre los fallos técnicos, timeouts o actividad sospechosa encontrados",
  "nivel_riesgo_general": "alto | medio | bajo",
  "fallos_por_tipo": [
    {{
      "tipo_fallo": "El texto exacto de motivo_fallo extraido de los datos",
      "cantidad": 0,
      "porcentaje_del_total_fallidos": 0.0
    }}
  ],
  "numeros_problematicos": [
    {{
      "numero_ofuscado": "",
      "intentos_fallidos": 0,
      "posible_causa": "Descripcion sencilla (ej. 'fuera de servicio')"
    }}
  ],
  "campanas_con_alto_fallo": [
    {{
      "nombre_campana": "Nombre real de la campaña",
      "tasa_fallo_porcentaje": 0.0,
      "total_mensajes": 0
    }}
  ],
  "webhooks_fallidos_periodo": 0,
  "numeros_nuevos_en_blacklist_periodo": 0,
  "actividad_fuera_horario": [
    {{
      "nombre_usuario": "Nombre de quien la creó",
      "hora_actividad": "La hora extraida de los datos",
      "nombre_campana": "Nombre real de la campaña"
    }}
  ],
  "alertas": [
    {{
      "nivel": "alto | medio | bajo",
      "mensaje": "Menciona especificamente el error más repetido (ej. timeouts)",
      "dato_relevante": "Dato clave"
    }}
  ],
  "recomendaciones": [
    "Recomendacion técnica para resolver los errores extraidos"
  ]
}}
"""
    try:
        t_inicio = __import__("time").time()
        respuesta = modelo.generate_content(
            limpiar_texto(prompt),
            generation_config={"temperature": 0.0, "max_output_tokens": 4096},
            request_options={"timeout": 240}
        )
        t_fin = __import__("time").time()
        try:
            guardar_registro_costos_sms(modulo="insights", cliente_id=cliente_id, fase="10_4_insights", descripcion="Generacion insights seguridad y alertas", modelo=MODELO_CHAT, tiempo=t_fin - t_inicio, tokens_in=respuesta.usage_metadata.prompt_token_count, tokens_out=respuesta.usage_metadata.candidates_token_count)
        except Exception:
            pass
        texto = respuesta.text.strip().replace("```json", "").replace("```", "").strip()
        inicio = texto.find("{")
        fin = texto.rfind("}") + 1
        if inicio != -1 and fin > inicio:
            texto = texto[inicio:fin]

        try:
            resultado = json.loads(texto)
        except json.JSONDecodeError:
            saneado = []
            dentro = False
            esc = False
            for ch in texto:
                if esc:
                    saneado.append(ch); esc = False; continue
                if ch == "\\":
                    saneado.append(ch); esc = True; continue
                if ch == '"':
                    dentro = not dentro; saneado.append(ch); continue
                if dentro and ch in ("\n", "\r", "\t"):
                    saneado.append(" "); continue
                saneado.append(ch)
            resultado = json.loads("".join(saneado))

        guardar_y_limpiar(f"fase10_4_{cliente_id}_{fecha_corte}.json", resultado)
        return resultado
    except Exception as e:
        print(f"[SMS_AI] Error generando insights 10.4: {e}")
        try:
            print(f"[SMS_AI] Texto raw 10.4 (primeros 300 chars): {respuesta.text[:300]}")
        except Exception:
            pass
        resultado = _respuesta_sin_datos("10_4", cliente_id, fecha_corte)
        resultado["_error_tecnico"] = str(e)
        return resultado

# =============================================================================
# FUNCIONES PUBLICAS (llamadas desde api_categorizador.py)
# =============================================================================

# ---------- CHATBOT FASE 10 ----------

def iniciar_sesion_chat(session_id: str, datos_sesion: dict):
    # Registra una nueva sesion de chat con el contexto del usuario autenticado.
    cliente_id = datos_sesion.get("cliente_id", 0)
    usuario_id = datos_sesion.get("usuario_id", 0)

    with lock_sesiones:
        SESIONES_CHAT[session_id] = {
            "contexto": datos_sesion,
            "historial": []
        }
    print(f"[SMS_AI] Sesion iniciada: {session_id} para cliente {cliente_id}")

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
        respuesta_texto = resultado.get("respuesta", "<p>No pude generar una respuesta en este momento. Intenta reformular tu pregunta o consulta el modulo correspondiente en el portal.</p>")
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
        queries = resultado.get("queries", [])
        if not queries:
            respuesta_fallback = "<p>No pude obtener la informacion necesaria en este momento. Intenta reformular tu pregunta o consulta el modulo correspondiente en el portal.</p>"
            with lock_sesiones:
                SESIONES_CHAT[session_id]["historial"].append({"q": pregunta, "a": respuesta_fallback})
                if len(SESIONES_CHAT[session_id]["historial"]) > 15:
                    SESIONES_CHAT[session_id]["historial"].pop(0)
            return {
                "session_id": session_id,
                "necesita_db": False,
                "respuesta": respuesta_fallback,
                "turnos_en_historial": len(SESIONES_CHAT[session_id]["historial"])
            }
        print(f"[SMS_AI][DEBUG] necesita_db=True | sesion={session_id} | queries={len(queries)} | pregunta='{pregunta[:60]}'")
        
        # --- INICIO DEL DEBUG DE SQL ---
        print("\n" + "-"*50)
        print("--- SQL GENERADO ----")
        print("-"*50)
        for i, q in enumerate(queries):
            print(f"\n--- QUERY {i+1} [{q.get('nombre', 'Sin nombre')}] ---")
            print(q.get('sql', 'No se generó SQL'))
        print("\n" + "="*50 + "\n")
        # --- FIN DEL DEBUG DE SQL ---

        return {
            "session_id": session_id,
            "necesita_db": True,
            "descripcion": resultado.get("descripcion", ""),
            "queries": queries
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

    # Validacion: si Gemini devuelve vacio o None, mostrar mensaje util en lugar de "Sin respuesta."
    if not respuesta_texto or not respuesta_texto.strip():
        respuesta_texto = "<p>No pude generar una respuesta con los datos recibidos. Intenta reformular tu pregunta o consulta el modulo correspondiente en el portal.</p>"

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