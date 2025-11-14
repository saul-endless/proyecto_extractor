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
from utils.image_preprocessing import prepare_image_for_ocr
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
        
        # Forzar uso de CPU si use_gpu=False
        if not self.use_gpu:
            import os
            os.environ['CUDA_VISIBLE_DEVICES'] = '-1'
        
        # Suprimir logs de PaddleOCR
        import logging
        logging.getLogger('ppocr').setLevel(logging.ERROR)
        
        print("Inicializando motor OCR (PaddleOCR). Esto puede tomar un momento...")
        
        # Inicializar PaddleOCR SIN el parámetro use_gpu
        self.ocr_engine = PaddleOCR(
            lang='es',
            use_angle_cls=True,
            det_db_thresh=0.2,
            det_db_box_thresh=0.3,
            rec_batch_num=16,
            det_limit_side_len=3000,
            det_limit_type='max'
        )
        
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
        Se extrae texto con OCR página por página CON PREPROCESAMIENTO.
        CRÍTICO: Preprocesa imágenes antes de OCR para mejorar detección.
        """
        paginas_texto = []
        try:
            doc = fitz.open(pdf_path)
            
            for page_num in range(len(doc)):
                page = doc[page_num]
                
                try:
                    # CRÍTICO: Preprocesar imagen antes de OCR
                    img_preprocessed = prepare_image_for_ocr(page, enhance_tables=True)
                    
                    # Ejecutar OCR en imagen preprocesada SIN cls
                    resultado_ocr = self.ocr_engine.ocr(img_preprocessed)  # ← SIN cls=True
                    
                    # Extraer texto de resultados
                    texto_pagina_actual = ""
                    if resultado_ocr and len(resultado_ocr) > 0 and resultado_ocr[0]:
                        for linea in resultado_ocr[0]:
                            if linea and len(linea) >= 2:
                                texto_pagina_actual += linea[1][0] + "\n"
                    
                    paginas_texto.append(texto_pagina_actual)
                    
                except Exception as e_page:
                    print(f"  > Error procesando página {page_num + 1} con OCR: {e_page}")
                    paginas_texto.append("")
            
            doc.close()
            
        except Exception as e:
            print(f"Error en extracción OCR: {e}")
        
        return paginas_texto

    def _detectar_banco_y_producto(self, paginas_texto):
        """
        Detecta el banco basado en el contenido.
        MEJORADO: Múltiples patrones de detección.
        """
        if not paginas_texto:
            return "desconocido"
            
        texto_completo = "".join(paginas_texto)
        texto_lower = texto_completo.lower()
        
        # Detectar BBVA con múltiples patrones
        if "bbva" in texto_lower or "bancomer" in texto_lower:
            if any(palabra in texto_lower for palabra in ["maestra pyme", "versatil negocios", "empresas"]):
                return "bbva_empresa"
            return "bbva_empresa"
        
        # Detectar Inbursa
        if "inbursa" in texto_lower:
            if "inbursact empresarial" in texto_lower or "empresarial" in texto_lower:
                return "inbursa_empresa"
            return "inbursa_empresa"
        
        # Detectar Banamex
        if "banamex" in texto_lower or "citibanamex" in texto_lower:
            if "inmovitur" in texto_lower or "empresarial" in texto_lower:
                return "banamex_empresa"
            return "banamex_empresa"
            
        return "desconocido"

    def _parsear_texto_mejorado(self, paginas_texto, parser_key):
        """
        Se ejecuta el parser mejorado de BBVA v2.0.
        SOLO para BBVA - usa la nueva estructura.
        """
        if parser_key == "bbva_empresa":
            parser = self.parsers[parser_key]
            
            # Convertir lista de páginas a texto completo
            texto_completo = "\n".join(paginas_texto)
            
            # El nuevo parser de BBVA espera texto completo
            resultado = parser.parse_bbva_empresa(texto_completo)
            
            # Adaptar formato de salida al esperado
            return {
                "datos_generales": resultado['metadata'],
                "transacciones": resultado['transactions']
            }
        else:
            # Para otros bancos, usar método original
            return self._parsear_texto(paginas_texto, parser_key)

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
        Se guardan los resultados en 3 archivos JSON con la estructura objetivo.
        """
        try:
            datos_generales = resultado_completo.get('datos_generales', {})
            transacciones = resultado_completo.get('transacciones', [])
            
            # CRÍTICO: Eliminar campos auxiliares antes de guardar
            datos_generales_limpios = {k: v for k, v in datos_generales.items() if not k.startswith('_')}
            
            # Se obtienen los valores para el nombre del archivo
            nombre_empresa = datos_generales_limpios.get('Nombre de la empresa del estado de cuenta', 'SIN_NOMBRE')
            periodo = datos_generales_limpios.get('Periodo del estado de cuenta', 'SIN_PERIODO')
            
            # Se limpia el nombre de la empresa para el nombre del archivo
            nombre_limpio = re.sub(r'[^A-Z0-9_\s]', '', str(nombre_empresa).upper())
            nombre_limpio = re.sub(r'\s+', '_', nombre_limpio.strip())
            
            # Se construye el nombre base del archivo
            base_filename = f"{nombre_limpio}_{periodo}"
            
            # Se separan las transacciones por clasificacion
            ingresos = [tx for tx in transacciones if tx.get('Clasificación') == 'Ingreso']
            egresos = [tx for tx in transacciones if tx.get('Clasificación') == 'Egreso']
            
            # Se definen las rutas de los archivos
            ruta_datos = output_dir / f"{base_filename}_DATOS.json"
            ruta_ingresos = output_dir / f"{base_filename}_INGRESOS.json"
            ruta_egresos = output_dir / f"{base_filename}_EGRESOS.json"
            
            # Se crea el directorio de salida si no existe
            output_dir.mkdir(parents=True, exist_ok=True)
            
            # Se guarda el archivo de datos generales (sin campos auxiliares)
            with open(ruta_datos, 'w', encoding='utf-8') as f:
                json.dump(datos_generales_limpios, f, indent=4, ensure_ascii=False)
            
            # Se guarda el archivo de ingresos
            with open(ruta_ingresos, 'w', encoding='utf-8') as f:
                json.dump(ingresos, f, indent=4, ensure_ascii=False)
            
            # Se guarda el archivo de egresos
            with open(ruta_egresos, 'w', encoding='utf-8') as f:
                json.dump(egresos, f, indent=4, ensure_ascii=False)
            
            print(f"Resultados guardados exitosamente en 3 archivos con base: {base_filename}")
            print(f"  - Datos generales: {ruta_datos.name}")
            print(f"  - Ingresos ({len(ingresos)} transacciones): {ruta_ingresos.name}")
            print(f"  - Egresos ({len(egresos)} transacciones): {ruta_egresos.name}")
            
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
            # Usar parser mejorado para BBVA
            if parser_key == "bbva_empresa":
                resultado_final = self._parsear_texto_mejorado(paginas_nativas, parser_key)
            else:
                resultado_final = self._parsear_texto(paginas_nativas, parser_key)
            
            print("Parsing completado.")
            
            # Imprimir estadísticas
            num_transacciones = len(resultado_final.get('transacciones', []))
            print(f"  > Se extrajeron {num_transacciones} transacciones")
            
        except Exception as e:
            print(f"  > Advertencia: El texto nativo no pudo ser parseado. Reintentando con OCR.")
            print(f"  > Error nativo: {e}")
            try:
                # Usar parser mejorado para BBVA
                if parser_key == "bbva_empresa":
                    resultado_final = self._parsear_texto_mejorado(paginas_ocr, parser_key)
                else:
                    resultado_final = self._parsear_texto(paginas_ocr, parser_key)
                
                print("Parsing completado con OCR.")
                
                # Imprimir estadísticas
                num_transacciones = len(resultado_final.get('transacciones', []))
                print(f"  > Se extrajeron {num_transacciones} transacciones")
                
            except Exception as e2:
                print(f"ERROR: No se pudieron extraer datos ni con metodo Nativo ni con OCR.")
                print(f"Detalle nativo: {e}")
                print(f"Detalle OCR: {e2}")
                import traceback
                traceback.print_exc()
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
            # Usar parser correspondiente
            if parser_key == "bbva_empresa":
                resultado_a = self._parsear_texto_mejorado(paginas_nativas, parser_key)
                resultado_b = self._parsear_texto_mejorado(paginas_ocr, parser_key)
            else:
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