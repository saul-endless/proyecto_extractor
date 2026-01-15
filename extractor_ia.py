# -*- coding: utf-8 -*-
"""
================================================================================
EXTRACTOR DE ESTADOS DE CUENTA BANCARIOS - VERSIÓN 6.8
================================================================================
"""

import os
import sys
import json
import re
import gc
import torch
import hashlib
from pdf2image import convert_from_path
from transformers import Qwen2_5_VLForConditionalGeneration, AutoProcessor
from qwen_vl_utils import process_vision_info
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from enum import Enum
import logging
from collections import defaultdict
import time

# Configurar logging
logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# =============================================================================
# CONFIGURACIÓN DEL MODELO
# =============================================================================

RUTA_LOCAL_MODELO = "/home/endless/FUNCIONALIDADES/PRUEBA MODELOS/MODELOS/Qwen2.5-VL-7B-Instruct"
DISPOSITIVO = "cuda"
VENTANA_CONTEXTO = 32000

CONFIG_GENERACION = {
    "temperature": 0.0,
    "do_sample": False,
    "repetition_penalty": 1.05,
    "top_p": 1.0,
    "top_k": 1,
}

CONFIG_IMAGEN = {
    "dpi": 400,
    "formato": "PNG",
}

# Configuración de reintentos para páginas problemáticas
CONFIG_EXTRACCION_V67 = {
    "max_reintentos": 2,  # Reducido
    "tokens_por_intento": [10000, 14000],  # Más tokens iniciales
    "umbral_transacciones_minimo": 2,  # Reducido
    "timeout_por_pagina": 90,  # Reducido
}

# =============================================================================
# ENUMERACIONES
# =============================================================================

class Banco(Enum):
    BBVA = "BBVA"
    INBURSA = "INBURSA"
    CITIBANAMEX = "CITIBANAMEX"
    SANTANDER = "SANTANDER"
    BANORTE = "BANORTE"
    HSBC = "HSBC"
    SCOTIABANK = "SCOTIABANK"
    GENERICO = "GENERICO"

class TipoPagina(Enum):
    MOVIMIENTOS = "MOVIMIENTOS"
    RESUMEN = "RESUMEN"
    RESUMEN_CON_MOVIMIENTOS = "RESUMEN_CON_MOVIMIENTOS"
    CFDI = "CFDI"
    GRAFICO = "GRAFICO"
    INFORMATIVA = "INFORMATIVA"
    VACIA = "VACIA"

# =============================================================================
# MAPEO DE MESES
# =============================================================================

MESES = {
    "ENE": "01", "FEB": "02", "MAR": "03", "ABR": "04", "MAY": "05", "JUN": "06",
    "JUL": "07", "AGO": "08", "SEP": "09", "OCT": "10", "NOV": "11", "DIC": "12",
    "ENERO": "01", "FEBRERO": "02", "MARZO": "03", "ABRIL": "04", "MAYO": "05",
    "JUNIO": "06", "JULIO": "07", "AGOSTO": "08", "SEPTIEMBRE": "09", "SEPT": "09",
    "OCTUBRE": "10", "NOVIEMBRE": "11", "DICIEMBRE": "12",
    "JAN": "01", "FEB": "02", "MAR": "03", "APR": "04", "MAY": "05", "JUN": "06",
    "JUL": "07", "AUG": "08", "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12",
}

MES_A_TEXTO = {
    "01": "ENE", "02": "FEB", "03": "MAR", "04": "ABR", "05": "MAY", "06": "JUN",
    "07": "JUL", "08": "AGO", "09": "SEP", "10": "OCT", "11": "NOV", "12": "DIC"
}

# =============================================================================
# PALABRAS CLAVE PARA EXCLUSIÓN
# =============================================================================

PALABRAS_BANCO_INVALIDAS = [
    "INSTITUCION DE BANCA", "GRUPO FINANCIERO", "BANCO INBURSA",
    "BBVA MEXICO", "BBVA BANCOMER", "SANTANDER MEXICO", "CITIBANAMEX",
    "BANAMEX", "BANCO NACIONAL", "ESTADO DE CUENTA", "RESUMEN DE CUENTA",
    "AVISO DE PRIVACIDAD", "GAT REAL", "GAT NOMINAL", "CONDUSEF",
    "PASEO DE LA REFORMA", "PASEO DE LAS PALMAS"
]

FILAS_IGNORAR = [
    "SALDO INICIAL", "SALDO ANTERIOR", "SALDO FINAL", "SALDO ACTUAL",
    "SALDO AL CORTE", "SALDO DE APERTURA", "SALDO DE CIERRE",
    "SALDO DISPONIBLE", "SALDO DE LIQUIDACION", "SALDO DE OPERACION", 
    "SALDO PROMEDIO", "BALANCE INICIAL", "BALANCE FINAL",
    "TOTAL DE MOVIMIENTOS", "TOTAL CARGOS", "TOTAL ABONOS",
    "SUMA DE CARGOS", "SUMA DE ABONOS", "TOTALES", "SUBTOTAL",
    "TOTAL IMPORTE", "TOTAL MOVIMIENTOS", "TOTAL IMPORTE CARGOS",
    "TOTAL IMPORTE ABONOS", "TOTAL MOVIMIENTOS CARGOS", "TOTAL MOVIMIENTOS ABONOS",
    "OTROS CARGOS", "OTROS CARGOS (-)", "COMISIONES (-)",
    "DEPÓSITOS / ABONOS", "DEPOSITOS / ABONOS", "DEPOSITOS / ABONOS (+)",
    "RETIROS / CARGOS", "RETIROS / CARGOS (-)", "SALDO FINAL (+)",
    "SALDO DE LIQUIDACIÓN INICIAL", "COMPORTAMIENTO", "RENDIMIENTO",
    "DETALLE DE OPERACIONES", "RESUMEN DE SALDOS", "RESUMEN GRAFICO"
]
# =============================================================================
# FILTROS PARA TRANSACCIONES FANTASMA (TOTALES DEL RESUMEN)
# =============================================================================

PATRONES_LINEA_RESUMEN_V68 = [
    r'^Dep[óo]sitos?\s*/?\s*Abonos?\s*\(?[+\-]\)?',
    r'^Retiros?\s*/?\s*Cargos?\s*\(?[+\-]\)?',
    r'^Total\s+(de\s+)?(dep[óo]sitos?|retiros?|abonos?|cargos?)',
    r'^Saldo\s+(inicial|final|promedio|anterior)',
    r'^(Cantidad|Total)\s+total\s+de',
    r'^\s*Deposito\s+en\s+cuenta\s*$',
    r'^Intereses?\s+a\s+favor\s*\([+\-]\)',
]

FECHAS_INVALIDAS_V68 = ['DD/MM', '00/00', '01/01/2025']



# =============================================================================
# MAPEO CLABE → BANCO
# =============================================================================

CLABE_A_BANCO = {
    "002": "CITIBANAMEX", "012": "BBVA", "014": "SANTANDER",
    "021": "HSBC", "030": "BAJIO", "036": "INBURSA",
    "044": "SCOTIABANK", "058": "BANREGIO", "072": "BANORTE",
    "106": "BANXICO", "127": "AZTECA", "137": "MULTIVA",
    "128": "AUTOFIN", "138": "COMPARTAMOS", "166": "BANSEFI",
}

# =============================================================================
# PATRONES DE CLASIFICACIÓN AMPLIADOS Y MEJORADOS
# =============================================================================

PATRONES_EGRESO_ABSOLUTO = [
    # === TRANSFERENCIAS Y PAGOS SALIENTES ===
    r"^TRASPASO\s+SPEI\s+INBURED\s+(?!.*RECIBIDO)",
    r"TRASPASO\s+SPEI\s+INBURED\s+BENEFICIARIO",
    r"TRASPASO\s+SPEI\s+INBURED\s+[A-Z]",
    r"^T17\s+SPEI\s+ENVIADO",
    r"^T17\s+",
    r"SPEI\s+ENVIADO\s+",
    r"SPEI\s+ENVIADO$",
    r"PAGO\s+INTERBANCARIO\s+A\s+",
    r"^PAGO\s+INTERBANCARIO\s+A",
    r"PAGO\s+INTERBANCARIO\s+A\s+\w+\s+AL\s+BENEF",
    r"PAGO\s+INTERBANCARIO\s+A\s*BBVA",
    r"PAGO\s+INTERBANCARIO\s+A\s*SANTANDER",
    r"PAGO\s+INTERBANCARIO\s+A\s*SCOTIABANK",
    r"PAGO\s+INTERBANCARIO\s+A\s*HSBC",
    r"PAGO\s+INTERBANCARIO\s+A\s*BANORTE",
    r"PAGO\s+INTERBANCARIO\s+A\s*CITI",
    r"PAGO\s+INTERBANCARIO\s+A\s*BANAMEX",
    r"PAGO\s+INTERBANCARIO\s+A\s*INBURSA",
    r"PAGO\s+INTERBANCARIO\s+A\s*STP",
    r"PAGO\s+INTERBANCARIO\s+A\s*AZTECA",
    r"PAGO\s+A\s+TERCEROS",
    r"^N06\s+PAGO\s+CUENTA\s+DE\s+TERCERO",
    r"^N06\s+",
    r"PAGO\s+CUENTA\s+DE\s+TERCERO",
    r"^P31\s+",
    r"^P14\s+",
    r"^P12\s+",
    r"^R01\s+PAGO\s+DE\s+NOMINA",
    r"^R01\s+",
    r"PAGO\s+DE\s+NOMINA",
    r"PAGO\s+NOMINA",
    r"^C03\s+CHEQUE\s+PAGADO",
    r"^C03\s+",
    r"CHEQUE\s+PAGADO",
    r"CHEQUE\s+NO\.?\s*\d+",
    r"^G30\s+",
    r"^S39\s+",
    r"^S40\s+",
    r"^X01\s+",
    r"^C68\s+",
    r"^A15\s+",
    r"^CA9\s+",
    
    # === COMISIONES E IMPUESTOS ===
    r"^COMISION\s+",
    r"^COMISION$",
    r"COMISION\s+POR\s+",
    r"COMISION\s+MANEJO",
    r"COMISION\s+MENSUALIDAD",
    r"COMISION\s+\d+\s+PAGO\s+INT",
    r"COMISION\s+\d+\s+MENSUALIDAD",
    r"COBRO\s+DE\s+COMISION",
    r"COBRO\s+COMISION",
    r"COBRO\s+DE\s+COMIS",
    r"COBRO\s+COM\s+",
    r"COBRO\s+COM\s+CUOT",
    r"COBRO\s+IMP\s+COM",
    r"COBRO\s+IMP\s+TPV",
    r"COBRO\s+DE\s+IVA",
    r"COBRO\s+DE\s+IMP",
    r"^IVA\s+COMISION",
    r"IVA\s+COMISION\s+",
    r"IVA\s+COMISION\s+MANEJO",
    r"IVA\s+COMISION\s+POR\s+MOVIMIENTOS",
    r"^ISR\s+RETENIDO",
    r"ISR\s+RETENIDO\s+",
    r"ISR\s+RETENIDO$",
    r"^CARGO\s+EN\s+CUENTA",
    r"^ANUALIDAD",
    
    # === DOMICILIACIONES ===
    r"^DOMICILIACION",
    r"^DOMI\s+",
    r"DOMI\s+AMERICAN\s+EXPRESS",
    r"AUT\s+DOMI\s+\d+",
    
    # === COBROS AUTOMÁTICOS - V6.7 NUEVOS ===
    r"^H09\s+COBRO\s+AUTOMATICO",
    r"^H09\s+",
    r"COBRO\s+AUTOMATICO\s+RECIBO",
    r"COBRO\s+AUTOMATICO\s+PREST",
    r"^S01\s+",
    r"^S02\s+",
    
    # === SERVICIOS Y COMERCIOS ===
    r"GOOGLE\s*\*?",
    r"FACEBOOK\s*\*?",
    r"META\s+PLATFORMS",
    r"AMAZON\s*\*?",
    r"AWS\s*\*?",
    r"UBER\s*\*?",
    r"DIDI\s*\*?",
    r"NETFLIX",
    r"SPOTIFY",
    r"APPLE",
    r"PAYPAL",
    r"MERCADO\s*PAGO",
    r"MERPAGO",
    r"CFE\s+SUMINISTRADOR",
    r"^CFE\s+",
    r"TELMEX",
    r"IZZI",
    r"TOTALPLAY",
    r"MEGACABLE",
    r"^SAT\s+",
    r"^IMSS\s+",
    r"^INFONAVIT",
    r"GASOL",
    r"GASOLINERA",
    r"ESTACION\s+DE\s+SERV",
    r"GAS\s+FRACC",
    r"OXXO",
    r"7.?ELEVEN",
    r"STARBUCKS",
    r"CHILIS",
    r"SUSHI",
    r"RESTAURANT",
    r"BODEGA",
    r"SAMS\s+",
    r"COSTCO",
    r"WALMART",
    r"LIVERPOOL",
    r"PALACIO\s+DE\s+HIERRO",
    r"SERV\s+ROKY",
    r"TCCF\s+",
    r"LOS\s+BISQUETS",
    r"GME\d+\s+SW",
    r"AERO\s+Y\s+SERVI",
    r"SEGURIDAD\s+PRIVADA",
    
    # === PATRONES GENÉRICOS DE EGRESO ===
    r"RETIRO\s+",
    r"^RETIRO\s+",
    r"RETIRO\s+ATM",
    r"RETIRO\s+CAJERO",
    r"CARGO\s+",
    r"^CARGO\s+",
    r"PAGO\s+TDC",
    r"PAGO\s+TARJETA",
    r"DISPOSICION\s+",
    r"COMPRA\s+",
    r"^COMPRA\s+",
]

PATRONES_INGRESO_ABSOLUTO = [
    # === DEPÓSITOS Y TRANSFERENCIAS ENTRANTES ===
    r"PAGO\s+RECIBIDO\s+DE\s+",
    r"^PAGO\s+RECIBIDO\s+DE",
    r"PAGO\s+RECIBIDO\s+DE\s+\w+",
    r"PAGO\s+RECIBIDO\s+DE\s+BBVA",
    r"PAGO\s+RECIBIDO\s+DE\s+SANTANDER",
    r"PAGO\s+RECIBIDO\s+DE\s+SCOTIABANK",
    r"PAGO\s+RECIBIDO\s+DE\s+BANAMEX",
    r"PAGO\s+RECIBIDO\s+DE\s+BANCO\s+ACTINVER",
    r"PAGO\s+RECIBIDO\s+DE\s+JP\s+MORGAN",
    r"PAGO\s+RECIBIDO\s+DE\s+SIST\s+TRANSFY",
    r"PAGO\s+RECIBIDO\s+DE\s+STP",
    r"PAGO\s+RECIBIDO\s+DE\s+HSBC",
    r"PAGO\s+RECIBIDO\s+DE\s+BANORTE",
    r"^DEPOSITO\s+SPEI\b",
    r"DEPOSITO\s+SPEI\s+[A-Z]",
    r"^T20\s+SPEI\s+RECIBIDO",
    r"^T20\s+",
    r"SPEI\s+RECIBIDO",
    r"^DEPOSITO\s+INBURED\b",
    r"DEPOSITO\s+INBURED\s+",
    r"^DEPOSITO\s+EFECTIVO",
    r"^DEP\s+EFECTIVO",
    r"^DEP\.?\s*CHEQUES",
    r"DEPOSITO\s+EN\s+EFECTIVO",
    r"DEPOSITO\s+EFECTIVO\s+SUC",
    r"DEPOSITO\s+DE\s+TERCERO",
    r"^W02\s+DEPOSITO\s+DE\s+TERCERO",
    r"^W02\s+",
    r"^AA7\s+DEPOSITO\s+EFECTIVO",
    r"^AA7\s+",
    r"DEPOSITO\s+EFECTIVO\s+PRACTIC",
    r"^C02\s+DEPOSITO\s+EN\s+EFECTIVO",
    r"^C02\s+",
    r"^C07\s+",
    r"^N16\s+",
    r"DEPOSITO\s+SALVO\s+BUEN\s+COBRO",
    r"DEPOSITO\s+MIXTO",
    r"^TEF\s+RECIBIDO",
    r"^T09\s+TEF\s+RECIBIDO",
    r"^T09\s+",
    r"TEF\s+RECIBIDO\s+",
    r"^T22\s+SPEI\s+DEVUELTO",
    r"^T22\s+",
    r"SPEI\s+DEVUELTO",
    
    # === INTERESES Y RENDIMIENTOS ===
    r"^INTERESES\s+GANADOS$",
    r"^INTERESES\s+GANADOS",
    r"INTERESES\s+GANADOS",
    r"^RENDIMIENTOS",
    r"RENDIMIENTO\s+",
    
    # === DEVOLUCIONES Y BONIFICACIONES ===
    r"^DEVOLUCION\s+",
    r"DEVOLUCION\s+DOCUMENTO",
    r"DEVOLUCION\s+DE\s+",
    r"^BONIFICACION",
    r"BONIFICACION\s+",
    r"^REEMBOLSO",
    r"REEMBOLSO\s+",
    r"ABONO\s+CANCELACION",
    r"ABONO\s+CANCELACION\s+DE\s+PAGO",
    r"^TRASPASO\s+REF\s+",
    
    # === PATRONES GENÉRICOS DE INGRESO ===
    r"^ABONO\s+",
    r"ABONO\s+EN\s+CUENTA",
    r"TRANSFERENCIA\s+RECIBIDA",
    r"DEPOSITO\s+",
    r"^DEP\s+",
    r"INGRESO\s+",
    r"ACREDITACION\s+",
]

# Códigos BBVA ampliados
CODIGOS_BBVA_EGRESO = [
    "T17", "P14", "P12", "P31", "S39", "S40", "X01", "C68", "A15", "CA9", 
    "R01", "N06", "C03", "G30", "H09", "S01", "S02", "P07", "P09", "C05"
]
CODIGOS_BBVA_INGRESO = [
    "T20", "T22", "T09", "C02", "C07", "AA7", "W02", "N16", "D01", "D02"
]

# =============================================================================
# CONFIGURACIÓN NVIDIA
# =============================================================================

def configurar_nvidia():
    try:
        import site
        paquetes = site.getsitepackages()
        if isinstance(paquetes, str):
            paquetes = [paquetes]
        rutas = []
        for paq in paquetes:
            nvidia_dir = os.path.join(paq, 'nvidia')
            if os.path.exists(nvidia_dir):
                for r, d, f in os.walk(nvidia_dir):
                    if 'lib' in d:
                        rutas.append(os.path.join(r, 'lib'))
        if rutas:
            ld = os.environ.get('LD_LIBRARY_PATH', '')
            os.environ['LD_LIBRARY_PATH'] = f"{':'.join(set(rutas))}:{ld}"
    except Exception as e:
        logger.warning(f"Config NVIDIA: {e}")

configurar_nvidia()

# =============================================================================
# CARGA DEL MODELO
# =============================================================================

def cargar_modelo() -> Tuple[Optional[Any], Optional[Any]]:
    logger.info(f"Cargando modelo V6.7 desde: {RUTA_LOCAL_MODELO}")
    try:
        attn = "sdpa"
        try:
            import flash_attn
            attn = "flash_attention_2"
            logger.info("Usando Flash Attention 2")
        except ImportError:
            logger.info("Usando SDPA")

        modelo = Qwen2_5_VLForConditionalGeneration.from_pretrained(
            RUTA_LOCAL_MODELO,
            torch_dtype=torch.bfloat16,
            attn_implementation=attn,
            device_map=None
        ).to(DISPOSITIVO)
        
        procesador = AutoProcessor.from_pretrained(RUTA_LOCAL_MODELO)
        
        logger.info("Modelo cargado exitosamente")
        return modelo, procesador
    except Exception as e:
        logger.error(f"Error cargando modelo: {e}")
        return None, None

def convertir_pdf(ruta_pdf: str) -> List:
    logger.info(f"Convirtiendo PDF: {os.path.basename(ruta_pdf)} a {CONFIG_IMAGEN['dpi']} DPI")
    try:
        return convert_from_path(
            ruta_pdf, 
            dpi=CONFIG_IMAGEN['dpi'],
            fmt=CONFIG_IMAGEN['formato'].lower()
        )
    except Exception as e:
        logger.error(f"Error convirtiendo PDF: {e}")
        return []

# =============================================================================
# DETECCIÓN DE BANCO
# =============================================================================

def detectar_banco_por_clabe(texto: str) -> Optional[Banco]:
    patrones = [
        r"CLABE[:\s]+(\d{18})",
        r"CLABE\s+Interbancaria[:\s]+(\d{18})",
        r"(\d{18})",
    ]
    for patron in patrones:
        matches = re.findall(patron, texto, re.IGNORECASE)
        for clabe in matches:
            if len(clabe) == 18:
                codigo = clabe[:3]
                if codigo in CLABE_A_BANCO:
                    nombre = CLABE_A_BANCO[codigo]
                    for banco in Banco:
                        if banco.value == nombre:
                            logger.info(f"✓ Banco por CLABE {codigo}***: {banco.value}")
                            return banco
    return None

def detectar_banco_por_patrones(texto: str) -> Optional[Banco]:
    patrones_banco = {
        "INBURSA": [
            r"Cliente\s+Inbursa", r"RESUMEN\s+DE\s+SALDOS", r"DEPOSITO\s+INBURED",
            r"TRASPASO\s+SPEI\s+INBURED", r"BANCO\s+INBURSA", r"CUENTA\s+500\d{8}",
            r"SANBORNS", r"INBURSA\s+BANCO"
        ],
        "BBVA": [
            r"Comportamiento", r"Saldo\s+de\s+Liquidaci[oó]n", r"BBVA\s+MEXICO",
            r"MAESTRA\s+PYME\s+BBVA", r"No\.\s+Cuenta\s+\d{10}", r"BNET\d+",
            r"BBVA\s+BANCOMER"
        ],
        "CITIBANAMEX": [
            r"ESTADO\s+DE\s+CUENTA\s+AL", r"RESUMEN\s+GENERAL", r"Banamex",
            r"CITIBANAMEX", r"DETALLE\s+DE\s+OPERACIONES", r"CITI"
        ],
        "SANTANDER": [r"SANTANDER", r"Super\s+Cuenta", r"SUPERCUENTA"],
        "BANORTE": [r"BANORTE", r"Cuenta\s+Enlace", r"IXE"],
        "HSBC": [r"HSBC", r"HONGKONG"],
        "SCOTIABANK": [r"SCOTIABANK", r"Scotia", r"INVERLAT"],
    }
    
    puntuaciones = {}
    for nombre, patrones in patrones_banco.items():
        punt = sum(1 for p in patrones if re.search(p, texto, re.IGNORECASE))
        if punt > 0:
            puntuaciones[nombre] = punt
    
    if puntuaciones:
        mejor = max(puntuaciones, key=puntuaciones.get)
        if puntuaciones[mejor] >= 2:
            for banco in Banco:
                if banco.value == mejor:
                    logger.info(f"✓ Banco por patrones ({puntuaciones[mejor]} matches): {banco.value}")
                    return banco
    return None

def detectar_banco_por_vision(modelo, procesador, imagen) -> Banco:
    prompt = """Observa esta imagen de estado de cuenta bancario mexicano.

IDENTIFICA EL BANCO buscando:
- Logo del banco
- Nombre en encabezado
- Colores corporativos

RESPONDE SOLO CON UNA OPCIÓN:
BBVA
INBURSA
CITIBANAMEX
SANTANDER
BANORTE
HSBC
SCOTIABANK
GENERICO"""

    mensajes = [{"role": "user", "content": [
        {"type": "image", "image": imagen},
        {"type": "text", "text": prompt}
    ]}]
    
    texto = procesador.apply_chat_template(mensajes, tokenize=False, add_generation_prompt=True)
    imgs, _ = process_vision_info(mensajes)
    inputs = procesador(text=[texto], images=imgs, padding=True, return_tensors="pt").to(DISPOSITIVO)
    
    with torch.no_grad():
        out = modelo.generate(**inputs, max_new_tokens=20, **CONFIG_GENERACION)
    
    resp = procesador.batch_decode(out, skip_special_tokens=True)[0]
    if "assistant" in resp.lower():
        resp = resp.split("assistant")[-1]
    resp = resp.strip().upper()
    
    for banco in Banco:
        if banco.value in resp:
            logger.info(f"✓ Banco por visión: {banco.value}")
            return banco
    
    logger.info("⚠ Banco: GENERICO")
    return Banco.GENERICO

def detectar_banco(modelo, procesador, imagen, texto_ocr: str = "") -> Banco:
    logger.info("═" * 50)
    logger.info("DETECTANDO BANCO...")
    
    prompt_texto = "Lee TODO el texto visible incluyendo números de CLABE (18 dígitos), nombre del banco, números de cuenta."
    mensajes = [{"role": "user", "content": [
        {"type": "image", "image": imagen},
        {"type": "text", "text": prompt_texto}
    ]}]
    
    texto_template = procesador.apply_chat_template(mensajes, tokenize=False, add_generation_prompt=True)
    imgs, _ = process_vision_info(mensajes)
    inputs = procesador(text=[texto_template], images=imgs, padding=True, return_tensors="pt").to(DISPOSITIVO)
    
    with torch.no_grad():
        out = modelo.generate(**inputs, max_new_tokens=500, **CONFIG_GENERACION)
    
    texto_extraido = procesador.batch_decode(out, skip_special_tokens=True)[0]
    if "assistant" in texto_extraido.lower():
        texto_extraido = texto_extraido.split("assistant")[-1]
    
    banco = detectar_banco_por_clabe(texto_extraido)
    if banco:
        return banco
    
    banco = detectar_banco_por_patrones(texto_extraido)
    if banco:
        return banco
    
    return detectar_banco_por_vision(modelo, procesador, imagen)

# =============================================================================
# CLASIFICACIÓN DE PÁGINAS
# =============================================================================

def clasificar_pagina(modelo, procesador, imagen, num_pagina: int = 0) -> TipoPagina:
    """Clasifica tipo de página con mejor detección de RESUMEN_CON_MOVIMIENTOS"""
    prompt = """Observa esta página de estado de cuenta bancario.

ANALIZA el contenido:

1. ¿Tiene una TABLA con columnas FECHA, CONCEPTO, RETIROS/CARGOS, DEPOSITOS/ABONOS, SALDO con filas de transacciones?
2. ¿Tiene sección de RESUMEN GENERAL o datos de cuenta?
3. ¿Es un CFDI/factura con "Folio Fiscal", "Sello digital"?
4. ¿Tiene principalmente gráficas?
5. ¿Tiene glosario, recomendaciones, CONDUSEF?

RESPONDE CON UNA DE ESTAS OPCIONES:
- MOVIMIENTOS (si hay tabla de transacciones sin resumen)
- RESUMEN_CON_MOVIMIENTOS (si hay RESUMEN GENERAL Y TAMBIÉN tabla DETALLE DE OPERACIONES con transacciones)
- RESUMEN (si solo hay datos generales sin tabla de transacciones)
- CFDI (si es factura)
- GRAFICO (si tiene gráficas)
- INFORMATIVA (si es informativa/glosario)"""

    mensajes = [{"role": "user", "content": [
        {"type": "image", "image": imagen},
        {"type": "text", "text": prompt}
    ]}]
    
    texto = procesador.apply_chat_template(mensajes, tokenize=False, add_generation_prompt=True)
    imgs, _ = process_vision_info(mensajes)
    inputs = procesador(text=[texto], images=imgs, padding=True, return_tensors="pt").to(DISPOSITIVO)
    
    with torch.no_grad():
        out = modelo.generate(**inputs, max_new_tokens=30, **CONFIG_GENERACION)
    
    resp = procesador.batch_decode(out, skip_special_tokens=True)[0]
    if "assistant" in resp.lower():
        resp = resp.split("assistant")[-1]
    resp = resp.strip().upper()
    
    # Detectar RESUMEN_CON_MOVIMIENTOS
    if "RESUMEN_CON_MOVIMIENTOS" in resp or "RESUMEN CON MOVIMIENTOS" in resp:
        return TipoPagina.RESUMEN_CON_MOVIMIENTOS
    
    for tipo in TipoPagina:
        if tipo.value in resp:
            return tipo
    
    return TipoPagina.MOVIMIENTOS

# =============================================================================
# FUNCIONES DE LIMPIEZA Y NORMALIZACIÓN
# =============================================================================

def limpiar_texto(texto: str) -> str:
    if not texto:
        return ""
    texto = str(texto)
    texto = texto.replace('\n', ' ').replace('\\n', ' ').replace('\r', ' ').replace('\t', ' ')
    texto = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', texto)
    texto = re.sub(r'\s+', ' ', texto)
    return texto.strip()

def limpiar_monto(valor: Any) -> float:
    if valor is None or valor == "":
        return 0.00
    if isinstance(valor, dict):
        valor = valor.get("valor", valor.get("monto", 0))
    if isinstance(valor, (int, float)):
        return round(abs(float(valor)), 2)
    
    s = str(valor).strip()
    
    # Manejar expresiones matemáticas simples (ej: "65333.88 + 26")
    if '+' in s or '-' in s:
        try:
            # Evaluar expresión simple de manera segura
            s_limpio = re.sub(r'[^\d\.\+\-\s]', '', s)
            partes = re.split(r'[\+\-]', s_limpio)
            total = 0.0
            for parte in partes:
                parte = parte.strip()
                if parte:
                    try:
                        total += float(parte)
                    except:
                        pass
            if total > 0:
                return round(abs(total), 2)
        except:
            pass
    
    s = s.replace("$", "").replace(",", "").replace(" ", "")
    if "(" in s and ")" in s:
        s = s.replace("(", "").replace(")", "")
    if s.endswith("-"):
        s = s[:-1]
    s = re.sub(r'[CRDB]+$', '', s.upper())
    s = s.replace("+", "").replace("-", "")
    
    try:
        return round(abs(float(s)), 2)
    except:
        return 0.00

def normalizar_fecha(fecha_str: str, anio: str = "2025") -> str:
    if not fecha_str or len(str(fecha_str).strip()) < 2:
        return ""
    
    fecha = str(fecha_str).strip().upper()
    fecha = fecha.replace(".", " ").replace("-", "/").replace("  ", " ").strip()
    
    # Formato DD/MM/AAAA o DD-MM-AAAA
    match = re.search(r'(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})', fecha)
    if match:
        dia = match.group(1).zfill(2)
        mes = match.group(2).zfill(2)
        a = match.group(3)
        if len(a) == 2:
            a = "20" + a if int(a) < 50 else "19" + a
        return f"{dia}/{mes}/{a}"
    
    # Formato MES DD o MES. DD
    match = re.search(r'([A-Z]{3,})\s*\.?\s*(\d{1,2})', fecha)
    if match:
        mes_txt = match.group(1)[:3]
        dia = match.group(2).zfill(2)
        mes_num = MESES.get(mes_txt, "01")
        return f"{dia}/{mes_num}/{anio}"
    
    # Formato DD MES o DD/MES
    match = re.search(r'(\d{1,2})[/\s]+([A-Z]{2,})', fecha)
    if match:
        dia = match.group(1).zfill(2)
        mes_txt = match.group(2)[:3]
        mes_num = MESES.get(mes_txt, "01")
        return f"{dia}/{mes_num}/{anio}"
    
    # Formato DD/MM sin año
    match = re.search(r'(\d{1,2})[/](\d{1,2})$', fecha)
    if match:
        dia = match.group(1).zfill(2)
        mes = match.group(2).zfill(2)
        return f"{dia}/{mes}/{anio}"
    
    # Formato DD MES (con espacio)
    match = re.search(r'(\d{1,2})\s+([A-Z]{3})', fecha)
    if match:
        dia = match.group(1).zfill(2)
        mes_txt = match.group(2)
        mes_num = MESES.get(mes_txt, "01")
        return f"{dia}/{mes_num}/{anio}"
    
    return fecha_str

def extraer_anio_periodo(periodo: str) -> str:
    match = re.search(r'20\d{2}', str(periodo))
    return match.group(0) if match else "2025"

def normalizar_periodo(periodo: str) -> str:
    if not periodo:
        return "PERIODO_ND"
    
    periodo = periodo.upper()
    fechas = []
    
    # Buscar fechas en formato DD/MM/AAAA
    for m in re.finditer(r'(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})', periodo):
        dia = m.group(1).zfill(2)
        mes = m.group(2).zfill(2)
        anio = m.group(3)
        mes_txt = MES_A_TEXTO.get(mes, "ENE")
        fechas.append(f"{dia}{mes_txt}{anio}")
    
    # Buscar fechas en formato DD DE MES DE AAAA
    for m in re.finditer(r'(\d{1,2})\s*(?:DE\s*)?([A-Z]{3,})[A-Z]*\.?\s*(?:DE\s*)?(\d{4})', periodo):
        dia = m.group(1).zfill(2)
        mes = m.group(2)[:3]
        anio = m.group(3)
        fechas.append(f"{dia}{mes}{anio}")
    
    if len(fechas) >= 2:
        return f"{fechas[0]}_{fechas[-1]}"
    elif fechas:
        return fechas[0]
    
    limpio = re.sub(r'[^A-Z0-9]', '', periodo)
    return limpio if len(limpio) >= 8 else "PERIODO_ND"

def limpiar_nombre_empresa(nombre: str) -> str:
    if not nombre:
        return ""
    nombre = limpiar_texto(nombre).upper()
    for invalido in PALABRAS_BANCO_INVALIDAS:
        if invalido in nombre:
            return ""
    nombre = re.sub(r'[^\w\s\.\,\&]', '', nombre)
    nombre = re.sub(r'\s+', ' ', nombre).strip()
    return nombre

def crear_nombre_archivo(nombre: str) -> str:
    limpio = re.sub(r'[^A-Za-z0-9]', '_', nombre.upper())
    limpio = re.sub(r'_+', '_', limpio).strip('_')
    return limpio[:80]

# =============================================================================
# CLASIFICACIÓN POR PATRONES MEJORADA CON IA ADAPTATIVA
# =============================================================================

def clasificar_por_patrones(descripcion: str, banco: str = "") -> Optional[str]:
    """Clasificación mejorada con patrones extendidos y lógica adaptativa"""
    if not descripcion:
        return None
        
    desc = limpiar_texto(descripcion).upper().strip()
    banco = banco.upper() if banco else ""
    
    # === REGLA PRIORITARIA ABSOLUTA: PAGO INTERBANCARIO A siempre es EGRESO ===
    if re.search(r'PAGO\s+INTERBANCARIO\s+A\s+', desc):
        return "Egreso"
    
    # === REGLA PRIORITARIA ABSOLUTA: PAGO RECIBIDO DE siempre es INGRESO ===
    if re.search(r'PAGO\s+RECIBIDO\s+DE\s+', desc):
        return "Ingreso"
    
    # === COMISIONES siempre EGRESO ===
    if re.search(r'COMISION', desc) or re.search(r'COBRO\s+(DE\s+)?COM', desc):
        return "Egreso"
    
    if re.search(r'IVA\s+COMISION', desc):
        return "Egreso"
    
    if re.search(r'ISR\s+RETENIDO', desc):
        return "Egreso"
    
    # === COBROS AUTOMÁTICOS (V6.7) ===
    if re.search(r'COBRO\s+AUTOMATICO', desc):
        return "Egreso"
    
    # === REGLAS ESPECÍFICAS INBURSA ===
    if "INBURSA" in banco:
        # DEPOSITO INBURED = Ingreso (pero NO si es TRASPASO)
        if desc.startswith("DEPOSITO INBURED") or "DEPOSITO INBURED" in desc:
            if "TRASPASO" not in desc:
                return "Ingreso"
        # DEPOSITO SPEI = Ingreso
        if desc.startswith("DEPOSITO SPEI") or "DEPOSITO SPEI" in desc:
            return "Ingreso"
        # TRASPASO SPEI INBURED = Egreso (pago saliente)
        if "TRASPASO SPEI INBURED" in desc:
            return "Egreso"
        # INTERESES GANADOS = Ingreso
        if desc == "INTERESES GANADOS" or desc.startswith("INTERESES GANADOS"):
            return "Ingreso"
        # ISR RETENIDO = Egreso
        if desc == "ISR RETENIDO" or desc.startswith("ISR RETENIDO"):
            return "Egreso"
    
    # === REGLAS ESPECÍFICAS BBVA ===
    if "BBVA" in banco:
        codigo_match = re.match(r'^([A-Z]\d{2})\s', desc)
        if codigo_match:
            codigo = codigo_match.group(1)
            if codigo in CODIGOS_BBVA_EGRESO:
                return "Egreso"
            if codigo in CODIGOS_BBVA_INGRESO:
                return "Ingreso"
        # También detectar código al inicio sin espacio
        codigo_match2 = re.match(r'^([A-Z]\d{2})', desc)
        if codigo_match2:
            codigo = codigo_match2.group(1)
            if codigo in CODIGOS_BBVA_EGRESO:
                return "Egreso"
            if codigo in CODIGOS_BBVA_INGRESO:
                return "Ingreso"
    
    # === REGLAS ESPECÍFICAS BANAMEX/CITIBANAMEX ===
    if "BANAMEX" in banco or "CITI" in banco:
        if re.search(r'PAGO\s+A\s+TERCEROS', desc):
            return "Egreso"
        # Domiciliaciones son egresos
        if re.search(r'^DOMI\s+', desc) or re.search(r'DOMICILIACION', desc):
            return "Egreso"
        # Depósitos específicos
        if re.search(r'DEPOSITO\s+EFECTIVO', desc):
            return "Ingreso"
        if re.search(r'DEPOSITO\s+SALVO\s+BUEN\s+COBRO', desc):
            return "Ingreso"
        if re.search(r'DEPOSITO\s+MIXTO', desc):
            return "Ingreso"
        # Abono por cancelación = Ingreso
        if re.search(r'ABONO\s+CANCELACION', desc):
            return "Ingreso"
    
    # === PATRONES ABSOLUTOS DE INGRESO ===
    for patron in PATRONES_INGRESO_ABSOLUTO:
        try:
            if re.search(patron, desc):
                return "Ingreso"
        except:
            pass
    
    # === PATRONES ABSOLUTOS DE EGRESO ===
    for patron in PATRONES_EGRESO_ABSOLUTO:
        try:
            if re.search(patron, desc):
                return "Egreso"
        except:
            pass
    
    # === REGLAS GENÉRICAS ADICIONALES ===
    if "DEPOSITO SPEI" in desc and "TRASPASO" not in desc and "CARGO" not in desc:
        return "Ingreso"
    
    if desc == "INTERESES GANADOS":
        return "Ingreso"
    
    # === INFERENCIA INTELIGENTE PARA CASOS DESCONOCIDOS ===
    return inferir_clasificacion_inteligente(desc, banco)

def inferir_clasificacion_inteligente(descripcion: str, banco: str = "") -> Optional[str]:
    """Inferencia inteligente para transacciones no reconocidas por patrones"""
    desc = descripcion.upper()
    
    # Palabras que FUERTEMENTE indican ingreso
    palabras_ingreso_fuerte = [
        "DEPOSITO", "ABONO", "RECIBIDO", "INTERESES GANADOS", 
        "RENDIMIENTO", "DEVOLUCION", "BONIFICACION", "REEMBOLSO",
        "ACREDITACION", "COBRADO", "INGRESO"
    ]
    
    # Palabras que FUERTEMENTE indican egreso
    palabras_egreso_fuerte = [
        "PAGO", "CARGO", "COMISION", "RETIRO", "TRASPASO", 
        "TRANSFERENCIA", "IVA", "ISR", "DOMICILIACION", "DEBITO",
        "COMPRA", "CONSUMO", "CHEQUE PAGADO", "ENVIADO"
    ]
    
    # Palabras neutras o que dependen del contexto
    palabras_neutras = ["MOVIMIENTO", "OPERACION", "TRANSACCION"]
    
    score_ingreso = 0
    score_egreso = 0
    
    # Calcular scores con pesos
    for p in palabras_ingreso_fuerte:
        if p in desc:
            # "PAGO RECIBIDO" tiene más peso que solo "RECIBIDO"
            if p == "RECIBIDO" and "PAGO RECIBIDO" in desc:
                score_ingreso += 5
            else:
                score_ingreso += 2
    
    for p in palabras_egreso_fuerte:
        if p in desc:
            # "PAGO INTERBANCARIO" tiene más peso
            if p == "PAGO" and "INTERBANCARIO" in desc:
                score_egreso += 5
            elif p == "CHEQUE PAGADO":
                score_egreso += 4
            else:
                score_egreso += 2
    
    # Ajustes contextuales
    if "PAGO RECIBIDO" in desc:
        score_ingreso += 5
    if "PAGO INTERBANCARIO A" in desc:
        score_egreso += 5
    if "SPEI ENVIADO" in desc:
        score_egreso += 4
    if "SPEI RECIBIDO" in desc:
        score_ingreso += 4
    
    # Detectar por posición de beneficiario/ordenante
    if "BENEFICIARIO" in desc:
        score_egreso += 2  # Generalmente indica pago saliente
    if "ORDENANTE" in desc or "POR ORDEN DE" in desc:
        score_ingreso += 2  # Generalmente indica pago entrante
    
    # Decidir
    if score_ingreso > score_egreso:
        return "Ingreso"
    elif score_egreso > score_ingreso:
        return "Egreso"
    
    # Si no hay suficiente evidencia, retornar None para que use clasificación del modelo
    return None

def inferir_clasificacion_por_descripcion(descripcion: str) -> str:
    """Inferencia de último recurso basada en palabras clave - usa inferencia inteligente primero"""
    resultado = inferir_clasificacion_inteligente(descripcion, "")
    if resultado:
        return resultado
    
    # Si la inferencia inteligente no pudo decidir, default a Egreso (más conservador)
    return "Egreso"

# =============================================================================
# RECLASIFICACIÓN POST-EXTRACCIÓN MEJORADA
# =============================================================================

def reclasificar_transaccion(tx: Dict, banco: str) -> Dict:
    """Reclasifica una transacción si la clasificación parece incorrecta"""
    desc = tx.get("Nombre de la transacción", "").upper()
    clasificacion_actual = tx.get("Clasificación", "")
    
    # Verificar con patrones
    clasificacion_patron = clasificar_por_patrones(desc, banco)
    
    if clasificacion_patron and clasificacion_patron != clasificacion_actual:
        logger.debug(f"V6.7 Reclasificando: {desc[:50]}... de {clasificacion_actual} a {clasificacion_patron}")
        tx["Clasificación"] = clasificacion_patron
    
    return tx

# =============================================================================
# EXTRACCIÓN DE JSON
# =============================================================================

def extraer_json(texto: str) -> Optional[Dict]:
    if not texto:
        return None
    
    texto = texto.replace("```json", "").replace("```", "").strip()
    texto = texto.replace("'", '"').replace('\\"', '"')
    texto = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', texto)
    
    # Intento 1: Extraer JSON principal
    try:
        inicio = texto.find('{')
        if inicio != -1:
            nivel = 0
            fin = -1
            for i in range(inicio, len(texto)):
                if texto[i] == '{':
                    nivel += 1
                elif texto[i] == '}':
                    nivel -= 1
                    if nivel == 0:
                        fin = i
                        break
            if fin > inicio:
                json_str = texto[inicio:fin+1]
                json_str = re.sub(r',\s*}', '}', json_str)
                json_str = re.sub(r',\s*]', ']', json_str)
                # Limpiar valores con operaciones matemáticas
                json_str = re.sub(r':\s*(\d+\.?\d*)\s*\+\s*(\d+\.?\d*)', lambda m: f': {float(m.group(1)) + float(m.group(2))}', json_str)
                try:
                    return json.loads(json_str)
                except:
                    pass
    except:
        pass
    
    # Intento 2: Buscar patrón de transacciones
    try:
        match = re.search(r'\{\s*"transacciones"\s*:\s*\[', texto)
        if match:
            start = match.start()
            brace = 0
            for i, c in enumerate(texto[start:]):
                if c == '{': brace += 1
                elif c == '}': brace -= 1
                if brace == 0 and i > 0:
                    json_str = texto[start:start+i+1]
                    try:
                        return json.loads(json_str)
                    except:
                        break
    except:
        pass
    
    # Intento 3: Extraer transacciones individuales con regex
    try:
        transacciones = []
        patron = r'\{\s*"fecha"\s*:\s*"([^"]+)"\s*,\s*"descripcion"\s*:\s*"([^"]+)"\s*,\s*"monto"\s*:\s*([\d.]+)\s*,\s*"clasificacion"\s*:\s*"([^"]+)"[^}]*\}'
        matches = re.findall(patron, texto, re.DOTALL | re.IGNORECASE)
        for m in matches:
            if len(m) >= 4:
                transacciones.append({
                    "fecha": m[0],
                    "descripcion": m[1],
                    "monto": float(m[2]),
                    "clasificacion": m[3]
                })
        if transacciones:
            return {"transacciones": transacciones}
    except:
        pass
    
    # Intento 4: Regex recursivo
    try:
        import regex
        m = regex.search(r'\{(?:[^{}]|(?R))*\}', texto, regex.DOTALL)
        if m:
            return json.loads(m.group(0))
    except:
        pass
    
    # Intento 5 - Extraer datos aunque el JSON esté malformado
    try:
        transacciones = []
        # Buscar bloques que parezcan transacciones
        bloques = re.findall(r'\{[^{}]*"fecha"[^{}]*"monto"[^{}]*\}', texto, re.DOTALL | re.IGNORECASE)
        for bloque in bloques:
            try:
                tx = json.loads(bloque)
                transacciones.append(tx)
            except:
                # Intentar extraer campos manualmente
                fecha = re.search(r'"fecha"\s*:\s*"([^"]+)"', bloque)
                desc = re.search(r'"descripcion"\s*:\s*"([^"]+)"', bloque)
                monto = re.search(r'"monto"\s*:\s*([\d.]+)', bloque)
                clasif = re.search(r'"clasificacion"\s*:\s*"([^"]+)"', bloque)
                
                if fecha and monto:
                    transacciones.append({
                        "fecha": fecha.group(1),
                        "descripcion": desc.group(1) if desc else "",
                        "monto": float(monto.group(1)),
                        "clasificacion": clasif.group(1) if clasif else ""
                    })
        
        if transacciones:
            return {"transacciones": transacciones}
    except:
        pass
    
    logger.warning(f"No se pudo extraer JSON. Primeros 200 chars: {texto[:200]}")
    return None

# =============================================================================
# PROMPTS METADATOS (MEJORADOS)
# =============================================================================

def get_prompt_metadatos(banco: Banco) -> str:
    base = """EXTRAE LOS METADATOS DE ESTE ESTADO DE CUENTA BANCARIO.

REGLAS:
1. "nombre_empresa" es el CLIENTE del banco (NO es "BBVA MEXICO", "BANCO INBURSA", etc.)
2. Montos con punto decimal y 2 decimales (ej: 15000.00)
3. Si no encuentras un dato, déjalo vacío "" o 0.00
4. NO incluyas operaciones matemáticas en los valores, solo el número final

"""
    
    if banco == Banco.INBURSA:
        especifico = """FORMATO INBURSA:
- nombre_empresa: SUPERIOR IZQUIERDA, debajo de "INBURSA Banco"
- numero_cuenta: En "CUENTA:" (11 dígitos, empieza con 500...)
- periodo: "PERIODO:" (Del DD Mes. AAAA al DD Mes. AAAA)

SALDOS en "RESUMEN DE SALDOS":
| SALDO ANTERIOR  | → saldo_inicial
| ABONOS          | → total_depositos (SUMA TOTAL, no cantidad de operaciones)
| CARGOS          | → total_retiros (SUMA TOTAL, no cantidad de operaciones)
| SALDO ACTUAL    | → saldo_final
| SALDO PROMEDIO  | → saldo_promedio

IMPORTANTE: En ABONOS y CARGOS extrae el MONTO TOTAL en pesos, no el número de operaciones.
"""
    elif banco == Banco.BBVA:
        especifico = """FORMATO BBVA:
- nombre_empresa: Esquina SUPERIOR IZQUIERDA, debajo del logo BBVA
- numero_cuenta: "No. de Cuenta" (10 dígitos)
- periodo: "Periodo" o rango de fechas

SALDOS en "Comportamiento":
| Saldo de Liquidación Inicial | → saldo_inicial
| Depósitos / Abonos (+)       | → total_depositos
| Retiros / Cargos (-)         | → total_retiros  
| Saldo Final (+)              | → saldo_final
"""
    elif banco == Banco.CITIBANAMEX:
        especifico = """FORMATO CITIBANAMEX/BANAMEX:
- nombre_empresa: Busca el nombre del CLIENTE (empresa) en la parte SUPERIOR IZQUIERDA
  Ejemplo: "INMOVITUR SA DE CV" - NO es "BANAMEX" ni "CITIBANAMEX"
  El nombre tiene formato: NOMBRE + SA DE CV, o NOMBRE + SC, o NOMBRE + SAPI
- numero_cuenta: En "CONTRATO" dentro de la tabla "RESUMEN GENERAL" (usualmente 10 dígitos)
- periodo: "ESTADO DE CUENTA AL DD DE MES DE AAAA" o "RESUMEN DEL: DD/MES/AAAA AL DD/MES/AAAA"

SALDOS en el RESUMEN GENERAL:
| Saldo Anterior          | → saldo_inicial
| (+) XX Depósitos $MONTO | → total_depositos (usa el MONTO en pesos, no la cantidad)
| (-) XX Retiros $MONTO   | → total_retiros (usa el MONTO en pesos, no la cantidad)
| SALDO AL [fecha]        | → saldo_final

MUY IMPORTANTE: El nombre de la empresa NO es el banco. Busca el nombre del cliente.
"""
    else:
        especifico = """FORMATO GENÉRICO:
- nombre_empresa: Nombre del CLIENTE en parte superior (con S.A., S.C., etc.)
- numero_cuenta: 10-18 dígitos cerca de "Cuenta", "Contrato", "CLABE"
- Saldos: Busca tabla con "SALDO", "RESUMEN"

IMPORTANTE: Identifica las columnas de la tabla para determinar:
- Dónde están los depósitos/abonos (dinero que entra)
- Dónde están los retiros/cargos (dinero que sale)
"""
    
    salida = """
RESPONDE SOLO CON ESTE JSON (sin operaciones matemáticas, solo números finales):

{
    "nombre_empresa": "NOMBRE DEL CLIENTE",
    "numero_cuenta": "1234567890",
    "periodo": "01ABR2025_30ABR2025",
    "saldo_inicial": 0.00,
    "saldo_final": 0.00,
    "saldo_promedio": 0.00,
    "total_depositos": 0.00,
    "total_retiros": 0.00
}"""
    
    return base + especifico + salida

# =============================================================================
# PROMPTS TRANSACCIONES (MEJORADO PARA PÁGINAS DENSAS)
# =============================================================================

def get_prompt_transacciones(banco: Banco, anio: str, contexto_pagina_anterior: str = "") -> str:
    """Prompt mejorado con contexto de página anterior para transacciones cortadas"""
    
    contexto = ""
    if contexto_pagina_anterior:
        contexto = f"""
CONTEXTO DE PÁGINA ANTERIOR:
La última transacción de la página anterior puede estar incompleta:
{contexto_pagina_anterior}

Si ves texto que parece ser continuación de esta transacción al inicio de esta página,
COMBÍNALO con la descripción anterior para formar una transacción completa.

"""
    
    base = f"""EXTRAE TODAS LAS TRANSACCIONES BANCARIAS DE ESTA PÁGINA.
{contexto}
AÑO DEL ESTADO DE CUENTA: {anio}

REGLAS FUNDAMENTALES:
1. UNA TRANSACCIÓN = UNA FILA CON MONTO en columna CARGOS o ABONOS
2. La descripción puede ocupar VARIAS LÍNEAS - concatena todo hasta la siguiente FECHA o MONTO
3. Extrae TODAS las transacciones, incluso centavos y comisiones pequeñas
4. IGNORA filas de "SALDO INICIAL", "BALANCE INICIAL", "TOTALES"
5. clasificacion: "Ingreso" si monto en ABONOS/DEPOSITOS, "Egreso" si en CARGOS/RETIROS
6. Si una transacción tiene monto en AMBAS columnas, crea DOS transacciones separadas

IMPORTANTE: NO omitas ninguna transacción. Extrae ABSOLUTAMENTE TODAS las que veas.

"""

    if banco == Banco.INBURSA:
        especifico = f"""FORMATO INBURSA - COLUMNAS: FECHA | REFERENCIA | CONCEPTO | CARGOS | ABONOS | SALDO

REGLAS ESPECÍFICAS INBURSA:
- FECHA formato "MES. DD" → convertir a "DD/MM/{anio}"
- Monto en columna CARGOS → clasificacion: "Egreso"
- Monto en columna ABONOS → clasificacion: "Ingreso"
- "DEPOSITO SPEI" o "DEPOSITO INBURED" → Ingreso
- "TRASPASO SPEI INBURED" → Egreso
- "INTERESES GANADOS" → Ingreso
- "ISR RETENIDO" → Egreso
- "COMISION" → Egreso

ATENCIÓN: Las descripciones largas pueden ocupar 2-3 líneas. Concatena TODO el texto hasta ver otra fecha.
"""
    elif banco == Banco.BBVA:
        especifico = f"""FORMATO BBVA:
CÓDIGOS BBVA (primera columna):
- T17, N06, P31, P14, P12, R01, C03, H09, G30 = Siempre Egreso
- T20, T09, C02, AA7, W02, T22 = Siempre Ingreso

- Monto en columna CARGOS → clasificacion: "Egreso"
- Monto en columna ABONOS → clasificacion: "Ingreso"

ATENCIÓN PÁGINAS DENSAS: Este banco tiene muchas transacciones por página.
Extrae CADA UNA sin omitir ninguna, aunque sean muchas.
Las descripciones con BNET, referencias largas, beneficiarios, etc. deben capturarse COMPLETAS.
"""
    elif banco == Banco.CITIBANAMEX:
        especifico = f"""FORMATO CITIBANAMEX/BANAMEX - COLUMNAS: FECHA | CONCEPTO | RETIROS | DEPOSITOS | SALDO

REGLAS CRÍTICAS DE CLASIFICACIÓN:
- Monto en columna RETIROS → clasificacion: "Egreso" (dinero que SALE)
- Monto en columna DEPOSITOS → clasificacion: "Ingreso" (dinero que ENTRA)
- "PAGO INTERBANCARIO A..." → Egreso (siempre está en RETIROS)
- "PAGO RECIBIDO DE..." → Ingreso (siempre está en DEPOSITOS)
- "DEPOSITO EFECTIVO" → Ingreso
- "DOMI..." (domiciliación) → Egreso
- "COMISION" → Egreso

FECHA "DD MES" → "DD/MM/{anio}"

ATENCIÓN: Las descripciones en Banamex son MUY LARGAS (incluyen CLAVE RASTREO, SUC, HORA, etc.)
Captura TODA la descripción completa.
"""
    else:
        especifico = f"""FORMATO GENÉRICO - IDENTIFICA LAS COLUMNAS:
Busca columnas que digan: CARGOS/RETIROS (egresos) vs ABONOS/DEPOSITOS (ingresos)

- Columna IZQUIERDA de montos (Cargos/Retiros) → clasificacion: "Egreso"
- Columna DERECHA de montos (Abonos/Depósitos) → clasificacion: "Ingreso"

PISTAS PARA CLASIFICAR:
- Si dice "DEPOSITO", "ABONO", "RECIBIDO" → probablemente Ingreso
- Si dice "PAGO", "CARGO", "RETIRO", "TRANSFERENCIA" → probablemente Egreso
- Fíjate en QUÉ COLUMNA está el monto para determinar la clasificación
"""

    salida = """
RESPONDE SOLO CON ESTE JSON:

{
    "transacciones": [
        {
            "fecha": "DD/MM/AAAA",
            "descripcion": "Descripción COMPLETA multilínea sin truncar",
            "monto": 0.00,
            "clasificacion": "Ingreso" o "Egreso",
            "referencia": "número si existe",
            "contraparte": "beneficiario u ordenante"
        }
    ]
}

IMPORTANTE:
- Si NO hay transacciones: {"transacciones": []}
- NUNCA inventes transacciones
- CADA transacción debe tener monto > 0
- clasificacion DEBE ser "Ingreso" o "Egreso" según la columna donde está el monto
- NO omitas transacciones aunque la página tenga muchas
- Captura descripciones COMPLETAS aunque sean largas
"""
    
    return base + especifico + salida

def get_prompt_transacciones_simplificado(banco: Banco, anio: str) -> str:
    """Prompt simplificado para reintentos en páginas problemáticas"""
    
    return f"""EXTRAE TODAS LAS TRANSACCIONES DE ESTA PÁGINA DE ESTADO DE CUENTA.

AÑO: {anio}

Por cada fila con un MONTO, extrae:
- fecha: formato DD/MM/{anio}
- descripcion: texto completo de la operación
- monto: el número (sin símbolos)
- clasificacion: "Ingreso" si está en columna de depósitos/abonos, "Egreso" si está en columna de cargos/retiros

RESPONDE CON JSON:
{{"transacciones": [
    {{"fecha": "DD/MM/AAAA", "descripcion": "texto", "monto": 0.00, "clasificacion": "Ingreso/Egreso"}}
]}}

EXTRAE ABSOLUTAMENTE TODAS LAS TRANSACCIONES SIN OMITIR NINGUNA.
"""

# =============================================================================
# EJECUCIÓN DE PROMPTS
# =============================================================================

def ejecutar_prompt(modelo, procesador, imagen, prompt: str, max_tokens: int = 8000) -> str:
    try:
        mensajes = [{"role": "user", "content": [
            {"type": "image", "image": imagen},
            {"type": "text", "text": prompt}
        ]}]
        
        texto = procesador.apply_chat_template(mensajes, tokenize=False, add_generation_prompt=True)
        imgs, _ = process_vision_info(mensajes)
        inputs = procesador(text=[texto], images=imgs, padding=True, return_tensors="pt").to(DISPOSITIVO)
        
        len_entrada = inputs.input_ids.shape[1]
        max_gen = min(max_tokens, VENTANA_CONTEXTO - len_entrada - 100)
        
        with torch.no_grad():
            out = modelo.generate(
                **inputs,
                max_new_tokens=max_gen,
                pad_token_id=procesador.tokenizer.pad_token_id,
                eos_token_id=procesador.tokenizer.eos_token_id,
                **CONFIG_GENERACION
            )
        
        resp = procesador.batch_decode(out, skip_special_tokens=True)[0]
        if "assistant" in resp.lower():
            resp = resp.split("assistant")[-1]
        
        del inputs, out
        torch.cuda.empty_cache()
        
        return resp
        
    except Exception as e:
        logger.error(f"Error en generación: {e}")
        torch.cuda.empty_cache()
        return ""

# =============================================================================
# EXTRACCIÓN DE METADATOS
# =============================================================================

def extraer_metadatos(modelo, procesador, imagen, banco: Banco) -> Dict:
    prompt = get_prompt_metadatos(banco)
    resp = ejecutar_prompt(modelo, procesador, imagen, prompt, max_tokens=2000)
    
    datos = extraer_json(resp)
    if not datos:
        return {}
    
    resultado = {
        "Nombre de la empresa del estado de cuenta": "",
        "Numero de cuenta del estado de cuenta": "",
        "Periodo del estado de cuenta": "",
        "Saldo inicial de la cuenta": 0.00,
        "Saldo final de la cuenta": 0.00,
        "Saldo promedio del periodo": 0.00,
        "Cantidad total de depositos": 0.00,
        "Cantidad total de retiros": 0.00,
        "Giro de la empresa": ""
    }
    
    nombre = datos.get("nombre_empresa", "") or datos.get("Nombre de la empresa del estado de cuenta", "")
    nombre = limpiar_nombre_empresa(nombre)
    if nombre:
        resultado["Nombre de la empresa del estado de cuenta"] = nombre
    
    cuenta = str(datos.get("numero_cuenta", "") or datos.get("Numero de cuenta del estado de cuenta", ""))
    cuenta = re.sub(r'\D', '', cuenta)
    if len(cuenta) >= 10 and not re.match(r'^0+$', cuenta) and cuenta != "500000000000":
        resultado["Numero de cuenta del estado de cuenta"] = cuenta
    
    periodo = datos.get("periodo", "") or datos.get("Periodo del estado de cuenta", "")
    resultado["Periodo del estado de cuenta"] = normalizar_periodo(periodo)
    
    for campo_dest, campos_orig in [
        ("Saldo inicial de la cuenta", ["saldo_inicial", "Saldo inicial de la cuenta"]),
        ("Saldo final de la cuenta", ["saldo_final", "Saldo final de la cuenta"]),
        ("Saldo promedio del periodo", ["saldo_promedio", "Saldo promedio del periodo"]),
        ("Cantidad total de depositos", ["total_depositos", "Cantidad total de depositos"]),
        ("Cantidad total de retiros", ["total_retiros", "Cantidad total de retiros"])
    ]:
        for campo in campos_orig:
            if campo in datos and datos[campo]:
                resultado[campo_dest] = limpiar_monto(datos[campo])
                break
    
    return resultado

# =============================================================================
# LIMPIEZA DE CONTRAPARTE MEJORADA
# =============================================================================

def limpiar_contraparte(texto: str) -> str:
    if not texto:
        return ""
    
    texto = limpiar_texto(texto)
    
    patrones_basura = [
        r'^CAJA\s+\d+\s*AUT\s*\d*',
        r'^AUT\s+\d+',
        r'^\d{18}$',
        r'^HORA\s+\d+:\d+',
        r'^SUC\s+\d+',
        r'^REF\.?\s*\d+',
        r'^RASTREO\s+\d+',
        r'CAJA\s+\d+AUT\s+\d+',
        r'^\d{10,}$',
        r'^BNET\d+$',
        r'^[A-Z]{3}\d{6}[A-Z0-9]{3}$',
    ]
    
    for patron in patrones_basura:
        if re.match(patron, texto, re.IGNORECASE):
            return ""
        texto = re.sub(patron, '', texto, flags=re.IGNORECASE)
    
    texto = texto.strip()
    
    if len(texto) < 3 or re.match(r'^\d+$', texto):
        return ""
    
    return texto[:80]

# =============================================================================
# EXTRACCIÓN DE TERCERO MEJORADA
# =============================================================================

def extraer_tercero(descripcion: str, clasificacion: str) -> str:
    """Extracción mejorada del beneficiario/ordenante"""
    desc = descripcion.upper()
    
    patrones = [
        r'AL\s+BENEF\.?\s*([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ\s,/]+?)(?:\s*\(DATO|\s+CTA\.?|\s+CLAVE|\s+REF\.?|\s+RASTREO|\s+MISMO|\s+CAJA|$)',
        r'POR\s+ORDEN\s+DE\s+([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ\s,/]+?)(?:\s+CTA\.?|\s+REF\.?|\s+RASTREO|$)',
        r'ORDENANTE\s*:?\s*([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ\s]{5,40}?)(?:\s+CTA|\s+REF|$)',
        r'BENEFICIARIO\s+([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ\s]{3,40}?)(?:\s+(?:BANORTE|BBVA|BANAMEX|HSBC|SANTANDER|AZTECA|SCOTIABANK|INBURSA|STP)|\s+\d{3}|$)',
        r'(?:BANORTE|BBVA|BANAMEX|HSBC|SANTANDER|AZTECA|SCOTIABANK|INBURSA|STP)\s+\d+\s+([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ\s\/]{5,40}?)(?:\s+\d{18}|\s+RFC|$)',
        r'([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ\s]+(?:SA\s+DE\s+CV|SC|SA|SAPI|SRL|SOFOM))',
    ]
    
    for patron in patrones:
        match = re.search(patron, desc)
        if match:
            nombre = match.group(1).strip()
            nombre = re.sub(r'\s+', ' ', nombre)
            nombre = re.sub(r'\s*(SA\s+DE\s+CV|SC|SAPI|SRL|SOFOM\s+ER).*$', '', nombre, flags=re.IGNORECASE)
            if len(nombre) > 3:
                return nombre.title()
    
    return ""

def extraer_cuenta(descripcion: str) -> str:
    """Extracción mejorada de cuenta CLABE o cuenta destino"""
    match = re.search(r'\b(\d{18})\b', descripcion)
    if match:
        return match.group(1)
    
    match = re.search(r'(?:BBVA|BANAMEX|BANORTE|HSBC|SANTANDER|SCOTIABANK|AZTECA|INBURSA|STP)\s+(\d{10,18})', descripcion.upper())
    if match:
        return match.group(1)
    
    match = re.search(r'CTA\.?\s*(?:BENEFICIARIO|ORDENANTE)\s*(\d{10,18})', descripcion.upper())
    if match:
        return match.group(1)
    
    return ""

def extraer_sucursal(descripcion: str) -> str:
    desc = descripcion.upper()
    
    match = re.search(r'SUC\.?\s*(\d{3,5})', desc)
    if match:
        return f"SUC {match.group(1)}"
    
    match = re.search(r'SUCURSAL\s+([A-ZÁÉÍÓÚÑ\s]+?)(?:\s+\d|$)', desc)
    if match:
        return match.group(1).strip().title()
    
    match = re.search(r'SUC\.?\s*([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ\s,]+?)(?:\s+CAJA|\s+AUT|$)', desc)
    if match:
        sucursal = match.group(1).strip()
        if len(sucursal) > 2 and not sucursal.isdigit():
            return sucursal.title()
    
    return ""

# =============================================================================
# EXTRACCIÓN DE REFERENCIA LIMPIA
# =============================================================================

def extraer_referencia_limpia(descripcion: str, referencia_raw: str = "") -> str:
    """Extrae referencia limpia sin texto adicional"""
    desc = descripcion.upper()
    
    if referencia_raw:
        ref_limpia = re.sub(r'\s+[A-Z]+.*$', '', str(referencia_raw))
        ref_limpia = re.sub(r'\s+CLAVE.*$', '', ref_limpia)
        ref_limpia = ref_limpia.strip()
        if ref_limpia:
            return ref_limpia
    
    patrones_ref = [
        r'REF\.?\s*:?\s*(\d{6,20})',
        r'FOLIO\s*:?\s*(\d{6,20})',
        r'AUT\.?\s*:?\s*(\d{6,20})',
        r'RASTREO\s*:?\s*([A-Z0-9]{15,30})',
        r'CLAVE\s+(?:DE\s+)?RASTREO\s+([A-Z0-9]{15,30})',
        r'(BNET\s*[A-Z0-9]{10,25})',
    ]
    
    for patron in patrones_ref:
        match = re.search(patron, desc)
        if match:
            return match.group(1).strip()
    
    return referencia_raw

# =============================================================================
# NOMBRE RESUMIDO INTELIGENTE - FUNCIONES AUXILIARES
# =============================================================================

def limpiar_beneficiario_v67(nombre: str) -> str:
    """Limpia y formatea el nombre del beneficiario"""
    if not nombre:
        return ""
    
    nombre = nombre.strip()
    
    patrones = [
        r'\(DATO\s*NO\s*VERIFICADO.*?\)',
        r'CTA\.?BENEFICIARIO',
        r'CTA\.?ORDENANTE',
        r'\d{10,}',
        r'RASTREO.*',
        r'REF\.?\s*\d+.*',
        r'CAJA\s+\d+.*',
        r'AUT\s+\d+.*',
        r'HORA\s+\d+.*',
        r'SUC\s+\d+.*',
    ]
    
    for patron in patrones:
        nombre = re.sub(patron, '', nombre, flags=re.IGNORECASE)
    
    nombre = re.sub(r'\s+', ' ', nombre).strip()
    
    sufijos = [
        r'\s+SA\s+DE\s+CV\s*$',
        r'\s+S\.?A\.?\s+DE\s+C\.?V\.?\s*$',
        r'\s+SC\s*$',
        r'\s+SAPI\s+DE\s+CV.*$',
        r'\s+SOFOM\s+ER.*$',
    ]
    
    for sufijo in sufijos:
        nombre = re.sub(sufijo, '', nombre, flags=re.IGNORECASE)
    
    nombre = nombre.strip()
    
    if nombre:
        nombre = nombre.title()
    
    if len(nombre) > 35:
        nombre = nombre[:32] + "..."
    
    return nombre

def crear_nombre_resumido(descripcion: str) -> str:
    """Versión original V6.5/V6.6 - se mantiene por compatibilidad"""
    if not descripcion:
        return "Movimiento"
    
    nombre = limpiar_texto(descripcion).upper()
    
    patrones_eliminar = [
        r'^[A-Z]\d{2}\s+',
        r'BNET[A-Z0-9]+',
        r'REF\.?\s*:?\s*[\w\d]+',
        r'FOLIO\s*:?\s*\d+',
        r'AUT\.?\s*:?\s*\d+',
        r'CLAVE\s+DE\s+RASTREO\s+[\w\d]+',
        r'\b\d{18}\b',
        r'\b\d{10,11}\b',
        r'\b[A-Z&Ñ]{3,4}\d{6}[A-Z0-9]{3}\b',
        r'RFC\s*:?\s*[A-Z0-9]+',
        r'036INBU[\d]+',
        r'00\d{15,}',
        r'CAJA\s+\d+\s*AUT\s+\d+',
        r'HORA\s+\d+:\d+',
        r'SUC\s+\d+',
        r'MISMO\s+DIA',
        r'RASTREO.*',
    ]
    
    for patron in patrones_eliminar:
        nombre = re.sub(patron, '', nombre, flags=re.IGNORECASE)
    
    prefijos = [
        "TRASPASO SPEI INBURED", "SPEI ENVIADO", "SPEI RECIBIDO",
        "DEPOSITO SPEI", "DEPOSITO INBURED", "PAGO INTERBANCARIO A",
        "PAGO RECIBIDO DE", "BENEFICIARIO", "ORDENANTE", "AL BENEF",
        "TEF RECIBIDO", "PAGO CUENTA DE TERCERO",
    ]
    for p in prefijos:
        nombre = nombre.replace(p, "")
    
    nombre = re.sub(r'\s+', ' ', nombre).strip()
    
    if len(nombre) < 5:
        match = re.search(r'([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ\s]+(?:SA|SC|CV|RL))', descripcion.upper())
        if match:
            nombre = match.group(1).strip()
        else:
            palabras = re.findall(r'\b[A-ZÁÉÍÓÚÑ]{3,}\b', descripcion.upper())
            if palabras:
                nombre = " ".join(palabras[:3])
    
    nombre = nombre.title()
    
    if len(nombre) > 60:
        nombre = nombre[:57] + "..."
    
    return nombre if nombre else "Movimiento Bancario"

# =============================================================================
# CONTINUACIÓN DE NOMBRE RESUMIDO Y FUNCIONES AUXILIARES
# =============================================================================

def crear_nombre_resumido_v67(descripcion: str, clasificacion: str = "") -> str:
    """Crea nombre resumido inteligente basado en la ACCIÓN de la transacción"""
    if not descripcion:
        return "Movimiento Bancario"
    
    desc = limpiar_texto(descripcion).upper()
    
    # === PAGO INTERBANCARIO A (Egreso) ===
    match = re.search(r'PAGO\s+INTERBANCARIO\s+A\s+(\w+).*?AL\s+BENEF\.?\s*([^(]+?)(?:\s*\(DATO|\s+CTA\.?|$)', desc)
    if match:
        beneficiario = limpiar_beneficiario_v67(match.group(2).strip())
        if beneficiario:
            return f"Pago a {beneficiario}"[:60]
        return "Pago Interbancario"
    
    match = re.search(r'PAGO\s+INTERBANCARIO\s+A\s+\w+', desc)
    if match:
        benef_match = re.search(r'AL\s+BENEF\.?\s*([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ\s,]+?)(?:\s*\(|\s+CTA|$)', desc)
        if benef_match:
            beneficiario = limpiar_beneficiario_v67(benef_match.group(1).strip())
            if beneficiario:
                return f"Pago a {beneficiario}"[:60]
        return "Pago Interbancario"
    
    # === PAGO RECIBIDO DE (Ingreso) ===
    match = re.search(r'PAGO\s+RECIBIDO\s+DE\s+(\w+).*?POR\s+ORDEN\s+DE\s+([^C]+?)(?:CTA|REF|RASTREO|$)', desc)
    if match:
        ordenante = limpiar_beneficiario_v67(match.group(2).strip())
        if ordenante:
            return f"Recibido de {ordenante}"[:60]
        return "Pago Recibido"
    
    match = re.search(r'PAGO\s+RECIBIDO\s+DE\s+\w+', desc)
    if match:
        orden_match = re.search(r'POR\s+ORDEN\s+DE\s+([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ\s,/]+?)(?:\s+CTA|\s+REF|$)', desc)
        if orden_match:
            ordenante = limpiar_beneficiario_v67(orden_match.group(1).strip())
            if ordenante:
                return f"Recibido de {ordenante}"[:60]
        return "Pago Recibido"
    
    # === TRASPASO SPEI INBURED (Egreso Inbursa) ===
    match = re.search(r'TRASPASO\s+SPEI\s+INBURED\s+(?:BENEFICIARIO\s+)?([A-Z][A-Z\s]+?)(?:\s+(?:BANAMEX|BBVA|BANORTE|SANTANDER|HSBC|AZTECA|SCOTIABANK|STP)|$)', desc)
    if match:
        beneficiario = limpiar_beneficiario_v67(match.group(1).strip())
        if beneficiario:
            return f"Traspaso SPEI a {beneficiario}"[:60]
        return "Traspaso SPEI Enviado"
    
    # === DEPOSITO SPEI (Ingreso) ===
    match = re.search(r'DEPOSITO\s+SPEI\s+([A-Z][A-Z\s,/]+?)(?:\s+(?:BANAMEX|BBVA|BANORTE|SANTANDER|HSBC|AZTECA|SCOTIABANK|STP)|\s+\d{18}|$)', desc)
    if match:
        ordenante = limpiar_beneficiario_v67(match.group(1).strip())
        if ordenante:
            return f"Depósito SPEI de {ordenante}"[:60]
        return "Depósito SPEI"
    
    # === DEPOSITO INBURED (Ingreso Inbursa) ===
    if re.search(r'DEPOSITO\s+INBURED', desc):
        match = re.search(r'DEPOSITO\s+INBURED\s+([A-Z\s]+?)(?:\s+$|$)', desc)
        if match:
            concepto = match.group(1).strip()
            if concepto and len(concepto) > 3:
                return f"Depósito Interno - {concepto.title()}"[:60]
        return "Depósito Interno Inbursa"
    
    # === DEPÓSITOS ESPECÍFICOS ===
    if re.search(r'DEPOSITO\s+EFECTIVO', desc):
        suc_match = re.search(r'SUC\.?\s*([A-Z\s,]+?)(?:\s+CAJA|$)', desc)
        if suc_match:
            return f"Depósito Efectivo - {suc_match.group(1).strip().title()}"[:60]
        return "Depósito en Efectivo"
    
    if re.search(r'DEPOSITO\s+MIXTO', desc):
        return "Depósito Mixto Efectivo/Documentos"
    
    if re.search(r'DEPOSITO\s+SALVO\s+BUEN\s+COBRO', desc):
        return "Depósito Cheque (Salvo Buen Cobro)"
    
    # === CHEQUES PAGADOS (Prioridad sobre depósito) ===
    if re.search(r'CHEQUE\s+PAGADO', desc):
        ref_match = re.search(r'REF\.?\s*(\d+)', desc)
        if ref_match:
            return f"Cheque Pagado #{ref_match.group(1)}"
        return "Cheque Pagado"
    
    # === COBROS AUTOMÁTICOS (V6.7) ===
    if re.search(r'COBRO\s+AUTOMATICO\s+RECIBO\s+PREST', desc):
        return "Cobro Automático Préstamo"
    if re.search(r'COBRO\s+AUTOMATICO', desc):
        return "Cobro Automático"
    
    # === COMISIONES ===
    if re.search(r'^COMISION\s+MANEJO', desc):
        return "Comisión Manejo de Cuenta"
    if re.search(r'^COMISION\s+POR\s+MOVIMIENTOS', desc):
        return "Comisión por Movimientos"
    if re.search(r'^COMISION\s+\d+\s+MENSUALIDAD', desc):
        return "Comisión Mensualidad Banca en Línea"
    if re.search(r'^COMISION\s+\d+\s+PAGO\s+INT', desc):
        return "Comisión Pagos Interbancarios"
    if re.search(r'COBRO\s+DE\s+COMISION', desc) or re.search(r'COBRO\s+COM', desc):
        return "Cobro de Comisión"
    if re.search(r'^COMISION', desc):
        return "Comisión Bancaria"
    
    # === IVA ===
    if re.search(r'^IVA\s+COMISION\s+MANEJO', desc):
        return "IVA Comisión Manejo de Cuenta"
    if re.search(r'^IVA\s+COMISION\s+POR\s+MOVIMIENTOS', desc):
        return "IVA Comisión por Movimientos"
    if re.search(r'COBRO\s+DE\s+IVA', desc) or re.search(r'COBRO\s+IMP', desc):
        return "IVA sobre Comisión"
    if re.search(r'^IVA\s+COMISION', desc):
        return "IVA sobre Comisión"
    
    # === ISR ===
    if re.search(r'^ISR\s+RETENIDO', desc):
        return "Retención ISR sobre Intereses"
    
    # === INTERESES ===
    if re.search(r'^INTERESES\s+GANADOS', desc):
        return "Intereses Ganados del Periodo"
    
    # === DOMICILIACIONES ===
    if re.search(r'DOMI\s+AMERICAN\s+EXPRESS', desc) or re.search(r'AMERICAN\s+EXPRESS', desc):
        return "Pago Domiciliado American Express"
    if re.search(r'^DOMI\s+', desc) or re.search(r'^DOMICILIACION', desc):
        return "Pago por Domiciliación"
    
    # === DEVOLUCIONES Y BONIFICACIONES ===
    if re.search(r'ABONO\s+CANCELACION', desc):
        return "Devolución por Cancelación de Pago"
    if re.search(r'DEVOLUCION\s+DOCUMENTO', desc):
        return "Devolución de Documento Depositado"
    if re.search(r'^DEVOLUCION', desc):
        return "Devolución"
    if re.search(r'^BONIFICACION', desc):
        return "Bonificación"
    if re.search(r'^REEMBOLSO', desc):
        return "Reembolso"
    
    # === CARGOS ===
    if re.search(r'^CARGO\s+EN\s+CUENTA', desc):
        return "Cargo en Cuenta"
    
    # === COMERCIOS ESPECÍFICOS ===
    comercios = {
        r'CHILIS': "Consumo Restaurante Chili's",
        r'STARBUCKS': 'Consumo Starbucks',
        r'OXXO': 'Compra OXXO',
        r'COSTCO': 'Compra Costco',
        r'WALMART': 'Compra Walmart',
        r'SAMS': "Compra Sam's Club",
        r'BODEGA': 'Compra Bodega Aurrera',
        r'LIVERPOOL': 'Compra Liverpool',
        r'GASOLINERA|GASOL|ESTACION\s+DE\s+SERV': 'Carga de Combustible',
        r'GAS\s+FRACC': 'Pago de Gas',
        r'MERPAGO|MERCADO\s*PAGO': 'Compra Mercado Libre',
        r'PAYPAL': 'Pago PayPal',
        r'AMAZON': 'Compra Amazon',
        r'NETFLIX': 'Suscripción Netflix',
        r'SPOTIFY': 'Suscripción Spotify',
        r'GOOGLE': 'Pago Google',
        r'APPLE': 'Pago Apple',
        r'UBER': 'Pago Uber',
        r'DIDI': 'Pago DiDi',
        r'CFE': 'Pago CFE (Luz)',
        r'TELMEX': 'Pago Telmex',
        r'IZZI': 'Pago Izzi',
        r'TOTALPLAY': 'Pago Totalplay',
    }
    
    for patron, nombre in comercios.items():
        if re.search(patron, desc):
            return nombre
    
    # === CÓDIGOS BBVA ===
    codigo_match = re.match(r'^([A-Z]\d{2})\s+(.+?)(?:\s+BNET|\s+REF|\s+\d{18}|$)', desc)
    if codigo_match:
        codigo = codigo_match.group(1)
        resto = codigo_match.group(2).strip()
        
        nombres_codigos = {
            "T17": "Transferencia SPEI Enviada",
            "T20": "Transferencia SPEI Recibida",
            "T09": "Transferencia TEF Recibida",
            "T22": "SPEI Devuelto",
            "N06": "Pago a Terceros",
            "C03": "Cheque Pagado",
            "C02": "Depósito en Efectivo",
            "AA7": "Depósito Efectivo Practicaja",
            "W02": "Depósito de Tercero",
            "R01": "Pago de Nómina",
            "H09": "Cobro Automático Préstamo",
            "P31": "Pago de Servicios",
            "G30": "Cargo por Servicios",
        }
        
        if codigo in nombres_codigos:
            return nombres_codigos[codigo]
    
    # === FALLBACK: Usar versión original mejorada ===
    return crear_nombre_resumido(descripcion)

# =============================================================================
# DETERMINACIÓN DE TIPO DE TRANSACCIÓN MEJORADA
# =============================================================================

def determinar_tipo(descripcion: str) -> str:
    """Determina tipo con prioridad corregida (CHEQUE antes que DEPOSITO)"""
    desc = descripcion.upper()
    
    # CHEQUE tiene prioridad sobre DEPOSITO
    if any(p in desc for p in ["CHEQUE PAGADO", "CHEQUE NO", "CHQ PAGADO"]):
        return "Cheque"
    
    if any(p in desc for p in ["SPEI", "TRANSFERENCIA", "TRASPASO", "TEF"]):
        return "Transferencia"
    
    if any(p in desc for p in ["DEPOSITO", "DEP "]):
        return "Depósito"
    
    if any(p in desc for p in ["COMISION", "COM ", "COBRO COM", "COBRO DE COM"]):
        return "Comisión"
    
    if any(p in desc for p in ["IVA", "ISR", "IMPUESTO"]):
        return "Impuesto"
    
    if any(p in desc for p in ["DOMICILIACION", "DOMI "]):
        return "Domiciliación"
    
    if any(p in desc for p in ["INTERES"]):
        return "Interés"
    
    if any(p in desc for p in ["PAGO INTERBANCARIO", "PAGO RECIBIDO", "PAGO"]):
        return "Pago"
    
    if any(p in desc for p in ["RETIRO", "ATM", "CAJERO"]):
        return "Retiro"
    
    if any(p in desc for p in ["TARJETA", "TDC", "TDD"]):
        return "Tarjeta"
    
    if any(p in desc for p in ["COBRO AUTOMATICO"]):
        return "Cobro Automático"
    
    return "Otro"

# =============================================================================
# DETERMINACIÓN DE MÉTODO DE PAGO MEJORADA
# =============================================================================

def determinar_metodo(descripcion: str) -> str:
    """Determina método con prioridad corregida (SPEI antes que EFECTIVO)"""
    desc = descripcion.upper()
    
    # Prioridad: SPEI > Tarjeta > Cheque > Domiciliación > Efectivo
    if "SPEI" in desc or "TEF" in desc:
        return "SPEI"
    
    if any(p in desc for p in ["TARJETA", "TDC", "TDD", "VISA", "MASTERCARD"]):
        return "Tarjeta"
    
    if any(p in desc for p in ["CHEQUE", "CHQ"]):
        return "Cheque"
    
    if any(p in desc for p in ["DOMICILIACION", "DOMI "]):
        return "Domiciliación"
    
    # EFECTIVO solo si explícitamente mencionado y no hay SPEI
    if any(p in desc for p in ["EFECTIVO", "PRACTIC"]):
        return "Efectivo"
    
    # CAJA/ATM indica efectivo solo si no hay transferencia
    if any(p in desc for p in ["ATM", "CAJERO"]) and "SPEI" not in desc:
        return "Efectivo"
    
    # Default a Transferencia (más común en banca empresarial)
    return "Transferencia"

# =============================================================================
# EXTRACCIÓN DE CLAVE RASTREO Y HORA
# =============================================================================

def extraer_clave_rastreo(descripcion: str) -> str:
    """Extrae la clave de rastreo de la descripción"""
    patrones = [
        r'RASTREO[:\s]+([A-Z0-9]{15,30})',
        r'CLAVE\s+(?:DE\s+)?RASTREO\s+([A-Z0-9]{15,30})',
        r'(BNET[A-Z0-9]{10,25})',
        r'(00260100\d{16,})',
        r'(0859\d{14,})',
    ]
    
    for patron in patrones:
        match = re.search(patron, descripcion.upper())
        if match:
            return match.group(1)
    
    return ""

def extraer_hora_tx(descripcion: str) -> str:
    """Extrae la hora de la descripción"""
    match = re.search(r'HORA\s+(\d{1,2}:\d{2})', descripcion.upper())
    if match:
        return match.group(1)
    return ""

# =============================================================================
# HASH Y DEDUPLICACIÓN MEJORADA
# =============================================================================

def generar_hash_transaccion(tx: Dict) -> str:
    """Hash mejorado que incluye clasificación"""
    fecha = tx.get("Fecha de la transacción", "")
    monto = tx.get("Monto de la transacción", 0)
    desc = tx.get("Nombre de la transacción", "")
    referencia = tx.get("Numero de referencia o folio", "")
    clasificacion = tx.get("Clasificación", "")
    
    clave_rastreo = extraer_clave_rastreo(desc)
    hora = extraer_hora_tx(desc)
    
    # Incluir clasificación en el hash
    key = f"{fecha}|{monto}|{desc[:100]}|{referencia}|{clave_rastreo}|{hora}|{clasificacion}"
    return hashlib.md5(key.encode()).hexdigest()


# =============================================================================
# DETECCIÓN Y FILTRADO DE TRANSACCIONES FANTASMA
# =============================================================================

def es_transaccion_fantasma_v68(transaccion: Dict, metadatos: Dict = None) -> bool:
    """Detecta si una transacción es en realidad una línea de resumen/total."""
    import re
    descripcion = str(transaccion.get('Nombre de la transacción', '')).upper().strip()
    fecha = str(transaccion.get('Fecha de la transacción', ''))
    monto = transaccion.get('Monto de la transacción', 0)
    
    # Verificar patrones de línea de resumen
    for patron in PATRONES_LINEA_RESUMEN_V68:
        if re.search(patron, descripcion, re.IGNORECASE):
            return True
    
    # Verificar fechas inválidas
    for fecha_inv in FECHAS_INVALIDAS_V68:
        if fecha_inv in fecha:
            return True
    
    # Si tenemos metadatos, verificar si el monto coincide con totales
    if metadatos:
        total_depositos = metadatos.get('Cantidad total de depositos', 0)
        total_retiros = metadatos.get('Cantidad total de retiros', 0)
        
        if monto > 0:
            if abs(monto - total_depositos) < 0.01:
                return True
            if abs(monto - total_retiros) < 0.01:
                return True
    
    # Descripción muy corta y genérica con monto muy grande
    if len(descripcion) < 25 and monto > 100000:
        genericas = ['DEPOSITO EN CUENTA', 'ABONO', 'CARGO', 'RETIRO', 'DEPOSITOS', 'RETIROS']
        if any(gen in descripcion for gen in genericas):
            return True
    
    return False


def validar_fecha_en_periodo_v68(fecha: str, periodo: str) -> bool:
    """Valida que la fecha esté dentro del periodo."""
    import re
    if not fecha or not periodo:
        return True
    
    for fecha_inv in FECHAS_INVALIDAS_V68:
        if fecha_inv in fecha:
            return False
    
    try:
        partes = fecha.split('/')
        if len(partes) != 3:
            return True
        
        mes_tx = int(partes[1])
        anio_tx = int(partes[2])
        
        meses_map = {
            'ENE': 1, 'FEB': 2, 'MAR': 3, 'ABR': 4, 'MAY': 5, 'JUN': 6,
            'JUL': 7, 'AGO': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DIC': 12
        }
        
        match_inicio = re.search(r'(\d{2})([A-Z]{3})(\d{4})', periodo)
        if match_inicio:
            mes_periodo = meses_map.get(match_inicio.group(2), 0)
            anio_periodo = int(match_inicio.group(3))
            
            if anio_tx != anio_periodo:
                return False
            if abs(mes_tx - mes_periodo) > 1:
                return False
        
        return True
    except:
        return True


def filtrar_transacciones_v68(transacciones: List[Dict], metadatos: Dict, periodo: str) -> List[Dict]:
    """Filtra transacciones eliminando fantasmas y fechas inválidas."""
    filtradas = []
    eliminadas = {'fantasma': 0, 'fecha_invalida': 0}
    
    for tx in transacciones:
        if es_transaccion_fantasma_v68(tx, metadatos):
            eliminadas['fantasma'] += 1
            continue
        
        fecha = tx.get('Fecha de la transacción', '')
        if not validar_fecha_en_periodo_v68(fecha, periodo):
            eliminadas['fecha_invalida'] += 1
            continue
        
        filtradas.append(tx)
    
    total_eliminadas = sum(eliminadas.values())
    if total_eliminadas > 0:
        logger.info(f"Eliminadas {total_eliminadas} transacciones problemáticas")
        if eliminadas['fantasma'] > 0:
            logger.info(f"       - {eliminadas['fantasma']} líneas de resumen (totales)")
        if eliminadas['fecha_invalida'] > 0:
            logger.info(f"       - {eliminadas['fecha_invalida']} con fechas inválidas")
    
    return filtradas


def deduplicar_transacciones(transacciones: List[Dict]) -> List[Dict]:
    vistos = {}
    resultado = []
    
    for tx in transacciones:
        hash_tx = generar_hash_transaccion(tx)
        
        if hash_tx not in vistos:
            vistos[hash_tx] = tx
            resultado.append(tx)
        else:
            existente = vistos[hash_tx]
            # Preferir la que tenga descripción más larga
            if len(tx.get("Nombre de la transacción", "")) > len(existente.get("Nombre de la transacción", "")):
                idx = resultado.index(existente)
                resultado[idx] = tx
                vistos[hash_tx] = tx
    
    return resultado

# =============================================================================
# SEPARACIÓN DE TRANSACCIONES MEZCLADAS
# =============================================================================

def separar_transacciones_mezcladas(tx: Dict, banco: str, anio: str) -> List[Dict]:
    descripcion = tx.get("descripcion", "") or tx.get("Nombre de la transaccion", "")
    descripcion = limpiar_texto(descripcion).upper()
    
    # Caso: INTERESES GANADOS + ISR RETENIDO
    if "INTERESES GANADOS" in descripcion and "ISR RETENIDO" in descripcion:
        monto_total = limpiar_monto(tx.get("monto", 0))
        
        tx_intereses = tx.copy()
        tx_intereses["descripcion"] = "INTERESES GANADOS"
        tx_intereses["clasificacion"] = "Ingreso"
        
        tx_isr = tx.copy()
        tx_isr["descripcion"] = "ISR RETENIDO"
        tx_isr["clasificacion"] = "Egreso"
        tx_isr["monto"] = 0
        
        return [tx_intereses]
    
    # Caso: COMISION + IVA COMISION
    if "COMISION" in descripcion and "IVA COMISION" in descripcion:
        monto_total = limpiar_monto(tx.get("monto", 0))
        
        montos = re.findall(r'(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)', descripcion)
        if len(montos) >= 2:
            tx_comision = tx.copy()
            tx_comision["descripcion"] = re.sub(r'IVA\s+COMISION.*', '', descripcion).strip()
            tx_comision["monto"] = limpiar_monto(montos[0])
            tx_comision["clasificacion"] = "Egreso"
            
            tx_iva = tx.copy()
            tx_iva["descripcion"] = "IVA COMISION"
            tx_iva["monto"] = limpiar_monto(montos[1]) if len(montos) > 1 else 0
            tx_iva["clasificacion"] = "Egreso"
            
            if tx_comision["monto"] > 0 and tx_iva["monto"] > 0:
                return [tx_comision, tx_iva]
    
    return [tx]

# =============================================================================
# EXTRACCIÓN DE TRANSACCIONES CON REINTENTOS
# =============================================================================

def extraer_transacciones_pagina(modelo, procesador, imagen, banco: Banco, anio: str, 
                                  contexto_anterior: str = "", num_pagina: int = 0) -> Tuple[List[Dict], str]:
    """Extracción con reintentos para páginas problemáticas y contexto entre páginas"""
    
    transacciones = []
    ultimo_contexto = ""
    
    # Primer intento con prompt completo
    try:
        prompt = get_prompt_transacciones(banco, anio, contexto_anterior)
        resp = ejecutar_prompt(modelo, procesador, imagen, prompt, max_tokens=8000)
        
        if resp and len(resp.strip()) >= 10:
            datos = extraer_json(resp)
            if datos:
                transacciones_raw = datos.get("transacciones", [])
                if isinstance(datos, list):
                    transacciones_raw = datos
                
                transacciones = procesar_transacciones_raw(transacciones_raw, banco, anio)
    except Exception as e:
        logger.warning(f"Error en primer intento página {num_pagina}: {e}")
        torch.cuda.empty_cache()
    
    # Si obtuvo pocas transacciones, reintentar con más tokens
    if len(transacciones) < CONFIG_EXTRACCION_V67["umbral_transacciones_minimo"]:
        logger.info(f"  Reintentando página {num_pagina} con más tokens...")
        
        for intento, max_tokens in enumerate(CONFIG_EXTRACCION_V67["tokens_por_intento"][1:], 2):
            try:
                # Usar prompt simplificado en reintentos
                prompt = get_prompt_transacciones_simplificado(banco, anio)
                resp = ejecutar_prompt(modelo, procesador, imagen, prompt, max_tokens=max_tokens)
                
                if resp and len(resp.strip()) >= 10:
                    datos = extraer_json(resp)
                    if datos:
                        transacciones_raw = datos.get("transacciones", [])
                        if isinstance(datos, list):
                            transacciones_raw = datos
                        
                        nuevas = procesar_transacciones_raw(transacciones_raw, banco, anio)
                        
                        if len(nuevas) > len(transacciones):
                            transacciones = nuevas
                            logger.info(f"    Intento {intento}: {len(nuevas)} transacciones")
                            break
                
                torch.cuda.empty_cache()
                gc.collect()
                
            except Exception as e:
                logger.warning(f"Error en intento {intento} página {num_pagina}: {e}")
                torch.cuda.empty_cache()
    
    # Extraer contexto de última transacción para siguiente página
    if transacciones:
        ultima = transacciones[-1]
        desc = ultima.get("Nombre de la transacción", "")
        # Si la descripción parece cortada (termina en medio de palabra o tiene caracteres incompletos)
        if desc and (len(desc) > 100 or not desc.rstrip().endswith((".", ")", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9"))):
            ultimo_contexto = f"Descripción parcial: {desc[-100:]}"
    
    return transacciones, ultimo_contexto

def procesar_transacciones_raw(transacciones_raw: List, banco: Banco, anio: str) -> List[Dict]:
    """Procesa lista de transacciones crudas del modelo"""
    transacciones = []
    
    for tx in transacciones_raw:
        if not isinstance(tx, dict):
            continue
        
        txs_separadas = separar_transacciones_mezcladas(tx, str(banco.value), anio)
        
        for tx_sep in txs_separadas:
            descripcion = tx_sep.get("descripcion", "") or tx_sep.get("concepto", "") or tx_sep.get("Nombre de la transaccion", "")
            descripcion = limpiar_texto(descripcion)
            
            desc_upper = descripcion.upper()
            if any(ignorar in desc_upper for ignorar in FILAS_IGNORAR):
                continue
            
            monto = limpiar_monto(tx_sep.get("monto", 0) or tx_sep.get("Monto de la transaccion", 0))
            
            # No descartar transacciones sin monto, marcarlas para revisión
            if monto <= 0:
                # Si tiene descripción significativa, podría ser error de extracción
                if len(descripcion) > 20 and not any(ignorar in desc_upper for ignorar in FILAS_IGNORAR):
                    logger.warning(f"  Transacción sin monto: {descripcion[:50]}...")
                continue
            
            fecha_raw = str(tx_sep.get("fecha", "") or tx_sep.get("Fecha de la transaccion", ""))
            fecha = normalizar_fecha(fecha_raw, anio)
            
            # Clasificación mejorada con doble verificación
            clasificacion_patron = clasificar_por_patrones(descripcion, str(banco.value))
            
            if clasificacion_patron:
                clasificacion = clasificacion_patron
            else:
                clasif_raw = str(tx_sep.get("clasificacion", "") or tx_sep.get("tipo", "")).upper().strip()
                
                if "INGRESO" in clasif_raw or "ABONO" in clasif_raw or "DEPOSITO" in clasif_raw:
                    clasificacion = "Ingreso"
                elif "EGRESO" in clasif_raw or "CARGO" in clasif_raw or "RETIRO" in clasif_raw:
                    clasificacion = "Egreso"
                else:
                    clasificacion = inferir_clasificacion_por_descripcion(descripcion)
            
            referencia_raw = limpiar_texto(str(tx_sep.get("referencia", "") or ""))
            referencia = extraer_referencia_limpia(descripcion, referencia_raw)
            
            contraparte = limpiar_texto(str(tx_sep.get("contraparte", "") or ""))
            contraparte = limpiar_contraparte(contraparte)
            if not contraparte:
                contraparte = extraer_tercero(descripcion, clasificacion)
                contraparte = limpiar_contraparte(contraparte)
            
            cuenta_extraida = extraer_cuenta(descripcion)
            cuenta_origen = cuenta_extraida if clasificacion == "Ingreso" else ""
            cuenta_destino = cuenta_extraida if clasificacion == "Egreso" else ""
            
            # Usar nombre resumido mejorado
            nombre_resumido = crear_nombre_resumido_v67(descripcion, clasificacion)
            
            transacciones.append({
                "Fecha de la transacción": fecha,
                "Nombre de la transacción": descripcion,
                "Nombre resumido": nombre_resumido,
                "Tipo de transacción": determinar_tipo(descripcion),
                "Clasificación": clasificacion,
                "Quien realiza o recibe el pago": contraparte,
                "Monto de la transacción": monto,
                "Numero de referencia o folio": referencia,
                "Numero de cuenta origen": cuenta_origen,
                "Numero de cuenta destino": cuenta_destino,
                "Metodo de pago": determinar_metodo(descripcion),
                "Sucursal o ubicacion": extraer_sucursal(descripcion),
                "Giro de la transacción": ""
            })
    
    return transacciones

# =============================================================================
# VALIDACIÓN MATEMÁTICA
# =============================================================================

def validar_transacciones(
    transacciones: List[Dict],
    total_depositos_meta: float,
    total_retiros_meta: float,
    saldo_inicial: float,
    saldo_final: float
) -> List[str]:
    advertencias = []
    
    ingresos = [t for t in transacciones if t["Clasificación"] == "Ingreso"]
    egresos = [t for t in transacciones if t["Clasificación"] == "Egreso"]
    
    suma_ingresos = sum(t["Monto de la transacción"] for t in ingresos)
    suma_egresos = sum(t["Monto de la transacción"] for t in egresos)
    
    if total_depositos_meta > 0:
        diff = abs(suma_ingresos - total_depositos_meta)
        pct = (diff / total_depositos_meta) * 100 if total_depositos_meta > 0 else 0
        if pct > 5:
            advertencias.append(
                f"INGRESOS: Extraído ${suma_ingresos:,.2f} vs Meta ${total_depositos_meta:,.2f} ({pct:.1f}%)"
            )
    
    if total_retiros_meta > 0:
        diff = abs(suma_egresos - total_retiros_meta)
        pct = (diff / total_retiros_meta) * 100 if total_retiros_meta > 0 else 0
        if pct > 5:
            advertencias.append(
                f"EGRESOS: Extraído ${suma_egresos:,.2f} vs Meta ${total_retiros_meta:,.2f} ({pct:.1f}%)"
            )
    
    if saldo_inicial > 0 and saldo_final > 0:
        saldo_calculado = saldo_inicial + suma_ingresos - suma_egresos
        diff_saldo = abs(saldo_calculado - saldo_final)
        
        if diff_saldo > 1:
            advertencias.append(
                f"SALDO: Calculado ${saldo_calculado:,.2f} vs Real ${saldo_final:,.2f}"
            )
    
    return advertencias

# =============================================================================
# VALIDACIÓN Y CORRECCIÓN POST-EXTRACCIÓN
# =============================================================================

def validar_y_corregir_clasificaciones(transacciones: List[Dict], banco: str) -> List[Dict]:
    """Valida y corrige clasificaciones después de la extracción"""
    for tx in transacciones:
        tx = reclasificar_transaccion(tx, banco)
        
        # Regenerar nombre resumido si está vacío o es genérico
        nombre_resumido = tx.get("Nombre resumido", "")
        if not nombre_resumido or nombre_resumido in ["Movimiento", "Movimiento Bancario", ""]:
            desc = tx.get("Nombre de la transacción", "")
            clasificacion = tx.get("Clasificación", "")
            tx["Nombre resumido"] = crear_nombre_resumido_v67(desc, clasificacion)
    
    return transacciones

# =============================================================================
# FUNCIÓN PRINCIPAL V6.7
# =============================================================================

def main_extraction_ia(ruta_pdf: str, directorio_salida: str) -> Dict:
    logger.info("=" * 80)
    logger.info("EXTRACTOR V6.8 - FILTRO TRANSACCIONES FANTASMA, CONTEXTO ENTRE PÁGINAS, IA ADAPTATIVA")
    logger.info("=" * 80)
    
    modelo, procesador = cargar_modelo()
    if not modelo:
        return {"error": "No se pudo cargar el modelo"}
    
    imagenes = convertir_pdf(ruta_pdf)
    if not imagenes:
        return {"error": "No se pudo convertir el PDF"}
    
    total_paginas = len(imagenes)
    logger.info(f"PDF: {total_paginas} páginas a {CONFIG_IMAGEN['dpi']} DPI")
    
    os.makedirs(directorio_salida, exist_ok=True)
    
    logger.info("\n[FASE 0] Detectando banco...")
    banco = detectar_banco(modelo, procesador, imagenes[0])
    
    logger.info("\n[FASE 1] Clasificando páginas...")
    tipos_pagina = []
    for i, img in enumerate(imagenes):
        tipo = clasificar_pagina(modelo, procesador, img, num_pagina=i)
        tipos_pagina.append(tipo)
        logger.info(f"  Página {i+1}: {tipo.value}")
    
    logger.info("\n[FASE 2] Extrayendo metadatos...")
    
    metadatos_final = {
        "Nombre de la empresa del estado de cuenta": "",
        "Numero de cuenta del estado de cuenta": "",
        "Periodo del estado de cuenta": "",
        "Saldo inicial de la cuenta": 0.00,
        "Saldo final de la cuenta": 0.00,
        "Saldo promedio del periodo": 0.00,
        "Cantidad total de depositos": 0.00,
        "Cantidad total de retiros": 0.00,
        "Giro de la empresa": ""
    }
    
    # Analizar más páginas para metadatos si es necesario
    paginas_meta = [i for i, t in enumerate(tipos_pagina) 
                    if t in [TipoPagina.RESUMEN, TipoPagina.RESUMEN_CON_MOVIMIENTOS, TipoPagina.MOVIMIENTOS]][:5]
    
    for i in paginas_meta:
        logger.info(f"  Analizando página {i+1}...")
        datos = extraer_metadatos(modelo, procesador, imagenes[i], banco)
        
        for campo, valor in datos.items():
            if campo == "Nombre de la empresa del estado de cuenta":
                if valor and len(valor) > len(metadatos_final[campo]):
                    metadatos_final[campo] = valor
            elif campo == "Numero de cuenta del estado de cuenta":
                if valor and len(valor) >= len(metadatos_final[campo]) and valor != "500000000000":
                    metadatos_final[campo] = valor
            elif campo == "Periodo del estado de cuenta":
                if valor and len(valor) > len(metadatos_final[campo]):
                    metadatos_final[campo] = valor
            elif isinstance(valor, float) and valor > metadatos_final.get(campo, 0):
                metadatos_final[campo] = valor
    
    anio = extraer_anio_periodo(metadatos_final["Periodo del estado de cuenta"])
    logger.info(f"  Año: {anio}")
    logger.info(f"  Empresa: {metadatos_final['Nombre de la empresa del estado de cuenta']}")
    
    logger.info(f"\n[FASE 3] Extrayendo transacciones...")
    
    todas_transacciones = []
    paginas_mov = [(i, img) for i, (img, tipo) in enumerate(zip(imagenes, tipos_pagina)) 
                   if tipo in [TipoPagina.MOVIMIENTOS, TipoPagina.RESUMEN_CON_MOVIMIENTOS]]
    
    logger.info(f"  Páginas con movimientos: {len(paginas_mov)}")
    
    # Contexto entre páginas
    contexto_anterior = ""
    
    for idx, (i, imagen) in enumerate(paginas_mov):
        logger.info(f"  Página {i+1} ({idx+1}/{len(paginas_mov)}): Procesando...")
        
        try:
            transacciones, nuevo_contexto = extraer_transacciones_pagina(
                modelo, procesador, imagen, banco, anio, 
                contexto_anterior=contexto_anterior,
                num_pagina=i+1
            )
            
            if transacciones:
                todas_transacciones.extend(transacciones)
                logger.info(f"    → {len(transacciones)} transacciones")
            else:
                logger.info(f"    → Sin transacciones")
            
            # Actualizar contexto para siguiente página
            contexto_anterior = nuevo_contexto
            
            torch.cuda.empty_cache()
            gc.collect()
            
        except Exception as e:
            logger.error(f"    → ERROR: {e}")
            torch.cuda.empty_cache()
            gc.collect()
    
    logger.info(f"\n[FASE 4] Deduplicando...")
    antes = len(todas_transacciones)
    todas_transacciones = deduplicar_transacciones(todas_transacciones)
    despues = len(todas_transacciones)
    if antes != despues:
        logger.info(f"  Eliminadas {antes - despues} duplicadas")
    logger.info(f"  Total: {despues} transacciones únicas")
    
    # Validar y corregir clasificaciones
    logger.info("\n[FASE 4.5] Validando clasificaciones...")
    todas_transacciones = validar_y_corregir_clasificaciones(todas_transacciones, banco.value)
    
    logger.info("\n[FASE 5] Clasificando...")
    
    ingresos = [t for t in todas_transacciones if t["Clasificación"] == "Ingreso"]
    egresos = [t for t in todas_transacciones if t["Clasificación"] == "Egreso"]
    
    logger.info(f"  Ingresos: {len(ingresos)}")
    logger.info(f"  Egresos: {len(egresos)}")
    
    logger.info("\n[FASE 6] Validando...")
    
    errores_validacion = validar_transacciones(
        todas_transacciones,
        metadatos_final.get("Cantidad total de depositos", 0),
        metadatos_final.get("Cantidad total de retiros", 0),
        metadatos_final.get("Saldo inicial de la cuenta", 0),
        metadatos_final.get("Saldo final de la cuenta", 0)
    )
    
    suma_ingresos = sum(t["Monto de la transacción"] for t in ingresos)
    suma_egresos = sum(t["Monto de la transacción"] for t in egresos)
    
    total_dep_meta = metadatos_final.get("Cantidad total de depositos", 0)
    total_ret_meta = metadatos_final.get("Cantidad total de retiros", 0)
    
    if total_dep_meta > 0:
        diff_ing = abs(suma_ingresos - total_dep_meta)
        if diff_ing < 1:
            logger.info(f"  ✓ Ingresos: ${suma_ingresos:,.2f}")
        else:
            logger.warning(f"  ⚠ Ingresos: ${suma_ingresos:,.2f} vs Meta ${total_dep_meta:,.2f}")
    
    if total_ret_meta > 0:
        diff_egr = abs(suma_egresos - total_ret_meta)
        if diff_egr < 1:
            logger.info(f"  ✓ Egresos: ${suma_egresos:,.2f}")
        else:
            logger.warning(f"  ⚠ Egresos: ${suma_egresos:,.2f} vs Meta ${total_ret_meta:,.2f}")
    
    logger.info("\n[FASE 7] Guardando archivos...")
    
    nombre_empresa = metadatos_final["Nombre de la empresa del estado de cuenta"]
    if not nombre_empresa:
        nombre_empresa = os.path.splitext(os.path.basename(ruta_pdf))[0]
    
    nombre_base = crear_nombre_archivo(nombre_empresa)
    periodo = metadatos_final["Periodo del estado de cuenta"]
    if not periodo or periodo == "PERIODO_ND":
        periodo = "PERIODO_ND"
    
    prefijo = f"{nombre_base}_{periodo}"
    
    # Guardar con encoding UTF-8 explícito
    # JSON 1: _DATOS.json (metadatos)
    ruta_datos = os.path.join(directorio_salida, f"{prefijo}_DATOS.json")
    with open(ruta_datos, 'w', encoding='utf-8') as f:
        json.dump(metadatos_final, f, ensure_ascii=False, indent=2)
    logger.info(f"  → {os.path.basename(ruta_datos)}")
    
    # JSON 2: _INGRESOS.json (transacciones de ingreso)
    ruta_ingresos = os.path.join(directorio_salida, f"{prefijo}_INGRESOS.json")
    with open(ruta_ingresos, 'w', encoding='utf-8') as f:
        json.dump(ingresos, f, ensure_ascii=False, indent=2)
    logger.info(f"  → {os.path.basename(ruta_ingresos)}")
    
    # JSON 3: _EGRESOS.json (transacciones de egreso)
    ruta_egresos = os.path.join(directorio_salida, f"{prefijo}_EGRESOS.json")
    with open(ruta_egresos, 'w', encoding='utf-8') as f:
        json.dump(egresos, f, ensure_ascii=False, indent=2)
    logger.info(f"  → {os.path.basename(ruta_egresos)}")
    
    # Resultado para API
    resultado_completo = {
        "archivo_origen": os.path.basename(ruta_pdf),
        "banco_detectado": banco.value,
        "version_extractor": "6.7",
        "metadatos": metadatos_final,
        "estadisticas": {
            "total_transacciones": len(todas_transacciones),
            "total_ingresos": len(ingresos),
            "total_egresos": len(egresos),
            "suma_ingresos": round(suma_ingresos, 2),
            "suma_egresos": round(suma_egresos, 2),
            "paginas_procesadas": total_paginas,
            "paginas_movimientos": len(paginas_mov)
        },
        "validacion": {
            "errores": errores_validacion,
            "es_valido": len(errores_validacion) == 0
        },
        "archivos_generados": [
            os.path.basename(ruta_datos),
            os.path.basename(ruta_ingresos),
            os.path.basename(ruta_egresos)
        ],
        "transacciones": todas_transacciones
    }
    
    logger.info("\n" + "=" * 80)
    logger.info("RESUMEN V6.7")
    logger.info("=" * 80)
    logger.info(f"Banco:          {banco.value}")
    logger.info(f"Empresa:        {metadatos_final['Nombre de la empresa del estado de cuenta']}")
    logger.info(f"Cuenta:         {metadatos_final['Numero de cuenta del estado de cuenta']}")
    logger.info(f"Periodo:        {periodo}")
    logger.info(f"Saldo Inicial:  ${metadatos_final['Saldo inicial de la cuenta']:,.2f}")
    logger.info(f"Saldo Final:    ${metadatos_final['Saldo final de la cuenta']:,.2f}")
    logger.info(f"Transacciones:  {len(todas_transacciones)} ({len(ingresos)} ingresos, {len(egresos)} egresos)")
    logger.info(f"Suma Ingresos:  ${suma_ingresos:,.2f}")
    logger.info(f"Suma Egresos:   ${suma_egresos:,.2f}")
    
    if errores_validacion:
        logger.warning("\n⚠ ADVERTENCIAS DE VALIDACIÓN:")
        for err in errores_validacion:
            logger.warning(f"  - {err}")
    else:
        logger.info("\n✓ Validación OK")
    
    logger.info("=" * 80)
    
    del modelo, procesador, imagenes
    torch.cuda.empty_cache()
    gc.collect()
    
    return resultado_completo


def procesar_directorio(directorio_entrada: str, directorio_salida: str) -> List[Dict]:
    resultados = []
    
    pdfs = [f for f in os.listdir(directorio_entrada) if f.lower().endswith('.pdf')]
    
    if not pdfs:
        logger.warning(f"No se encontraron PDFs en: {directorio_entrada}")
        return resultados
    
    logger.info(f"Encontrados {len(pdfs)} archivos PDF")
    
    for i, pdf in enumerate(pdfs, 1):
        logger.info(f"\n{'='*80}")
        logger.info(f"PROCESANDO {i}/{len(pdfs)}: {pdf}")
        logger.info(f"{'='*80}")
        
        ruta_pdf = os.path.join(directorio_entrada, pdf)
        
        try:
            resultado = main_extraction_ia(ruta_pdf, directorio_salida)
            resultados.append({
                "archivo": pdf,
                "exito": "error" not in resultado,
                "resultado": resultado
            })
        except Exception as e:
            logger.error(f"Error procesando {pdf}: {e}")
            resultados.append({
                "archivo": pdf,
                "exito": False,
                "error": str(e)
            })
        
        torch.cuda.empty_cache()
        gc.collect()
    
    return resultados


# =============================================================================
# PUNTO DE ENTRADA
# =============================================================================

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Extractor de Estados de Cuenta Bancarios V6.7"
    )
    parser.add_argument(
        "entrada",
        help="Ruta al PDF o directorio con PDFs"
    )
    parser.add_argument(
        "-o", "--output",
        default="./salida_v6_7",
        help="Directorio de salida (default: ./salida_v6_7)"
    )
    
    args = parser.parse_args()
    
    if os.path.isfile(args.entrada):
        resultado = main_extraction_ia(args.entrada, args.output)
        
        if "error" in resultado:
            logger.error(f"Error: {resultado['error']}")
            sys.exit(1)
        else:
            logger.info("Proceso completado exitosamente")
            sys.exit(0)
            
    elif os.path.isdir(args.entrada):
        resultados = procesar_directorio(args.entrada, args.output)
        
        exitosos = sum(1 for r in resultados if r["exito"])
        fallidos = len(resultados) - exitosos
        
        logger.info(f"\n{'='*80}")
        logger.info(f"RESUMEN FINAL: {exitosos} exitosos, {fallidos} fallidos de {len(resultados)} total")
        logger.info(f"{'='*80}")
        
        sys.exit(0 if fallidos == 0 else 1)
    else:
        logger.error(f"No se encuentra: {args.entrada}")
        sys.exit(1)