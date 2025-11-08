# Se importaran los modulos del sistema
import sys
import json
import fitz  # PyMuPDF
from paddleocr import PaddleOCR
from decimal import Decimal
import os
import re
from pathlib import Path
from datetime import datetime

# Se importaran los modulos locales
# Se asume que los parsers estan en un directorio 'parsers'
from parsers import banamex_empresa_parser, bbva_parser, inbursa_parser
from utils import validators

# Se suprimiran las advertencias de PaddleOCR
import logging
logging.disable(logging.WARNING)
import warnings
warnings.filterwarnings('ignore')

# --- CONFIGURACION DE RUTAS ---
# Se define la ruta base del script (donde esta main_extractor.py)
SCRIPT_DIR = Path(__file__).parent.resolve()
# Se define la ruta de entrada RELATIVA AL SCRIPT
INPUT_DIR = SCRIPT_DIR / "input"
# Se define la ruta de salida RELATIVA AL SCRIPT
OUTPUT_DIR = SCRIPT_DIR / "output"
# ------------------------------

class BankStatementExtractor:
    """
    Orquestador principal del pipeline de extraccion de estados de cuenta.
    Se implementara la logica hibrida (Nativo + OCR), la deteccion de banco,
    la carga del parser especifico y la validacion.
    """
    
    # Se define el mapa de meses para formatear fechas
    MONTH_MAP = {
        'enero': 'JAN', 'feb': 'FEB', 'mar': 'MAR', 'abr': 'APR', 'may': 'MAY', 'jun': 'JUN',
        'jul': 'JUL', 'ago': 'AUG', 'sep': 'SEP', 'oct': 'OCT', 'nov': 'NOV', 'dic': 'DEC',
        'ENERO': 'JAN', 'FEB': 'FEB', 'MAR': 'MAR', 'ABR': 'APR', 'MAY': 'MAY', 'JUN': 'JUN',
        'JUL': 'JUL', 'AGO': 'AUG', 'SEP': 'SEP', 'OCT': 'OCT', 'NOV': 'NOV', 'DIC': 'DEC',
        'ENE': 'JAN', # Banamex Empresa
    }
    
    def __init__(self, use_gpu=False):
        """
        Se inicializara el extractor y el motor OCR.
        El flag use_gpu se pasa a PaddleOCR para escalabilidad.
        """
        # Se configurara el flag de GPU
        self.use_gpu = use_gpu
        # Se informara al usuario sobre la inicializacion
        print("Inicializando motor OCR (PaddleOCR). Esto puede tomar un momento...")
        # Se inicializara PaddleOCR
        # Se han eliminado use_gpu y show_log (obsoletos) y se ha actualizado use_angle_cls
        self.ocr_engine = PaddleOCR(use_textline_orientation=True, lang='es')
        print("Motor OCR listo.")
        
        # Se definira el mapa de parsers
        self.parsers = {
            "banamex_empresa": banamex_empresa_parser,
            "bbva_empresa": bbva_parser,
            "inbursa_empresa": inbursa_parser
        }

    def _extract_text_native(self, pdf_path):
        """
        Se extraera el texto seleccionable (nativo) del PDF pagina por pagina.
        Se devolvera una lista de strings.
        """
        # Se inicializara la lista de paginas
        paginas_texto = []
        try:
            # Se abrira el documento
            doc = fitz.open(pdf_path)
            # Se iterara sobre cada pagina
            for page in doc:
                # Se extraera el texto y se agregara a la lista
                paginas_texto.append(page.get_text("text"))
            # Se cerrara el documento
            doc.close()
        except Exception as e:
            # Se manejara la excepcion
            print(f"Error en extraccion nativa: {e}")
        # Se retornara la lista de textos por pagina
        return paginas_texto

    def _extract_text_ocr(self, pdf_path):
        """
        Se extraera texto usando OCR pagina por pagina.
        Se devolvera una lista de strings.
        """
        # Se inicializara la lista de paginas
        paginas_texto = []
        try:
            # Se ejecutara el OCR sobre el archivo
            # SE HA CORREGIDO EL ERROR: Se elimino el argumento 'cls=True'
            resultado_ocr = self.ocr_engine.ocr(str(pdf_path))
            
            # Se iterara sobre los resultados de cada pagina
            if resultado_ocr:
                for pagina_resultado in resultado_ocr:
                    texto_pagina_actual = ""
                    if pagina_resultado:
                        # Se iterara sobre cada linea detectada en la pagina
                        for linea in pagina_resultado:
                            # Se agregara el texto de la linea
                            texto_pagina_actual += linea[1][0] + "\n"
                    # Se agregara el texto completo de la pagina a la lista
                    paginas_texto.append(texto_pagina_actual)
        except Exception as e:
            # Se manejara la excepcion
            print(f"Error en extraccion OCR: {e}")
        # Se retornara la lista de textos por pagina
        return paginas_texto

    def _detectar_banco_y_producto(self, paginas_texto):
        """
        Se detectara el banco y el producto basado en palabras clave unicas.
        Se usara el texto completo para la deteccion.
        """
        # Se unira el texto de todas las paginas para la deteccion
        if not paginas_texto:
            return "desconocido"
            
        texto_completo = "".join(paginas_texto)
        # Se convertira a minusculas
        texto_lower = texto_completo.lower()
        
        # Se aplicaran reglas de deteccion
        if "bbva" in texto_lower:
            # Se identificara BBVA
            if "maestra pyme" in texto_lower or "versatil negocios" in texto_lower:
                return "bbva_empresa"
            return "bbva_empresa" # Se usara como default
            
        if "inbursa" in texto_lower:
            # Se identificara Inbursa
            if "inbursact empresarial" in texto_lower:
                return "inbursa_empresa"
            return "inbursa_empresa" # Se usara como default
            
        if "banamex" in texto_lower:
            # Se distinguira entre los formatos de Banamex
            if "inmovitur" in texto_lower:
                return "banamex_empresa"
            if "costco banamex" in texto_lower:
                return "desconocido" 
            
            # Se usara un default si no se puede especificar
            if "banamex_empresa" in self.parsers:
                 return "banamex_empresa"
            
        # Se retornara desconocido si no hay coincidencias
        return "desconocido"

    def _parsear_texto(self, paginas_texto, parser_key):
        """
        Se seleccionara el parser correcto desde el mapa y se ejecutara.
        Se pasara la lista de paginas al parser.
        """
        # Se verificara si el parser existe
        if parser_key in self.parsers:
            # Se obtendra el modulo del parser
            parser = self.parsers[parser_key]
            
            # Se ejecutara el parsing de datos generales
            datos_generales = parser.parsear_datos_generales(paginas_texto)
            # Se ejecutara el parsing de transacciones
            transacciones = parser.parsear_transacciones(paginas_texto, datos_generales.get('saldo_inicial', 0))
            
            # Se retornaran los resultados
            return {"datos_generales": datos_generales, "transacciones": transacciones}
        else:
            # Se lanzara un error si el parser no esta implementado
            raise NotImplementedError(f"El parser para la llave '{parser_key}' no esta implementado.")

    def _default_json_serializer(self, obj):
        """
        Se convertiran tipos no serializables (como Decimal) a string.
        """
        # Se verificara si es Decimal
        if isinstance(obj, Decimal):
            # Se convertira a string
            return str(obj)
        # Se lanzara error para otros tipos
        raise TypeError(f"Objeto de tipo {obj.__class__.__name__} no es serializable en JSON")

    def _formatear_periodo(self, periodo_str):
        """
        Se normalizaran los diferentes formatos de periodo a (DDMMMYYYY, DDMMMYYYY).
        """
        if not periodo_str:
            return "FECHA_INICIO", "FECHA_FIN"
            
        periodo_str = periodo_str.strip()
        
        try:
            # Formato 1: BBVA (DEL 01/04/2025 AL 30/04/2025)
            match1 = re.match(r"DEL (\d{2})/(\d{2})/(\d{4}) AL (\d{2})/(\d{2})/(\d{4})", periodo_str, re.IGNORECASE)
            if match1:
                f_inicio = datetime.strptime(f"{match1.group(1)}-{match1.group(2)}-{match1.group(3)}", "%d-%m-%Y")
                f_fin = datetime.strptime(f"{match1.group(4)}-{match1.group(5)}-{match1.group(6)}", "%d-%m-%Y")
                return f_inicio.strftime('%d%b%Y').upper(), f_fin.strftime('%d%b%Y').upper()

            # Formato 2: Inbursa (Del 01 Mar. 2025 al 31 Mar. 2025)
            match2 = re.match(r"Del (\d{2}) (\w{3})\. (\d{4}) al (\d{2}) (\w{3})\. (\d{4})", periodo_str, re.IGNORECASE)
            if match2:
                mes_inicio = self.MONTH_MAP.get(match2.group(2).lower(), 'JAN')
                mes_fin = self.MONTH_MAP.get(match2.group(5).lower(), 'DEC')
                f_inicio = datetime.strptime(f"{match2.group(1)}-{mes_inicio}-{match2.group(3)}", "%d-%b-%Y")
                f_fin = datetime.strptime(f"{match2.group(4)}-{mes_fin}-{match2.group(6)}", "%d-%b-%Y")
                return f_inicio.strftime('%d%b%Y').upper(), f_fin.strftime('%d%b%Y').upper()

            # Formato 3: Banamex Empresa (RESUMEN DEL: 01/JUN/2025 AL 30/JUN/2025)
            match3 = re.match(r"RESUMEN DEL: (\d{2})/([A-Z]{3})/(\d{4}) AL (\d{2})/([A-Z]{3})/(\d{4})", periodo_str, re.IGNORECASE)
            if match3:
                mes_inicio = self.MONTH_MAP.get(match3.group(2).upper(), 'JAN')
                mes_fin = self.MONTH_MAP.get(match3.group(5).upper(), 'DEC')
                f_inicio = datetime.strptime(f"{match3.group(1)}-{mes_inicio}-{match3.group(3)}", "%d-%b-%Y")
                f_fin = datetime.strptime(f"{match3.group(4)}-{mes_fin}-{match3.group(6)}", "%d-%b-%Y")
                return f_inicio.strftime('%d%b%Y').upper(), f_fin.strftime('%d%b%Y').upper()

        except Exception as e:
            print(f"  > Advertencia: No se pudo formatear el periodo '{periodo_str}'. Error: {e}")
            return "FECHA_INICIO", "FECHA_FIN"
            
        # Se retornaran valores por defecto si ningun formato coincide
        print(f"  > Advertencia: El formato de periodo '{periodo_str}' no coincide con ningun patron.")
        return "FECHA_INICIO", "FECHA_FIN"

    def _formatear_nombre_archivo(self, datos_generales):
        """
        Se generara el nombre base del archivo usando los datos extraidos.
        Formato: NOMBREEMPRESA_FECHAINICIO_FECHAFIN
        """
        # Se obtendra el nombre de la empresa, se limpiara y se convertira a mayusculas
        nombre = datos_generales.get('nombre_empresa', 'SIN_NOMBRE')
        if not nombre: nombre = 'SIN_NOMBRE'
        nombre_limpio = re.sub(r'[^A-Z0-9_\s]', '', str(nombre).upper())
        nombre_limpio = re.sub(r'\s+', '_', nombre_limpio.strip())
        
        # Se obtendra el periodo
        periodo = datos_generales.get('periodo', 'SIN_PERIODO')
        if not periodo: periodo = 'SIN_PERIODO'
        
        # Se formatearan las fechas
        fecha_inicio, fecha_fin = self._formatear_periodo(periodo)
        
        # Se retornara el nombre base
        return f"{nombre_limpio}_{fecha_inicio}_{fecha_fin}"

    def guardar_resultados(self, resultado_completo, output_dir):
        """
        Se dividira el resultado en 3 partes (Datos, Ingresos, Egresos)
        y se guardara con el formato de nombre solicitado.
        """
        try:
            # Se obtendran los datos generales
            datos_generales = resultado_completo.get('datos_generales', {})
            # Se obtendra la lista completa de transacciones
            transacciones = resultado_completo.get('transacciones', [])
            
            # 1. Se generara el nombre base del archivo
            base_filename = self._formatear_nombre_archivo(datos_generales)
            
            # 2. Se dividiran las transacciones en ingresos y egresos
            ingresos = [tx for tx in transacciones if tx.get('clasificacion') == 'Ingreso']
            egresos = [tx for tx in transacciones if tx.get('clasificacion') == 'Egreso']
            
            # 3. Se crearan las rutas de salida
            ruta_datos = output_dir / f"{base_filename}_DATOS.json"
            ruta_ingresos = output_dir / f"{base_filename}_INGRESOS.json"
            ruta_egresos = output_dir / f"{base_filename}_EGRESOS.json"
            
            # 4. Se asegurara que el directorio de salida exista
            output_dir.mkdir(parents=True, exist_ok=True)
            
            # 5. Se guardaran los tres archivos
            with open(ruta_datos, 'w', encoding='utf-8') as f:
                json.dump(datos_generales, f, indent=4, ensure_ascii=False, default=self._default_json_serializer)
            
            with open(ruta_ingresos, 'w', encoding='utf-8') as f:
                json.dump(ingresos, f, indent=4, ensure_ascii=False, default=self._default_json_serializer)
                
            with open(ruta_egresos, 'w', encoding='utf-8') as f:
                json.dump(egresos, f, indent=4, ensure_ascii=False, default=self._default_json_serializer)

            print(f"Resultados guardados exitosamente en 3 archivos con base: {base_filename}")
            
        except Exception as e:
            # Se manejara el error
            print(f"Error al guardar los 3 archivos de resultados: {e}")
            import traceback
            traceback.print_exc()

    def procesar_pdf(self, pdf_path, output_dir):
        """
        Se ejecutara el pipeline completo de extraccion hibrida y validacion.
        """
        # Se imprimira el inicio del proceso
        print(f"\n--- Iniciando Procesamiento Hibrido para: {pdf_path.name} ---")
        
        # --- Estrategia de Extraccion Hibrida ---
        
        # 1. Se ejecutara la extraccion Nativa (Metodo 1)
        print("Paso 1: Ejecutando extraccion Nativa (PyMuPDF)...")
        paginas_nativas = self._extract_text_native(pdf_path)
        
        # 2. Se ejecutara la extraccion OCR (Metodo 2)
        print("Paso 2: Ejecutando extraccion OCR (PaddleOCR)...")
        paginas_ocr = self._extract_text_ocr(pdf_path)
        
        # --- INICIO DE LA LOGICA DE REINTENTO ---
        # 3. Se detectara el banco y producto
        print("Paso 3: Detectando banco y producto...")
        # Se usara cualquier texto disponible (nativo u ocr) para la deteccion
        parser_key = self._detectar_banco_y_producto(paginas_nativas or paginas_ocr)
        print(f"Parser seleccionado: {parser_key.upper()}")

        if parser_key == "desconocido":
            # Se informara si el banco no es soportado
            print(f"ERROR: Banco no reconocido en {pdf_path.name}. Se necesita crear un parser.")
            return

        # 4. Se Parseara el texto (Intento 1: Nativo)
        print(f"Paso 4: Ejecutando parser especifico para '{parser_key}' (Intento 1: Nativo)...")
        metodo_usado = "Nativo"
        resultado_final = None
        try:
            resultado_final = self._parsear_texto(paginas_nativas, parser_key)
        except Exception as e:
            print(f"  > Error critico durante el parsing Nativo: {e}")
            resultado_final = None # Se asegura de que el resultado este vacio
            
        # 4.1. Se verificara si el parsing nativo fallo y se reintentara con OCR
        if not resultado_final or not resultado_final.get('datos_generales'):
            print(f"  > Advertencia: El texto nativo no pudo ser parseado. Reintentando con OCR.")
            metodo_usado = "OCR"
            try:
                resultado_final = self._parsear_texto(paginas_ocr, parser_key)
            except Exception as e:
                print(f"  > Error critico durante el parsing OCR: {e}")
                return # Se rinde si ambos metodos fallan

        # 4.2. Se verificara si ambos metodos fallaron
        if not resultado_final or not resultado_final.get('datos_generales'):
            print(f"ERROR: No se pudieron extraer datos ni con metodo Nativo ni con OCR.")
            return
            
        print(f"Parsing completado (usando metodo: {metodo_usado}).")
        # --- FIN DE LA LOGICA DE REINTENTO ---

        # 5. Se ejecutara la Validacion de Balance
        print("Paso 5: Ejecutando Validacion de Balance...")
        reporte_balance = validators.validar_balance(
            resultado_final['datos_generales'],
            resultado_final['transacciones']
        )
        # Se agregara el reporte al resultado
        resultado_final['validacion_balance'] = reporte_balance
        for msg in reporte_balance['mensajes']:
            # Se imprimiran los mensajes
            print(f"  > {msg}")

        # 6. Se ejecutara la Validacion Cruzada (Estrategia de doble extraccion)
        print("Paso 6: Ejecutando Validacion Cruzada (Nativo vs OCR)...")
        try:
            # Se parseara el texto nativo
            resultado_a = self._parsear_texto(paginas_nativas, parser_key)
            # Se parseara el texto ocr
            resultado_b = self._parsear_texto(paginas_ocr, parser_key)
            # Se ejecutara la validacion
            reporte_cruzado = validators.validar_cruzada(resultado_a, resultado_b)
            # Se agregara el reporte al resultado
            resultado_final['validacion_cruzada'] = reporte_cruzado
            for msg in reporte_cruzado['mensajes']:
                # Se imprimiran los mensajes
                print(f"  > {msg}")
        except Exception as e:
            # Se informara si falla la validacion cruzada
            print(f"  > Advertencia: No se pudo completar la validacion cruzada: {e}")

        # 7. Se guardaran los resultados en los 3 archivos
        print(f"Paso 7: Guardando resultados...")
        # Se guardara el archivo
        self.guardar_resultados(resultado_final, output_dir)
        
        # Se informara la finalizacion
        print(f"--- Procesamiento Finalizado para: {pdf_path.name} ---")


def main():
    """
    Funcion principal que escanea un directorio de entrada y procesa
    todos los archivos PDF que encuentra.
    """
    # Se asegurara que los directorios de entrada y salida existan
    INPUT_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)
    
    print(f"Iniciando el sistema de extraccion automatica.")
    print(f"Buscando archivos PDF en el directorio: {INPUT_DIR.resolve()}")
    
    # Se inicializara el extractor principal
    # Se ejecutara en modo CPU (optimizado sin GPU)
    # Para usar GPU: cambiar a use_gpu=True
    extractor = BankStatementExtractor(use_gpu=False)
    
    # Se buscaran todos los archivos PDF en la carpeta de entrada
    pdf_files = list(INPUT_DIR.glob("*.pdf"))
    
    if not pdf_files:
        print(f"No se encontraron archivos PDF en la carpeta '{INPUT_DIR}'.")
        print("Por favor, agregue estados de cuenta en PDF y vuelva a ejecutar el script.")
        return

    print(f"Se encontraron {len(pdf_files)} archivos PDF para procesar.")
    
    # Se procesara cada archivo PDF
    for pdf_path in pdf_files:
        try:
            extractor.procesar_pdf(pdf_path, OUTPUT_DIR)
        except Exception as e:
            print(f"ERROR INESPERADO: No se pudo procesar el archivo {pdf_path.name}.")
            print(f"Detalle: {e}")
            
    print("\nProcesamiento de todos los archivos completado.")


# --- Punto de Entrada del Script ---
if __name__ == "__main__":
    main()