# Se importan los módulos del sistema
import sys
import json
import fitz
from paddleocr import PaddleOCR
from decimal import Decimal
import os
import re
from pathlib import Path
from datetime import datetime

# Se importan los módulos locales
from parsers import banamex_empresa_parser, bbva_parser, inbursa_parser
from utils import validators

# Se suprimen las advertencias de PaddleOCR
import logging
logging.disable(logging.WARNING)
import warnings
warnings.filterwarnings('ignore')

# Se definen las rutas relativas al script
SCRIPT_DIR = Path(__file__).parent.resolve()
INPUT_DIR = SCRIPT_DIR / "input"
OUTPUT_DIR = SCRIPT_DIR / "output"

class BankStatementExtractor:
    """
    Se implementa el extractor principal del sistema.
    """
    
    MONTH_MAP = {
        'enero': 'JAN', 'feb': 'FEB', 'mar': 'MAR', 'abr': 'APR', 'may': 'MAY', 'jun': 'JUN',
        'jul': 'JUL', 'ago': 'AUG', 'sep': 'SEP', 'oct': 'OCT', 'nov': 'NOV', 'dic': 'DEC',
        'ENERO': 'JAN', 'FEB': 'FEB', 'MAR': 'MAR', 'ABR': 'APR', 'MAY': 'MAY', 'JUN': 'JUN',
        'JUL': 'JUL', 'AGO': 'AUG', 'SEP': 'SEP', 'OCT': 'OCT', 'NOV': 'NOV', 'DIC': 'DEC',
        'ENE': 'JAN',
    }
    
    def __init__(self, use_gpu=False):
        """
        Se inicializa el extractor.
        """
        self.use_gpu = use_gpu
        print("Inicializando motor OCR (PaddleOCR). Esto puede tomar un momento...")
        self.ocr_engine = PaddleOCR(use_angle_cls=True, lang='es')
        print("Motor OCR listo.")
        
        self.parsers = {
            "banamex_empresa": banamex_empresa_parser,
            "bbva_empresa": bbva_parser,
            "inbursa_empresa": inbursa_parser
        }

    def _extract_text_native(self, pdf_path):
        """
        Se extrae texto nativo pagina por pagina.
        Se devuelve lista de strings.
        """
        paginas_texto = []
        try:
            doc = fitz.open(pdf_path)
            for page in doc:
                paginas_texto.append(page.get_text("text"))
            doc.close()
        except Exception as e:
            print(f"Error en extraccion nativa: {e}")
        return paginas_texto

    def _extract_text_ocr(self, pdf_path):
        """
        Se extrae texto con OCR pagina por pagina.
        Se devuelve lista de strings.
        """
        paginas_texto = []
        try:
            resultado_ocr = self.ocr_engine.ocr(str(pdf_path))
            
            if resultado_ocr:
                for pagina_resultado in resultado_ocr:
                    texto_pagina_actual = ""
                    if pagina_resultado:
                        for linea in pagina_resultado:
                            texto_pagina_actual += linea[1][0] + "\n"
                    paginas_texto.append(texto_pagina_actual)
        except Exception as e:
            print(f"Error en extraccion OCR: {e}")
        return paginas_texto

    def _detectar_banco_y_producto(self, paginas_texto):
        """
        Se detecta el banco basado en el contenido.
        """
        if not paginas_texto:
            return "desconocido"
            
        texto_completo = "".join(paginas_texto)
        texto_lower = texto_completo.lower()
        
        if "bbva" in texto_lower:
            if "maestra pyme" in texto_lower or "versatil negocios" in texto_lower:
                return "bbva_empresa"
            return "bbva_empresa"
            
        if "inbursa" in texto_lower:
            if "inbursact empresarial" in texto_lower:
                return "inbursa_empresa"
            return "inbursa_empresa"
            
        if "banamex" in texto_lower:
            if "inmovitur" in texto_lower:
                return "banamex_empresa"
            return "banamex_empresa"
            
        return "desconocido"

    def _parsear_texto(self, paginas_texto, parser_key):
        """
        Se ejecuta el parser correspondiente.
        Se pasa lista de paginas al parser.
        """
        if parser_key in self.parsers:
            parser = self.parsers[parser_key]
            
            datos_generales = parser.parsear_datos_generales(paginas_texto)
            transacciones = parser.parsear_transacciones(paginas_texto, datos_generales.get('saldo_inicial', 0))
            
            return {"datos_generales": datos_generales, "transacciones": transacciones}
        else:
            raise NotImplementedError(f"El parser para la llave '{parser_key}' no esta implementado.")

    def _default_json_serializer(self, obj):
        """
        Se convierte Decimal a string para JSON.
        """
        if isinstance(obj, Decimal):
            return str(obj)
        raise TypeError(f"Objeto de tipo {obj.__class__.__name__} no es serializable en JSON")

    def _formatear_periodo(self, periodo_str):
        """
        Se formatea el periodo al formato requerido.
        """
        if not periodo_str or periodo_str == 'SIN_PERIODO':
            return "FECHA_INICIO", "FECHA_FIN"
        
        try:
            patron_del_al = re.search(r"DEL\s+(\d{2}/\d{2}/\d{4})\s+AL\s+(\d{2}/\d{2}/\d{4})", periodo_str, re.IGNORECASE)
            if patron_del_al:
                fecha_ini_str = patron_del_al.group(1)
                fecha_fin_str = patron_del_al.group(2)
                
                fecha_ini_obj = datetime.strptime(fecha_ini_str, "%d/%m/%Y")
                fecha_fin_obj = datetime.strptime(fecha_fin_str, "%d/%m/%Y")
                
                mes_ini = self.MONTH_MAP.get(fecha_ini_obj.strftime("%B").lower(), fecha_ini_obj.strftime("%b").upper())
                mes_fin = self.MONTH_MAP.get(fecha_fin_obj.strftime("%B").lower(), fecha_fin_obj.strftime("%b").upper())
                
                fecha_ini_formateada = f"{fecha_ini_obj.day:02d}{mes_ini}{fecha_ini_obj.year}"
                fecha_fin_formateada = f"{fecha_fin_obj.day:02d}{mes_fin}{fecha_fin_obj.year}"
                
                return fecha_ini_formateada, fecha_fin_formateada
        except Exception as e:
            print(f"Error al formatear periodo: {e}")
            return "FECHA_INICIO", "FECHA_FIN"
            
        return "FECHA_INICIO", "FECHA_FIN"

    def _formatear_nombre_archivo(self, datos_generales):
        """
        Se genera el nombre base del archivo.
        """
        nombre = datos_generales.get('nombre_empresa', 'SIN_NOMBRE')
        if not nombre: 
            nombre = 'SIN_NOMBRE'
        nombre_limpio = re.sub(r'[^A-Z0-9_\s]', '', str(nombre).upper())
        nombre_limpio = re.sub(r'\s+', '_', nombre_limpio.strip())
        
        periodo = datos_generales.get('periodo', 'SIN_PERIODO')
        if not periodo: 
            periodo = 'SIN_PERIODO'
        
        fecha_inicio, fecha_fin = self._formatear_periodo(periodo)
        
        return f"{nombre_limpio}_{fecha_inicio}_{fecha_fin}"

    def guardar_resultados(self, resultado_completo, output_dir):
        """
        Se guardan los resultados en 3 archivos JSON.
        """
        try:
            datos_generales = resultado_completo.get('datos_generales', {})
            transacciones = resultado_completo.get('transacciones', [])
            
            base_filename = self._formatear_nombre_archivo(datos_generales)
            
            ingresos = [tx for tx in transacciones if tx.get('clasificacion') == 'Ingreso']
            egresos = [tx for tx in transacciones if tx.get('clasificacion') == 'Egreso']
            
            ruta_datos = output_dir / f"{base_filename}_DATOS.json"
            ruta_ingresos = output_dir / f"{base_filename}_INGRESOS.json"
            ruta_egresos = output_dir / f"{base_filename}_EGRESOS.json"
            
            output_dir.mkdir(parents=True, exist_ok=True)
            
            with open(ruta_datos, 'w', encoding='utf-8') as f:
                json.dump(datos_generales, f, indent=4, ensure_ascii=False, default=self._default_json_serializer)
            
            with open(ruta_ingresos, 'w', encoding='utf-8') as f:
                json.dump(ingresos, f, indent=4, ensure_ascii=False, default=self._default_json_serializer)
                
            with open(ruta_egresos, 'w', encoding='utf-8') as f:
                json.dump(egresos, f, indent=4, ensure_ascii=False, default=self._default_json_serializer)

            print(f"Resultados guardados exitosamente en 3 archivos con base: {base_filename}")
            
        except Exception as e:
            print(f"Error al guardar los 3 archivos de resultados: {e}")
            import traceback
            traceback.print_exc()

    def procesar_pdf(self, pdf_path, output_dir):
        """
        Se ejecuta el pipeline completo de extraccion.
        """
        print(f"\n--- Iniciando Procesamiento Hibrido para: {pdf_path.name} ---")
        
        print("Paso 1: Ejecutando extraccion Nativa (PyMuPDF)...")
        paginas_nativas = self._extract_text_native(pdf_path)
        
        print("Paso 2: Ejecutando extraccion OCR (PaddleOCR)...")
        paginas_ocr = self._extract_text_ocr(pdf_path)
        
        print("Paso 3: Detectando banco y producto...")
        parser_key = self._detectar_banco_y_producto(paginas_nativas or paginas_ocr)
        print(f"Parser seleccionado: {parser_key.upper()}")

        if parser_key == "desconocido":
            print(f"ERROR: Banco no reconocido en {pdf_path.name}. Se necesita crear un parser.")
            return

        print(f"Paso 4: Ejecutando parser especifico para '{parser_key}' (Intento 1: Nativo)...")
        resultado_final = None
        try:
            resultado_final = self._parsear_texto(paginas_nativas, parser_key)
            print("Parsing completado.")
        except Exception as e:
            print(f"  > Advertencia: El texto nativo no pudo ser parseado. Reintentando con OCR.")
            try:
                resultado_final = self._parsear_texto(paginas_ocr, parser_key)
                print("Parsing completado con OCR.")
            except Exception as e2:
                print(f"ERROR: No se pudieron extraer datos ni con metodo Nativo ni con OCR.")
                print(f"Detalle: {e2}")
                return

        print("Paso 5: Ejecutando Validacion de Balance...")
        reporte_balance = validators.validar_balance(
            resultado_final['datos_generales'],
            resultado_final['transacciones']
        )
        resultado_final['validacion_balance'] = reporte_balance
        for msg in reporte_balance['mensajes']:
            print(f"  > {msg}")

        print("Paso 6: Ejecutando Validacion Cruzada (Nativo vs OCR)...")
        try:
            resultado_a = self._parsear_texto(paginas_nativas, parser_key)
            resultado_b = self._parsear_texto(paginas_ocr, parser_key)
            reporte_cruzado = validators.validar_cruzada(resultado_a, resultado_b)
            resultado_final['validacion_cruzada'] = reporte_cruzado
            for msg in reporte_cruzado['mensajes']:
                print(f"  > {msg}")
        except Exception as e:
            print(f"  > Advertencia: No se pudo completar la validacion cruzada: {e}")

        print(f"Paso 7: Guardando resultados...")
        self.guardar_resultados(resultado_final, output_dir)
        
        print(f"--- Procesamiento Finalizado para: {pdf_path.name} ---")


def main():
    """
    Se ejecuta la funcion principal del sistema.
    """
    INPUT_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)
    
    print(f"Iniciando el sistema de extraccion automatica.")
    print(f"Buscando archivos PDF en el directorio: {INPUT_DIR.resolve()}")
    
    extractor = BankStatementExtractor(use_gpu=False)
    
    pdf_files = list(INPUT_DIR.glob("*.pdf"))
    
    if not pdf_files:
        print(f"No se encontraron archivos PDF en la carpeta '{INPUT_DIR}'.")
        print("Por favor, agregue estados de cuenta en PDF y vuelva a ejecutar el script.")
        return

    print(f"Se encontraron {len(pdf_files)} archivos PDF para procesar.")
    
    for pdf_path in pdf_files:
        try:
            extractor.procesar_pdf(pdf_path, OUTPUT_DIR)
        except Exception as e:
            print(f"ERROR INESPERADO: No se pudo procesar el archivo {pdf_path.name}.")
            print(f"Detalle: {e}")
            
    print("\nProcesamiento de todos los archivos completado.")


if __name__ == "__main__":
    main()