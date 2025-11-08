# -*- coding: utf-8 -*-
"""
Parser BBVA Empresarial - VERSIÓN CORREGIDA
Formato: MAESTRA PYME BBVA / VERSATIL NEGOCIOS
"""

import re
from decimal import Decimal
from utils.validators import limpiar_monto

# =============================================================================
# PATRONES PARA DATOS GENERALES
# =============================================================================

PATRON_NOMBRE_EMPRESA = re.compile(
    r"^([A-ZÁÉÍÓÚÑ\s]+(?:SA|S\.A\.|DE CV|S\. DE R\.L\.|SC|S\.C\.)[^\n]*)",
    re.MULTILINE | re.IGNORECASE
)

PATRON_PERIODO = re.compile(
    r"Periodo\s+(?:DEL\s+)?(\d{2}/\d{2}/\d{4})\s+AL\s+(\d{2}/\d{2}/\d{4})",
    re.IGNORECASE
)

PATRON_CLABE = re.compile(
    r"(?:No\.\s*)?Cuenta\s*CLABE\s+(\d{18})",
    re.IGNORECASE
)

PATRON_SALDO_INICIAL = re.compile(
    r"Saldo de (?:Operación|Liquidación) Inicial\s+([\d,.-]+)",
    re.IGNORECASE
)

PATRON_DEPOSITOS = re.compile(
    r"Depósitos\s*/?\\?\s*Abonos\s*\(\+\)\s+(\d+)\s+([\d,.-]+)",
    re.IGNORECASE
)

PATRON_RETIROS = re.compile(
    r"Retiros\s*/?\\?\s*Cargos\s*\(\-\)\s+(\d+)\s+([\d,.-]+)",
    re.IGNORECASE
)

PATRON_SALDO_FINAL = re.compile(
    r"Saldo\s+(?:Final|de\s+Operación\s+Final)\s*\(\+\)\s+([\d,.-]+)",
    re.IGNORECASE
)

PATRON_SALDO_PROMEDIO = re.compile(
    r"Saldo\s+Promedio(?:\s+Mínimo\s+Mensual\s+Hasta)?:?\s+([\d,.-]+)",
    re.IGNORECASE
)

# =============================================================================
# PATRONES PARA TRANSACCIONES (FORMATO REAL BBVA)
# =============================================================================

# CRÍTICO: El monto está al FINAL de la primera línea
PATRON_TRANSACCION_COMPLETA = re.compile(
    r"^(\d{2}/[A-Z]{3})\s+(\d{2}/[A-Z]{3})\s+([A-Z]\d{2})\s+(.*?)\s+([\d,]+\.\d{2})$",
    re.MULTILINE
)

# Códigos típicos de EGRESO
CODIGOS_EGRESO = {
    'T17', 'N06', 'A15', 'G30', 'S39', 'S40', 'P14',
    'CI', 'COM', 'CHQ', 'RET', 'CGO'
}

# Códigos típicos de INGRESO
CODIGOS_INGRESO = {
    'T20', 'W02', 'T22', 'Y45', 'DEP', 'ABO', 'TRA'
}

# =============================================================================
# FUNCIÓN: PARSEAR DATOS GENERALES
# =============================================================================

def parsear_datos_generales(paginas_texto):
    """
    Extrae datos generales del estado de cuenta BBVA.
    """
    texto_completo = "\n".join(paginas_texto)
    
    # Buscar página de Información Financiera
    pagina_info = _encontrar_pagina_info_financiera(paginas_texto)
    if not pagina_info:
        print("  > Advertencia: No se encontró la página de Información Financiera")
        pagina_info = paginas_texto[0] if paginas_texto else ""
    
    datos = {}
    
    # Extraer nombre
    nombre = _extraer_nombre_empresa_robusto(texto_completo)
    datos['nombre_empresa'] = nombre if nombre else "EMPRESA NO DETECTADA"
    
    # Extraer periodo
    match_periodo = PATRON_PERIODO.search(pagina_info)
    if match_periodo:
        datos['periodo'] = f"DEL {match_periodo.group(1)} AL {match_periodo.group(2)}"
    else:
        datos['periodo'] = "PERIODO NO DETECTADO"
    
    # Extraer CLABE
    match_clabe = PATRON_CLABE.search(pagina_info)
    datos['numero_cuenta_clabe'] = match_clabe.group(1) if match_clabe else "NO DETECTADO"
    
    # Extraer montos
    match_saldo_ini = PATRON_SALDO_INICIAL.search(pagina_info)
    match_depositos = PATRON_DEPOSITOS.search(pagina_info)
    match_retiros = PATRON_RETIROS.search(pagina_info)
    match_saldo_fin = PATRON_SALDO_FINAL.search(pagina_info)
    match_saldo_prom = PATRON_SALDO_PROMEDIO.search(pagina_info)
    
    datos['saldo_inicial'] = limpiar_monto(match_saldo_ini.group(1)) if match_saldo_ini else "0"
    datos['total_depositos'] = limpiar_monto(match_depositos.group(2)) if match_depositos else "0"
    datos['total_retiros'] = limpiar_monto(match_retiros.group(2)) if match_retiros else "0"
    datos['saldo_final'] = limpiar_monto(match_saldo_fin.group(1)) if match_saldo_fin else "0"
    datos['saldo_promedio'] = limpiar_monto(match_saldo_prom.group(1)) if match_saldo_prom else "0"
    
    return datos

# =============================================================================
# FUNCIÓN: PARSEAR TRANSACCIONES (MÉTODO CORREGIDO)
# =============================================================================

def parsear_transacciones(paginas_texto, saldo_inicial):
    """
    Extrae transacciones del estado de cuenta BBVA.
    CORREGIDO para capturar monto en primera línea.
    """
    texto_completo = "\n".join(paginas_texto)
    
    # Buscar inicio de tabla
    match_inicio = re.search(r"Detalle de Movimientos Realizados", texto_completo, re.IGNORECASE)
    if not match_inicio:
        print("  > Advertencia: No se encontró el 'Detalle de Movimientos Realizados' en BBVA.")
        return []
    
    # Extraer sección de movimientos
    texto_movimientos = texto_completo[match_inicio.end():]
    
    # Buscar fin de tabla
    match_fin = re.search(r"Total de Movimientos", texto_movimientos, re.IGNORECASE)
    if match_fin:
        texto_movimientos = texto_movimientos[:match_fin.start()]
    
    transacciones = []
    
    # MÉTODO 1: Buscar transacciones completas (primera línea con monto)
    for match in PATRON_TRANSACCION_COMPLETA.finditer(texto_movimientos):
        fecha_oper = match.group(1)
        fecha_liq = match.group(2)
        codigo = match.group(3)
        descripcion_base = match.group(4).strip()
        monto_str = match.group(5)
        
        # Limpiar monto
        monto = limpiar_monto(monto_str)
        
        # Buscar líneas adicionales de descripción
        pos_fin = match.end()
        lineas_adicionales = []
        
        # Leer líneas siguientes que comienzan con espacio
        lineas = texto_movimientos[pos_fin:].split('\n')
        for linea in lineas[:10]:  # Máximo 10 líneas adicionales
            if linea and linea[0] == ' ':
                lineas_adicionales.append(linea.strip())
            else:
                break
        
        # Construir descripción completa
        descripcion_completa = descripcion_base
        if lineas_adicionales:
            descripcion_completa += " " + " ".join(lineas_adicionales)
        
        descripcion_completa = re.sub(r'\s+', ' ', descripcion_completa).strip()
        
        # Determinar clasificación
        if codigo in CODIGOS_EGRESO:
            clasificacion = "Egreso"
        elif codigo in CODIGOS_INGRESO:
            clasificacion = "Ingreso"
        else:
            # Determinar por palabras clave
            desc_upper = descripcion_completa.upper()
            if any(palabra in desc_upper for palabra in ['ENVIADO', 'PAGO', 'CARGO', 'COMISION', 'RETIRO']):
                clasificacion = "Egreso"
            elif any(palabra in desc_upper for palabra in ['RECIBIDO', 'DEPOSITO', 'ABONO']):
                clasificacion = "Ingreso"
            else:
                # Por defecto, si tiene T17 o N06 = Egreso, si T20 o W02 = Ingreso
                clasificacion = "Egreso" if codigo.startswith('T17') or codigo.startswith('N') else "Ingreso"
        
        # Extraer información adicional
        referencia = _extraer_referencia(descripcion_completa)
        cuenta = _extraer_cuenta(descripcion_completa)
        contraparte = _extraer_contraparte(descripcion_completa)
        tipo_transaccion = _determinar_tipo_transaccion(codigo, descripcion_completa)
        metodo_pago = _determinar_metodo_pago(codigo, descripcion_completa)
        
        # Crear transacción
        transaccion = {
            "fecha": fecha_oper,
            "nombre_transaccion": descripcion_completa[:200],
            "nombre_resumido": _generar_nombre_resumido(descripcion_completa),
            "tipo_transaccion": tipo_transaccion,
            "clasificacion": clasificacion,
            "quien_realiza_o_recibe": contraparte,
            "monto": str(monto),
            "numero_referencia_folio": referencia,
            "numero_cuenta_origen_destino": cuenta,
            "metodo_pago": metodo_pago,
            "sucursal_o_ubicacion": ""
        }
        
        transacciones.append(transaccion)
    
    print(f"  > Se extrajeron {len(transacciones)} transacciones")
    return transacciones

# =============================================================================
# FUNCIONES AUXILIARES
# =============================================================================

def _extraer_referencia(descripcion):
    """Extrae número de referencia."""
    match = re.search(r'Ref\.?\s*:?\s*(\w+)', descripcion, re.IGNORECASE)
    if match:
        return match.group(1)
    
    match = re.search(r'\b(\d{7,})\b', descripcion)
    if match:
        return match.group(1)
    
    return ""

def _extraer_cuenta(descripcion):
    """Extrae número de cuenta CLABE."""
    match = re.search(r'\b(\d{18})\b', descripcion)
    if match:
        return match.group(1)
    
    match = re.search(r'\b(\d{10,16})\b', descripcion)
    if match:
        return match.group(1)
    
    return ""

def _extraer_contraparte(descripcion):
    """Extrae nombre de contraparte."""
    patrones = [
        r'(?:ENVIADO|RECIBIDO|PAGO A|DE)\s+([A-ZÁÉÍÓÚÑ\s]{5,}?)(?:\s+Ref|\s+\d|$)',
        r'([A-ZÁÉÍÓÚÑ\s]{10,}?)(?:\s+Ref|\s+BMRCASH|\s+\d{4,}|$)'
    ]
    
    for patron in patrones:
        match = re.search(patron, descripcion, re.IGNORECASE)
        if match:
            nombre = match.group(1).strip()
            if len(nombre) > 5 and not nombre.isdigit():
                return nombre[:50]
    
    return ""

def _determinar_tipo_transaccion(codigo, descripcion):
    """Determina el tipo de transacción."""
    tipos = {
        'T17': 'Transferencia SPEI Enviada',
        'T20': 'Transferencia SPEI Recibida',
        'T22': 'SPEI Devuelto',
        'W02': 'Depósito en Efectivo',
        'N06': 'Pago a Tercero',
        'A15': 'Pago con Tarjeta',
        'G30': 'Recibo de Pago',
        'S39': 'Servicio Bancario',
        'S40': 'IVA Comisión',
        'P14': 'Pago SAT',
        'Y45': 'Compensación'
    }
    return tipos.get(codigo, 'Operación Bancaria')

def _determinar_metodo_pago(codigo, descripcion):
    """Determina método de pago."""
    if codigo in ['T17', 'T20', 'T22']:
        return 'SPEI'
    elif codigo == 'W02':
        return 'Efectivo'
    elif codigo == 'N06':
        return 'BNET'
    elif codigo == 'A15':
        return 'Tarjeta'
    elif 'BMRCASH' in descripcion.upper():
        return 'BMR Cash'
    else:
        return 'Transferencia'

def _generar_nombre_resumido(descripcion):
    """Genera nombre resumido."""
    palabras = descripcion.split()[:5]
    resumido = ' '.join(palabras)
    if len(resumido) > 50:
        resumido = resumido[:47] + '...'
    return resumido

def _encontrar_pagina_info_financiera(paginas):
    """Busca página con Información Financiera."""
    marcadores = [
        "Información Financiera",
        "MONEDA NACIONAL",
        "Comportamiento"
    ]
    
    for pagina in paginas:
        if all(m in pagina for m in marcadores):
            return pagina
    
    for pagina in paginas:
        if sum(1 for m in marcadores if m in pagina) >= 2:
            return pagina
    
    return paginas[0] if paginas else ""

def _extraer_nombre_empresa_robusto(texto):
    """Extrae nombre de empresa."""
    match = PATRON_NOMBRE_EMPRESA.search(texto)
    if match:
        nombre = match.group(1).strip()
        if len(nombre) > 5 and 'BBVA' not in nombre.upper():
            return nombre
    
    lineas = texto.split('\n')[:20]
    for linea in lineas:
        if any(termino in linea.upper() for termino in ['SA DE CV', 'S.A. DE C.V.', 'S. DE R.L.', 'SC']):
            if 'BBVA' not in linea.upper() and len(linea.strip()) > 10:
                return linea.strip()
    
    return None