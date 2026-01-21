import google.generativeai as genai
import json
import os
import time
import re
from pathlib import Path
from pypdf import PdfReader, PdfWriter
import shutil

# -----------------------------------------------------------------------------
# CONFIGURACION
# -----------------------------------------------------------------------------
API_KEY = ""
NOMBRE_MODELO = 'gemini-3-pro-preview'

# Definición de rutas base del sistema
BASE_PATH = Path("/home/endless/FUNCIONALIDADES/PROYECTO EXTRACTOR")
DIR_INPUT = BASE_PATH / "input"
DIR_OUTPUT = BASE_PATH / "output"

# -----------------------------------------------------------------------------
# PROMPT COMPLETO (SIN SIMPLIFICAR - TODAS LAS REGLAS ORIGINALES)
# -----------------------------------------------------------------------------
PROMPT_FASE_1 = """### ROL DEL SISTEMA
Eres un auditor financiero experto. Extrae datos de estados de cuenta bancarios con precisión absoluta.

### INSTRUCCIONES CRÍTICAS

1. **FECHAS (IMPORTANTE):**
   - El PDF suele tener el año en el encabezado (ej: 2025) y los días en las filas (ej: "01 ABR").
   - **OBLIGATORIO:** Combina el día/mes de la transacción con el AÑO del encabezado.
   - FORMATO FINAL: "DD/MM/AAAA" (Ej: "01/04/2025"). No me des fechas sin año.

2. **DATOS DE PORTADA / RESUMEN (CRÍTICO):**
   - Ve a la **primera página** o a la sección "RESUMEN DE CUENTA". Extrae los datos "TAL CUAL" aparecen ahí:
   - "Nombre de la empresa" (Titular).
   - **"Numero de Cuenta":** Busca el número de cuenta o CLABE completo.
   - **"Periodo":** Identifica el rango y FORMATÉALO así: "DD/MM/AAAA al DD/MM/AAAA".
   - **SALDOS Y TOTALES (De la tabla de resumen, NO calculados):**
     - "Saldo Inicial" (o Saldo Anterior).
     - "Saldo Final" (o Saldo Actual/Nuevo).
     - "Saldo Promedio" (Si aparece en el resumen).
     - "Total Abonos/Depósitos" (El monto total que dice el banco en el resumen).
     - "Total Cargos/Retiros" (El monto total que dice el banco en el resumen).

3. **FILTRADO DE DUPLICADOS (NUEVA REGLA DE ORO):**
   - **SOLO** extrae transacciones de la sección principal llamada "DETALLE DE OPERACIONES", "MOVIMIENTOS" o "ESTADO DE CUENTA".
   - **IGNORA COMPLETAMENTE** tablas secundarias o anexas que suelen estar al final del PDF, tales como:
     - "Domiciliación Banamex" (o similar).
     - "Resumen de Cheques Girados".
     - "Movimientos por Concepto".
     - "Detalle de Inversiones".
   - **RAZÓN:** Estas tablas repiten transacciones que ya aparecieron en el detalle principal. Si las extraes, duplicarás los montos. Extrae CADA transacción UNA SOLA VEZ del listado cronológico principal.

4. **TRANSACCIONES (DETALLE - ANALISIS VISUAL DE COLUMNAS AVANZADO):**
   - **Nombre de la transaccion:** Copia LITERAL, incluyendo RFCs, referencias y códigos. NO simplifiques. Si la descripción ocupa varias líneas, concaténalas en una sola cadena.
   - **Clasificacion (EXTREMA PRECAUCIÓN):**
     - **REFERENCIA VERTICAL:** Traza una línea vertical imaginaria desde los encabezados de la tabla ("Retiros/Cargos" vs "Depósitos/Abonos").
     - **COLUMNA IZQUIERDA (Cargos):** Si el número está alineado bajo "Retiros/Cargos", es "Egreso".
     - **COLUMNA DERECHA (Abonos):** Si el número está alineado bajo "Depósitos/Abonos", es "Ingreso".
     - **PALABRAS CLAVE:** Si la descripción dice "PAGO RECIBIDO", "TRANSFERENCIA A SU FAVOR", "DEPOSITO" o "ABONO", debe ser un "Ingreso" casi con seguridad. Si lo ves en la columna de Egresos, REVISA VISUALMENTE DE NUEVO, probablemente sea un error de lectura de columna.
     - **MULTIPLICIDAD:** Si hay muchas operaciones idénticas seguidas (ej. "FACTORAJE 1 DE 17", "2 DE 17", etc.), **EXTRAE TODAS Y CADA UNA DE ELLAS**. No agrupes. Si hay 17 filas de 500,000, dame 17 objetos JSON.
     - **Monto de la transaccion:** Extrae solo el número (float).

5. **VERIFICACION Y AUTOCORRECCION (OBLIGATORIO):**
   - **PASO CRITICO:** Antes de generar el JSON final, suma internamente todas las transacciones extraídas.
   - Compara tu suma contra los "Totales de la Portada" que leíste en el paso 2.
   - Si existe alguna diferencia (aunque sea de 1 peso), VUELVE A LEER EL PDF y busca la transacción faltante (probablemente una en una lista densa) o mal clasificada (un ingreso puesto como egreso).
   - Asegúrate de que la suma de filas cuadre exactamente con el total del banco. Tu objetivo es ERROR CERO.
   - Pon tus sumas verificadas en `metadatos_validacion`.

### FORMATO DE SALIDA (JSON ÚNICO)
Devuelve solo este objeto JSON raw. No cambies las claves:

{
  "datos_generales": {
      "nombre_empresa_detectado": "Texto...",
      "numero_cuenta_detectado": "Texto...",
      "periodo_detectado": "Texto...",
      "saldo_inicial_extracto": 0.00,
      "saldo_final_extracto": 0.00,
      "saldo_promedio_extracto": 0.00,
      "total_depositos_portada": 0.00,
      "total_retiros_portada": 0.00
  },
  "transacciones": [
    {
      "Fecha de la transaccion": "DD/MM/AAAA",
      "Nombre de la transaccion": "TEXTO EXACTO",
      "Clasificacion": "Ingreso" o "Egreso",
      "Monto de la transaccion": 0.00
    }
  ],
  "metadatos_validacion": {
    "suma_ingresos_filas": 0.00,
    "suma_egresos_filas": 0.00
  }
}
"""

# -----------------------------------------------------------------------------
# INSTRUCCIÓN DE SOLAPAMIENTO (SE AGREGA DINÁMICAMENTE A BLOQUES 2+)
# -----------------------------------------------------------------------------
INSTRUCCION_SOLAPAMIENTO = """
### INSTRUCCIÓN DE SOLAPAMIENTO (CRÍTICO PARA ESTE BLOQUE)
Este PDF incluye la ÚLTIMA PÁGINA del bloque anterior al principio para darte contexto.

**REGLAS:**
1. **LA PRIMERA PÁGINA ES CONTEXTO:** NO extraigas transacciones que estén COMPLETAS en la primera página (ya fueron extraídas antes).
2. **TRANSACCIONES CORTADAS:** Si una transacción EMPIEZA al final de la primera página y CONTINÚA en la segunda, reconstruye esa transacción completa.
3. **TU EXTRACCIÓN PRINCIPAL:** Comienza desde la SEGUNDA página en adelante.
4. **NO DUPLICAR:** Si ves una transacción completa en la primera página, IGNÓRALA.
"""

# -----------------------------------------------------------------------------
# FUNCIONES AUXILIARES
# -----------------------------------------------------------------------------

def configurar_gemini():
    genai.configure(api_key=API_KEY)
    return genai.GenerativeModel(NOMBRE_MODELO)

def limpiar_respuesta_json(texto):
    texto_limpio = texto.strip()
    if '```json' in texto_limpio:
        inicio = texto_limpio.find('```json') + 7
        fin = texto_limpio.find('```', inicio)
        if fin != -1:
            texto_limpio = texto_limpio[inicio:fin].strip()
    elif '```' in texto_limpio:
        inicio = texto_limpio.find('```') + 3
        fin = texto_limpio.find('```', inicio)
        if fin != -1:
            texto_limpio = texto_limpio[inicio:fin].strip()
    return texto_limpio

def dividir_pdf_en_bloques(pdf_path, paginas_por_bloque=4):
    """
    Divide un PDF en archivos temporales con VENTANA DESLIZANTE.
    
    Ejemplo con paginas_por_bloque=4:
      - Bloque 1: Páginas 0, 1, 2, 3
      - Bloque 2: Páginas 3, 4, 5, 6 (página 3 repetida como contexto)
      - Bloque 3: Páginas 6, 7, 8, 9 (página 6 repetida como contexto)
    """
    reader = PdfReader(pdf_path)
    total_paginas = len(reader.pages)
    bloques_info = []
    
    temp_dir = pdf_path.parent / "temp_chunks"
    temp_dir.mkdir(exist_ok=True)

    paginas_todas = reader.pages

    print(f"   > PDF tiene {total_paginas} páginas. Procesando con SOLAPAMIENTO.", flush=True)

    # Step = paginas_por_bloque - 1 (para que la última página se repita)
    step = paginas_por_bloque - 1 if paginas_por_bloque > 1 else 1
    
    chunk_index = 0
    i = 0
    
    while i < total_paginas:
        writer = PdfWriter()
        
        # Rango del bloque actual
        fin = min(i + paginas_por_bloque, total_paginas)
        
        # Agregar páginas al escritor
        for j in range(i, fin):
            writer.add_page(paginas_todas[j])
        
        chunk_name = temp_dir / f"{pdf_path.stem}_part_{chunk_index}.pdf"
        with open(chunk_name, "wb") as f:
            writer.write(f)
        
        # Marcar si este bloque tiene página de contexto (no es el primero)
        tiene_contexto_previo = (i > 0)
        bloques_info.append({
            "path": chunk_name,
            "tiene_contexto": tiene_contexto_previo,
            "paginas": f"{i+1}-{fin}"  # Para debug
        })
        
        print(f"      Bloque {chunk_index + 1}: Páginas {i+1} a {fin} {'(con contexto)' if tiene_contexto_previo else '(inicio)'}", flush=True)
        
        chunk_index += 1
        
        # Si ya llegamos al final, terminar
        if fin == total_paginas:
            break
        
        # Avanzar con solapamiento
        i = i + step
    
    return bloques_info

# -----------------------------------------------------------------------------
# PROCESAMIENTO PRINCIPAL CON MANEJO ROBUSTO DE TIMEOUTS
# -----------------------------------------------------------------------------

def procesar_fase_1(pdf_path, model, output_dir=None):
    print(f"\n{'='*60}", flush=True)
    print(f"--- Procesando archivo: {pdf_path.name} ---", flush=True)
    print(f"{'='*60}", flush=True)
    
    # Dividir PDF en bloques con solapamiento
    bloques_info = dividir_pdf_en_bloques(pdf_path, paginas_por_bloque=4)
    print(f"\n>> PDF dividido en {len(bloques_info)} bloques.", flush=True)

    transacciones_acumuladas = []
    datos_generales_final = {}
    
    for idx, info_bloque in enumerate(bloques_info):
        bloque_path = info_bloque["path"]
        es_bloque_con_contexto = info_bloque["tiene_contexto"]
        
        print(f"\n   > Procesando bloque {idx + 1}/{len(bloques_info)} ({info_bloque['paginas']})...", flush=True)
        if es_bloque_con_contexto:
            print("     [Modo Solapamiento: Primera página = contexto]", flush=True)
        
        # =====================================================================
        # SUBIDA DEL BLOQUE A GOOGLE
        # =====================================================================
        archivo_subido = None
        try:
            archivo_subido = genai.upload_file(path=bloque_path)
            # Esperar procesamiento con timeout
            intentos_upload = 0
            while archivo_subido.state.name == "PROCESSING" and intentos_upload < 60:
                time.sleep(2)
                archivo_subido = genai.get_file(archivo_subido.name)
                intentos_upload += 1
                
            if archivo_subido.state.name == "PROCESSING":
                print(f"      ⚠ Timeout esperando procesamiento del archivo", flush=True)
                continue
                
        except Exception as e:
            print(f"      ✗ Error subida bloque {idx+1}: {e}", flush=True)
            continue
        
        if archivo_subido.state.name == "FAILED":
            print("      ✗ Falló procesamiento en Google.", flush=True)
            continue

        # =====================================================================
        # CONSTRUCCIÓN DEL PROMPT
        # =====================================================================
        if es_bloque_con_contexto:
            prompt_actual = PROMPT_FASE_1 + INSTRUCCION_SOLAPAMIENTO
        else:
            prompt_actual = PROMPT_FASE_1

        # =====================================================================
        # LÓGICA DE REINTENTOS CON BACKOFF EXPONENCIAL
        # =====================================================================
        intentos_max = 4
        intento_actual = 0
        exito_bloque = False
        
        while intento_actual < intentos_max and not exito_bloque:
            intento_actual += 1
            
            # BACKOFF EXPONENCIAL: Esperar antes de cada reintento
            if intento_actual > 1:
                tiempo_espera = 5 * (2 ** (intento_actual - 2))  # 5s, 10s, 20s
                print(f"      ... Esperando {tiempo_espera}s antes del reintento {intento_actual}...", flush=True)
                time.sleep(tiempo_espera)

            try:
                inicio_proceso = time.time()
                
                # =========================================================
                # LLAMADA A LA API CON TIMEOUT EXPLÍCITO
                # =========================================================
                respuesta = model.generate_content(
                    [prompt_actual, archivo_subido],
                    generation_config={
                        "temperature": 0.0,
                        "max_output_tokens": 65536
                    },
                    request_options={
                        "timeout": 300  # 5 minutos de timeout
                    }
                )
                
                fin_proceso = time.time()
                tiempo_segundos = fin_proceso - inicio_proceso

                print(f"      [MÉTRICAS] Input: {respuesta.usage_metadata.prompt_token_count} | Output: {respuesta.usage_metadata.candidates_token_count} | Tiempo: {tiempo_segundos:.2f}s", flush=True)

                # Limpiar y parsear respuesta
                texto_respuesta = limpiar_respuesta_json(respuesta.text)
                data_bloque = json.loads(texto_respuesta)
                
                # Acumular transacciones válidas
                nuevas_trans = data_bloque.get("transacciones", [])
                nuevas_trans_validas = [t for t in nuevas_trans if t.get("Monto de la transaccion") != 0 or t.get("Nombre de la transaccion") != ""]
                
                transacciones_acumuladas.extend(nuevas_trans_validas)
                print(f"      ✓ Encontradas {len(nuevas_trans_validas)} transacciones.", flush=True)

                # Capturar datos generales (prioridad al primer bloque)
                datos_nuevos = data_bloque.get("datos_generales", {})
                if idx == 0:
                    datos_generales_final = datos_nuevos
                    print("      ✓ Datos de portada capturados.", flush=True)
                else:
                    # Recuperar datos faltantes de bloques posteriores
                    if not datos_generales_final.get("numero_cuenta_detectado") and datos_nuevos.get("numero_cuenta_detectado"):
                        datos_generales_final["numero_cuenta_detectado"] = datos_nuevos["numero_cuenta_detectado"]
                        print("      ✓ Número de cuenta recuperado de bloque posterior.", flush=True)
                
                exito_bloque = True

            except json.JSONDecodeError as e:
                print(f"      ✗ Error JSON en intento {intento_actual}: {e}", flush=True)
                # Para errores de JSON, agregar instrucción de corrección
                prompt_actual = PROMPT_FASE_1 + "\n\n### CORRECCIÓN: Responde SOLO con JSON válido, sin texto adicional ni markdown."
                if es_bloque_con_contexto:
                    prompt_actual += INSTRUCCION_SOLAPAMIENTO
                    
            except Exception as e:
                error_msg = str(e)
                print(f"      ✗ Error en intento {intento_actual}: {error_msg}", flush=True)
                
                # Si es timeout (504), simplemente reintentar con backoff
                if "504" in error_msg or "Deadline" in error_msg or "timeout" in error_msg.lower():
                    print("      >>> Timeout detectado, reintentando con backoff...", flush=True)
                elif intento_actual < intentos_max:
                    # Otro error: retroalimentar para autocorrección
                    print("      >>> Retroalimentando error para autocorrección...", flush=True)
                    adicional_error = f"\n\n### REPORTE DE ERROR SISTEMA\nError anterior: '{error_msg}'. Corrige la sintaxis JSON."
                    prompt_actual = PROMPT_FASE_1 + adicional_error
                    if es_bloque_con_contexto:
                        prompt_actual += INSTRUCCION_SOLAPAMIENTO

        if not exito_bloque:
            print(f"      ✗ Se agotaron los intentos para el bloque {idx+1}.", flush=True)

        # Pausa entre bloques para no saturar la API
        if idx < len(bloques_info) - 1:
            print("      ... Pausa de 3s entre bloques...", flush=True)
            time.sleep(3)

    # =========================================================================
    # PROCESAMIENTO DE RESULTADOS
    # =========================================================================
    
    print(f"\n>> Total global extraído: {len(transacciones_acumuladas)} transacciones.", flush=True)

    lista_ingresos_limpia = []
    lista_egresos_limpia = []
    suma_ingresos_py = 0.0
    suma_egresos_py = 0.0

    for t in transacciones_acumuladas:
        raw_monto = t.get("Monto de la transaccion", 0)
        try:
            if isinstance(raw_monto, str):
                monto_float = float(raw_monto.replace("$", "").replace(",", ""))
            else:
                monto_float = float(raw_monto)
        except:
            monto_float = 0.0
        
        clasif = t.get("Clasificacion", "").strip() 

        obj_limpio = {
            "Fecha de la transaccion": t.get("Fecha de la transaccion", ""),
            "Nombre de la transaccion": t.get("Nombre de la transaccion", ""),
            "Clasificacion": clasif,
            "Monto de la transaccion": monto_float
        }

        if "Ingreso" in clasif or "ingreso" in clasif:
            lista_ingresos_limpia.append(obj_limpio)
            suma_ingresos_py += monto_float
        elif "Egreso" in clasif or "egreso" in clasif:
            lista_egresos_limpia.append(obj_limpio)
            suma_egresos_py += monto_float
        else:
            lista_egresos_limpia.append(obj_limpio)
            suma_egresos_py += monto_float

    # Obtener valores de portada
    saldo_ini = datos_generales_final.get("saldo_inicial_extracto", 0.0)
    saldo_fin = datos_generales_final.get("saldo_final_extracto", 0.0)
    saldo_prom = datos_generales_final.get("saldo_promedio_extracto", 0.0)
    num_cuenta = datos_generales_final.get("numero_cuenta_detectado", "No detectado")
    
    banco_total_abonos = datos_generales_final.get("total_depositos_portada", 0.0)
    banco_total_cargos = datos_generales_final.get("total_retiros_portada", 0.0)
    
    empresa = datos_generales_final.get("nombre_empresa_detectado", pdf_path.stem.replace("_", " "))
    periodo = datos_generales_final.get("periodo_detectado", "No detectado")

    json_datos = {
        "Nombre de la empresa del estado de cuenta": empresa,
        "Numero de cuenta del estado de cuenta": num_cuenta,
        "Periodo del estado de cuenta": periodo,
        "Saldo inicial de la cuenta": saldo_ini,
        "Saldo final de la cuenta": saldo_fin,
        "Saldo promedio del periodo": saldo_prom,
        "Cantidad total de depositos": len(lista_ingresos_limpia),
        "Cantidad total de retiros": len(lista_egresos_limpia)
    }
    
    # =========================================================================
    # REPORTE DE VALIDACIÓN
    # =========================================================================
    
    print("\n" + "="*60, flush=True)
    print("REPORTE DE VALIDACIÓN (PORTADA VS FILAS ACUMULADAS)", flush=True)
    print("="*60, flush=True)
    
    diff_ingresos = abs(suma_ingresos_py - banco_total_abonos)
    print(f"INGRESOS -> Suma Filas: {suma_ingresos_py:,.2f} | Portada Banco: {banco_total_abonos:,.2f}", flush=True)
    if diff_ingresos < 1.0:
        print("VALIDACIÓN INGRESOS: ✓ CORRECTA", flush=True)
    else:
        print(f"⚠ ADVERTENCIA: Diferencia de {diff_ingresos:,.2f} en ingresos.", flush=True)

    diff_egresos = abs(suma_egresos_py - banco_total_cargos)
    print(f"EGRESOS  -> Suma Filas: {suma_egresos_py:,.2f} | Portada Banco: {banco_total_cargos:,.2f}", flush=True)
    if diff_egresos < 1.0:
        print("VALIDACIÓN EGRESOS: ✓ CORRECTA", flush=True)
    else:
        print(f"⚠ ADVERTENCIA: Diferencia de {diff_egresos:,.2f} en egresos.", flush=True)
    
    # =========================================================================
    # GUARDADO DE ARCHIVOS
    # =========================================================================
    
    def sanear_nombre(texto):
        return str(texto).replace("/", "-").replace(":", "").replace(" ", "_").strip()

    def formatear_periodo_archivo(texto):
        if not texto or "No detectado" in str(texto):
            return "PERIODO_DESCONOCIDO"
        
        mapa_meses = {
            "01": "ENE", "02": "FEB", "03": "MAR", "04": "ABR",
            "05": "MAY", "06": "JUN", "07": "JUL", "08": "AGO",
            "09": "SEP", "10": "OCT", "11": "NOV", "12": "DIC"
        }

        def reemplazo_fecha(match):
            dia, mes, anio = match.groups()
            mes_abbr = mapa_meses.get(mes, mes)
            anio_corto = anio[-2:]
            return f"{dia}{mes_abbr}{anio_corto}"

        try:
            texto_procesado = re.sub(r'(\d{2})/(\d{2})/(\d{4})', reemplazo_fecha, str(texto))
            return texto_procesado.replace(" al ", "_AL_").replace(" ", "")
        except:
            return "PERIODO_ERROR"

    empresa_file = sanear_nombre(empresa) if empresa else "EMPRESA"
    periodo_file = formatear_periodo_archivo(periodo)
    nombre_base = f"{empresa_file}_{periodo_file}"
    
    directorio_destino = Path(output_dir) if output_dir else DIR_OUTPUT
    directorio_destino.mkdir(parents=True, exist_ok=True)

    ruta_ing = directorio_destino / f"{nombre_base}_INGRESOS.json"
    ruta_egr = directorio_destino / f"{nombre_base}_EGRESOS.json"
    ruta_dat = directorio_destino / f"{nombre_base}_DATOS.json"

    with open(ruta_ing, 'w', encoding='utf-8') as f:
        json.dump(lista_ingresos_limpia, f, indent=4, ensure_ascii=False)
    
    with open(ruta_egr, 'w', encoding='utf-8') as f:
        json.dump(lista_egresos_limpia, f, indent=4, ensure_ascii=False)
        
    with open(ruta_dat, 'w', encoding='utf-8') as f:
        json.dump(json_datos, f, indent=4, ensure_ascii=False)

    print(f"\n>> Archivos generados en {directorio_destino}:", flush=True)
    print(f"   1. {ruta_ing.name}", flush=True)
    print(f"   2. {ruta_egr.name}", flush=True)
    print(f"   3. {ruta_dat.name}", flush=True)

def main():
    print("=" * 60, flush=True)
    print("MODULO FASE 1: EXTRACTOR CON SOLAPAMIENTO (ROBUSTO)", flush=True)
    print("=" * 60, flush=True)
    
    if not DIR_INPUT.exists():
        print(f"Error: Crea la carpeta {DIR_INPUT}", flush=True)
        return

    archivos_pdf = list(DIR_INPUT.glob("*.pdf"))
    
    if not archivos_pdf:
        print(f"No hay PDFs en {DIR_INPUT}", flush=True)
        return

    print(f"Procesando {len(archivos_pdf)} archivos...", flush=True)
    
    model = configurar_gemini()

    for archivo in archivos_pdf:
        procesar_fase_1(archivo, model)
        time.sleep(2)

def main_extraction_ia(ruta_pdf: str, directorio_salida: str) -> dict:
    """Función de entrada compatible con el orquestador"""
    print("=" * 80, flush=True)
    print("EXTRACTOR GEMINI API - INICIADO", flush=True)
    print("=" * 80, flush=True)
    
    pdf_path = Path(ruta_pdf)
    
    if not pdf_path.exists():
        return {"error": "Archivo no encontrado"}
        
    try:
        model = configurar_gemini()
        temp_dir = pdf_path.parent / "temp_chunks"
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
            
        procesar_fase_1(pdf_path, model, output_dir=directorio_salida)
        
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
            
        return {"status": "ok", "mensaje": "Extracción Gemini completada"}
    except Exception as e:
        print(f"Error crítico en extractor: {e}", flush=True)
        return {"error": str(e)}

if __name__ == "__main__":
    main()