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
from parsers import banamex_empresa_parser, banamex_personal_parser, bbva_parser, inbursa_parser
from utils import validators

# Se suprimiran las advertencias de PaddleOCR
import logging
logging.disable(logging.WARNING)
import warnings
warnings.filterwarnings('ignore')

# --- CONFIGURACION DE RUTAS ---
INPUT_DIR = Path("input")
OUTPUT_DIR = Path("output")
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
        # Se deshabilitara el log para una salida limpia
        self.ocr_engine = PaddleOCR(use_angle_cls=True, lang='es', use_gpu=self.use_gpu, show_log=False)
        print("Motor OCR listo.")
        
        # Se definira el mapa de parsers
        # Se registraran los parsers especificos que hemos creado
        self.parsers = {
            "banamex_empresa": banamex_empresa_parser,
            "banamex_personal": banamex_personal_parser,
            "bbva_empresa": bbva_parser,
            "inbursa_empresa": inbursa_parser
            # Se agregaran futuros parsers aqui (ej. "banorte_empresa": banorte_parser)
        }

    def _extract_text_native(self, pdf_path):
        """
        Se extraera el texto seleccionable (nativo) del PDF usando PyMuPDF.
        """
        # Se inicializara el texto
        texto_completo = ""
        try:
            # Se abrira el documento
            doc = fitz.open(pdf_path)
            # Se iterara sobre cada pagina
            for page in doc:
                # Se extraera el texto
                texto_completo += page.get_text("text")
            # Se cerrara el documento
            doc.close()
        except Exception as e:
            # Se manejara la excepcion
            print(f"Error en extraccion nativa: {e}")
        # Se retornara el texto
        return texto_completo

    def _extract_text_ocr(self, pdf_path):
        """
        Se extraera texto usando OCR (PaddleOCR).
        Esto funciona para PDFs basados en imagenes o texto no seleccionable.
        """
        # Se inicializara el texto
        texto_completo = ""
        try:
            # Se ejecutara el OCR sobre el archivo
            resultado_ocr = self.ocr_engine.ocr(str(pdf_path), cls=True)
            # Se iterara sobre los resultados de cada pagina
            for pagina in resultado_ocr:
                # Se iterara sobre cada linea detectada
                if pagina:
                    for linea in pagina:
                        # Se agregara el texto de la linea
                        texto_completo += linea[1][0] + "\n"
        except Exception as e:
            # Se manejara la excepcion
            print(f"Error en extraccion OCR: {e}")
        # Se retornara el texto
        return texto_completo

    def _detectar_banco_y_producto(self, texto):
        """
        Se detectara el banco y el producto basado en palabras clave unicas.
        Este es el "router" que selecciona el parser correcto.
        """
        # Se convertira a minusculas
        texto_lower = texto.lower()
        
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
            if "micuenta" in texto_lower:
                return "banamex_personal"
            if "inmovitur" in texto_lower:
                return "banamex_empresa"
            if "costco banamex" in texto_lower:
                return "desconocido" # (Parser de Tarjeta de Credito no implementado)
            
            # Se usara un default si no se puede especificar
            return "banamex_personal" 
            
        # Se retornara desconocido si no hay coincidencias
        return "desconocido"

    def _parsear_texto(self, texto, parser_key):
        """
        Se seleccionara el parser correcto desde el mapa y se ejecutara.
        """
        # Se verificara si el parser existe
        if parser_key in self.parsers:
            # Se obtendra el modulo del parser
            parser = self.parsers[parser_key]
            
            # Se llamara al "contrato" (la interfaz) que todos los parsers deben tener
            # Se ejecutara el parsing de datos generales
            datos_generales = parser.parsear_datos_generales(texto)
            # Se ejecutara el parsing de transacciones
            transacciones = parser.parsear_transacciones(texto, datos_generales.get('saldo_inicial', 0))
            
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
            # Formato 1: BBVA (DEL 01/08/2025 AL 31/08/2025)
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
                
            # Formato 4: Banamex Personal (Período del 18 de mayo al 17 de junio del 2025)
            match4 = re.match(r"Período del (\d{1,2}) de (\w+) al (\d{1,2}) de (\w+) del (\d{4})", periodo_str, re.IGNORECASE)
            if match4:
                mes_inicio = self.MONTH_MAP.get(match4.group(2).lower(), 'JAN')
                mes_fin = self.MONTH_MAP.get(match4.group(4).lower(), 'JAN')
                f_inicio = datetime.strptime(f"{match4.group(1)}-{mes_inicio}-{match4.group(5)}", "%d-%b-%Y")
                f_fin = datetime.strptime(f"{match4.group(3)}-{mes_fin}-{match4.group(5)}", "%d-%b-%Y")
                return f_inicio.strftime('%d%b%Y').upper(), f_fin.strftime('%d%b%Y').upper()

        except Exception as e:
            print(f"  > Advertencia: No se pudo formatear el periodo '{periodo_str}'. Error: {e}")
            return "FECHA_INICIO", "FECHA_FIN"
            
        # Se retornaran valores por defecto si ningun formato coincide
        return "FECHA_INICIO", "FECHA_FIN"

    def _formatear_nombre_archivo(self, datos_generales):
        """
        Se generara el nombre base del archivo usando los datos extraidos.
        Formato: NOMBREEMPRESA_FECHAINICIO_FECHAFIN
        """
        # Se obtendra el nombre de la empresa, se limpiara y se convertira a mayusculas
        nombre = datos_generales.get('nombre_empresa', 'SIN_NOMBRE')
        if not nombre: nombre = 'SIN_NOMBRE'
        nombre_limpio = re.sub(r'[^A-Z0-9_\s]', '', nombre.upper())
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
        texto_nativo = self._extract_text_native(pdf_path)
        
        # 2. Se ejecutara la extraccion OCR (Metodo 2)
        print("Paso 2: Ejecutando extraccion OCR (PaddleOCR)...")
        texto_ocr = self._extract_text_ocr(pdf_path)
        
        # Se seleccionara el mejor texto (estrategia: el mas largo)
        if len(texto_nativo) > 200:
            texto_final = texto_nativo
            metodo_usado = "Nativo"
        else:
            texto_final = texto_ocr
            metodo_usado = "OCR"
            
        print(f"Paso 2.1: Metodo de extraccion seleccionado: {metodo_usado} ({len(texto_final)} caracteres)")

        if len(texto_final) < 100:
            # Se informara si no se pudo extraer texto
            print(f"ERROR: No se pudo extraer texto significativo de {pdf_path.name}.")
            return

        # 3. Se detectara el banco y producto
        print("Paso 3: Detectando banco y producto...")
        parser_key = self._detectar_banco_y_producto(texto_final)
        print(f"Parser seleccionado: {parser_key.upper()}")

        if parser_key == "desconocido":
            # Se informara si el banco no es soportado
            print(f"ERROR: Banco no reconocido en {pdf_path.name}. Se necesita crear un parser.")
            return

        # 4. Se Parseara el texto
        print(f"Paso 4: Ejecutando parser especifico para '{parser_key}'...")
        resultado_final = None
        try:
            # Se obtendran los resultados del parser principal
            resultado_final = self._parsear_texto(texto_final, parser_key)
            print("Parsing completado.")
        except NotImplementedError as e:
            # Se manejara el error de parser no implementado
            print(e)
            return
        except Exception as e:
            # Se manejara un error general de parsing
            print(f"Error critico durante el parsing de {pdf_path.name}: {e}")
            return

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
            resultado_a = self._parsear_texto(texto_nativo, parser_key)
            # Se parseara el texto ocr
            resultado_b = self._parsear_texto(texto_ocr, parser_key)
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
        print("No se encontraron archivos PDF en la carpeta 'input'.")
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