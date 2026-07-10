from google import genai
from google.genai import types
import json
import os
import time
import re
import shutil
import threading
import queue
import pdfplumber
import csv
from datetime import datetime
from pathlib import Path
from pypdf import PdfReader, PdfWriter
from pdf2image import convert_from_path
import concurrent.futures
import uuid

# Importaciones para el motor Qwen
import torch
import gc
from transformers import AutoModelForCausalLM, AutoTokenizer

# -----------------------------------------------------------------------------
# CONFIGURACION
# -----------------------------------------------------------------------------

from gemini_keys import (
    EXTRACTOR_API_KEY            as API_KEY,
    EXTRACTOR_API_KEY_CALIBRACION as API_KEY_CALIBRACION,
    EXTRACTOR_NOMBRE_MODELO      as NOMBRE_MODELO,
)

# Inicialización del cliente global
client = genai.Client(api_key=API_KEY)
client_calibracion = genai.Client(api_key=API_KEY_CALIBRACION)

# Ruta del modelo local
RUTA_MODELO_QWEN = "/home/endless/FUNCIONALIDADES/PRUEBA MODELOS/MODELOS/Qwen2.5-7B-Instruct"

# Definición de rutas base del sistema
BASE_PATH = Path("/home/endless/FUNCIONALIDADES/PROYECTO EXTRACTOR")
DIR_INPUT = BASE_PATH / "input"
DIR_OUTPUT = BASE_PATH / "output"

# -----------------------------------------------------------------------------
# VARIABLES GLOBALES PARA PARALELISMO
# -----------------------------------------------------------------------------
cola_transacciones = queue.Queue()
lock_resultados = threading.Lock()
evento_titular_listo = threading.Event()
contexto_compartido = {
    "datos_generales": {},
    "vistos_global": set(),        # Para evitar duplicados físicos
    "ultima_tx_bloque": None,      # Para dar contexto al siguiente bloque
    "mapa_columnas": {}
}
listas_finales = {"ingresos": [], "egresos": []}
contador_transacciones = 0 
CACHE_LECTURA_PDF_RAM = {}

# -----------------------------------------------------------------------------
# CONFIGURACIÓN DEL PROMPT FASE 0 (CALIBRACIÓN ADAPTATIVA)
# -----------------------------------------------------------------------------
PROMPT_FASE_0 = """Actúa como un experto en análisis de documentos financieros y OCR de alta precisión. Tu objetivo es calibrar las coordenadas horizontales (X) para las columnas de la tabla de movimientos.

CRÍTICO - DESALINEACIÓN VISUAL DE COLUMNAS: 
En muchos bancos, el texto del ENCABEZADO está centrado, pero los NÚMEROS (los montos reales) están alineados a la DERECHA. 
Si calculas las coordenadas usando el texto del título, ¡LA EXTRACCIÓN FALLARÁ!

Instrucciones de Búsqueda y Cálculo:
1. Localiza físicamente los NÚMEROS (ej. 1,500.00) que están debajo de la columna de EGRESOS (Cargos, Retiros, Salidas, Débitos) y la columna de INGRESOS (Abonos, Depósitos, Entradas, Créditos).
2. IGNORA EL TEXTO DEL ENCABEZADO. Mide el centro geométrico (x_centro) directamente sobre los DÍGITOS de los montos de las transacciones reales.
3. Escala de salida: Ancho de página = 612 unidades. 
4. Compensación: Si notas que los números caen más hacia la derecha del título, asegúrate de que el valor "x_centro" esté desplazado hacia la derecha coincidiendo con los números.

Salida Requerida (ÚNICAMENTE JSON):
{
  "calibracion_layout": {
    "columna_egresos": {
      "texto_detectado": "Texto exacto del título (ej. RETIROS)",
      "x_centro": 0.0
    },
    "columna_ingresos": {
      "texto_detectado": "Texto exacto del título (ej. DEPOSITOS)",
      "x_centro": 0.0
    },
    "ancho_total_pagina": 612,
    "notas_alineacion": "Breve nota técnica indicando si los números están alineados a la derecha del título para justificar el x_centro."
  }
}"""

# -----------------------------------------------------------------------------
# CONFIGURACIÓN DEL PROMPT FASE 1
# -----------------------------------------------------------------------------
PROMPT_FASE_1 = """### ROL DEL SISTEMA
Eres un auditor financiero experto. Extrae datos de estados de cuenta bancarios con precisión absoluta.

### INSTRUCCIÓN CRÍTICA DE FORMATO E IDIOMA (¡LEER CON ATENCIÓN!)
Actúa como un analista financiero experto bilingüe (Español y Portugués). Tu tarea es extraer y analizar los datos del documento financiero proporcionado y devolverlos ESTRICTAMENTE en formato JSON.

REGLAS DE ORO BILINGÜES E INDISPENSABLES:
1. ESTRUCTURA EN ESPAÑOL (MANDATORIO): Absolutamente todos los nombres de las variables, claves (keys), arrays y objetos del JSON DEBEN permanecer exactamente en ESPAÑOL, tal como se definen en el esquema proporcionado. NO traduzcas, no modifiques, ni cambies una sola letra de las claves.
2. CONTENIDO EN EL IDIOMA ORIGINAL (ADAPTATIVO): El contenido de las variables (valores, textos, descripciones de transacciones) DEBE extraerse y mantenerse estrictamente en el IDIOMA ORIGINAL DEL DOCUMENTO. Si el estado de cuenta está en Español, extrae el texto en Español. Si está en Portugués de Brasil, extrae en Portugués. ¡NO TRADUZCAS EL CONTENIDO, respeta la naturaleza del PDF!
3. EXCEPCIÓN - IDIOMA DEL DOCUMENTO EN ESPAÑOL: El valor para la variable "idioma_detectado" DEBE responderse SIEMPRE EN ESPAÑOL en mayúsculas y sin acentos (ejemplo: "ESPAÑOL", "PORTUGUES"), sin importar el idioma del PDF original.
4. EXCEPCIÓN - CLASIFICACIÓN EN ESPAÑOL: El valor de la variable "Clasificacion" DEBE responderse SIEMPRE EN ESPAÑOL estricto ("Ingreso" o "Egreso"). ¡PROHIBIDO usar "Ingresso" o "Egresso"!

Ejemplo estricto de lo que se espera:
INCORRECTO (Clave traducida): {"nome_da_empresa": "Fábrica de Sapatos Ltda"}
INCORRECTO (Cruzar idiomas): El PDF está en español pero los valores se tradujeron al portugués.
CORRECTO (Claves en ES, Valores en IDIOMA ORIGINAL): {"nombre_empresa_detectado": "Fábrica de Sapatos Ltda"} (si el PDF está en portugués) o {"nombre_empresa_detectado": "Fábrica de Zapatos Ltda"} (si el PDF está en español).
CORRECTO (Idioma del PDF siempre en ES): {"idioma_detectado": "PORTUGUES"}

### INSTRUCCIONES CRÍTICAS

Utiliza tus capacidades de visión para trazar una línea vertical imaginaria desde los encabezados 'CARGOS' y 'ABONOS'.
Cualquier monto cuya coordenada X central esté alineada con la columna de 'CARGOS' debe ser Egreso, sin importar si la 
descripción dice 'ABONO' o 'TRANSFERENCIA'.

1. **FECHAS (IMPORTANTE):**
   - El PDF suele tener el año en el encabezado (ej: 2025) y los días en las filas (ej: "01 ABR").
   - **OBLIGATORIO:** Combina el día/mes de la transacción con el AÑO del encabezado.
   - FORMATO FINAL: "DD/MM/AAAA" (Ej: "01/04/2025"). No me des fechas sin año.

2. **DATOS DE PORTADA / RESUMEN (CRÍTICO):**
   - Ve a la **primera página** o a la sección "RESUMEN DE CUENTA".
   
   - **"Banco":** Identifica el banco que emite el estado de cuenta (ej. BBVA, Banamex, Santander, HSBC, etc.).
   
   - **"Nombre de la empresa" (Titular):** Este dato extráelo "TAL CUAL" (literal).

    - **"Numero de Cuenta":** Busca el número de cuenta o CLABE. EXTRAE SOLO LOS NÚMEROS (incluyendo guiones si los hay). Omite palabras como "Conta", "Agência", "C/C" o "Cuenta". Ejemplo: Si el PDF dice "Agência 0001 Conta 774588258-6", debes devolver estrictamente "0001774588258-6" o "774588258-6".

    - **"Idioma":** Identifica el idioma principal de las DESCRIPCIONES DE LAS TRANSACCIONES (ignora si los textos legales, membretes o el logo están en otro idioma).
     - **REGLA ESTRICTA (CATÁLOGO CERRADO):** Debes evaluar el texto y devolver EXACTAMENTE una de estas tres palabras, en mayúsculas y sin acentos: "ESPAÑOL", "PORTUGUES" o "INGLES".
     - Si el texto está en portugués de Brasil, devuelve estrictamente "PORTUGUES".
     - **FALLBACK (PARACAÍDAS):** Si el documento mezcla idiomas, no estás seguro, o es un idioma distinto a los tres mencionados, tu respuesta por defecto DEBE ser "ESPAÑOL". ¡No inventes otros idiomas!

   - **"Periodo" (REGLA DE NORMALIZACIÓN ESTRICTA):**
     - **NO** extraigas el periodo "tal cual". Tu trabajo aquí es **TRADUCIR** a números.
     - Si el PDF dice "Abr.", "Abril", "Apr" -> TÚ ESCRIBES "04".
     - Si el PDF dice "Ene.", "Enero", "Jan" -> TÚ ESCRIBES "01".
     - **FORMATO DE SALIDA OBLIGATORIO:** "DD/MM/AAAA al DD/MM/AAAA".
     - Ejemplo: Si ves "Del 01 Abr. 2025 al 30 Abr. 2025", tu salida JSON **DEBE** ser "01/04/2025 al 30/04/2025".
     - **PROHIBIDO:** Usar letras o puntos en este campo.

   - **SALDOS Y TOTALES (De la tabla de resumen, NO calculados):**
     - "Saldo Inicial" (o Saldo Anterior).
     - "Saldo Final" (o Saldo Actual/Nuevo).
     - "Saldo Promedio" (Si aparece en el resumen).
     - "Total Abonos/Depósitos" (El monto total que dice el banco en el resumen).
     - "Total Cargos/Retiros" (El monto total que dice el banco en el resumen).

3. **FILTRO ESTRICTO DE TABLAS (KILL SWITCH PARA ACTINVER Y OTROS):**
   - **TABLA PERMITIDA:** ÚNICAMENTE puedes extraer filas que estén físicamente bajo el título "DETALLE DE MOVIMIENTOS DE LA CUENTA ACTINVER (CUENTA EJE)" o la tabla principal de movimientos ("ESTADO DE CUENTA", "DETALLE DE OPERACIONES").
   - **ZONA PROHIBIDA (PARADA INMEDIATA):** En el momento en que leas el título "DETALLE DE MOVIMIENTOS DEL MES DEL CONTRATO MONEDA NACIONAL", "INVERSIONES" o "RESUMEN DE CHEQUES", **TIENES ESTRICTAMENTE PROHIBIDO EXTRAER FILAS DE AHÍ**.
   - **FILTRO DE CONCEPTOS (ACTINVER):** Tienes PROHIBIDO extraer filas cuya descripción contenga "REPORTO", "VTO. REPORTOS", "COMPRA DIRECTA DE REPORTOS" o "CETES". Si ves esas palabras, sáltate la fila. Extraerlas causará un fallo matemático masivo en la auditoría.

4. **TRANSACCIONES (DETALLE - ANALISIS VISUAL DE COLUMNAS AVANZADO):**
   - **Nombre de la transaccion:** Copia LITERAL, incluyendo RFCs, referencias y códigos. NO simplifiques. Si la descripción ocupa varias líneas, concaténalas en una sola cadena.

   - **DETECTAR ERRORES DE LECTURA (REGLAS DE CORRECCIÓN - PRIORIDAD ALTA):**
     - **CASO 1: LÍNEAS FUSIONADAS (MERGED ROWS):** Si ves una línea de texto que contiene DOS veces la palabra "Ref." o "Folio", o dos nombres de personas distintos mezclados con números intermedios (ej: "...Juan Perez Ref 123 0050... Maria Lopez Ref 456"), **DEBES SEPARARLAS**. Crea DOS objetos JSON distintos. NO las dejes como una sola transacción gigante.
     - **CASO 2: TRANSACCIONES FANTASMA/BASURA:** Si lees una descripción que dice SOLO "N06 PAGO CUENTA DE TERCERO" (o texto genérico similar) y **NO** tiene números de referencia, ni nombre de beneficiario, ni detalles adicionales, **IGNÓRALA**. Es un error de lectura de un encabezado o basura. Una transacción real siempre tiene un identificador único o nombre.
   
   - **Clasificacion (DETECCIÓN VISUAL ABSOLUTA - IGNORAR PALABRAS):**
     - **PASO 1 - UBICAR ENCABEZADOS:** Identifica en qué página está la tabla de movimientos y localiza los encabezados de columnas. Generalmente verás algo como:
       - Columna Izquierda: "CARGOS" / "RETIROS" / "DÉBITOS"
       - Columna Derecha: "ABONOS" / "DEPÓSITOS" / "CRÉDITOS"
       - NOTA ESPECIAL: Si en la tabla existe una columna llamada "Operación" que dice literalmente "Cargo" o "Abono" fila por fila, usa esa indicación directa para clasificar, ignorando la posición visual.
     
     - **PASO 2 - ANÁLISIS DE POSICIÓN HORIZONTAL DEL MONTO:**
       Para CADA transacción:
       a) Ignora completamente el texto descriptivo
       b) Mide la posición horizontal (coordenada X) donde aparece el NÚMERO del monto
       c) Compara esa posición X con las posiciones de los encabezados:
          - Si el monto está en la MISMA posición X que "CARGOS/RETIROS" → es "Egreso"
          - Si el monto está en la MISMA posición X que "ABONOS/DEPÓSITOS" → es "Ingreso"
     
     - **PASO 3 - VERIFICACIÓN CRUZADA (SOLO SI HAY DUDA):**
       Si visualmente el número aparece en AMBAS columnas o no está claro:
       a) Verifica el SALDO: Si el saldo DISMINUYE después de esta transacción → "Egreso"
       b) Si el saldo AUMENTA después de esta transacción → "Ingreso"
     
     - **REGLA DE ORO:** NO uses palabras como "PAGO", "RECIBIDO", "TRANSFERENCIA", "DEPOSITO" para clasificar
       USA ÚNICAMENTE la posición espacial del número en la página
       
     - **EJEMPLO VISUAL:**
       Fecha | Descripción                | CARGOS  | ABONOS
       15/09 | PAGO CUENTA TERCERO        | 5,394.00|
       15/09 | DEPOSITO RECIBIDO          |         | 28,420.00

         - Primera línea: Número en columna CARGOS → Clasificacion: "Egreso"
         - Segunda línea: Número en columna ABONOS → Clasificacion: "Ingreso"
         
     - **CASOS ESPECIALES:**
                 - Si el PDF tiene el monto en UNA SOLA COLUMNA con signo (+/-):
                   * Monto con "-" o sin signo → "Egreso"
                   * Monto con "+" → "Ingreso"
                 - **MONTOS CON SIGNO NEGATIVO AL FINAL (CRÍTICO):** Si el monto impreso en el PDF termina con un signo negativo (ej. "8,500.00-", "480.00-"), clasifícalo estrictamente según la columna en la que se ubica visualmente (generalmente es "Egreso" por estar en la columna de Retiros). PERO, al extraer el valor, **DEBES pasarlo como un número negativo (float negativo)** en tu JSON (ej. -8500.00). ¡NO lo conviertas a "Ingreso" asumiendo que es una devolución, y NO ignores el signo negativo!
               
     - **MULTIPLICIDAD (MANDATO ABSOLUTO - PROHIBIDO AGRUPAR):** Si aparecen múltiples operaciones IDÉNTICAS (misma fecha, misma descripción exacta, mismo monto exacto) una tras otra, **TIENES QUE EXTRAER TODAS Y CADA UNA POR SEPARADO**.
        * **ESTÁ ESTRICTAMENTE PROHIBIDO** agrupar, simplificar, consolidar o unificar transacciones repetidas. La IA NO debe intentar ser "inteligente" ahorrando espacio; debe ser un "espejo" exacto del PDF.
        * **EJEMPLO CRÍTICO:** Si el estado de cuenta muestra 5 filas consecutivas de "$10,000.00" con la descripción "PAGO PRESTAMO" el mismo día, tu salida JSON **DEBE** contener 5 objetos independientes.
        * Si extraes solo 1 objeto en lugar de los 5, la suma final no cuadrará y fallarás la tarea. Tu objetivo es transcribir la realidad fila por fila, no resumirla. Si hay 50 filas iguales, dame 50 objetos iguales.
     
     - **CONTINUIDAD DE PÁGINA (ANTI-PÉRDIDA DE DATOS):** - Presta atención extrema al **final de cada página** y al **inicio de la siguiente**. Es el lugar más común donde se pierden filas.
       - Si una página termina con una transacción y la siguiente empieza con otra (incluso si son idénticas en fecha y monto), **AMBAS EXISTEN**. No asumas que es un error de impresión o un duplicado visual.
       - Ignora encabezados intermedios repetidos (como "SALDO ANTERIOR" al inicio de página) pero **CAPTURA la primera transacción real** de la nueva página.
               
     - **Monto de la transaccion:** Extrae solo el número en formato float puro. REGLA CRÍTICA DE DECIMALES (BRASIL vs JSON): Los estados de cuenta en Brasil usan coma para decimales y punto para miles (ej. "R$ 1.500,50"). Al pasarlo al JSON, DEBES convertirlo estrictamente al formato numérico de programación, usando PUNTO para decimales y SIN separadores de miles (ej. 1500.50). Si el número impreso tiene un signo negativo al final (ej. "8.500,00-"), el float debe ser negativo (ej. -8500.00).

5. **VERIFICACION ESTRUCTURAL DE COLUMNAS (OBLIGATORIO ANTES DE EXTRAER):**
   - Antes de extraer cualquier transacción, responde mentalmente estas preguntas:
     a) ¿En qué página(s) está la tabla principal de movimientos?
     b) ¿Cuántas columnas numéricas tiene? (generalmente 2: cargos y abonos)
     c) ¿Cuál es el encabezado de cada columna numérica?
     d) ¿En qué posición horizontal (izquierda/centro/derecha) está cada columna?
   
   - Crea un "mapa mental" de la estructura:
     ESTRUCTURA DETECTADA:
     - Página de movimientos: 3-8
     - Columna 1 (Posición: Izquierda): "CARGOS" → Egresos
     - Columna 2 (Posición: Derecha): "ABONOS" → Ingresos

   
   - Usa este mapa para clasificar TODAS las transacciones de forma consistente.
   - Si una transacción tiene el monto en la posición "Izquierda" → Egreso (aunque diga "DEPOSITO")
   - Si una transacción tiene el monto en la posición "Derecha" → Ingreso (aunque diga "PAGO")

6. **VERIFICACION Y AUTOCORRECCION (OBLIGATORIO Y EXHAUSTIVO):**
   - **PASO CRITICO 1:** Antes de generar el JSON final, suma internamente todas las transacciones extraídas.
   - **PASO CRITICO 2:** Compara tu suma contra los "Totales de la Portada" que leíste en el paso 2.
   - **PASO CRITICO 3 (SOLUCIÓN DE ERRORES):** Si existe alguna diferencia (aunque sea de 1 peso):
     a) VUELVE A LEER EL PDF buscando **transacciones en los bordes de página** (última fila de pág X, primera fila de pág Y).
     b) Busca **transacciones repetitivas** (mismo monto, misma fecha) que hayas omitido por creer que eran duplicados.
     c) **CRÍTICO:** Revisa VISUALMENTE la columna de cada monto discrepante:
        - ¿El número está realmente en la columna de CARGOS o ABONOS?
        - Ignora el texto, solo verifica la POSICIÓN del número
     d) Verifica si clasificaste mal algún monto por confiar en palabras clave en lugar de la posición visual.
   
   - **REGLA ABSOLUTA DE VALIDACIÓN:**
     * Suma de montos clasificados como "Ingreso" DEBE = "total_depositos_portada"
     * Suma de montos clasificados como "Egreso" DEBE = "total_retiros_portada"
     * Si no coinciden → ERROR en clasificación de columnas → REVISAR POSICIÓN VISUAL
   
   - Asegúrate de que la suma de filas cuadre exactamente con el total del banco. Tu objetivo es ERROR CERO.
   - Pon tus sumas verificadas en `metadatos_validacion`.

### FORMATO DE SALIDA (JSON ÚNICO)
Devuelve solo este objeto JSON raw. No cambies las claves:

{
  "datos_generales": {
      "banco_detectado": "Texto...",
      "nombre_empresa_detectado": "Texto...",
      "numero_cuenta_detectado": "Texto...",
      "idioma_detectado": "Texto...",
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
      "Monto de la transacción": 0.00
    }
  ],
  "metadatos_validacion": {
    "suma_ingresos_filas": 0.00,
    "suma_egresos_filas": 0.00
  }
}

### SALIDA OBLIGATORIA ADICIONAL: MAPA DE COLUMNAS (EN TODOS LOS BLOQUES)
Incluye SIEMPRE este objeto ANTES del JSON principal en tu respuesta:
{
  "mapa_columnas_detectado": {
    "descripcion_estructura": "Breve descripción de la estructura de columnas (ej: 'Dos columnas numéricas: izquierda=CARGOS, derecha=ABONOS')",
    "posicion_cargos": "izquierda | centro-izquierda | coordenada aproximada X (ej: 150-300px) | otra",
    "posicion_abonos": "derecha | centro-derecha | coordenada aproximada X (ej: 500-700px) | otra",
    "encabezado_cargos": "Texto exacto del encabezado de cargos (ej: 'CARGOS' o 'RETIROS')",
    "encabezado_abonos": "Texto exacto del encabezado de abonos (ej: 'ABONOS' o 'DEPÓSITOS')",
    "paginas_tabla_principal": "Rango aproximado (ej: 3-12)",
    "notas_especiales": "Cualquier variación observada (ej: 'En páginas con encabezado repetido, ignora la fila SALDO ANTERIOR'; 'Montos de cargos a veces 10px más a la derecha en páginas pares')"
  }
}

Usa este mapa en TODOS los bloques posteriores para mantener consistencia absoluta. NO recalcules posiciones si ya existe un mapa heredado.

### FORMATO DE SALIDA (JSON ÚNICO)
Devuelve solo este objeto JSON raw. No cambies las claves:
{
  "mapa_columnas_detectado": { ... },  # <-- Añade esto como primer key si no existe
  "datos_generales": { ... },
  "transacciones": [ ... ],
  "metadatos_validacion": { ... }
}

"Para la extracción de datos y, sobre todo, para la validación de las sumas matemáticas, es OBLIGATORIO que escribas y ejecutes código Python interno para procesar el texto y realizar los cálculos. No hagas las sumas 'mentalmente'.
"""

# -----------------------------------------------------------------------------
# INSTRUCCIÓN DE INTERCALADO
# -----------------------------------------------------------------------------
INSTRUCCION_INTERCALADO = """
### REGLA SUPREMA DE NO-DUPLICIDAD (PARA PÁGINAS DE TRASLAPE)
Este bloque de imágenes contiene una "Página de Contexto" (la primera imagen). 
Para evitar duplicar movimientos:
1. SE PROHÍBE extraer cualquier transacción que aparezca completa en la PRIMERA IMAGEN.
2. ÚNICAMENTE extrae movimientos que nazcan (fecha y descripción) en la SEGUNDA imagen en adelante.
3. La primera imagen solo sirve para completar descripciones que quedaron "mochadas" o cortadas al final del bloque anterior.
4. Si ves un movimiento que ya viste antes, IGNÓRALO. Es mejor que falte uno a que sobre, ya que el validador de sumas lo detectará.
"""

# -----------------------------------------------------------------------------
# CLASE PARA MOTOR QWEN CON LIMPIEZA DE DATOS
# -----------------------------------------------------------------------------
class MotorQwen:
    def __init__(self, model_path):
        self.model_path = model_path
        self.model = None
        self.tokenizer = None
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"\n[QWEN] Motor inicializado en espera. Carga bajo demanda configurada.", flush=True)

    def cargar_recursos(self):
        if self.model is not None:
            return
        print(f"\n[QWEN] Cargando modelo local desde: {self.model_path}...", flush=True)
        try:
            self.tokenizer = AutoTokenizer.from_pretrained(self.model_path, trust_remote_code=True)
            self.model = AutoModelForCausalLM.from_pretrained(
                self.model_path,
                device_map=self.device, 
                torch_dtype=torch.float16,
                trust_remote_code=True
            )
            print(f"[QWEN] Modelo cargado exitosamente en: {self.model.device}", flush=True)
        except Exception as e:
            print(f"[QWEN] Error crítico cargando modelo: {e}", flush=True)
            raise e

    def liberar_recursos(self):
        if self.model is not None:
            print("\n[QWEN] Liberando recursos de GPU...", flush=True)
            del self.model
            del self.tokenizer
            self.model = None
            self.tokenizer = None
            gc.collect()
            torch.cuda.empty_cache()
            print("[QWEN] GPU liberada (0% VRAM uso por modelo).", flush=True)

    def limpiar_json_sucio(self, texto_raw):
        """Intenta reparar JSONs con saltos de línea internos o comillas mal cerradas."""
        try:
            # 1. Intentar encontrar el bloque de lista [] primero
            match_array = re.search(r'\[.*\]', texto_raw, re.DOTALL)
            if match_array:
                content = match_array.group(0)
                # Eliminamos el replace de saltos de línea para no romper JSON válidos
                return json.loads(content, strict=False)
            
            # 2. Si Qwen respondió sin corchetes (común al enviar solo 1 elemento), buscamos el objeto {}
            match_dict = re.search(r'\{.*\}', texto_raw, re.DOTALL)
            if match_dict:
                content = match_dict.group(0)
                resultado_unico = json.loads(content, strict=False)
                # CRÍTICO: Envolvemos el diccionario en una lista para evitar el Mismatch (1 vs 0)
                return [resultado_unico] 
                
            return []
        except:
            return []

    def procesar_lote_enriquecimiento(self, lote_transacciones, titular_cuenta, numero_cuenta_propia, idioma_detectado="ESPAÑOL"):
        if self.model is None:
            self.cargar_recursos()

        # Input simplificado
        input_optimizado = []
        for t in lote_transacciones:
            input_optimizado.append({
                "ID": t.get("Nombre de la transaccion", "")[:50], # Contexto breve
                "Texto_Completo": t.get("Nombre de la transaccion", ""),
                "Monto": t.get("Monto de la transacción", 0),
                "Clasificacion_Gemini": t.get("Clasificacion", "Desconocido")
            })

        json_input = json.dumps(input_optimizado, ensure_ascii=False, indent=2)
        num_items = len(lote_transacciones)
        
        # ---------------------------------------------------------------------
        # SELECCIÓN DINÁMICA Y BLINDAJE DE IDIOMA PARA QWEN
        # ---------------------------------------------------------------------
        import unicodedata
        
        # Limpieza robusta de acentos y espacios para asegurar que no falle el condicional
        idioma_raw = str(idioma_detectado).strip().upper() if idioma_detectado else "ESPAÑOL"
        idioma_limpio = ''.join(c for c in unicodedata.normalize('NFD', idioma_raw) if unicodedata.category(c) != 'Mn')

        if "PORTUGUE" in idioma_limpio or "BRASIL" in idioma_limpio:
            idioma_salida = "PORTUGUES"
        elif "INGLE" in idioma_limpio or "ENGLISH" in idioma_limpio:
            idioma_salida = "INGLES"
        else:
            idioma_salida = "ESPAÑOL"

        if idioma_salida == "PORTUGUES":
            # PROMPT ADAPTADO PARA BRASIL (Com cadeado anti-tradução forte)
            prompt_sistema = f"""Você é um especialista em extração de dados bancários brasileiros.
SEU OBJETIVO: Analisar o texto das descrições bancárias e estruturar os dados.

DADOS DO TITULAR (Dono do extrato bancário):
- Titular: "{titular_cuenta}"
- Conta Própria: "{numero_cuenta_propia}"

INSTRUÇÕES DE EXTRAÇÃO (LEIA COM ATENÇÃO):

1. **CONTAS E CHAVES (PRIORIDADE ALTA):**
   - Busque AGRESSIVAMENTE dentro do "Texto_Completo" qualquer sequência de números que represente uma conta, agência, CNPJ (14 dígitos), CPF (11 dígitos) ou chaves PIX.
   - Se encontrar uma Conta/Agência no texto que NÃO seja a "{numero_cuenta_propia}", essa é a CONTA DA CONTRAPARTE.
   - Se for uma Transferência enviada (Egreso) -> A conta encontrada é o "Destino" (destino).
   - Se for um Depósito recebido (Ingreso) -> A conta encontrada é a "Origen" (origem).
   - Se NÃO houver números de conta no texto, deixe os campos vazios ("").

2. **TIPO DE TRANSAÇÃO (NORMALIZADO EM ESPANHOL):**ápido 
   - Use SOMENTE estes tipos (em espanhol para manter o sistema backend): "Transferencia", "Compra", "Depósito", "Pago", "Comisión", "Retiro Cajero", "Intereses", "Cheque".
   - Se disser "PIX", "TED", "DOC" ou "Transferência", classifique como "Transferencia".
   - Se disser "TARIFA", "MENSALIDADE", "TAXA" ou "IOF", classifique como "Comisión" (NÃO Egreso).
   - Se for uma cobrança em maquininha de cartão (débito/crédito), classifique como "Compra".
   - Se disser "Pagamento de boleto" ou "DARF", classifique como "Pago".

3. **NOME RESUMIDO E CONTRAPARTE (CADEADO DE IDIOMA):**
   - "Nombre resumido": REGRA ABSOLUTA E INQUEBRÁVEL: O valor desta chave DEVE ser escrito 100% em PORTUGUÊS DO BRASIL (PT-BR). É ESTRITAMENTE PROIBIDO traduzir o conteúdo para o espanhol, mesmo que as chaves do JSON estejam em espanhol. A descrição deve ser particular, limpa e concisa. Elimine datas e lixo corporativo. (Exemplos: "Pagamento de fatura para Uber", "Transferência recebida de João Silva", "Tarifa de manutenção de conta").
   - "Quien realiza o recebe el pago": Extraia o nome da empresa ou pessoa (NÃO o titular). Mantenha no idioma original. Se for "TARIFA BANCARIA", o receptor é o Banco.

4. **REFERÊNCIA:**
   - Extraia APENAS a parte alfanumérica ou CNPJ/CPF principal (ex: "32.556.172/0001-32").
   - Ignore palavras como "REF", "AUT", "ID" no valor final. Apenas o código.

ESTRUTURA DE SAÍDA ESPERADA (As chaves DEVEM estar em Espanhol. DEVOLVA EXATAMENTE UM ARRAY DE {num_items} OBJETOS JSON):
[
  {{
    "Nombre resumido": "...",
    "Tipo de transacción": "...",
    "Quien realiza o recebe el pago": "...",
    "Numero de referencia o folio": "...",
    "Numero de cuenta origen": "...",
    "Numero de cuenta destino": "...",
    "Metodo de pago": "Pix/Tarjeta/Boleto/Otro",
    "Sucursal o ubicacion": "..."
  }}
]

MUITO IMPORTANTE: RESPONDA APENAS COM O JSON ARRAY DE {num_items} ITENS. ZERO TEXTO ADICIONAL ANTES OU DEPOIS.
"""
        else:
            # PROMPT ADAPTATIVO (MÉXICO/LATAM/EEUU/RESTO DEL MUNDO)
            prompt_sistema = f"""Eres un experto extractor de datos bancarios.
TU OBJETIVO: Analizar el texto de descripciones bancarias y estructurar los datos.

DATOS PROPIOS (Del dueño del estado de cuenta):
- Dueño: "{titular_cuenta}"
- Cuenta Propia: "{numero_cuenta_propia}"

INSTRUCCIONES DE EXTRACCIÓN (LEER ATENTAMENTE):

1. **CUENTAS Y CLABES (PRIORIDAD ALTA):**
   - Busca AGRESIVAMENTE dentro del "Texto_Completo" cualquier secuencia de números de 10, 16 o 18 dígitos.
   - Si encuentras una CLABE/Cuenta en el texto que NO sea la "{numero_cuenta_propia}", esa es la CUENTA DE LA CONTRAPARTE.
   - Si es una Transferencia enviada -> La cuenta encontrada es la "Destino".
   - Si es un Depósito recibido -> La cuenta encontrada es la "Origen".
   - Si NO hay números en el texto, deja los campos de cuenta vacíos ("").

2. **TIPO DE TRANSACCIÓN (NORMALIZADO EN ESPAÑOL):**
   - Usa SOLO estos tipos (las llaves y tipos siempre van en español): "Transferencia", "Compra", "Depósito", "Pago", "Comisión", "Retiro Cajero", "Intereses", "Cheque".
   - Si dice "SPEI" o "Wire", es "Transferencia".
   - Si dice "COMISION", "FEE", "MANEJO DE CUENTA" o "IVA", es "Comisión" (NO Egreso).
   - Si es un cargo en terminal punto de venta, es "Compra".

3. **NOMBRE RESUMIDO Y CONTRAPARTE (ADAPTATIVO AL IDIOMA):**
   - "Nombre resumido": REGLA ABSOLUTA SÍ O SÍ: El resultado DEBE mantenerse 100% en el idioma original del documento ({idioma_salida}). Debe ser una descripción particular y muy detallada sobre la entidad y el concepto, pero mucho más resumida, limpia y concisa que el texto original (evita que quede ambiguo). Elimina fechas, códigos, números de referencia y basura corporativa. Extrae la esencia exacta. (Ejemplos que debes imitar: "Pago de factura a Uber" si es español, "Uber ride payment" si es inglés). ¡ESTÁ ESTRICTAMENTE PROHIBIDO TRADUCIR EL CONTENIDO A OTRO IDIOMA QUE NO SEA {idioma_salida}!
   - "Quien realiza o recibe el pago": Extrae el nombre de la empresa oa persona (NO el dueño). Si es "COMISION...", el receptor es el Banco.

4. **REFERENCIA:**
   - Extrae SOLO la parte alfanumérica clave (ej: "0044752039").
   - Ignore palabras como "REF", "FOLIO", "AUT", "RASTREO", "ID" en el valor final. Solo el código.

SALIDA ESPERADA (JSON Array puro, DEVUELVE EXACTAMENTE UN ARRAY DE {num_items} OBJETOS JSON. Las llaves (keys) siempre en ESPAÑOL):
[
  {{
    "Nombre resumido": "...",
    "Tipo de transacción": "...",
    "Quien realiza o recibe el pago": "...",
    "Numero de referencia o folio": "...",
    "Numero de cuenta origen": "...",
    "Numero de cuenta destino": "...",
    "Metodo de pago": "SPEI/Tarjeta/Otro",
    "Sucursal o ubicacion": "..."
  }}
]

MUY IMPORTANTE: RESPONDE ÚNICAMENTE CON EL JSON ARRAY DE {num_items} ÍTEMS. CERO TEXTO ADICIONAL ANTES O DESPUÉS.
"""
        # ---------------------------------------------------------------------

        messages = [
            {"role": "system", "content": prompt_sistema},
            {"role": "user", "content": f"Extrae los datos de este JSON:\n{json_input}"}
        ]

        text = self.tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        model_inputs = self.tokenizer([text], return_tensors="pt").to(self.device)

        try:
            with torch.no_grad():
                generated_ids = self.model.generate(
                    model_inputs.input_ids,
                    max_new_tokens=2500, 
                    temperature=0.1,
                    do_sample=False
                )
            
            response = self.tokenizer.batch_decode(generated_ids, skip_special_tokens=True)[0]
            if "assistant" in response: 
                response = response.split("assistant")[-1]
            
            response = response.replace("```json", "").replace("```", "").strip()

            # Usar la función de limpieza segura
            datos_extraidos = self.limpiar_json_sucio(response)
            return datos_extraidos

        except Exception as e:
            print(f"[QWEN] Error en inferencia: {e}", flush=True)
            return []
        
# -----------------------------------------------------------------------------
# FUNCIONES AUXILIARES
# -----------------------------------------------------------------------------

def configurar_gemini():
    return client

def limpiar_archivos_api():
    """Borra archivos de AMBAS cuentas (Principal y Calibración)."""
    print("[SISTEMA] Iniciando limpieza de almacenamiento en la nube...", flush=True)
    
    def limpiar_cliente(c, nombre):
        try:
            count = 0
            for f in c.files.list():
                c.files.delete(name=f.name)
                count += 1
            print(f"   > {nombre}: {count} archivos borrados.", flush=True)
        except Exception as e:
            print(f"   > {nombre}: Error {e}", flush=True)

    # Limpiar cuenta principal
    limpiar_cliente(client, "Cuenta Principal")
    # Limpiar cuenta calibración
    limpiar_cliente(client_calibracion, "Cuenta Calibración")

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

def worker_conversion_imagenes(pdf_path, cola_salida, paginas_por_bloque=3):
    """
    Convierte rangos del PDF a IMÁGENES JPG individuales (300 DPI) en segundo plano
    y las coloca en una cola para que Gemini las procese inmediatamente.
    """
    # Usamos pypdf solo para contar páginas rápido
    reader = PdfReader(pdf_path)
    total_paginas = len(reader.pages)
    
    temp_dir = pdf_path.parent / "temp_chunks"
    temp_dir.mkdir(exist_ok=True)

    print(f"   > [CONVERTIDOR] Iniciando conversión en segundo plano ({total_paginas} págs)...", flush=True)

    # Step = paginas_por_bloque - 1 (para mantener el intercalado de contexto)
    step = paginas_por_bloque - 1 if paginas_por_bloque > 1 else 1
    
    chunk_index = 0
    i = 0
    
    while i < total_paginas:
        fin = min(i + paginas_por_bloque, total_paginas)
        
        # Subcarpeta para este bloque para organización y no mezclar archivos
        bloque_dir = temp_dir / f"bloque_{chunk_index}"
        bloque_dir.mkdir(exist_ok=True)
        
        # Convertimos SOLO las páginas de este rango
        try:
            imagenes = convert_from_path(
                str(pdf_path), 
                first_page=i+1, 
                last_page=fin,
                dpi=300,        
                fmt="png"
            )
        except Exception as e:
            print(f"Error crítico convirtiendo PDF a imagen (verifica poppler): {e}")
            break
        
        rutas_imagenes = []
        for idx_img, img in enumerate(imagenes):
            # Guardamos cada página como imagen física
            ruta_img = bloque_dir / f"pag_{i + idx_img}.png"
            img.save(ruta_img, "PNG")
            rutas_imagenes.append(ruta_img)
        
        tiene_contexto_previo = (i > 0)
        
        info_bloque = {
            "rutas_imagenes": rutas_imagenes,
            "tiene_contexto": tiene_contexto_previo,
            "paginas": f"{i+1}-{fin}",
            "index": chunk_index
        }
        
        # Ponemos el lote en la cola. Si la cola está llena, espera aquí.
        cola_salida.put(info_bloque)
        # print(f"      Bloque {chunk_index + 1} puesto en cola (Pags {i+1}-{fin}).", flush=True)
        
        chunk_index += 1
        if fin == total_paginas:
            break
        i = i + step
    
    # Señal de fin
    cola_salida.put(None)
    print("   > [CONVERTIDOR] Finalizó la conversión de todos los bloques.", flush=True)

def calibrar_layout_fase_0(rutas_imagenes, client_obj):
    """
    Fase inicial para detectar las coordenadas exactas de las columnas (SDK Nuevo).
    """
    print(f" [FASE 0] Detectando coordenadas adaptativas de columnas (Usando primer batch de imágenes)...", flush=True)
    intentos_f0_max = 3
    for intento_f0 in range(intentos_f0_max):
        try:
            if intento_f0 > 0:
                espera = 10 * intento_f0
                print(f" [FASE 0] Reintento {intento_f0 + 1}/{intentos_f0_max} (esperando {espera}s, resubiendo archivos)...", flush=True)
                time.sleep(espera)

            # Subida DENTRO del loop: en cada reintento se suben archivos frescos
            archivos_subidos = []
            for ruta in rutas_imagenes:
                time.sleep(3.0)
                archivo = client_obj.files.upload(file=ruta, config={'mime_type': 'image/png'})
                intentos_upload = 0
                while archivo.state.name == "PROCESSING" and intentos_upload < 30:
                    time.sleep(1)
                    archivo = client_obj.files.get(name=archivo.name)
                    intentos_upload += 1
                if archivo.state.name == "FAILED":
                    print(f"Fallo subida imagen fase 0: {ruta.name}")
                    continue
                archivos_subidos.append(archivo)

            if not archivos_subidos:
                raise Exception("No se pudieron subir imágenes para calibración")

            contenido = [PROMPT_FASE_0] + archivos_subidos

            t_inicio_f0 = time.time()
            respuesta = client_obj.models.generate_content(
                model=NOMBRE_MODELO,
                contents=contenido,
                config=types.GenerateContentConfig(
                    temperature=0.0,
                    max_output_tokens=65536,
                    tools=[types.Tool(code_execution=types.ToolCodeExecution())],
                    thinking_config=types.ThinkingConfig(thinking_level="high")
                )
            )
            t_fin_f0 = time.time()
            tiempo_f0 = t_fin_f0 - t_inicio_f0

            # Guardia contra respuesta vacía (server disconnect sin excepción)
            if not respuesta.text:
                raise Exception("Respuesta vacía del servidor (posible desconexión silenciosa)")

            texto_limpio = limpiar_respuesta_json(respuesta.text)
            datos_calibracion = json.loads(texto_limpio)

            calibracion = datos_calibracion.get("calibracion_layout")
            if calibracion:
                print(f" ✓ Calibración exitosa: Egresos ({calibracion['columna_egresos']['texto_detectado']} @ {calibracion['columna_egresos']['x_centro']}) | Ingresos ({calibracion['columna_ingresos']['texto_detectado']} @ {calibracion['columna_ingresos']['x_centro']}) [T: {tiempo_f0:.2f}s]", flush=True)
                return calibracion, respuesta.usage_metadata.prompt_token_count, respuesta.usage_metadata.candidates_token_count, tiempo_f0
            break  # Respuesta válida pero sin calibracion_layout — no reintentar

        except Exception as e:
            print(f" ✗ Error Fase 0 intento {intento_f0 + 1}: {e}", flush=True)
            if intento_f0 == intentos_f0_max - 1:
                print(f" ✗ Fase 0 agotó todos los reintentos. Continuando sin calibración.", flush=True)

    return None, 0, 0, 0.0

# -----------------------------------------------------------------------------
# PROCESAMIENTO PRINCIPAL CON MANEJO DE TIMEOUTS
# -----------------------------------------------------------------------------

def procesar_fase_1(pdf_path, model, output_dir=None):
    global contador_transacciones

    print(f"\n{'-'*60}", flush=True)
    print(f"--- Procesando archivo: {pdf_path.name} (PIPELINE: CONVERSIÓN + VISIÓN) ---", flush=True)
    print(f"{'-'*60}", flush=True)
    
    # 1. Crear cola y lanzar hilo convertidor (Productor)
    # maxsize=3 evita llenar la RAM si Gemini es lento; el convertidor esperará.
    cola_imagenes = queue.Queue(maxsize=3) 
    
    thread_converter = threading.Thread(
        target=worker_conversion_imagenes, 
        args=(pdf_path, cola_imagenes, 35)
    )
    thread_converter.start()
    
    bloque_actual = None
    es_primer_bloque = True
    detener_proceso_total = False
    
    while True:
        # 2. Obtenemos el siguiente bloque de la cola
        try:
            # Esperamos hasta 5 min por si la conversión de un bloque es lenta
            bloque_actual = cola_imagenes.get(timeout=300) 
        except queue.Empty:
            print("Tiempo de espera agotado esperando imágenes del convertidor.")
            break
            
        if bloque_actual is None: # Señal de fin del convertidor
            break
            
        rutas_imgs = bloque_actual["rutas_imagenes"] # Lista de paths PNG
        idx_bloque = bloque_actual["index"]
        es_bloque_con_contexto = bloque_actual["tiene_contexto"]
        
        print(f"\n > Procesando bloque {idx_bloque + 1} ({bloque_actual['paginas']})...", flush=True)

        # -----------------------------------------------------------------------------
        # FASE 0: EJECUCIÓN EN PARALELO (HILO INDEPENDIENTE)
        # -----------------------------------------------------------------------------
        if es_primer_bloque:
            def ejecutar_fase_0_bg(rutas, cliente):
                calib_res, in_tok, out_tok, tiempo_f0 = calibrar_layout_fase_0(rutas, cliente)
                with lock_resultados:
                    if calib_res:
                        contexto_compartido["calibracion"] = calib_res
                    contexto_compartido["tokens_fase0_in"] = in_tok
                    contexto_compartido["tokens_fase0_out"] = out_tok
                    contexto_compartido["tiempo_fase0"] = tiempo_f0
            
            hilo_fase0 = threading.Thread(
                target=ejecutar_fase_0_bg, 
                args=(rutas_imgs[:5], client_calibracion)
            )
            hilo_fase0.start()
            
            es_primer_bloque = False

        # -----------------------------------------------------------------------------
        # SUBIDA DE IMÁGENES (Gemini Consumidor) - PARALELIZADA
        # -----------------------------------------------------------------------------
        archivos_subidos_obj = []
        try:
            def subir_y_esperar(ruta):
                archivo = client.files.upload(file=ruta, config={'mime_type': 'image/png'})
                intentos = 0
                while archivo.state.name == "PROCESSING" and intentos < 20:
                    time.sleep(1)
                    archivo = client.files.get(name=archivo.name)
                    intentos += 1
                if archivo.state.name == "FAILED":
                    raise Exception(f"Falló procesamiento de imagen {ruta.name}")
                return archivo

            # Sube las imágenes del bloque al mismo tiempo (hasta 2 hilos)
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                futuros = [executor.submit(subir_y_esperar, ruta_img) for ruta_img in rutas_imgs]
                for futuro in concurrent.futures.as_completed(futuros):
                    archivos_subidos_obj.append(futuro.result())
                    
        except Exception as e:
            print(f" ✗ Error crítico subiendo imágenes bloque {idx_bloque+1}: {e}", flush=True)
            cola_imagenes.task_done()
            continue
        
        # -----------------------------------------------------------------------------
        # CONSTRUCCIÓN DEL PROMPT Y REQUEST
        # -----------------------------------------------------------------------------
        with lock_resultados:
            mapa_heredado = contexto_compartido.get("mapa_columnas", {})
            ultima_tx = contexto_compartido.get("ultima_tx_bloque") 
        
        prompt_actual = PROMPT_FASE_1
        if mapa_heredado:
            prompt_actual += f"\n\nMAPA DE COLUMNAS GLOBAL HEREDADO (USAR OBLIGATORIAMENTE):\n{json.dumps(mapa_heredado, indent=2, ensure_ascii=False)}"
        
        if es_bloque_con_contexto:
            if ultima_tx:
                prompt_actual += f"""
### CONTEXTO DE CONTINUIDAD (CRÍTICO)
En el bloque anterior, la ÚLTIMA transacción que extrajiste fue:
- Fecha: {ultima_tx.get('Fecha de la transaccion')}
- Descripción: {ultima_tx.get('Nombre de la transaccion')}
- Monto: {ultima_tx.get('Monto de la transacción')}

Usa esta información para ubicarte en la primera página (contexto visual). NO vuelvas a extraer esta transacción ni ninguna anterior a ella. Comienza estrictamente a partir del siguiente movimiento para no duplicar datos.
"""
            else:
                prompt_actual += INSTRUCCION_INTERCALADO
        
        # -----------------------------------------------------------------------------
        # INFERENCIA (REINTENTOS - SDK NUEVO)
        # -----------------------------------------------------------------------------
        intentos_max = 4
        intento_actual = 0
        exito_bloque = False
        
        while intento_actual < intentos_max and not exito_bloque:
            intento_actual += 1
            if intento_actual > 1:
                print(f" ... Esperando antes del reintento {intento_actual}...", flush=True)
                time.sleep(5 * (2 ** (intento_actual - 2)))
                
            try:
                inicio_proceso = time.time()
                
                # En el nuevo SDK, contents debe ser una lista de partes
                contenido_request = [prompt_actual] + archivos_subidos_obj
                
                # NUEVA LLAMADA DE GENERACIÓN
                respuesta = client.models.generate_content(
                    model=NOMBRE_MODELO,
                    contents=contenido_request,
                    config=types.GenerateContentConfig(
                        temperature=0.0,
                        max_output_tokens=65536,
                        response_mime_type="application/json",
                        tools=[types.Tool(code_execution=types.ToolCodeExecution())],
                        thinking_config=types.ThinkingConfig(thinking_level="high")
                    )
                )
                
                fin_proceso = time.time()
                tiempo_segundos = fin_proceso - inicio_proceso

                print(f"\n[RAW GEMINI]: {respuesta.text[:1000]}", flush=True)
                
                # --- LÓGICA DE PARSING Y DEDUPLICACIÓN ---
                texto_respuesta = limpiar_respuesta_json(respuesta.text)
                data_bloque = json.loads(texto_respuesta)
                
                mapa_nuevo = data_bloque.get("mapa_columnas_detectado", {})
                if mapa_nuevo:
                    with lock_resultados:
                        contexto_compartido["mapa_columnas"] = mapa_nuevo
                        print(f" [MAPA] Actualizado: {mapa_nuevo.get('descripcion_estructura', 'N/A')}", flush=True)

                nuevas_trans = data_bloque.get("transacciones", [])
                procesadas_en_este_bloque = 0

                for t in nuevas_trans:
                    monto = t.get("Monto de la transacción", 0)
                    desc_raw = str(t.get("Nombre de la transaccion", "")).strip()
                    fecha = t.get("Fecha de la transaccion", "")
                    
                    desc_norm = "".join(e for e in desc_raw.lower() if e.isalnum())
                    
                    # --- KILL SWITCH PYTHON ---
                    # Si el LLM fue terco y extrajo tabla de inversiones, las eliminamos aquí mismo.
                    es_inversion_actinver = any(palabra in desc_norm for palabra in ["reporto", "cetes", "vtoreporto", "compradirecta"])
                    if es_inversion_actinver:
                        continue # Salta esta fila y la destruye silenciosamente
                    # -------------------------------------------------------------------
                    
                    # Incrementar contador para cada transacción procesada
                    contador_transacciones += 1
                    huella = f"{fecha}|{monto}|{desc_norm}|TX{contador_transacciones}"
                    
                    with lock_resultados:
                        es_transaccion_valida = (monto != 0 or desc_raw != "")
                        
                        if es_transaccion_valida:
                            contexto_compartido["vistos_global"].add(huella)
                            cola_transacciones.put(t)
                            contexto_compartido["ultima_tx_bloque"] = t
                            procesadas_en_este_bloque += 1
                
                if procesadas_en_este_bloque > 0:
                    print(f" ✓ Enviadas {procesadas_en_este_bloque} transacciones nuevas a la COLA.", flush=True)
                
                if not nuevas_trans and idx_bloque > 0:
                      print(f" STOP >> 0 transacciones en bloque {idx_bloque + 1}.", flush=True)
                      detener_proceso_total = True
                
                if idx_bloque == 0:
                    datos_nuevos = data_bloque.get("datos_generales", {})
                    with lock_resultados:
                        contexto_compartido["datos_generales"] = datos_nuevos
                        evento_titular_listo.set()
                    print(" ✓ Datos de portada capturados.", flush=True)
                
                exito_bloque = True
                # Métricas
                input_tok = respuesta.usage_metadata.prompt_token_count
                output_tok = respuesta.usage_metadata.candidates_token_count
                
                with lock_resultados:
                    contexto_compartido["tokens_fase1_in"] = contexto_compartido.get("tokens_fase1_in", 0) + input_tok
                    contexto_compartido["tokens_fase1_out"] = contexto_compartido.get("tokens_fase1_out", 0) + output_tok
                    contexto_compartido["tiempo_fase1"] = contexto_compartido.get("tiempo_fase1", 0.0) + tiempo_segundos
                
                print(f" [OK] T:{tiempo_segundos:.2f}s | In:{input_tok} Out:{output_tok}", flush=True)

            except json.JSONDecodeError:
                print(f" ✗ Error JSON intento {intento_actual}", flush=True)
                prompt_actual += "\n\n### ERROR JSON PREVIO: Corrige el formato y devuelve solo JSON."
            except Exception as e:
                print(f" ✗ Error intento {intento_actual}: {str(e)}", flush=True)

        if not exito_bloque:
            print(f" ✗ Se agotaron los intentos para el bloque {idx_bloque+1}.", flush=True)

        # Borra las imágenes PNG del bloque ya procesado para liberar disco inmediatamente
        try:
            if bloque_actual and bloque_actual.get("rutas_imagenes"):
                for ruta_img in bloque_actual["rutas_imagenes"]:
                    if ruta_img.exists():
                        ruta_img.unlink()
                carpeta_bloque = bloque_actual["rutas_imagenes"][0].parent
                if carpeta_bloque.exists() and not any(carpeta_bloque.iterdir()):
                    carpeta_bloque.rmdir()
        except Exception:
            pass

        cola_imagenes.task_done()

        if detener_proceso_total:
            break
            
        time.sleep(2)
    
    print(" [SISTEMA] Finalizando hilos restantes...", flush=True)
    while thread_converter.is_alive():
        try:
            cola_imagenes.get(block=False)
            cola_imagenes.task_done()
        except queue.Empty:
            pass # La cola está vacía
        
        # Esperamos brevemente a que el hilo muera ahora que tiene espacio
        thread_converter.join(timeout=0.1)

    cola_transacciones.put(None)
    print("\n--- [GEMINI] Fin de lectura. ---", flush=True)

# -----------------------------------------------------------------------------
# VERSIÓN MEJORADA DE auditoria_espacial
# -----------------------------------------------------------------------------
def auditoria_espacial(ruta_pdf, transaccion, indice_ocurrencia=0, mapa_columnas=None, calibracion=None):
    """
    Busca el monto en el PDF considerando duplicados.
    OPTIMIZACIÓN: Lee el PDF una sola vez y lo guarda en RAM (CACHE_LECTURA_PDF_RAM).
    """
    monto_float = transaccion.get("Monto de la transacción", 0.0)
    descripcion = transaccion.get("Nombre de la transaccion", "").upper()
    
    # Variantes de formato
    monto_fmt = "{:,.2f}".format(abs(monto_float))
    monto_simple = "{:.2f}".format(abs(monto_float))
    variantes_monto = [monto_fmt, monto_simple, monto_fmt.replace(".00", "")]

    # Tokens de descripción (palabras clave > 3 letras)
    tokens_desc = [w for w in re.split(r'\W+', descripcion) if len(w) > 3 and not w.isdigit()]
    if not tokens_desc: tokens_desc = [descripcion[:5]]

    # Coordenadas (Calibración)
    x_egr_ref = 380 
    x_ing_ref = 520
    if calibracion:
        x_egr_ref = calibracion["columna_egresos"]["x_centro"]
        x_ing_ref = calibracion["columna_ingresos"]["x_centro"]
    
    punto_medio = (x_egr_ref + x_ing_ref) / 2
    buffer = abs(x_ing_ref - x_egr_ref) * 0.15 
    
    limite_izquierdo = punto_medio - buffer
    limite_derecho = punto_medio + buffer

    # Determinar qué columna está físicamente a la izquierda
    egresos_a_la_izq = (x_egr_ref < x_ing_ref)
    TOLERANCIA_Y = 6.0

    candidatos_encontrados = [] # Lista para guardar TODAS las coincidencias

    # --- INICIO MODIFICACIÓN: CACHÉ DE LECTURA (VELOCIDAD x100) ---
    ruta_str = str(ruta_pdf)
    
    # 1. Verificar si ya tenemos este PDF en memoria RAM
    if ruta_str not in CACHE_LECTURA_PDF_RAM:
        print(f" [CPU] Cargando PDF en memoria RAM para validación masiva: {ruta_pdf.name}...", flush=True)
        try:
            datos_paginas = []
            with pdfplumber.open(ruta_pdf) as pdf:
                for pagina in pdf.pages:
                    # Extraemos las palabras UNA SOLA VEZ y las guardamos
                    datos_paginas.append(pagina.extract_words())
            CACHE_LECTURA_PDF_RAM[ruta_str] = datos_paginas
        except Exception as e:
            print(f"Error crítico leyendo PDF para caché: {e}")
            return 0, "Error Lectura", False

    # 2. Recuperar datos desde la RAM (Instantáneo)
    paginas_cacheadas = CACHE_LECTURA_PDF_RAM[ruta_str]

    # 3. Iterar sobre los datos en memoria (Ya no se abre el archivo)
    for num_pag, words in enumerate(paginas_cacheadas):
        # (Aquí sigue tu lógica original intacta, pero usando 'words' de la RAM)
        
        # 1. Mapear filas donde aparece la descripción
        filas_desc_y = []
        for word in words:
            if word['text'].upper() in tokens_desc:
                y_centro = (word['top'] + word['bottom']) / 2
                filas_desc_y.append(y_centro)

        if not filas_desc_y: continue

        # 2. Buscar montos alineados con esas filas
        for word in words:
            texto_limpio = word['text'].replace("$", "").replace(",", "")
            
            if texto_limpio.endswith('-'):
                texto_limpio = "-" + texto_limpio[:-1]
                
            es_monto = False
            try:
                # Retiramos el abs() para mantener la precisión estricta de signos matemáticos
                if float(texto_limpio) == monto_float: es_monto = True
            except:
                pass
            
            variantes_negativas = [v + "-" for v in variantes_monto]
            
            if not es_monto and word['text'] not in variantes_monto and word['text'] not in variantes_negativas:
                continue

            y_monto = (word['top'] + word['bottom']) / 2
            
            # Verificar alineación Y con descripción
            alineado = False
            for y_desc in filas_desc_y:
                if abs(y_monto - y_desc) < TOLERANCIA_Y:
                    alineado = True
                    break
            
            if alineado:
                # Guardamos el candidato: (NumeroPagina, Y, X, ObjetoWord)
                candidatos_encontrados.append({
                    "pag": num_pag,
                    "y": y_monto,
                    "x": (word['x0'] + word['x1']) / 2,
                    "word": word
                })
    
    if not candidatos_encontrados:
        return 0, "No encontrado", False

    # Ordenamos por página y luego por altura (Y)
    candidatos_encontrados.sort(key=lambda c: (c["pag"], c["y"]))

    # Si pedimos la ocurrencia X y EXISTE en el PDF, la tomamos.
    if indice_ocurrencia < len(candidatos_encontrados):
        match_final = candidatos_encontrados[indice_ocurrencia]
    else:
        return 0, "Error: Fantasma inexistente", True

    # --- CLASIFICACIÓN DEL MATCH ELEGIDO ---
    x_centro = match_final["x"]
    zona = "Ambiguo"
    if x_centro < limite_izquierdo: 
        zona = "Egreso" if egresos_a_la_izq else "Ingreso"
    elif x_centro > limite_derecho: 
        zona = "Ingreso" if egresos_a_la_izq else "Egreso"
    
    clasif_ia = transaccion.get("Clasificacion", "Desconocido")
    conflicto = False
    sugerencia = clasif_ia
    
    if zona == "Egreso" and clasif_ia == "Ingreso":
        conflicto = True
        sugerencia = "Egreso"
    elif zona == "Ingreso" and clasif_ia == "Egreso":
        conflicto = True
        sugerencia = "Ingreso"
        
    return x_centro, sugerencia, conflicto

# -----------------------------------------------------------------------------
# WORKER CONSUMIDOR QWEN
# -----------------------------------------------------------------------------
def worker_qwen_consumidor(motor, listas_finales):
    print("\n--- [QWEN] Esperando datos de titular... ---", flush=True)
    evento_titular_listo.wait(timeout=60) 
    
    # 1. Lectura inicial (puede estar vacía al principio)
    with lock_resultados:
        datos_gen = contexto_compartido.get("datos_generales", {})
        titular = datos_gen.get("nombre_empresa_detectado", "TITULAR_GENERICO")
        mi_cuenta_real = str(datos_gen.get("numero_cuenta_detectado", "")).replace(" ", "")
        idioma_pdf = datos_gen.get("idioma_detectado", "ESPAÑOL")
    
    # Limpieza del titular
    titular_simple = re.sub(r'\s+(SA DE CV|SC|SAPI|LTD|INC).*', '', titular, flags=re.IGNORECASE).strip()
    
    print(f"--- [QWEN] Procesando con Titular: {titular_simple} | Cuenta Inicial: {mi_cuenta_real if mi_cuenta_real else 'PENDIENTE'} ---", flush=True)
    
    buffer = []
    
    # Campos vacíos para Fase 3 (Analysis)
    CAMPOS_VACIOS_FASE_3 = {
        "Giro de la transacción": "",
        "Giro sugerido": "",
        "Análisis monto": "",
        "Análisis contraparte": "",
        "Análisis naturaleza": ""
    }
    
    try:
        while True:
            try:
                item = cola_transacciones.get(timeout=5)
            except queue.Empty:
                # Si la cola se vacía temporalmente, procesamos lo que haya en el buffer para no estancar la GPU
                if buffer:
                    procesar_buffer_qwen(motor, buffer, titular_simple, mi_cuenta_real, listas_finales, CAMPOS_VACIOS_FASE_3, idioma_pdf)
                    buffer = []
                continue
                
            if item is None: # Fin del proceso
                if buffer:
                    # Procesar remanente
                    procesar_buffer_qwen(motor, buffer, titular_simple, mi_cuenta_real, listas_finales, CAMPOS_VACIOS_FASE_3, idioma_pdf)
                break
            
            # Actualización dinámica de cuenta propia
            if not mi_cuenta_real:
                with lock_resultados:
                    nueva_lectura = contexto_compartido.get("datos_generales", {}).get("numero_cuenta_detectado", "")
                    if nueva_lectura:
                        mi_cuenta_real = str(nueva_lectura).replace(" ", "")
                        print(f"--- [QWEN] ¡Cuenta detectada tardíamente! Actualizando a: {mi_cuenta_real} ---", flush=True)

            buffer.append(item)
            
            if len(buffer) >= 1:
                procesar_buffer_qwen(motor, buffer, titular_simple, mi_cuenta_real, listas_finales, CAMPOS_VACIOS_FASE_3, idioma_pdf)
                buffer = []
                
            cola_transacciones.task_done()
            
    finally:
        print("--- [QWEN] Fin del procesamiento. Liberando Motor... ---", flush=True)
        motor.liberar_recursos()


def procesar_buffer_qwen(motor, buffer, titular_nombre, mi_cuenta_num, listas_finales, campos_vacios, idioma_pdf):
    # 1. Enriquecimiento con LLM
    resultados_qwen = motor.procesar_lote_enriquecimiento(buffer, titular_nombre, mi_cuenta_num, idioma_pdf)
    
    usar_qwen = (len(resultados_qwen) == len(buffer))
    if not usar_qwen:
        print(f"⚠ [QWEN] Mismatch ({len(buffer)} vs {len(resultados_qwen)}). Usando datos crudos + Regex.", flush=True)

    # Funciones de limpieza y normalización
    
    def limpiar_referencia(texto):
        """Limpia basura de referencias"""
        if not texto: return ""
        t = str(texto).upper()
        t = re.sub(r'(REF\.|REFERENCIA|FOLIO|AUT\.|RASTREO|CLAVE|BNET|HSB|SPEI|SUC\.|GUIA[:\.]?)', '', t).strip()
        tokens = t.split()
        if len(tokens) > 1:
            mejor_token = max(tokens, key=len)
            if len(mejor_token) > 4: return mejor_token
            return tokens[-1]
        return t

    def limpiar_tipo(texto_qwen, texto_original, es_ingreso):
        """Normaliza tipos (Soporte Bilingüe ES/PT-BR)"""
        t = str(texto_qwen).capitalize()
        orig = str(texto_original).upper()
        
        # Reglas México
        if "SPEI" in orig or "TRASPASO" in orig: return "Transferencia"
        # Reglas Brasil
        if "PIX" in orig or "TED" in orig or "DOC" in orig: return "Transferencia"
        if "BOLETO" in orig or "DARF" in orig or "DAS" in orig: return "Pago"
        
        # Generales
        if "CHEQUE" in orig: return "Cheque"
        if "COMISION" in orig or "MANEJO DE CUENTA" in orig or "TARIFA" in orig or "TAXA" in orig or "MENSALIDADE" in orig: return "Comisión"
        if "IVA " in orig or "IOF" in orig: return "Impuesto"
        if "INTERES" in orig or "RENDIMIENTO" in orig or "RENDIMENTO" in orig: return "Interés"
        if "RETIRO" in orig or "DISPOSICION" in orig or "SAQUE" in orig: return "Retiro"
        
        validos = ["Transferencia", "Depósito", "Cheque", "Comisión", "Impuesto", "Pago", "Retiro", "Tarjeta", "Interés"]
        if t in validos: return t
        return "Depósito" if es_ingreso else "Pago"

    def limpiar_cuenta_bancaria(texto):
        """
        Limpieza de caracteres no numéricos en cuentas.
        Deja solo números. Si es muy largo (pegado con texto), intenta rescatar los últimos 18.
        """
        if not texto: return ""
        solo_numeros = re.sub(r'\D', '', str(texto)) # Quita TODO lo que no sea dígito
        largo = len(solo_numeros)
        
        # Lógica de rescate de CLABE/Cuenta
        if largo > 18: 
            return solo_numeros[-18:] # Probablemente se pegó texto al inicio
        elif largo in [10, 11, 15, 16, 18]:
            return solo_numeros
        elif largo > 8: # Aceptamos cuentas de 9-10 digitos
            return solo_numeros
        else:
            return "" # Basura (ej "0044")

    # Bucle principal
    for i, tx_gemini in enumerate(buffer):
        tx_final = tx_gemini.copy()
        tx_final.update(campos_vacios)
        
        datos_qwen = {
            "Nombre resumido": "", "Tipo de transacción": "", "Quien realiza o recibe el pago": "",
            "Numero de referencia o folio": "", "Metodo de pago": "Otro", "Sucursal o ubicacion": "",
            "Numero de cuenta origen": "", "Numero de cuenta destino": ""
        }
        
        if usar_qwen:
            qwen_raw = resultados_qwen[i]
            for k in datos_qwen.keys():
                if k in qwen_raw and qwen_raw[k]:
                      datos_qwen[k] = str(qwen_raw[k]).strip()

        # Aplicación de reglas
        
        clasif_txt = str(tx_final.get("Clasificacion", "")).lower()
        # Ampliamos la captura para soportar las variantes que devuelve el LLM en portugués
        es_ingreso = (
            "ingreso" in clasif_txt or 
            "ingresso" in clasif_txt or 
            "depósito" in clasif_txt or 
            "deposito" in clasif_txt or 
            "abono" in clasif_txt
        )
        
        # 1. Tipo y Referencia
        tx_final["Tipo de transacción"] = limpiar_tipo(datos_qwen["Tipo de transacción"], tx_final["Nombre de la transaccion"], es_ingreso)
        tx_final["Numero de referencia o folio"] = limpiar_referencia(datos_qwen["Numero de referencia o folio"])

        # 2. CUENTAS (LOGICA + LIMPIEZA)
        cuenta_propia_safe = mi_cuenta_num if mi_cuenta_num else "CUENTA_PROPIA"
        
        # Buscar cuenta de tercero
        cuenta_tercero = ""
        if datos_qwen.get("Numero de cuenta origen") and es_ingreso:
             cuenta_tercero = datos_qwen["Numero de cuenta origen"]
        elif datos_qwen.get("Numero de cuenta destino") and not es_ingreso:
             cuenta_tercero = datos_qwen["Numero de cuenta destino"]
        
        # Fallback Regex
        if not cuenta_tercero or len(cuenta_tercero) < 10:
            match_cuenta = re.search(r'\b(\d{10}|\d{16}|\d{18})\b', tx_final["Nombre de la transaccion"])
            if match_cuenta:
                cuenta_tercero = match_cuenta.group(0)

        # Aplicar limpieza a la cuenta de tercero
        cuenta_tercero = limpiar_cuenta_bancaria(cuenta_tercero)

        # Asignación final
        if es_ingreso:
            tx_final["Numero de cuenta origen"] = cuenta_tercero
            tx_final["Numero de cuenta destino"] = cuenta_propia_safe
        else: 
            tx_final["Numero de cuenta origen"] = cuenta_propia_safe
            tx_final["Numero de cuenta destino"] = cuenta_tercero

        # Resto de campos
        tx_final["Nombre resumido"] = datos_qwen["Nombre resumido"] if datos_qwen["Nombre resumido"] else tx_final["Nombre de la transaccion"][:30]
        tx_final["Quien realiza o recibe el pago"] = datos_qwen["Quien realiza o recibe el pago"]
        tx_final["Metodo de pago"] = datos_qwen["Metodo de pago"]
        tx_final["Sucursal o ubicacion"] = datos_qwen["Sucursal o ubicacion"]

        try:
            monto_str = str(tx_final.get("Monto de la transacción", 0)).replace("$", "").replace(",", "")
            tx_final["Monto de la transacción"] = round(float(monto_str), 2)
        except:
            tx_final["Monto de la transacción"] = 0.00

        with lock_resultados:
            if es_ingreso:
                listas_finales["ingresos"].append(tx_final)
            else:
                listas_finales["egresos"].append(tx_final)

# -----------------------------------------------------------------------------
# FUNCIÓN DE GUARDADO DE RESULTADOS
# -----------------------------------------------------------------------------
def guardar_resultados_finales(output_dir, ruta_pdf_input):
    print("\n" + "-"*60, flush=True)
    print("GUARDADO DE ARCHIVOS FINALES (GEMINI + QWEN + AUTO-CORRECCIÓN)", flush=True)
    print("-"*60, flush=True)
    
    directorio_destino = Path(output_dir)
    directorio_destino.mkdir(parents=True, exist_ok=True)
    
    # Convertir ruta a Path object para manejo seguro
    ruta_pdf_origen = Path(ruta_pdf_input)
    pdf_name = ruta_pdf_origen.stem # Extraemos el nombre para usarlo en archivos de salida
    
    # Recuperar datos
    datos_gen = contexto_compartido["datos_generales"]
    # Recuperar mapa para auditoría
    mapa_global = contexto_compartido.get("mapa_columnas", None)
    calibracion_layout = contexto_compartido.get("calibracion", None)
    
    lista_ingresos = listas_finales["ingresos"]
    lista_egresos = listas_finales["egresos"]

    # -------------------------------------------------------------------------
    # DEFINICIÓN DE NOMBRE BASE (TU CÓDIGO ORIGINAL RESTAURADO)
    # -------------------------------------------------------------------------
    # Funciones de saneamiento
    def sanear_nombre(texto):
        return str(texto).replace("/", "-").replace(":", "").replace(" ", "_").strip()

    def formatear_periodo_archivo(texto):
        if not texto or "No detectado" in str(texto):
            return "PERIODO_DESCONOCIDO"
        
        mapa_meses = {"01": "ENE", "02": "FEB", "03": "MAR", "04": "ABR", "05": "MAY", "06": "JUN", "07": "JUL", "08": "AGO", "09": "SEP", "10": "OCT", "11": "NOV", "12": "DIC"}
        
        def reemplazo_fecha(match):
            dia, mes, anio = match.groups()
            mes_abbr = mapa_meses.get(mes, mes)
            anio_corto = anio[-2:]
            return f"{dia}{mes_abbr}{anio_corto}"
        
        try:
            texto_procesado = re.sub(r'(\d{2})/(\d{2})/(\d{4})', reemplazo_fecha, str(texto))
            
            texto_con_separador = re.sub(r'\s+(al|a|-|–)\s+', '_AL_', texto_procesado, flags=re.IGNORECASE)
            
            texto_limpio = texto_con_separador.replace(" ", "").replace("/", "-").replace("\\", "-")
            
            return texto_limpio
        except:
            return "PERIODO_ERROR"

    empresa = datos_gen.get("nombre_empresa_detectado", pdf_name.replace("_", " "))
    periodo = datos_gen.get("periodo_detectado", "No detectado")
    
    empresa_file = sanear_nombre(empresa) if empresa else "EMPRESA"
    periodo_file = formatear_periodo_archivo(periodo)
    nombre_base = f"{empresa_file}_{periodo_file}"
    
    # -------------------------------------------------------------------------
    # Reemplazo de marcador de cuenta propia por número real (TU CÓDIGO ORIGINAL)
    # -------------------------------------------------------------------------
    cuenta_real_detectada = str(datos_gen.get("numero_cuenta_detectado", "")).replace(" ", "").strip()
    
    if cuenta_real_detectada and len(cuenta_real_detectada) > 5:
        print(f"\n[BARRIDO FINAL] Aplicando cuenta real '{cuenta_real_detectada}' a transacciones...", flush=True)
        
        # 1. Barrido en ingresos
        count_ing = 0
        for tx in lista_ingresos:
            cta_dest = str(tx.get("Numero de cuenta destino", "")).strip()
            # Si dice "CUENTA_PROPIA", está vacío, o es "PENDIENTE"
            if cta_dest in ["CUENTA_PROPIA", "PENDIENTE", ""] or len(cta_dest) < 5:
                tx["Numero de cuenta destino"] = cuenta_real_detectada
                count_ing += 1

        # 2. Barrido en egresos
        count_egr = 0
        for tx in lista_egresos:
            cta_orig = str(tx.get("Numero de cuenta origen", "")).strip()
            if cta_orig in ["CUENTA_PROPIA", "PENDIENTE", ""] or len(cta_orig) < 5:
                tx["Numero de cuenta origen"] = cuenta_real_detectada
                count_egr += 1
                
        print(f"    ✓ Corregidos {count_ing} ingresos y {count_egr} egresos con la cuenta real.", flush=True)
    else:
        print("\n[BARRIDO FINAL] ⚠ No se detectó cuenta maestra válida en datos generales. Se omitió el reemplazo.", flush=True)
    # -------------------------------------------------------------------------

    # Cálculos de validación final
    suma_ingresos = sum(t["Monto de la transacción"] for t in lista_ingresos)
    suma_egresos = sum(t["Monto de la transacción"] for t in lista_egresos)

    # -------------------------------------------------------------------------
    # [MODIFICADO] AUDITORÍA HÍBRIDA CON MANEJO DE DUPLICADOS
    # -------------------------------------------------------------------------
    print("\n[AUDITORÍA] Iniciando verificación espacial (con soporte para duplicados)...", flush=True)
    
    todas_transacciones = lista_ingresos + lista_egresos
    revisar_espacial = []
    
    # NUEVO: Diccionario para contar duplicados: clave = (Fecha + Descripcion + Monto)
    contador_ocurrencias = {}

    if ruta_pdf_origen.exists():
        for tx in todas_transacciones:
            # Crear una clave única para identificar duplicados visuales
            clave_duplicado = (
                tx.get("Fecha de la transaccion", ""),
                tx.get("Nombre de la transaccion", ""),
                tx.get("Monto de la transacción", 0)
            )
            
            # Determinar qué número de ocurrencia es esta (0, 1, 2...)
            idx_ocurrencia = contador_ocurrencias.get(clave_duplicado, 0)
            contador_ocurrencias[clave_duplicado] = idx_ocurrencia + 1
            
            # --- LLAMADA A LA FUNCIÓN CON EL ÍNDICE ---
            clasificacion_actual = "Ingreso" if tx in lista_ingresos else "Egreso"
            
            x_coord, clasif_visual, conflicto = auditoria_espacial(
                ruta_pdf_origen, 
                tx, 
                indice_ocurrencia=idx_ocurrencia, # <--- ¡AQUI ESTÁ LA MAGIA!
                mapa_columnas=mapa_global, 
                calibracion=calibracion_layout
            )

            if conflicto:
                tx["_alerta_auditoria"] = f"CONFLICTO: IA dice {clasificacion_actual} pero visualmente (#{idx_ocurrencia+1}) es {clasif_visual}"
                tx["_clasificacion_sugerida"] = clasif_visual
                # Nota: No necesitamos agregar a 'revisar_espacial' aquí porque
                # la auto-corrección leerá directo de la tx, pero lo mantenemos para el log.
                revisar_espacial.append(tx)
                
    else:
        print(f"⚠ No se encontró el PDF original en {ruta_pdf_origen} para auditoría.")

    if revisar_espacial:
        print(f"⚠ ALERTA: Se detectaron {len(revisar_espacial)} transacciones mal clasificadas visualmente.", flush=True)

    # -------------------------------------------------------------------------
    # [NUEVO] AUTO-CORRECCIÓN INTELIGENTE (PIVOT + ESPACIAL)
    # -------------------------------------------------------------------------
    print("\n[AUTO-CORRECCIÓN] Analizando candidatos para corrección automática...", flush=True)

    banco_total_abonos = datos_gen.get("total_depositos_portada", 0.0)
    banco_total_cargos = datos_gen.get("total_retiros_portada", 0.0)

    # Recalculamos diferencias iniciales
    suma_ingresos = sum(t["Monto de la transacción"] for t in lista_ingresos)
    suma_egresos = sum(t["Monto de la transacción"] for t in lista_egresos)
    
    diff_ing = suma_ingresos - banco_total_abonos
    diff_egr = suma_egresos - banco_total_cargos
    
    tolerancia = 0.99
    movimientos_realizados = 0

    # Combinamos para iterar (usamos una copia para no romper el loop al mover cosas)
    todas_tx_copia = lista_ingresos[:] + lista_egresos[:]

    for tx in todas_tx_copia:
        # CONDICIÓN 1: Tiene alerta espacial (Auditoría visual falló)
        if "_alerta_auditoria" not in tx:
            continue
            
        monto = tx["Monto de la transacción"]
        clasif_actual = tx["Clasificacion"] # Lo que dijo la IA
        sugerencia_visual = tx.get("_clasificacion_sugerida", "")

        # CONDICIÓN 2: El monto coincide con la diferencia matemática (Es un Pivote)
        es_pivote_ingreso = (
            clasif_actual == "Ingreso" and 
            sugerencia_visual == "Egreso" and
            abs(diff_ing - monto) < tolerancia 
        )
        
        es_pivote_egreso = (
            clasif_actual == "Egreso" and 
            sugerencia_visual == "Ingreso" and
            abs(diff_egr - monto) < tolerancia
        )

        if es_pivote_ingreso or es_pivote_egreso:
            print(f"   ★ ¡MATCH PERFECTO DETECTADO! Auto-corrigiendo: {tx['Nombre de la transaccion']} (${monto:,.2f})", flush=True)
            
            # --- EJECUTAR EL CAMBIO DE LISTA ---
            if es_pivote_ingreso:
                # Mover de Ingresos -> Egresos
                if tx in lista_ingresos:
                    lista_ingresos.remove(tx)
                    tx["Clasificacion"] = "Egreso" # Corregir etiqueta
                    # Limpiar alertas ya que lo arreglamos
                    del tx["_alerta_auditoria"]
                    del tx["_clasificacion_sugerida"]
                    lista_egresos.append(tx)
                    movimientos_realizados += 1
                    
            elif es_pivote_egreso:
                # Mover de Egresos -> Ingresos
                if tx in lista_egresos:
                    lista_egresos.remove(tx)
                    tx["Clasificacion"] = "Ingreso" # Corregir etiqueta
                    del tx["_alerta_auditoria"]
                    del tx["_clasificacion_sugerida"]
                    lista_ingresos.append(tx)
                    movimientos_realizados += 1

    # --- RECALCULO FINAL DE SUMAS DESPUÉS DE LA AUTO-CORRECCIÓN ---
    if movimientos_realizados > 0:
        suma_ingresos = sum(t["Monto de la transacción"] for t in lista_ingresos)
        suma_egresos = sum(t["Monto de la transacción"] for t in lista_egresos)
        
        # Actualizamos diferencias
        diff_ing = suma_ingresos - banco_total_abonos
        diff_egr = suma_egresos - banco_total_cargos
        
        print(f"   ✓ Se auto-corrigieron {movimientos_realizados} transacciones. Nuevas diferencias: Ing({diff_ing:.2f}) | Egr({diff_egr:.2f})", flush=True)
    else:
        print("   . No se encontraron casos de doble confirmación (Pivot + Visual) para corregir.", flush=True)

    # -------------------------------------------------------------------------
    # Reporte de validación
    # -------------------------------------------------------------------------
    print("\n" + "-"*60, flush=True)
    print("REPORTE DE VALIDACIÓN (PORTADA VS FILAS ACUMULADAS)", flush=True)
    print("-"*60, flush=True)

    # Volvemos a leer de datos_gen por si acaso
    banco_total_abonos = datos_gen.get("total_depositos_portada", 0.0)
    banco_total_cargos = datos_gen.get("total_retiros_portada", 0.0)

    diff_ingresos = abs(suma_ingresos - banco_total_abonos)
    print(f"INGRESOS -> Suma Filas: {suma_ingresos:,.2f} | Portada Banco: {banco_total_abonos:,.2f}", flush=True)
    if diff_ingresos < 1.0:
        print("VALIDACIÓN INGRESOS: ✓ CORRECTA", flush=True)
    else:
        print(f"⚠ ADVERTENCIA: Diferencia de {diff_ingresos:,.2f} en ingresos.", flush=True)

    diff_egresos = abs(suma_egresos - banco_total_cargos)
    print(f"EGRESOS  -> Suma Filas: {suma_egresos:,.2f} | Portada Banco: {banco_total_cargos:,.2f}", flush=True)
    if diff_egresos < 1.0:
        print("VALIDACIÓN EGRESOS: ✓ CORRECTA", flush=True)
    else:
        print(f"⚠ ADVERTENCIA: Diferencia de {diff_egresos:,.2f} en egresos.", flush=True)

    # -------------------------------------------------------------------------
    # GENERACIÓN DE _REVISAR.json (solo si hay discrepancia)
    # -------------------------------------------------------------------------
    tolerancia = 0.99
    revisar = []

    # Agregar los errores espaciales QUE SOBREVIVIERON (No se auto-corrigieron)
    for tx in lista_ingresos + lista_egresos:
        if "_alerta_auditoria" in tx:
            revisar.append(tx)

    diff_ing = suma_ingresos - banco_total_abonos
    diff_egr = suma_egresos - banco_total_cargos

    if abs(diff_ing) > tolerancia or abs(diff_egr) > tolerancia:
        print(f"\n⚠ DISCREPANCIA DETECTADA: Ingresos {diff_ing:+,.2f} | Egresos {diff_egr:+,.2f}", flush=True)
        
        monto_diff = abs(diff_ing)  # misma magnitud que diff_egr
        
        # Regla 1: Pivot único que explica toda la diferencia
        candidatos_pivot_ing = [tx for tx in lista_ingresos if abs(tx["Monto de la transacción"] - monto_diff) < tolerancia]
        candidatos_pivot_egr = [tx for tx in lista_egresos if abs(tx["Monto de la transacción"] - monto_diff) < tolerancia]
        
        if len(candidatos_pivot_ing) + len(candidatos_pivot_egr) == 1:
            pivot = candidatos_pivot_ing[0] if candidatos_pivot_ing else candidatos_pivot_egr[0]
            print(f"✓ Pivot único detectado: {pivot['Nombre de la transaccion']} ({pivot['Monto de la transacción']:,.2f}) - Posible error de clasificación", flush=True)
            pivot["_duda"] = f"Pivot único para diferencia {monto_diff:,.2f}"
            revisar.append(pivot)
        else:
            print(f"⚠ {len(candidatos_pivot_ing) + len(candidatos_pivot_egr)} candidatos pivot. Agregando a revisión.", flush=True)
            revisar.extend(candidatos_pivot_ing)
            revisar.extend(candidatos_pivot_egr)
        
        # Regla 3: Montos cercanos a mitad de diferencia
        if len(revisar) < 2 and monto_diff > 1000:
            mitad = monto_diff / 2
            candidatos_mitad = sorted(
                lista_ingresos + lista_egresos,
                key=lambda tx: abs(tx["Monto de la transacción"] - mitad)
            )[:4] 
            for tx in candidatos_mitad:
                if tx not in revisar:
                    tx["_duda"] = f"Monto cercano a mitad de diferencia ({mitad:,.2f})"
                    revisar.append(tx)
        
        # Eliminar duplicados
        revisar_unique = []
        vistos = set()
        for tx in revisar:
            key = (tx.get("Fecha de la transaccion", ""), tx.get("Nombre de la transaccion", ""), tx.get("Monto de la transacción", 0))
            if key not in vistos:
                vistos.add(key)
                revisar_unique.append(tx)
        revisar = revisar_unique
        
        if revisar:
            ruta_rev = directorio_destino / f"{nombre_base}_REVISAR.json"
            with open(ruta_rev, 'w', encoding='utf-8') as f:
                json.dump(revisar, f, indent=4, ensure_ascii=False)
            print(f"→ {len(revisar)} transacciones enviadas a revisión manual en {ruta_rev.name}", flush=True)
            print("    Revisa estas y mueve manualmente a INGRESOS o EGRESOS si es necesario.", flush=True)
        else:
            print("→ No se detectaron candidatas claras para revisión (verifica sumas manualmente).", flush=True)
    else:
        print("✓ Sumatorias cuadran perfectamente. No se genera _REVISAR.json", flush=True)
    # -------------------------------------------------------------------------
    
    # Preparar JSON DATOS final
    json_datos = {
        "Nombre de la empresa del estado de cuenta": empresa,
        "Numero de cuenta del estado de cuenta": datos_gen.get("numero_cuenta_detectado", ""),
        "Periodo del estado de cuenta": periodo,
        "Idioma del Estado de Cuenta": datos_gen.get("idioma_detectado", "No detectado"),
        "Saldo inicial de la cuenta": datos_gen.get("saldo_inicial_extracto", 0.0),
        "Saldo final de la cuenta": datos_gen.get("saldo_final_extracto", 0.0),
        "Saldo promedio del periodo": datos_gen.get("saldo_promedio_extracto", 0.0),
        "Cantidad total de depositos": len(lista_ingresos),
        "Cantidad total de retiros": len(lista_egresos),
        "Nombre banco": datos_gen.get("banco_detectado", "No detectado")
    }
    
    # Escritura (Igual que antes)
    ruta_ing = directorio_destino / f"{nombre_base}_INGRESOS.json"
    ruta_egr = directorio_destino / f"{nombre_base}_EGRESOS.json"
    ruta_dat = directorio_destino / f"{nombre_base}_DATOS.json"

    with open(ruta_ing, 'w', encoding='utf-8') as f:
        json.dump(lista_ingresos, f, indent=4, ensure_ascii=False)
    
    with open(ruta_egr, 'w', encoding='utf-8') as f:
        json.dump(lista_egresos, f, indent=4, ensure_ascii=False)
        
    with open(ruta_dat, 'w', encoding='utf-8') as f:
        json.dump(json_datos, f, indent=4, ensure_ascii=False)

    print(f"\n>> Archivos generados en {directorio_destino}:", flush=True)
    print(f"    1. {ruta_ing.name}", flush=True)
    print(f"    2. {ruta_egr.name}", flush=True)
    print(f"    3. {ruta_dat.name}", flush=True)
    
    # Validación simple por consola
    print(f"\nResumen: {len(lista_ingresos)} Ingresos (${suma_ingresos:,.2f}) | {len(lista_egresos)} Egresos (${suma_egresos:,.2f})")

# -----------------------------------------------------------------------------
# MAIN MODIFICADO PARA TEST LOCAL
# -----------------------------------------------------------------------------
def main():
    print("-" * 60, flush=True)
    print("MODULO FASE 1 COMPLETA (GEMINI + QWEN LOCAL PARALELO)", flush=True)
    print("-" * 60, flush=True)

    # Reinicializar clientes por si el script se reutiliza
    global client, client_calibracion
    client = genai.Client(api_key=API_KEY)
    client_calibracion = genai.Client(api_key=API_KEY_CALIBRACION)
    
    if not DIR_INPUT.exists():
        print(f"Error: Crea la carpeta {DIR_INPUT}", flush=True)
        return

    archivos_pdf = list(DIR_INPUT.glob("*.pdf"))
    
    if not archivos_pdf:
        print(f"No hay PDFs en {DIR_INPUT}", flush=True)
        return

    print(f"Procesando {len(archivos_pdf)} archivos...", flush=True)
    
    # Configurar Modelos
    try:
        model_gemini = configurar_gemini()
        limpiar_archivos_api()
        motor_qwen = MotorQwen(RUTA_MODELO_QWEN)
    except Exception as e:  
        print(f"Error inicializando modelos: {e}")
        return

    for archivo in archivos_pdf:
        # Reiniciar estados globales
        global contador_transacciones
        contador_transacciones = 0 

        while not cola_transacciones.empty(): cola_transacciones.get()
        listas_finales["ingresos"], listas_finales["egresos"] = [], []
        
        with lock_resultados:
            contexto_compartido.clear()
            contexto_compartido["datos_generales"] = {}
            contexto_compartido["vistos_global"] = set()
            contexto_compartido["ultima_tx_bloque"] = None
            contexto_compartido["mapa_columnas"] = {}
            
            # Inicialización de métricas de costos y tiempos
            contexto_compartido["tokens_fase0_in"] = 0
            contexto_compartido["tokens_fase0_out"] = 0
            contexto_compartido["tiempo_fase0"] = 0.0
            contexto_compartido["tokens_fase1_in"] = 0
            contexto_compartido["tokens_fase1_out"] = 0
            contexto_compartido["tiempo_fase1"] = 0.0
            contexto_compartido["inicio_procesamiento"] = time.time()
        
        evento_titular_listo.clear()
        
        # Lanzar Hilos
        t_gemini = threading.Thread(target=procesar_fase_1, args=(archivo, model_gemini))
        t_qwen = threading.Thread(target=worker_qwen_consumidor, args=(motor_qwen, listas_finales))
        
        t_gemini.start()
        t_qwen.start()
        
        t_gemini.join()
        t_qwen.join()
        
        # Guardar
        guardar_resultados_finales(DIR_OUTPUT, archivo)
        guardar_registro_costos(archivo.stem, cliente_id=0)
        
        # Borra la carpeta de imágenes temporales del proceso local
        temp_chunks_local = archivo.parent / "temp_chunks"
        try:
            if temp_chunks_local.exists():
                shutil.rmtree(temp_chunks_local)
        except Exception:
            pass
        
        # Borra el PDF de EXT_INPUT una vez procesado
        try:
            if archivo.exists():
                archivo.unlink()
        except Exception:
            pass
        
        time.sleep(2)

# -----------------------------------------------------------------------------
# FUNCIÓN DE GUARDADO DE REGISTRO DE COSTOS (CSV)
# -----------------------------------------------------------------------------
def guardar_registro_costos(pdf_name, cliente_id=0):
    directorio_csv = Path("/home/endless/FUNCIONALIDADES/PROYECTO EXTRACTOR")
    directorio_csv.mkdir(parents=True, exist_ok=True)
    archivo_csv = directorio_csv / "registro_costos_extractor.csv"
    existe = archivo_csv.exists()

    with lock_resultados:
        datos_gen = contexto_compartido.get("datos_generales", {})
        banco = datos_gen.get("banco_detectado", "NO_DETECTADO")
        empresa = datos_gen.get("nombre_empresa_detectado", pdf_name)
        if not empresa or "No detectado" in empresa:
            empresa = pdf_name
            
        t_in_0 = contexto_compartido.get("tokens_fase0_in", 0)
        t_out_0 = contexto_compartido.get("tokens_fase0_out", 0)
        tiempo_f0 = contexto_compartido.get("tiempo_fase0", 0.0)
        
        t_in_1 = contexto_compartido.get("tokens_fase1_in", 0)
        t_out_1 = contexto_compartido.get("tokens_fase1_out", 0)
        tiempo_f1 = contexto_compartido.get("tiempo_fase1", 0.0)
        
        inicio = contexto_compartido.get("inicio_procesamiento", time.time())

    tiempo_total = time.time() - inicio
    fecha_hora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Calculos monetarios ($0.50 input / $3.00 output por millon)
    costo_in_0 = (t_in_0 / 1000000) * 0.50
    costo_out_0 = (t_out_0 / 1000000) * 3.00
    costo_in_1 = (t_in_1 / 1000000) * 0.50
    costo_out_1 = (t_out_1 / 1000000) * 3.00
    costo_total = costo_in_0 + costo_out_0 + costo_in_1 + costo_out_1

    try:
        with open(archivo_csv, mode='a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            if not existe:
                writer.writerow([
                    "Fecha_Hora", "Cliente_ID", "Banco", "Empresa",
                    "Tiempo_Total_Procesamiento",
                    "Modelo_Coordenadas", "Tiempo_Respuesta_Coord", "Input_Tokens_Coord", "Output_Tokens_Coord",
                    "Costo_Input_Coord", "Costo_Output_Coord",
                    "Modelo_Transacciones", "Tiempo_Respuesta_Trans", "Input_Tokens_Trans", "Output_Tokens_Trans",
                    "Costo_Input_Trans", "Costo_Output_Trans", "Costo_Total_Operacion"
                ])

            writer.writerow([
                fecha_hora,
                cliente_id,
                banco,
                empresa,
                f"{tiempo_total:.2f} s",
                NOMBRE_MODELO,
                f"{tiempo_f0:.2f} s",
                t_in_0,
                t_out_0,
                f"${costo_in_0:.6f}",
                f"${costo_out_0:.6f}",
                NOMBRE_MODELO,
                f"{tiempo_f1:.2f} s",
                t_in_1,
                t_out_1,
                f"${costo_in_1:.6f}",
                f"${costo_out_1:.6f}",
                f"${costo_total:.6f}"
            ])
        print(f"\n[COSTOS] Registro guardado en CSV. (Banco: {banco}) | Cliente {cliente_id} | Costo de operacion: ${costo_total:.6f} USD", flush=True)
    except Exception as e:
        print(f"\n[COSTOS] Error al guardar el CSV de costos: {e}", flush=True)

# -----------------------------------------------------------------------------
# MAIN PARA API ORQUESTADOR
# -----------------------------------------------------------------------------
def main_extraction_ia(ruta_pdf: str, directorio_salida: str, cliente_id: int = 0) -> dict:
    # Funcion de entrada compatible con el orquestador
    global client, client_calibracion
    client = genai.Client(api_key=API_KEY)
    client_calibracion = genai.Client(api_key=API_KEY_CALIBRACION)

    print("-" * 80, flush=True)
    print("EXTRACTOR HÍBRIDO (GEMINI + QWEN) - INICIADO", flush=True)
    print("-" * 80, flush=True)
    
    pdf_path_original = Path(ruta_pdf)
    
    if not pdf_path_original.exists():
        return {"error": "Archivo no encontrado"}
        
    try:
        id_proceso = str(uuid.uuid4())
        directorio_aislado = pdf_path_original.parent / f"proceso_{id_proceso}"
        directorio_aislado.mkdir(parents=True, exist_ok=True)
        
        pdf_path = directorio_aislado / pdf_path_original.name
        shutil.copy(str(pdf_path_original), str(pdf_path))
        
        # Configurar
        model_gemini = configurar_gemini()
        limpiar_archivos_api()
        motor_qwen = MotorQwen(RUTA_MODELO_QWEN)
        
        # Limpieza previa
        temp_dir = pdf_path.parent / "temp_chunks"
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
            
        # Reiniciar estados
        global contador_transacciones
        contador_transacciones = 0

        while not cola_transacciones.empty(): cola_transacciones.get()
        listas_finales["ingresos"], listas_finales["egresos"] = [], []
        
        with lock_resultados:
            contexto_compartido.clear()
            contexto_compartido["datos_generales"] = {}
            contexto_compartido["vistos_global"] = set()
            contexto_compartido["ultima_tx_bloque"] = None
            contexto_compartido["mapa_columnas"] = {}
            
            # Inicialización de métricas de costos y tiempos
            contexto_compartido["tokens_fase0_in"] = 0
            contexto_compartido["tokens_fase0_out"] = 0
            contexto_compartido["tiempo_fase0"] = 0.0
            contexto_compartido["tokens_fase1_in"] = 0
            contexto_compartido["tokens_fase1_out"] = 0
            contexto_compartido["tiempo_fase1"] = 0.0
            contexto_compartido["inicio_procesamiento"] = time.time()
            
        evento_titular_listo.clear()
        
        # Hilos
        t_gemini = threading.Thread(target=procesar_fase_1, args=(pdf_path, model_gemini))
        t_qwen = threading.Thread(target=worker_qwen_consumidor, args=(motor_qwen, listas_finales))
        
        t_gemini.start()
        t_qwen.start()
        
        t_gemini.join()
        t_qwen.join()
        
        # Guardado final
        guardar_resultados_finales(directorio_salida, pdf_path)
        guardar_registro_costos(pdf_path.stem, cliente_id=cliente_id)
        
        if str(pdf_path) in CACHE_LECTURA_PDF_RAM:
            del CACHE_LECTURA_PDF_RAM[str(pdf_path)]
            
        if directorio_aislado.exists():
            shutil.rmtree(directorio_aislado)
            
        return {"status": "ok", "mensaje": "Extracción Híbrida completada"}
    except Exception as e:
        print(f"Error crítico en extractor híbrido: {e}", flush=True)
        return {"error": str(e)}

if __name__ == "__main__":
    main()